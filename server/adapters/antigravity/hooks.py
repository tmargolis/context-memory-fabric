"""hooks.json merge-installer for the Antigravity Stop-hook accelerant.

Antigravity's hooks schema (https://antigravity.google/docs/hooks/) is
hook-name-keyed at the top level, not event-keyed like Claude Code's
settings.json: `{"<hook-name>": {"Stop": [...], "PreToolUse": [...], ...}}`.
`PreInvocation`/`PostInvocation`/`Stop` take a flat handler list (no
matcher); `PreToolUse`/`PostToolUse` take `{"matcher": ..., "hooks": [...]}`
entries instead -- irrelevant here since this module only installs a `Stop`
hook.

Same "must not block the session" constraint as server.adapters.
claude_code.hooks: a hook command receives JSON on stdin and must return
JSON on stdout within its `timeout` (Antigravity default 30s) -- the hook
body here deliberately ignores that stdin payload and just launches a
detached worker pass, returning immediately. Nothing depends on trusting
hook-provided fields (conversationId, transcriptPath, etc.); the worker
discovers whatever changed on its own via server.adapters.antigravity.
transcript_reader.discover_transcript_files.

Global scope only (`~/.gemini/config/hooks.json`) -- per-workspace hooks
(`.agents/hooks.json`) are a distinct, narrower mechanism this module
doesn't touch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
import json

DEFAULT_HOOKS_PATH = Path.home() / ".gemini" / "config" / "hooks.json"

HOOK_NAME = "cmf-antigravity-worker"
HOOK_MARKER = HOOK_NAME


def _worker_command() -> str:
    # nohup ... & disown: launches detached, returns to the hook caller
    # immediately -- same rationale as claude_code/hooks.py's own
    # _worker_command, doubly so here since Antigravity's hook timeout
    # (30s default) is tighter than a real tail+consolidation pass.
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    return (
        f"nohup {repo_root}/.venv/bin/python -m server.adapters.antigravity.cli tail --once "
        f">> {Path.home()}/.claude/cmf-antigravity-worker.log 2>&1 & disown "
        f"# {HOOK_MARKER}"
    )


def build_hook_config() -> dict[str, Any]:
    return {
        "Stop": [
            {
                "type": "command",
                "command": _worker_command(),
                "timeout": 5,
            }
        ]
    }


def install_hooks(hooks_path: Optional[Path] = None) -> dict[str, Any]:
    """Merge the cmf-antigravity-worker Stop hook into hooks.json. Idempotent
    -- re-running does not duplicate an already-installed entry (matched via
    HOOK_MARKER in the command). Only ever touches its own top-level
    HOOK_NAME key; any other hook already present in the file is untouched.

    Returns {"installed": bool, "already_present": bool, "hooks_path": str}.
    """
    hooks_path = hooks_path or DEFAULT_HOOKS_PATH
    config: dict[str, Any] = {}
    if hooks_path.exists():
        config = json.loads(hooks_path.read_text(encoding="utf-8"))

    existing = config.get(HOOK_NAME)
    if existing is not None and any(
        HOOK_MARKER in h.get("command", "") for h in existing.get("Stop", [])
    ):
        return {"installed": False, "already_present": True, "hooks_path": str(hooks_path)}

    config[HOOK_NAME] = build_hook_config()
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    return {"installed": True, "already_present": False, "hooks_path": str(hooks_path)}
