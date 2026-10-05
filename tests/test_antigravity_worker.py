"""Tests for server.adapters.antigravity.worker.process_pending against
tmp_path transcript fixtures and a tmp journal.db -- never the real
~/.gemini/antigravity tree or production journal.
"""

from datetime import datetime
import json

from server.adapters.antigravity.worker import process_pending
from server.journal.store import SqliteEventStore


def _transcript_path(app_data_dir, conversation_id):
    path = app_data_dir / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_lines(path, lines):
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")


def _user_turn(step_index, text, created_at="2026-09-23T14:44:30Z"):
    return {
        "step_index": step_index,
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "status": "DONE",
        "created_at": created_at,
        "content": text,
    }


def test_process_pending_journals_new_events_and_dedupes_on_rerun(tmp_path):
    app_dir = tmp_path / "antigravity"
    transcript = _transcript_path(app_dir, "conv-1")
    _write_lines(transcript, [_user_turn(0, "hello there, first real turn")])

    db_path = tmp_path / "journal.db"
    with SqliteEventStore(db_path) as store:
        stats = process_pending(store, app_data_dirs=[app_dir], run_consolidation=False)
        assert stats.files_scanned == 1
        assert stats.files_with_new_bytes == 1
        assert stats.events_journaled == 1
        assert stats.events_deduped == 0

        stats2 = process_pending(store, app_data_dirs=[app_dir], run_consolidation=False)
        assert stats2.files_with_new_bytes == 0
        assert stats2.events_journaled == 0


def test_process_pending_appends_new_turn_on_second_pass(tmp_path):
    app_dir = tmp_path / "antigravity"
    transcript = _transcript_path(app_dir, "conv-1")
    _write_lines(transcript, [_user_turn(0, "first turn")])

    db_path = tmp_path / "journal.db"
    with SqliteEventStore(db_path) as store:
        process_pending(store, app_data_dirs=[app_dir], run_consolidation=False)

        _write_lines(transcript, [_user_turn(1, "second turn")])
        stats = process_pending(store, app_data_dirs=[app_dir], run_consolidation=False)
        assert stats.events_journaled == 1

        event = store.get("antigravity:conv-1:1")
        assert event is not None
        assert event.content["text"] == "second turn"


def test_process_pending_scans_multiple_app_data_dirs(tmp_path):
    app_dir_a = tmp_path / "antigravity"
    app_dir_b = tmp_path / "antigravity-ide"
    _write_lines(_transcript_path(app_dir_a, "conv-a"), [_user_turn(0, "from antigravity")])
    _write_lines(_transcript_path(app_dir_b, "conv-b"), [_user_turn(0, "from antigravity-ide")])

    db_path = tmp_path / "journal.db"
    with SqliteEventStore(db_path) as store:
        stats = process_pending(store, app_data_dirs=[app_dir_a, app_dir_b], run_consolidation=False)
        assert stats.files_scanned == 2
        assert stats.events_journaled == 2


def test_until_cutoff_leaves_offset_at_last_kept_line_for_later_backfill(tmp_path):
    """The behavior this adapter specifically needed beyond claude_code's:
    a --until-capped pass must not advance the tail offset past lines after
    the cutoff, so a later since-only pass still sees them."""
    app_dir = tmp_path / "antigravity"
    transcript = _transcript_path(app_dir, "conv-1")
    _write_lines(
        transcript,
        [
            _user_turn(0, "before midnight", created_at="2026-09-23T23:00:00Z"),
            _user_turn(1, "after midnight", created_at="2026-09-24T01:00:00Z"),
        ],
    )

    db_path = tmp_path / "journal.db"
    cutoff = datetime.fromisoformat("2026-09-24T00:00:00+00:00")
    with SqliteEventStore(db_path) as store:
        stats = process_pending(store, app_data_dirs=[app_dir], run_consolidation=False, until=cutoff)
        assert stats.events_journaled == 1
        assert store.get("antigravity:conv-1:0") is not None
        assert store.get("antigravity:conv-1:1") is None

        # A later, wider pass (no until) must still pick up the post-cutoff line.
        stats2 = process_pending(store, app_data_dirs=[app_dir], run_consolidation=False)
        assert stats2.events_journaled == 1
        assert store.get("antigravity:conv-1:1") is not None


def test_since_drops_events_but_does_not_cap_offset(tmp_path):
    app_dir = tmp_path / "antigravity"
    transcript = _transcript_path(app_dir, "conv-1")
    _write_lines(transcript, [_user_turn(0, "old turn", created_at="2020-01-01T00:00:00Z")])

    db_path = tmp_path / "journal.db"
    since = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
    with SqliteEventStore(db_path) as store:
        stats = process_pending(store, app_data_dirs=[app_dir], run_consolidation=False, since=since)
        assert stats.events_journaled == 0
        assert stats.events_skipped_before_cutoff == 1

        # Offset advanced past the dropped line -- rerunning with the same
        # since doesn't re-count it.
        stats2 = process_pending(store, app_data_dirs=[app_dir], run_consolidation=False, since=since)
        assert stats2.events_skipped_before_cutoff == 0
        assert stats2.files_with_new_bytes == 0
