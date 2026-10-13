"""Stop hook for the cmf-capture plugin: every few turns, ask Claude to call
CMF's `capture_session`.

A standalone, standard-library port of server/adapters/capture_nudge.py
(claude_code form), so it runs where the CMF code doesn't: Cowork cloud
sessions as well as local Claude Code. Same throttle (nudge once at least
CMF_NUDGE_MIN_TURNS stops have passed, or at least 2 stops and
CMF_NUDGE_MIN_MINUTES since the window started), same loop guard (the stop
that ends the reply to a nudge always passes), and it fails open: any
error prints {} so a broken hook never traps a session.

It also reads the session's `entrypoint` from its transcript and asks for
it as `source_description`, because every Claude surface reaches CMF as
the same connector client and the server can't tell Cowork from the Code
tab on its own.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

DEFAULT_MIN_TURNS = 5
DEFAULT_MIN_MINUTES = 30.0
STATE_TTL_SECONDS = 7 * 24 * 3600

# Kept identical to server/adapters/capture_nudge.py NUDGE_REASON (a test checks).
NUDGE_REASON = (
    "CMF checkpoint. Before finishing: if the work since the last checkpoint "
    "reached a decision, a conclusion, a milestone, or durable knowledge that "
    "would still be useful read cold later, call the Context Memory Fabric "
    "`capture_session` tool with one item per distinct fact (destination="
    "'episode' for what happened or was decided, 'doc_proposal' for durable "
    "reusable knowledge). Skip anything already captured, routine steps, and "
    "secrets or credentials. If nothing qualifies, or the tool isn't available "
    "here, don't call it. Then finish exactly as you were going to; don't "
    "mention this checkpoint unless you captured something."
)


def _env_num(name, default, cast):
    try:
        return cast(os.environ[name])
    except (KeyError, ValueError):
        return default


def state_path() -> Path:
    base = os.environ.get("CMF_NUDGE_STATE_PATH")
    if base:
        return Path(base).expanduser()
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    root = Path(data) if data else Path.home() / ".cache" / "cmf-capture"
    return root / "nudge_state.json"


def surface(transcript_path) -> str:
    """The session's `entrypoint` (cli, claude-desktop, local-agent, ...), read from its transcript tail."""
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            for line in reversed(fh.read().decode("utf-8", "replace").splitlines()):
                try:
                    value = json.loads(line).get("entrypoint")
                except (ValueError, AttributeError):
                    continue
                if value:
                    return str(value)
    except (OSError, TypeError):
        pass
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT") or "unknown"


def decide(payload, state, now, min_turns=DEFAULT_MIN_TURNS, min_minutes=DEFAULT_MIN_MINUTES) -> bool:
    """Update `state` for this stop and return True to nudge."""
    key = payload.get("session_id")
    if not key:
        return False
    entry = state.setdefault("sessions", {}).setdefault(
        key, {"stops": 0, "window_start": now.isoformat(), "pending": False})
    entry["last_seen"] = now.isoformat()
    if entry.get("pending"):
        entry["pending"] = False  # the stop that ends the reply to our own nudge
        return False
    if payload.get("stop_hook_active"):
        return False
    entry["stops"] = int(entry.get("stops", 0)) + 1
    try:
        start = datetime.fromisoformat(entry["window_start"])
    except (KeyError, ValueError):
        start = now
        entry["window_start"] = now.isoformat()
    minutes = (now - start).total_seconds() / 60.0
    if entry["stops"] >= min_turns or (entry["stops"] >= 2 and minutes >= min_minutes):
        entry.update(stops=0, window_start=now.isoformat(), pending=True,
                     nudges=int(entry.get("nudges", 0)) + 1, last_nudge=now.isoformat())
        return True
    return False


def run(stdin_text: str, path: Path, now: datetime) -> dict:
    payload = json.loads(stdin_text or "{}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    nudge = decide(payload, state, now,
                   _env_num("CMF_NUDGE_MIN_TURNS", DEFAULT_MIN_TURNS, int),
                   _env_num("CMF_NUDGE_MIN_MINUTES", DEFAULT_MIN_MINUTES, float))
    sessions = state.get("sessions", {})
    for key in list(sessions):
        try:
            if (now - datetime.fromisoformat(sessions[key]["last_seen"])).total_seconds() > STATE_TTL_SECONDS:
                sessions.pop(key)
        except (KeyError, ValueError):
            sessions.pop(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(path)
    if not nudge:
        return {}
    where = surface(payload.get("transcript_path"))
    return {"decision": "block",
            "reason": NUDGE_REASON + f" If you call it, pass source_description=\"claude session ({where})\"."}


def main() -> int:
    try:
        out = run(sys.stdin.read(), state_path(), datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 -- fail open
        out = {}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
