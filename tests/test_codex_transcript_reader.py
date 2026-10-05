"""Tests for server.adapters.codex.transcript_reader.

Covers:
1. TailStateStore persistence, offset tracking, and initial capture cutoff.
2. WorkerLock non-blocking mutual exclusion.
3. read_new_lines: complete line boundary enforcement, partial line deferral,
   and file truncation/replacement detection.
4. discover_transcript_files: directory discovery, explicit file scoping, filtering.
5. preview_transcript_file: true read-only preview without journal or tail updates.
6. capture_file_to_journal: incremental capture, offset progression, crash safety,
   dedup, and repeat-pass no-op guarantee.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from server.adapters.codex.transcript_reader import (
    CaptureResult,
    TailStateStore,
    WorkerLock,
    capture_file_to_journal,
    discover_transcript_files,
    preview_transcript_file,
    read_new_lines,
)
from server.journal.store import SqliteEventStore


def _write_jsonl(path: Path, items: list[dict], trailing_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it) + "\n")
        if not trailing_newline and items:
            # write without trailing newline
            pass


def test_tail_state_store_offsets(tmp_path):
    db_path = tmp_path / "journal.db"
    file_a = tmp_path / "session_a.jsonl"

    with TailStateStore(db_path) as store:
        assert store.get_offset(file_a) == 0

        store.set_offset(
            file_a,
            session_id="sess-1",
            conversation_id="conv-1",
            project_slug="my-proj",
            byte_offset=150,
            line_count_delta=5,
        )
        assert store.get_offset(file_a) == 150
        state = store.get_tail_state(file_a)
        assert state["byte_offset"] == 150
        assert state["last_line_count"] == 5
        assert state["session_id"] == "sess-1"
        assert state["project_slug"] == "my-proj"

        # Updating advances offset and accumulates lines
        store.set_offset(
            file_a,
            byte_offset=300,
            line_count_delta=4,
        )
        assert store.get_offset(file_a) == 300
        state2 = store.get_tail_state(file_a)
        assert state2["byte_offset"] == 300
        assert state2["last_line_count"] == 9
        assert state2["session_id"] == "sess-1"  # preserved via COALESCE


def test_tail_state_initial_cutoff(tmp_path):
    db_path = tmp_path / "journal.db"
    with TailStateStore(db_path) as store:
        assert store.get_initial_cutoff() is None

        now = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
        cutoff = store.get_or_create_initial_cutoff(default_now=now)
        assert cutoff == now

        # Subsequent call returns the persisted cutoff
        later = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
        assert store.get_or_create_initial_cutoff(default_now=later) == now
        assert store.get_initial_cutoff() == now


def test_worker_lock_mutual_exclusion(tmp_path):
    lock_file = tmp_path / "worker.lock"
    lock1 = WorkerLock(lock_file)
    lock2 = WorkerLock(lock_file)

    assert lock1.acquire() is True
    # Second acquisition fails while lock1 is held
    assert lock2.acquire() is False

    lock1.release()
    # Now lock2 can acquire
    assert lock2.acquire() is True
    lock2.release()


def test_read_new_lines_complete_and_partial(tmp_path):
    file_path = tmp_path / "rollout-test.jsonl"
    line1 = json.dumps({"timestamp": "2026-09-29T21:00:00Z", "ordinal": 0, "type": "session_meta", "payload": {"id": "s1"}})
    line2 = json.dumps({"timestamp": "2026-09-29T21:00:01Z", "ordinal": 1, "type": "turn_context", "payload": {"turn_id": "t1"}})
    partial = '{"timestamp": "2026-09-29T21:00:02Z", "ordinal": 2, "type": "respo'

    file_path.write_bytes(f"{line1}\n{line2}\n{partial}".encode("utf-8"))

    results = list(read_new_lines(file_path, start_offset=0))
    # Only the first two complete lines should be yielded
    assert len(results) == 2
    assert json.loads(results[0][0])["ordinal"] == 0
    assert json.loads(results[1][0])["ordinal"] == 1
    offset_after_line2 = results[1][1]

    # Append completion of the partial line
    with file_path.open("ab") as f:
        f.write(b'nse_item"}\n')

    # Read starting from offset_after_line2
    res2 = list(read_new_lines(file_path, start_offset=offset_after_line2))
    assert len(res2) == 1
    assert json.loads(res2[0][0])["type"] == "response_item"


def test_read_new_lines_file_truncation_resets_offset(tmp_path):
    file_path = tmp_path / "truncated.jsonl"
    file_path.write_text("line1\nline2\nline3\n", encoding="utf-8")
    initial_size = file_path.stat().st_size

    # Overwrite file with smaller content
    file_path.write_text("new_line1\n", encoding="utf-8")

    # Asking for initial_size should reset to 0 and read new_line1
    results = list(read_new_lines(file_path, start_offset=initial_size))
    assert len(results) == 1
    assert results[0][0].strip() == "new_line1"


def test_discover_transcript_files(tmp_path):
    root = tmp_path / "sessions"
    file1 = root / "2026" / "09" / "28" / "rollout-2026-09-28T10-00-00-11111111-2222-3333-4444-555555555555.jsonl"
    file2 = root / "2026" / "09" / "29" / "rollout-2026-09-29T12-00-00-66666666-7777-8888-9999-000000000000.jsonl"
    ignored = root / "2026" / "09" / "29" / "notes.txt"

    for f in (file1, file2, ignored):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("{}\n")

    discovered = discover_transcript_files(root)
    assert len(discovered) == 2
    assert discovered[0].session_id == "11111111-2222-3333-4444-555555555555"
    assert discovered[1].session_id == "66666666-7777-8888-9999-000000000000"

    # Test explicit_path scoping
    explicit = discover_transcript_files(root, explicit_path=file2)
    assert len(explicit) == 1
    assert explicit[0].path == file2

    # Test session filter
    filtered = discover_transcript_files(root, session_id_filter="11111111")
    assert len(filtered) == 1
    assert filtered[0].session_id == "11111111-2222-3333-4444-555555555555"

    # Test limit
    limited = discover_transcript_files(root, limit=1)
    assert len(limited) == 1


def test_preview_transcript_file_is_read_only(tmp_path):
    db_path = tmp_path / "journal.db"
    file_path = tmp_path / "rollout-test.jsonl"

    lines = [
        {"timestamp": "2026-09-29T21:00:00Z", "ordinal": 0, "type": "session_meta", "payload": {"id": "s1", "cwd": "/Users/mockuser/Dev/proj1"}},
        {"timestamp": "2026-09-29T21:00:01Z", "ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}},
        {"timestamp": "2026-09-29T21:00:02Z", "ordinal": 2, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "world"}]}},
    ]
    _write_jsonl(file_path, lines)

    with TailStateStore(db_path) as tail_store:
        preview = preview_transcript_file(file_path, tail_store)
        assert preview["start_offset"] == 0
        assert preview["final_offset"] > 0
        assert preview["lines_seen"] == 3
        assert preview["kept_events_count"] == 2
        assert preview["project"] == "proj1"

        # Tail state remains untouched (read-only)
        assert tail_store.get_offset(file_path) == 0


def test_capture_file_to_journal_incremental_and_idempotent(tmp_path):
    db_path = tmp_path / "journal.db"
    file_path = tmp_path / "rollout-capture-test.jsonl"

    lines = [
        {"timestamp": "2026-09-29T21:00:00Z", "ordinal": 0, "type": "session_meta", "payload": {"id": "s1", "cwd": "/Users/mockuser/Dev/proj1"}},
        {"timestamp": "2026-09-29T21:00:01Z", "ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}},
        {"timestamp": "2026-09-29T21:00:02Z", "ordinal": 2, "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "world"}]}},
    ]
    _write_jsonl(file_path, lines)

    with SqliteEventStore(db_path) as j_store, TailStateStore(db_path) as t_store:
        # First capture
        res1 = capture_file_to_journal(file_path, j_store, t_store)
        assert res1.lines_read == 3
        assert res1.events_captured == 2
        assert res1.events_deduped == 0
        assert res1.failed_records == 0
        assert t_store.get_offset(file_path) > 0

        # Check events in journal
        events = j_store.query(harness="codex")
        assert len(events) == 2
        assert events[0].actor_type == "user"
        assert events[1].actor_type == "assistant"
        assert events[0].metadata["project"] == "proj1"

        # Second capture pass is a no-op (no new lines in file)
        res2 = capture_file_to_journal(file_path, j_store, t_store)
        assert res2.lines_read == 0
        assert res2.events_captured == 0
        assert res2.events_deduped == 0

        # Append new line and capture again
        _write_jsonl(file_path, [
            {"timestamp": "2026-09-29T21:00:03Z", "ordinal": 3, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "next turn"}]}}
        ])

        res3 = capture_file_to_journal(file_path, j_store, t_store)
        assert res3.lines_read == 1
        assert res3.events_captured == 1
        assert len(j_store.query(harness="codex")) == 3


def test_capture_crash_recovery_preserves_offset(tmp_path):
    """If an error occurs while writing an event, the offset stops before the failed line."""
    db_path = tmp_path / "journal.db"
    file_path = tmp_path / "rollout-crash-test.jsonl"

    lines = [
        {"timestamp": "2026-09-29T21:00:00Z", "ordinal": 0, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "good line 1"}]}},
        {"timestamp": "2026-09-29T21:00:01Z", "ordinal": 1, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "bad line 2"}]}},
    ]
    _write_jsonl(file_path, lines)

    class FailingStore:
        def __init__(self, real_store):
            self.real_store = real_store
            self.call_count = 0

        def append(self, ev):
            self.call_count += 1
            if self.call_count == 2:
                raise RuntimeError("simulated disk/db error")
            return self.real_store.append(ev)

    with SqliteEventStore(db_path) as j_store, TailStateStore(db_path) as t_store:
        failing_j_store = FailingStore(j_store)
        res = capture_file_to_journal(file_path, failing_j_store, t_store)
        assert res.events_captured == 1
        assert res.failed_records == 1

        # The offset must only have advanced past line 1, not line 2
        offset_after_crash = t_store.get_offset(file_path)
        assert offset_after_crash > 0

        # Now run again with healthy store: line 2 is retried and captured successfully
        res_retry = capture_file_to_journal(file_path, j_store, t_store)
        assert res_retry.events_captured == 1
        assert res_retry.failed_records == 0
        assert len(j_store.query(harness="codex")) == 2
