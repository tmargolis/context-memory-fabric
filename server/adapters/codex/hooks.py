"""hooks.json merge-installer for Codex lifecycle hooks (MS4d Step 4).

Supports:
1. Stop hook: accelerates capture at the end of a turn/response.
2. SessionStart hook: catches up and enqueues newly started sessions.
3. Sentinel test hooks: verifies whether the installed Codex client actually
   fires hooks, before full poller or worker operational activation.
4. Launchd poller plist generator matching the 900-second pattern from
   Claude Code and Antigravity.

Invariants (from docs/CODEX-CAPTURE-PLAN.md):
- Non-blocking: Commands output valid JSON ({}) immediately and launch a detached
  worker (`nohup ... & disown`) so Codex session execution is never stalled.
- Idempotent: Re-running does not duplicate entries (tracked by HOOK_MARKER).
- Safe merge: Backs up hooks.json before writing (.bak-<timestamp>) and leaves
  all user hooks, other event keys, and config.toml notification commands untouched.
- Clean uninstall: Removes only CMF entries and prunes empty event arrays.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from typing import Any, Optional

DEFAULT_HOOKS_PATH = Path.home() / ".codex" / "hooks.json"
DEFAULT_LOG_PATH = Path.home() / ".codex" / "cmf-codex-worker.log"
DEFAULT_SENTINEL_LOG = Path.home() / ".codex" / "cmf-hook-sentinel.log"

HOOK_MARKER = "cmf-codex-worker"
SENTINEL_MARKER = "cmf-codex-sentinel"
LAUNCHD_LABEL = "local.cmf.codex-poller"
DEFAULT_POLLER_INTERVAL_SECONDS = 900


def _get_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent.parent


def _worker_command(
    repo_root: Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> str:
    """Build non-blocking detached command that outputs valid JSON and launches worker."""
    root = repo_root or _get_repo_root()
    log = log_path or DEFAULT_LOG_PATH
    venv_py = root / ".venv" / "bin" / "python3"
    # Codex Stop hooks expect valid JSON on stdout; output {} immediately,
    # then spawn background detached worker pass with disown.
    return (
        f'sh -c \'echo "{{}}" && nohup {venv_py} -m server.adapters.codex.cli tail --once '
        f">> {log} 2>&1 & disown' # {HOOK_MARKER}"
    )


def build_hook_entry(
    repo_root: Optional[Path] = None,
    log_path: Optional[Path] = None,
    timeout: int = 5,
) -> dict[str, Any]:
    return {
        "type": "command",
        "command": _worker_command(repo_root=repo_root, log_path=log_path),
        "timeout": timeout,
    }


def _load_config(path: Path) -> dict[str, Any]:
    """Read and normalize legacy flat entries without changing the source file.

    Codex expects {"hooks": {event: [{"hooks": [handler]}]}}. Preserve
    matcher groups and metadata; refuse malformed input instead of dropping it.
    """
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("Hooks configuration must be an object")
    events = config.setdefault("hooks", {})
    if not isinstance(events, dict):
        raise ValueError("hooks must be an object")
    for event in list(config):
        if event in ("hooks", "description"):
            continue
        legacy = config[event]
        if not isinstance(legacy, list):
            raise ValueError(f"Unexpected hooks configuration field: {event}")
        existing = events.setdefault(event, [])
        if not isinstance(existing, list):
            raise ValueError(f"Hook event {event} must be an array")
        existing.extend(legacy)
        del config[event]
    for event, groups in events.items():
        if not isinstance(groups, list):
            raise ValueError(f"Hook event {event} must be an array")
        normalized = []
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError(f"Hook group for {event} must be an object")
            if "type" in group and "hooks" not in group:
                group = {"hooks": [group]}
            if not isinstance(group.get("hooks"), list) or any(
                not isinstance(handler, dict) for handler in group["hooks"]
            ):
                raise ValueError(f"Hook group for {event} needs a hooks array")
            normalized.append(group)
        events[event] = normalized
    return config


def _save_config(path: Path, config: dict[str, Any]) -> Optional[Path]:
    backup = None
    if path.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_suffix(f".json.bak-{timestamp}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return backup


def _handlers(groups: list[dict[str, Any]]):
    for group in groups:
        yield from group["hooks"]


def _has_marker(groups: list[dict[str, Any]], marker: str) -> bool:
    return any(marker in h.get("command", "") for h in _handlers(groups))


def _remove_markers(events: dict[str, Any], markers: tuple[str, ...]) -> list[str]:
    removed = []
    for event, groups in list(events.items()):
        kept_groups = []
        changed = False
        for group in groups:
            kept = [h for h in group["hooks"] if not any(
                marker in h.get("command", "") for marker in markers
            )]
            if len(kept) == len(group["hooks"]):
                kept_groups.append(group)
            else:
                changed = True
                if kept:
                    kept_groups.append({**group, "hooks": kept})
        if changed:
            removed.append(event)
            if kept_groups:
                events[event] = kept_groups
            else:
                del events[event]
    return removed


def install_hooks(
    hooks_path: Optional[Path] = None,
    events: tuple[str, ...] = ("SessionStart", "Stop"),
    repo_root: Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Merge worker handlers into Codex's nested schema, migrating legacy files."""
    path = hooks_path or DEFAULT_HOOKS_PATH
    config = _load_config(path)
    installed, already_present = [], []
    for event in events:
        groups = config["hooks"].setdefault(event, [])
        if _has_marker(groups, HOOK_MARKER):
            already_present.append(event)
        else:
            groups.append({"hooks": [build_hook_entry(repo_root, log_path)]})
            installed.append(event)
    backup = _save_config(path, config)
    return {
        "installed": installed, "already_present": already_present,
        "hooks_path": str(path), "backup_path": str(backup) if backup else None,
    }


