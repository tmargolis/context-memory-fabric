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
import logging
from pathlib import Path
import shutil
from typing import Any, Optional

logger = logging.getLogger(__name__)

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


def install_hooks(
    hooks_path: Optional[Path] = None,
    events: tuple[str, ...] = ("SessionStart", "Stop"),
    repo_root: Optional[Path] = None,
    log_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Merge CMF hook entries into hooks.json under root event keys.

    Backs up the file first (timestamped .bak-<timestamp>).
    Preserves all existing user hooks and unrelated event keys.
    """
    path = hooks_path or DEFAULT_HOOKS_PATH
    config: dict[str, Any] = {}
    backup_path: Optional[Path] = None

    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Could not parse existing {path}: {exc}") from exc

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = path.with_suffix(f".json.bak-{timestamp}")
        shutil.copy2(path, backup_path)

    installed = []
    already_present = []

    for event in events:
        event_hooks = config.setdefault(event, [])
        if not isinstance(event_hooks, list):
            logger.warning("Event %s in %s is not a list; replacing with empty list", event, path)
            event_hooks = []
            config[event] = event_hooks

        if any(HOOK_MARKER in h.get("command", "") for h in event_hooks if isinstance(h, dict)):
            already_present.append(event)
            continue

        entry = build_hook_entry(repo_root=repo_root, log_path=log_path)
        event_hooks.append(entry)
        installed.append(event)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    return {
        "installed": installed,
        "already_present": already_present,
        "hooks_path": str(path),
        "backup_path": str(backup_path) if backup_path else None,
    }


def uninstall_hooks(
    hooks_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Remove CMF hook entries from hooks.json.

    Backs up the file first. Preserves all other entries.
    """
    path = hooks_path or DEFAULT_HOOKS_PATH
    if not path.exists():
        return {"uninstalled": [], "hooks_path": str(path), "backup_path": None}

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Could not parse existing {path}: {exc}") from exc

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_suffix(f".json.bak-{timestamp}")
    shutil.copy2(path, backup_path)

    uninstalled = []
    keys_to_delete = []

    for event, hooks in list(config.items()):
        if not isinstance(hooks, list):
            continue
        original_len = len(hooks)
        filtered = [
            h for h in hooks
            if not (isinstance(h, dict) and (HOOK_MARKER in h.get("command", "") or SENTINEL_MARKER in h.get("command", "")))
        ]
        if len(filtered) < original_len:
            uninstalled.append(event)
            if filtered:
                config[event] = filtered
            else:
                keys_to_delete.append(event)

    for k in keys_to_delete:
        config.pop(k, None)

    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    return {
        "uninstalled": uninstalled,
        "hooks_path": str(path),
        "backup_path": str(backup_path),
    }


def install_sentinel_hooks(
    hooks_path: Optional[Path] = None,
    sentinel_log: Optional[Path] = None,
) -> dict[str, Any]:
    """Install a lightweight sentinel hook to test whether Codex triggers hooks."""
    path = hooks_path or DEFAULT_HOOKS_PATH
    s_log = sentinel_log or DEFAULT_SENTINEL_LOG

    config: dict[str, Any] = {}
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))

    command = f'sh -c \'echo "{{}}" && date +%Y-%m-%dT%H:%M:%S%z >> {s_log}\' # {SENTINEL_MARKER}'
    installed = []

    for event in ("SessionStart", "Stop"):
        event_hooks = config.setdefault(event, [])
        if any(SENTINEL_MARKER in h.get("command", "") for h in event_hooks if isinstance(h, dict)):
            continue
        event_hooks.append({
            "type": "command",
            "command": command,
            "timeout": 5,
        })
        installed.append(event)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return {"installed": installed, "sentinel_log": str(s_log), "hooks_path": str(path)}


def check_sentinel_fired(sentinel_log: Optional[Path] = None) -> bool:
    """Check if the sentinel hook logged any executions."""
    s_log = sentinel_log or DEFAULT_SENTINEL_LOG
    return s_log.exists() and s_log.stat().st_size > 0


