"""Stop-hook capture nudge: make the agent itself call `capture_session`.

The transcript pollers read every raw session on disk. This is the
alternative that reads nothing: a harness's Stop hook runs this command,
and every few turns it tells the harness to keep the agent going for one
more step with an instruction to record what was decided via CMF's
`capture_session` tool. The model chooses what is worth keeping; the hook
only supplies the deterministic trigger that instructions alone don't
(the journal showed agents follow "read CMF" instructions but rarely
"write to CMF" ones).

One command serves every harness; only the stdin fields and the stdout
shape differ:

| harness      | session key      | already-continued flag | continue output                     |
|--------------|------------------|------------------------|-------------------------------------|
| claude_code  | session_id       | stop_hook_active       | {"decision": "block", "reason": …}  |
| codex        | session_id       | stop_hook_active       | {"decision": "block", "reason": …}  |
| antigravity  | conversationId   | (none; tracked here)   | {"decision": "continue", "reason": …} |

Throttle (per session, state in a small JSON file next to the journal):
nudge once at least CMF_NUDGE_MIN_TURNS stops have passed since the last
nudge, or at least 2 stops and CMF_NUDGE_MIN_MINUTES since the window
started. The stop right after a nudge always passes (the agent's capture
reply), which is also Antigravity's only loop guard.

Fails open: any error prints the harness's "let it stop" output and
exits 0, so a broken nudge never traps a session.

Usage:
    python -m server.adapters.capture_nudge run --harness claude_code   # the hook body
    python -m server.adapters.capture_nudge install --harness all
    python -m server.adapters.capture_nudge uninstall --harness all
    python -m server.adapters.capture_nudge status
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Optional

from server.journal.store import DEFAULT_JOURNAL_PATH

HARNESSES = ("claude_code", "codex", "antigravity")
NUDGE_MARKER = "cmf-capture-nudge"

DEFAULT_MIN_TURNS = 6
DEFAULT_MIN_MINUTES = 30.0
# Sessions idle this long are dropped from the state file.
STATE_TTL_SECONDS = 7 * 24 * 3600

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


def default_state_path() -> Path:
    override = os.getenv("CMF_NUDGE_STATE_PATH")
    if override:
        return Path(override).expanduser()
    return DEFAULT_JOURNAL_PATH.parent / "capture_nudge_state.json"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, ""))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, ""))
    except ValueError:
        return default


def session_key(harness: str, payload: dict[str, Any]) -> Optional[str]:
    if harness == "antigravity":
        sid = payload.get("conversationId")
    else:
        sid = payload.get("session_id")
    return f"{harness}:{sid}" if sid else None


def should_skip(harness: str, payload: dict[str, Any]) -> bool:
    """Stops that must never be nudged, regardless of the throttle."""
    if harness in ("claude_code", "codex") and payload.get("stop_hook_active"):
        return True
    if harness == "antigravity":
        # Only a real, finished, error-free stop; not a pause with background
        # tasks still running.
        if payload.get("error") or payload.get("fullyIdle") is False:
            return True
    return False


def decide(
    harness: str,
    payload: dict[str, Any],
    state: dict[str, Any],
    now: datetime,
    min_turns: int = DEFAULT_MIN_TURNS,
    min_minutes: float = DEFAULT_MIN_MINUTES,
) -> bool:
    """Update `state` in place for this stop and return True to nudge."""
    key = session_key(harness, payload)
    if key is None:
        return False
    sessions = state.setdefault("sessions", {})
    entry = sessions.setdefault(key, {"stops": 0, "window_start": now.isoformat(), "pending": False})
    entry["last_seen"] = now.isoformat()

    if entry.get("pending"):
        # The stop that ends the agent's reply to our own nudge.
        entry["pending"] = False
        return False
    if should_skip(harness, payload):
        return False

    entry["stops"] = int(entry.get("stops", 0)) + 1
    try:
        window_start = datetime.fromisoformat(entry["window_start"])
    except (KeyError, ValueError):
        window_start = now
        entry["window_start"] = now.isoformat()
    minutes = (now - window_start).total_seconds() / 60.0

    if entry["stops"] >= min_turns or (entry["stops"] >= 2 and minutes >= min_minutes):
        entry.update(
            stops=0,
            window_start=now.isoformat(),
            pending=True,
            nudges=int(entry.get("nudges", 0)) + 1,
            last_nudge=now.isoformat(),
        )
        return True
    return False


def render(harness: str, nudge: bool) -> dict[str, Any]:
    if harness == "antigravity":
        # `decision` is required; anything but "continue" lets it stop.
        return {"decision": "continue", "reason": NUDGE_REASON} if nudge else {"decision": "stop"}
    return {"decision": "block", "reason": NUDGE_REASON} if nudge else {}


def _load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _prune(state: dict[str, Any], now: datetime) -> None:
    sessions = state.get("sessions", {})
    for key in list(sessions):
        try:
            seen = datetime.fromisoformat(sessions[key].get("last_seen", ""))
        except ValueError:
            sessions.pop(key)
            continue
        if (now - seen).total_seconds() > STATE_TTL_SECONDS:
            sessions.pop(key)


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def run_hook(harness: str, stdin_text: str, state_path: Optional[Path] = None, now: Optional[datetime] = None) -> dict[str, Any]:
    """The hook body, minus the I/O: returns the JSON object to print."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        path = state_path or default_state_path()
        now = now or datetime.now(timezone.utc)
        state = _load_state(path)
        nudge = decide(
            harness,
            payload,
            state,
            now,
            min_turns=_env_int("CMF_NUDGE_MIN_TURNS", DEFAULT_MIN_TURNS),
            min_minutes=_env_float("CMF_NUDGE_MIN_MINUTES", DEFAULT_MIN_MINUTES),
        )
        _prune(state, now)
        _save_state(path, state)
        return render(harness, nudge)
    except Exception:
        return render(harness, False)