def uninstall_hooks(hooks_path: Optional[Path] = None) -> dict[str, Any]:
    """Remove only worker/sentinel handlers, preserving other grouped handlers."""
    path = hooks_path or DEFAULT_HOOKS_PATH
    if not path.exists():
        return {"uninstalled": [], "hooks_path": str(path), "backup_path": None}
    config = _load_config(path)
    removed = _remove_markers(config["hooks"], (HOOK_MARKER, SENTINEL_MARKER))
    backup = _save_config(path, config)
    return {"uninstalled": removed, "hooks_path": str(path), "backup_path": str(backup)}


def install_sentinel_hooks(
    hooks_path: Optional[Path] = None,
    sentinel_log: Optional[Path] = None,
) -> dict[str, Any]:
    """Install lightweight, grouped hooks that record lifecycle execution."""
    path = hooks_path or DEFAULT_HOOKS_PATH
    s_log = sentinel_log or DEFAULT_SENTINEL_LOG
    config = _load_config(path)
    command = f"sh -c 'echo \"{{}}\" && date +%Y-%m-%dT%H:%M:%S%z >> {s_log}' # {SENTINEL_MARKER}"
    installed = []
    for event in ("SessionStart", "Stop"):
        groups = config["hooks"].setdefault(event, [])
        if not _has_marker(groups, SENTINEL_MARKER):
            groups.append({"hooks": [{"type": "command", "command": command, "timeout": 5}]})
            installed.append(event)
    _save_config(path, config)
    return {"installed": installed, "sentinel_log": str(s_log), "hooks_path": str(path)}


def check_sentinel_fired(sentinel_log: Optional[Path] = None) -> bool:
    s_log = sentinel_log or DEFAULT_SENTINEL_LOG
    return s_log.exists() and s_log.stat().st_size > 0