def get_hooks_status(hooks_path: Optional[Path] = None) -> dict[str, Any]:
    """Inspect current hook installation status."""
    path = hooks_path or DEFAULT_HOOKS_PATH
    if not path.exists():
        return {
            "hooks_path": str(path),
            "file_exists": False,
            "cmf_hooks_installed": False,
            "events_configured": {},
        }

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "hooks_path": str(path),
            "file_exists": True,
            "error": f"Invalid JSON: {exc}",
            "cmf_hooks_installed": False,
            "events_configured": {},
        }

    cmf_events = []
    events_summary = {}

    for event, hooks in config.items():
        if isinstance(hooks, list):
            has_cmf = any(HOOK_MARKER in h.get("command", "") for h in hooks if isinstance(h, dict))
            has_sentinel = any(SENTINEL_MARKER in h.get("command", "") for h in hooks if isinstance(h, dict))
            if has_cmf:
                cmf_events.append(event)
            events_summary[event] = {
                "total_handlers": len(hooks),
                "has_cmf_worker": has_cmf,
                "has_sentinel": has_sentinel,
            }

    return {
        "hooks_path": str(path),
        "file_exists": True,
        "cmf_hooks_installed": len(cmf_events) > 0,
        "cmf_events": cmf_events,
        "events_configured": events_summary,
    }


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


def _has_nudge(event_hooks: Any) -> bool:
    from server.adapters.capture_nudge import NUDGE_MARKER

    return isinstance(event_hooks, list) and any(
        NUDGE_MARKER in h.get("command", "") for h in event_hooks if isinstance(h, dict)
    )


def install_nudge(hooks_path: Optional[Path] = None) -> dict[str, Any]:
    """Add the capture-nudge Stop hook (server.adapters.capture_nudge),
    synchronous because Codex acts on its stdout decision. Codex runs it
    only after the hook is trusted via `/hooks` in the CLI. Idempotent;
    backs hooks.json up first."""
    from server.adapters.capture_nudge import hook_command

    path = hooks_path or DEFAULT_HOOKS_PATH
    config: dict[str, Any] = {}
    backup_path: Optional[Path] = None
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Could not parse existing {path}: {exc}") from exc
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = path.with_suffix(f".json.bak-{timestamp}")
        shutil.copy2(path, backup_path)

    stop_hooks = config.get("Stop")
    if not isinstance(stop_hooks, list):
        stop_hooks = []
        config["Stop"] = stop_hooks
    if _has_nudge(stop_hooks):
        return {"installed": False, "already_present": True, "hooks_path": str(path)}

    stop_hooks.append({"type": "command", "command": hook_command("codex"), "timeout": 10})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return {
        "installed": True,
        "already_present": False,
        "hooks_path": str(path),
        "backup_path": str(backup_path) if backup_path else None,
    }


def uninstall_nudge(hooks_path: Optional[Path] = None) -> dict[str, Any]:
    """Remove only the capture-nudge Stop entry; prune Stop if it empties."""
    from server.adapters.capture_nudge import NUDGE_MARKER

    path = hooks_path or DEFAULT_HOOKS_PATH
    if not path.exists():
        return {"removed": False, "hooks_path": str(path)}
    config = json.loads(path.read_text(encoding="utf-8"))
    stop_hooks = config.get("Stop")
    if not _has_nudge(stop_hooks):
        return {"removed": False, "hooks_path": str(path)}

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_suffix(f".json.bak-{timestamp}")
    shutil.copy2(path, backup_path)
    kept = [h for h in stop_hooks if not (isinstance(h, dict) and NUDGE_MARKER in h.get("command", ""))]
    if kept:
        config["Stop"] = kept
    else:
        config.pop("Stop", None)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    return {"removed": True, "hooks_path": str(path), "backup_path": str(backup_path)}


def nudge_installed(hooks_path: Optional[Path] = None) -> bool:
    path = hooks_path or DEFAULT_HOOKS_PATH
    if not path.exists():
        return False
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    return _has_nudge(config.get("Stop"))
