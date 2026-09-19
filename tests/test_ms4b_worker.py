"""Tests for server.adapters.claude_code.worker.process_pending against
tmp_path transcript fixtures and a tmp journal.db -- never the real
~/.claude/projects tree or production journal.
"""

import json

from server.adapters.claude_code.worker import process_pending
from server.journal.store import SqliteEventStore


def _write_lines(path, lines):
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _user_turn(uuid, text, parent=None):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": "2026-09-18T12:00:00.000Z",
        "message": {"role": "user", "content": text},
    }


def test_process_pending_journals_new_events_and_dedupes_on_rerun(tmp_path):
    project_dir = tmp_path / "projects" / "-p"
    project_dir.mkdir(parents=True)
    transcript = project_dir / "session-1.jsonl"
    _write_lines(transcript, [_user_turn("u1", "hello there, first real turn")])

    db_path = tmp_path / "journal.db"
    with SqliteEventStore(db_path) as store:
        stats = process_pending(store, projects_root=tmp_path / "projects", run_consolidation=False)
        assert stats.files_scanned == 1
        assert stats.files_with_new_bytes == 1
        assert stats.events_journaled == 1
        assert stats.events_deduped == 0

        # Re-running with no new bytes: nothing new to tail, nothing to dedupe either.
        stats2 = process_pending(store, projects_root=tmp_path / "projects", run_consolidation=False)
        assert stats2.files_with_new_bytes == 0
        assert stats2.events_journaled == 0


def test_process_pending_appends_new_turn_on_second_pass(tmp_path):
    project_dir = tmp_path / "projects" / "-p"
    project_dir.mkdir(parents=True)
    transcript = project_dir / "session-1.jsonl"
    _write_lines(transcript, [_user_turn("u1", "first turn")])

    db_path = tmp_path / "journal.db"
    with SqliteEventStore(db_path) as store:
        process_pending(store, projects_root=tmp_path / "projects", run_consolidation=False)

        _write_lines(transcript, [_user_turn("u2", "second turn", parent="u1")])
        stats = process_pending(store, projects_root=tmp_path / "projects", run_consolidation=False)
        assert stats.events_journaled == 1

        event = store.get("claude_code:session-1:u2")
        assert event is not None
        assert event.parent_event_ids == ["claude_code:session-1:u1"]


def test_process_pending_skips_project_denied_via_env(tmp_path, monkeypatch):
    project_dir = tmp_path / "projects" / "-secret-project"
    project_dir.mkdir(parents=True)
    transcript = project_dir / "session-1.jsonl"
    _write_lines(transcript, [_user_turn("u1", "should not be journaled")])

    monkeypatch.setenv("CMF_CLAUDE_CODE_PROJECT_DENY", "-secret-project")
    db_path = tmp_path / "journal.db"
    with SqliteEventStore(db_path) as store:
        stats = process_pending(store, projects_root=tmp_path / "projects", run_consolidation=False)
        assert stats.files_scanned == 0
        assert stats.events_journaled == 0
