"""Tests for server.adapters.claude_code.hooks -- always against a
tmp_path settings.json, NEVER the real ~/.claude/settings.json. This repo's
own MS4a2 note: "Notification already in use" is a real, live hook block on
this machine that must never be touched by a test.
"""

import json

from server.adapters.claude_code.hooks import (
    HOOK_MARKER,
    check_sentinel_fired,
    install_hooks,
    install_sentinel_hooks,
)


def test_install_hooks_adds_sessionstart_and_stop(tmp_path):
    settings_path = tmp_path / "settings.json"
    result = install_hooks(settings_path)
    assert set(result["installed"]) == {"SessionStart", "Stop"}
    assert result["backup_path"] is None  # no file existed to back up

    data = json.loads(settings_path.read_text())
    assert HOOK_MARKER in data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert HOOK_MARKER in data["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_install_hooks_is_idempotent(tmp_path):
    settings_path = tmp_path / "settings.json"
    install_hooks(settings_path)
    result = install_hooks(settings_path)
    assert result["installed"] == []
    assert set(result["already_present"]) == {"SessionStart", "Stop"}

    data = json.loads(settings_path.read_text())
    assert len(data["hooks"]["SessionStart"]) == 1  # not duplicated


def test_install_hooks_preserves_existing_notification_block(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"hooks": {"Notification": [{"matcher": "", "hooks": [{"type": "command", "command": "existing-notif"}]}]}})
    )
    install_hooks(settings_path)

    data = json.loads(settings_path.read_text())
    assert data["hooks"]["Notification"][0]["hooks"][0]["command"] == "existing-notif"
    assert "SessionStart" in data["hooks"]
    assert "Stop" in data["hooks"]


def test_install_hooks_backs_up_existing_file(tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({"statusLine": {"type": "static", "value": "ok"}}))
    result = install_hooks(settings_path)

    assert result["backup_path"] is not None
    backup = json.loads(open(result["backup_path"]).read())
    assert backup == {"statusLine": {"type": "static", "value": "ok"}}

    # statusLine untouched by the merge.
    data = json.loads(settings_path.read_text())
    assert data["statusLine"] == {"type": "static", "value": "ok"}


def test_install_hooks_refuses_notification_event(tmp_path):
    import pytest

    settings_path = tmp_path / "settings.json"
    with pytest.raises(ValueError):
        install_hooks(settings_path, events=("Notification",))


def test_sentinel_hook_lifecycle(tmp_path):
    settings_path = tmp_path / "settings.json"
    sentinel_log = tmp_path / "sentinel.log"

    assert check_sentinel_fired(sentinel_log) is False

    install_sentinel_hooks(settings_path, sentinel_log)
    data = json.loads(settings_path.read_text())
    assert "SessionStart" in data["hooks"]
    assert "Stop" in data["hooks"]

    # Simulate the hook actually firing.
    sentinel_log.write_text("2026-09-18T12:00:00Z\n")
    assert check_sentinel_fired(sentinel_log) is True