def get_hooks_status(hooks_path: Optional[Path] = None) -> dict[str, Any]:
    """Inspect installation; distinguish legacy configuration needing migration."""
    path = hooks_path or DEFAULT_HOOKS_PATH
    status = {"hooks_path": str(path), "file_exists": path.exists(),
              "cmf_hooks_installed": False, "events_configured": {}}
    if not path.exists():
        return status
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        config = _load_config(path)
    except (ValueError, OSError) as exc:
        return {**status, "error": str(exc)}
    cmf_events = []
    summary = {}
    for event, groups in config["hooks"].items():
        has_cmf = _has_marker(groups, HOOK_MARKER)
        if has_cmf:
            cmf_events.append(event)
        summary[event] = {
            "total_handlers": len(list(_handlers(groups))),
            "has_cmf_worker": has_cmf,
            "has_sentinel": _has_marker(groups, SENTINEL_MARKER),
        }
    return {**status, "cmf_hooks_installed": bool(cmf_events),
            "cmf_events": cmf_events, "events_configured": summary,
            "requires_migration": raw != config}


def generate_launchd_plist(
    repo_root: Optional[Path] = None,
    home_dir: Optional[Path] = None,
    interval_seconds: int = DEFAULT_POLLER_INTERVAL_SECONDS,
) -> str:
    """Generate launchd plist XML content for the Codex 900s background poller."""
    root = (repo_root or _get_repo_root()).resolve()
    home = (home_dir or Path.home()).resolve()
    python_bin = root / ".venv" / "bin" / "python3"
    out_log = home / "Library" / "Logs" / "cmf-codex-poller.log"
    err_log = home / "Library" / "Logs" / "cmf-codex-poller-err.log"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>{LAUNCHD_LABEL}</string>
	<key>ProgramArguments</key>
	<array>
		<string>{python_bin}</string>
		<string>-m</string>
		<string>server.adapters.codex.cli</string>
		<string>tail</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardErrorPath</key>
	<string>{err_log}</string>
	<key>StandardOutPath</key>
	<string>{out_log}</string>
	<key>StartInterval</key>
	<integer>{interval_seconds}</integer>
	<key>WorkingDirectory</key>
	<string>{root}</string>
</dict>
</plist>
"""


def install_nudge(hooks_path: Optional[Path] = None) -> dict[str, Any]:
    """Install a synchronous Stop nudge; trust remains a user action in /hooks."""
    from server.adapters.capture_nudge import NUDGE_MARKER, hook_command

    path = hooks_path or DEFAULT_HOOKS_PATH
    config = _load_config(path)
    groups = config["hooks"].setdefault("Stop", [])
    present = _has_marker(groups, NUDGE_MARKER)
    if not present:
        groups.append({"hooks": [{"type": "command", "command": hook_command("codex"), "timeout": 10}]})
    # Save even if present: a legacy flat configuration still needs migration.
    backup = _save_config(path, config)
    return {"installed": not present, "already_present": present,
            "hooks_path": str(path), "backup_path": str(backup) if backup else None}


def uninstall_nudge(hooks_path: Optional[Path] = None) -> dict[str, Any]:
    """Remove only capture-nudge handlers, preserving matcher groups and metadata."""
    from server.adapters.capture_nudge import NUDGE_MARKER

    path = hooks_path or DEFAULT_HOOKS_PATH
    if not path.exists():
        return {"removed": False, "hooks_path": str(path)}
    config = _load_config(path)
    removed = _remove_markers(config["hooks"], (NUDGE_MARKER,))
    backup = _save_config(path, config)
    return {"removed": bool(removed), "hooks_path": str(path), "backup_path": str(backup)}


def nudge_installed(hooks_path: Optional[Path] = None) -> bool:
    from server.adapters.capture_nudge import NUDGE_MARKER

    path = hooks_path or DEFAULT_HOOKS_PATH
    try:
        config = _load_config(path)
    except (ValueError, OSError):
        return False
    return _has_marker(config["hooks"].get("Stop", []), NUDGE_MARKER)
