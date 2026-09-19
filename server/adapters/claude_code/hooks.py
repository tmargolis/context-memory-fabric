"""settings.json merge-installer for the optional Stop/SessionStart hook
accelerant, plus the live hook-applicability sentinel test.

Deep-merge only under `hooks.<EventName>` -- never touches `Notification`
(already in real use, MS4a's own capture-health notification), `statusLine`,
or `enabledPlugins`. Backs the file up before writing (timestamped sibling,
never overwritten).

The hook body launches a fully detached worker and returns immediately --
"no synchronous LLM work" per plan-active.md's MS4b task list, since a
Claude Code/Desktop hook blocks the session until it returns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
import json
import shutil

DEFAULT_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Untouched by any merge this module performs.
PROTECTED_TOP_LEVEL_KEYS = {"statusLine", "enabledPlugins"}
PROTECTED_HOOK_EVENTS = {"Notification"}

HOOK_MARKER = "cmf-claude-code-worker"


def _worker_command() -> str:
    # nohup ... & disown: launches detached, returns to the hook caller
    # immediately -- the hook process must not block on a poll cycle.
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    return (
        f"nohup {repo_root}/.venv/bin/python -m server.adapters.claude_code.cli tail --once "
        f">> {Path.home()}/.claude/cmf-claude-code-worker.log 2>&1 & disown "
        f"# {HOOK_MARKER}"
    )


def build_hook_entry() -> dict[str, Any]:
    return {
        "matcher": "",
        "hooks": [{"type": "command", "command": _worker_command()}],
    }


def install_hooks(
    settings_path: Optional[Path] = None,
    events: tuple[str, ...] = ("SessionStart", "Stop"),
) -> dict[str, Any]:
    """Merge a detached-worker-launching hook entry under hooks.<event> for
    each event in `events`. Idempotent: re-running does not duplicate an
    already-installed CMF entry (matched via HOOK_MARKER in the command).

    Backs up the settings file first (never overwrites the backup).
    Returns a summary dict: {"installed": [...], "already_present": [...],
    "backup_path": str|None}.
    """
    settings_path = settings_path or DEFAULT_SETTINGS_PATH
    settings: dict[str, Any] = {}
    backup_path: Optional[Path] = None

    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = settings_path.with_suffix(f".json.bak-{timestamp}")
        shutil.copy2(settings_path, backup_path)

    hooks_block = settings.setdefault("hooks", {})
    installed = []
    already_present = []

    for event in events:
        if event in PROTECTED_HOOK_EVENTS:
            raise ValueError(f"refusing to touch protected hook event {event!r}")

        event_hooks = hooks_block.setdefault(event, [])
        if any(
            HOOK_MARKER in entry_hook.get("command", "")
            for entry in event_hooks
            for entry_hook in entry.get("hooks", [])
        ):
            already_present.append(event)
            continue

        event_hooks.append(build_hook_entry())
        installed.append(event)

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")

    return {
        "installed": installed,
        "already_present": already_present,
        "backup_path": str(backup_path) if backup_path else None,
    }


def install_sentinel_hooks(
    settings_path: Optional[Path] = None,
    sentinel_log: Optional[Path] = None,
) -> dict[str, Any]:
    """Live hook-applicability test: install a trivial SessionStart/Stop
    hook that just appends a timestamped line to `sentinel_log`. Used to
    confirm whether Desktop's Code tab actually fires these hooks at all --
    open a real Code-tab session afterward and check whether the log grew.

    Separate from install_hooks() (the real worker-launching hook) so this
    can be tried without also standing up the actual poller.
    """
    settings_path = settings_path or DEFAULT_SETTINGS_PATH
    sentinel_log = sentinel_log or (Path.home() / ".claude" / "cmf-hook-sentinel.log")

    settings: dict[str, Any] = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8"))

    hooks_block = settings.setdefault("hooks", {})
    command = f"date -u +%Y-%m-%dT%H:%M:%SZ >> {sentinel_log} # cmf-sentinel-test"

    for event in ("SessionStart", "Stop"):
        event_hooks = hooks_block.setdefault(event, [])
        if any("cmf-sentinel-test" in h.get("command", "") for e in event_hooks for h in e.get("hooks", [])):
            continue
        event_hooks.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return {"sentinel_log": str(sentinel_log), "settings_path": str(settings_path)}


def check_sentinel_fired(sentinel_log: Optional[Path] = None) -> bool:
    sentinel_log = sentinel_log or (Path.home() / ".claude" / "cmf-hook-sentinel.log")
    return sentinel_log.exists() and sentinel_log.stat().st_size > 0
