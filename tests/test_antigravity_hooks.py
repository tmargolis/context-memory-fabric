"""Tests for server.adapters.antigravity.hooks -- always against a
tmp_path hooks.json, NEVER the real ~/.gemini/config/hooks.json.
"""

import json

from server.adapters.antigravity.hooks import HOOK_MARKER, HOOK_NAME, install_hooks


def test_install_hooks_adds_stop_hook(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    result = install_hooks(hooks_path)
    assert result["installed"] is True
    assert result["already_present"] is False

    data = json.loads(hooks_path.read_text())
    assert HOOK_MARKER in data[HOOK_NAME]["Stop"][0]["command"]


def test_install_hooks_is_idempotent(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    install_hooks(hooks_path)
    result = install_hooks(hooks_path)
    assert result["installed"] is False
    assert result["already_present"] is True

    data = json.loads(hooks_path.read_text())
    assert len(data[HOOK_NAME]["Stop"]) == 1  # not duplicated


def test_install_hooks_preserves_existing_other_hooks(tmp_path):
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(
        json.dumps({"some-other-plugin-hook": {"PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "existing"}]}]}})
    )
    install_hooks(hooks_path)

    data = json.loads(hooks_path.read_text())
    assert data["some-other-plugin-hook"]["PostToolUse"][0]["hooks"][0]["command"] == "existing"
    assert HOOK_NAME in data