def hook_command(harness: str) -> str:
    repo_root = Path(__file__).resolve().parent.parent.parent
    return f"{repo_root}/.venv/bin/python3 -m server.adapters.capture_nudge run --harness {harness} # {NUDGE_MARKER}"


def _installers():
    from server.adapters.antigravity import hooks as antigravity_hooks
    from server.adapters.claude_code import hooks as claude_code_hooks
    from server.adapters.codex import hooks as codex_hooks

    return {"claude_code": claude_code_hooks, "codex": codex_hooks, "antigravity": antigravity_hooks}


def _selected(harness: str) -> tuple[str, ...]:
    return HARNESSES if harness == "all" else (harness,)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.adapters.capture_nudge", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="action", required=True)
    p_run = sub.add_parser("run", help="Hook body: read the Stop payload on stdin, print the decision JSON")
    p_run.add_argument("--harness", choices=HARNESSES, required=True)
    for name, help_text in (("install", "Add the nudge Stop hook"), ("uninstall", "Remove the nudge Stop hook")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--harness", choices=HARNESSES + ("all",), default="all")
    sub.add_parser("status", help="Show where the nudge is installed and per-session counts")
    args = parser.parse_args(argv)

    if args.action == "run":
        print(json.dumps(run_hook(args.harness, sys.stdin.read())))
        return 0

    installers = _installers()
    if args.action in ("install", "uninstall"):
        report = {}
        for harness in _selected(args.harness):
            module = installers[harness]
            fn = module.install_nudge if args.action == "install" else module.uninstall_nudge
            report[harness] = fn()
        print(json.dumps(report, indent=2))
        return 0

    state = _load_state(default_state_path())
    sessions = state.get("sessions", {})
    report = {
        "installed": {h: installers[h].nudge_installed() for h in HARNESSES},
        "state_path": str(default_state_path()),
        "sessions_tracked": len(sessions),
        "nudges_by_harness": {
            h: sum(int(e.get("nudges", 0)) for k, e in sessions.items() if k.startswith(f"{h}:")) for h in HARNESSES
        },
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
