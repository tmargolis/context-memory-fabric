"""Tests for server.adapters.codex.hooks (MS4d Step 4).

Covers:
1. Idempotent hooks installer:
   - Merges SessionStart and Stop under hooks.json nested groups
   - Creates timestamped backup before modification
   - Preserves existing user hooks and unrelated keys
   - Re-running is a no-op (does not duplicate)
2. Safe uninstaller:
   - Removes only CMF entries
   - Preserves user hooks
   - Creates backup
   - Prunes empty event arrays
3. Sentinel hook installer and detection:
   - Installs test hook that outputs valid JSON and appends timestamp to log
   - Verifies check_sentinel_fired detects log writes
4. Launchd poller plist generator:
   - Proper label: local.cmf.codex-poller
   - StartInterval: 900 seconds
   - Explicit venv python interpreter, working directory, and standard error/out logs
5. Hook nonblocking structure:
   - Hook command starts with valid JSON output (`echo "{}"`) and launches detached
6. Polling recovery on missed hooks:
   - When hooks fail to fire, scheduled poller (`tail`) catches up evidence and pending extractions
"""

import json
from pathlib import Path
import xml.etree.ElementTree as ET
import pytest

from server.adapters.codex.cli import main as cli_main
from server.adapters.codex.hooks import (
    HOOK_MARKER,
    LAUNCHD_LABEL,
    SENTINEL_MARKER,
    check_sentinel_fired,
    generate_launchd_plist,
    get_hooks_status,
    install_hooks,
    install_sentinel_hooks,
    uninstall_hooks,
)
from server.adapters.codex.transcript_reader import (
    TailStateStore,
    discover_transcript_files,
)
from server.adapters.codex.worker import process_pending
from server.journal.store import SqliteEventStore


def test_install_hooks_fresh_and_idempotent(tmp_path):
    hooks_file = tmp_path / "hooks.json"
    repo_root = tmp_path / "repo"
    log_path = tmp_path / "worker.log"

    # Pass 1: Fresh install
    res1 = install_hooks(hooks_path=hooks_file, repo_root=repo_root, log_path=log_path)
    assert res1["installed"] == ["SessionStart", "Stop"]
    assert res1["already_present"] == []
    assert hooks_file.exists()

    data = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]
    assert "SessionStart" in data
    assert "Stop" in data
    assert len(data["SessionStart"]) == 1
    assert len(data["Stop"]) == 1
    cmd = data["Stop"][0]["hooks"][0]["command"]
    assert HOOK_MARKER in cmd
    assert 'echo "{}"' in cmd
    assert "tail --once" in cmd

    # Pass 2: Re-install must be idempotent
    res2 = install_hooks(hooks_path=hooks_file, repo_root=repo_root, log_path=log_path)
    assert res2["installed"] == []
    assert res2["already_present"] == ["SessionStart", "Stop"]

    data2 = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]
    assert len(data2["SessionStart"]) == 1
    assert len(data2["Stop"]) == 1

    # Check backup was created on re-install
    backups = list(tmp_path.glob("hooks.json.bak-*"))
    assert len(backups) >= 1


def test_install_and_uninstall_preserves_user_hooks(tmp_path):
    hooks_file = tmp_path / "hooks.json"
    user_config = {
        "Stop": [
            {
                "type": "command",
                "command": "/usr/local/bin/user-notifier.sh",
                "timeout": 10,
            }
        ],
        "UserPromptSubmit": [
            {
                "type": "command",
                "command": "python3 /path/to/custom-guard.py",
            }
        ],
    }
    hooks_file.write_text(json.dumps(user_config, indent=2) + "\n", encoding="utf-8")

    # Install CMF hooks
    install_hooks(hooks_path=hooks_file)
    installed_data = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]

    # User's other event key preserved
    assert "UserPromptSubmit" in installed_data
    assert len(installed_data["UserPromptSubmit"]) == 1

    # Stop now has 2 entries: user's and CMF's
    assert len(installed_data["Stop"]) == 2
    assert installed_data["Stop"][0]["hooks"][0]["command"] == "/usr/local/bin/user-notifier.sh"
    assert HOOK_MARKER in installed_data["Stop"][1]["hooks"][0]["command"]

    # Now uninstall CMF hooks
    unres = uninstall_hooks(hooks_path=hooks_file)
    assert "Stop" in unres["uninstalled"]
    assert "SessionStart" in unres["uninstalled"]

    uninstalled_data = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]
    # User's Stop hook and UserPromptSubmit hook are preserved exactly
    assert "UserPromptSubmit" in uninstalled_data
    assert len(uninstalled_data["Stop"]) == 1
    assert uninstalled_data["Stop"][0]["hooks"][0]["command"] == "/usr/local/bin/user-notifier.sh"
    # SessionStart was empty so it was pruned
    assert "SessionStart" not in uninstalled_data


def test_sentinel_hooks_lifecycle(tmp_path):
    hooks_file = tmp_path / "hooks.json"
    sentinel_log = tmp_path / "sentinel.log"

    # Initially sentinel has not fired
    assert not check_sentinel_fired(sentinel_log)

    # Install sentinel
    res = install_sentinel_hooks(hooks_path=hooks_file, sentinel_log=sentinel_log)
    assert res["installed"] == ["SessionStart", "Stop"]

    data = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]
    assert any(SENTINEL_MARKER in h["command"] for group in data["Stop"] for h in group["hooks"])

    # Simulate sentinel firing (writing a line to log)
    sentinel_log.write_text("2026-09-30T10:00:00+0000\n", encoding="utf-8")
    assert check_sentinel_fired(sentinel_log)

    # Uninstall removes sentinel too
    uninstall_hooks(hooks_path=hooks_file)
    data_after = json.loads(hooks_file.read_text(encoding="utf-8"))["hooks"]
    assert "Stop" not in data_after
    assert "SessionStart" not in data_after


def test_get_hooks_status(tmp_path):
    hooks_file = tmp_path / "hooks.json"

    # Status when file doesn't exist
    st1 = get_hooks_status(hooks_file)
    assert not st1["file_exists"]
    assert not st1["cmf_hooks_installed"]

    # Status after install
    install_hooks(hooks_path=hooks_file)
    st2 = get_hooks_status(hooks_file)
    assert st2["file_exists"]
    assert st2["cmf_hooks_installed"]
    assert "Stop" in st2["cmf_events"]
    assert "SessionStart" in st2["cmf_events"]


def test_generate_launchd_plist(tmp_path):
    repo_root = tmp_path / "my_repo"
    home_dir = tmp_path / "mock_home"
    plist_xml = generate_launchd_plist(repo_root=repo_root, home_dir=home_dir, interval_seconds=900)

    # Parse XML
    root = ET.fromstring(plist_xml)
    assert root.tag == "plist"
    dict_elem = root.find("dict")
    assert dict_elem is not None

    keys = [elem.text for elem in dict_elem.findall("key")]
    assert "Label" in keys
    assert "ProgramArguments" in keys
    assert "StartInterval" in keys
    assert "WorkingDirectory" in keys

    assert LAUNCHD_LABEL in plist_xml
    assert "server.adapters.codex.cli" in plist_xml
    assert "<integer>900</integer>" in plist_xml
    assert str(repo_root) in plist_xml
    assert str(home_dir / "Library" / "Logs" / "cmf-codex-poller.log") in plist_xml


def test_cli_hooks_subcommands(tmp_path, capsys):
    hooks_file = tmp_path / "hooks.json"
    sentinel_log = tmp_path / "sentinel.log"

    # 1. hooks install
    rc_inst = cli_main(["hooks", "--hooks-path", str(hooks_file), "install"])
    assert rc_inst == 0
    out_inst, _ = capsys.readouterr()
    assert '"installed"' in out_inst

    # 2. hooks status
    rc_stat = cli_main(["hooks", "--hooks-path", str(hooks_file), "status"])
    assert rc_stat == 0
    out_stat, _ = capsys.readouterr()
    assert '"cmf_hooks_installed": true' in out_stat

    # 3. hooks sentinel
    rc_sent = cli_main([
        "hooks",
        "--hooks-path", str(hooks_file),
        "--sentinel-log", str(sentinel_log),
        "sentinel",
    ])
    assert rc_sent == 0

    # 4. hooks plist
    plist_out = tmp_path / "local.cmf.codex-poller.plist"
    rc_plist = cli_main(["hooks", "plist", "--out", str(plist_out)])
    assert rc_plist == 0
    assert plist_out.exists()
    assert LAUNCHD_LABEL in plist_out.read_text(encoding="utf-8")

    # 5. hooks uninstall
    rc_uninst = cli_main(["hooks", "--hooks-path", str(hooks_file), "uninstall"])
    assert rc_uninst == 0
    out_uninst, _ = capsys.readouterr()
    assert '"uninstalled"' in out_uninst


def test_polling_recovers_missed_hooks(tmp_path):
    """If hooks never fire, scheduled polling (tail) recovers all missed sessions cleanly."""
    db_path = tmp_path / "journal.db"
    sessions_root = tmp_path / "sessions"
    session_file = sessions_root / "2026" / "09" / "30" / "rollout-2026-09-30T10-00-00-conv-missed-hook.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)

    # User conducted a session, but no hook was triggered
    lines = [
        {
            "timestamp": "2026-09-30T10:00:00Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "id": "conv-missed-hook",
                "session_id": "conv-missed-hook",
                "cwd": "/Users/mockuser/Dev/context-memory-fabric",
            },
        },
        {
            "timestamp": "2026-09-30T10:00:01Z",
            "ordinal": 1,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Testing poller catch-up"}],
            },
        },
        {
            "timestamp": "2026-09-30T10:00:02Z",
            "ordinal": 2,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Poller will catch this."}],
            },
        },
    ]
    with session_file.open("w", encoding="utf-8") as f:
        for it in lines:
            f.write(json.dumps(it) + "\n")

    # Scheduled 900s launchd poller runs `tail`
    with SqliteEventStore(db_path) as j_store:
        stats = process_pending(
            j_store,
            sessions_root=sessions_root,
            run_consolidation=False,  # journal only
            use_lock=False,
        )

        assert stats.files_scanned == 1
        assert stats.files_with_new_bytes == 1
        assert stats.events_journaled == 2
        assert "conv-missed-hook" in stats.conversations_touched

        events = j_store.query(harness="codex", conversation_id="conv-missed-hook")
        assert len(events) == 2

        # Extraction was enqueued for processing
        with TailStateStore(db_path) as tail_store:
            pending = tail_store.get_pending_extractions()
            assert len(pending) == 1
            assert pending[0]["conversation_id"] == "conv-missed-hook"


def test_migrate_legacy_nudge_without_duplicate(tmp_path):
    from server.adapters.capture_nudge import NUDGE_MARKER
    from server.adapters.codex.hooks import install_nudge, nudge_installed

    path = tmp_path / "hooks.json"
    legacy = {"Stop": [{"type": "command", "command": f"echo test # {NUDGE_MARKER}"}]}
    original = json.dumps(legacy)
    path.write_text(original)
    assert get_hooks_status(path)["requires_migration"]
    result = install_nudge(path)
    assert result["already_present"]
    assert Path(result["backup_path"]).read_text() == original
    assert json.loads(path.read_text()) == {"hooks": {"Stop": [{"hooks": legacy["Stop"]}]}}
    assert nudge_installed(path)
    assert not get_hooks_status(path)["requires_migration"]


def test_nested_groups_preserve_matchers_and_unrelated_handlers(tmp_path):
    from server.adapters.capture_nudge import NUDGE_MARKER
    from server.adapters.codex.hooks import uninstall_nudge

    path = tmp_path / "hooks.json"
    user = {"type": "command", "command": "echo user", "timeout": 7}
    config = {"description": "User hooks", "hooks": {"Stop": [
        {"matcher": "", "hooks": [user, {"type": "command", "command": f"echo # {NUDGE_MARKER}"}]}
    ], "PreToolUse": [{"matcher": "Bash", "hooks": [user]}]}}
    path.write_text(json.dumps(config))
    install_hooks(path)
    uninstall_nudge(path)
    uninstall_hooks(path)
    expected = {"description": "User hooks", "hooks": {
        "Stop": [{"matcher": "", "hooks": [user]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [user]}],
    }}
    assert json.loads(path.read_text()) == expected


def test_invalid_event_shape_is_not_overwritten(tmp_path):
    path = tmp_path / "hooks.json"
    original = '{"hooks": {"Stop": "invalid"}}'
    path.write_text(original)
    with pytest.raises(ValueError):
        install_hooks(path)
    assert path.read_text() == original
