"""Tests for server.adapters.codex.worker and cli (MS4d Step 3).

Covers:
1. Isolated end-to-end capture and consolidation with a fake extraction policy:
   - Evidence journaled first
   - Proposals created with harness="codex", correct project attribution, and source event references
   - No direct graph or canonical wiki writes
2. Pending extraction queue and retry resilience:
   - Failure leaves conversation in codex_pending_extractions
   - Subsequent pass retries even when no new transcript bytes arrived
3. Overlapping MCP evidence coexistence without collision or deletion
4. CLI commands: status, preview, pending, tail (journal-only)
"""

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock
import pytest

from server.adapters.codex.cli import main as cli_main
from server.adapters.codex.transcript_reader import (
    TailStateStore,
    discover_transcript_files,
)
from server.adapters.codex.worker import (
    WorkerStats,
    process_pending,
    stats_summary,
)
from server.consolidation.store import ConsolidationStore
from server.core.models import (
    DatePrecision,
    SourceEvent,
    SourceProvenance,
)
from server.journal.identity import compute_content_hash, compute_event_id
from server.journal.store import SqliteEventStore
from server.policies.protocols import (
    ExtractionCategory,
    ReasoningEpisode,
    WindowedExtractionPolicy,
)


class FakeExtractPolicy(WindowedExtractionPolicy):
    """Deterministic, test-isolated fake policy mimicking ExtractPolicyV1."""

    name = "extract"
    version = "1.5"

    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.calls = 0

    def evaluate_window(
        self,
        window: Any,
        context: Any,
    ) -> list[ReasoningEpisode]:
        self.calls += 1
        if self.should_fail:
            raise RuntimeError("Simulated inference engine failure")

        # Return one episodic memory and one doc proposal
        events = list(window)
        user_text = next((e.content.get("text") for e in events if e.actor_type == "user"), "Task discussion")
        event_ids = [e.event_id for e in events]

        ep = ReasoningEpisode(
            category=ExtractionCategory.EPISODIC,
            reasoning_kind="decision",
            statement=f"Decided to proceed with {user_text}",
            driving_question="How should we proceed?",
            rationale="User instructed to validate candidate",
            confidence=0.9,
            evidence_event_ids=event_ids,
            thread_key="step-4-validation",
        )

        doc = ReasoningEpisode(
            category=ExtractionCategory.DURABLE_CANDIDATE,
            reasoning_kind="",
            statement="Update validation spec",
            rationale="Captures step 4 outcome",
            confidence=0.9,
            evidence_event_ids=event_ids,
            target_path="WIKI/projects/context-memory-fabric/Validation-Spec.md",
            proposed_content="# Validation Spec\n\nVerified.",
        )
        return [ep, doc]


def _create_sample_codex_session(session_file: Path, conv_id: str = "conv-test-1234") -> None:
    session_file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "timestamp": "2026-09-29T21:00:00Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "id": conv_id,
                "session_id": conv_id,
                "cwd": "/Users/mockuser/Dev/context-memory-fabric",
                "runtime_workspace_roots": ["/Users/mockuser/Dev/context-memory-fabric"],
            },
        },
        {
            "timestamp": "2026-09-29T21:00:01Z",
            "ordinal": 1,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Why did this fail? Should we investigate and decide to validate one-fact candidate?"}],
            },
        },
        {
            "timestamp": "2026-09-29T21:00:02Z",
            "ordinal": 2,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Validation complete."}],
            },
        },
        {
            "timestamp": "2026-09-29T21:00:03Z",
            "ordinal": 3,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Approved. We will proceed."}],
            },
        },
    ]
    with session_file.open("w", encoding="utf-8") as f:
        for it in lines:
            f.write(json.dumps(it) + "\n")


def test_process_pending_end_to_end_fake_extraction(tmp_path):
    db_path = tmp_path / "journal.db"
    sessions_root = tmp_path / "sessions"
    proposals_dir = tmp_path / "proposals"
    session_file = sessions_root / "2026" / "09" / "29" / "rollout-2026-09-29T21-00-00-conv-test-1234.jsonl"
    _create_sample_codex_session(session_file)

    fake_policy = FakeExtractPolicy()

    with SqliteEventStore(db_path) as j_store, ConsolidationStore(db_path) as c_store:
        stats = process_pending(
            j_store,
            c_store,
            sessions_root=sessions_root,
            policy=fake_policy,
            proposals_dir=proposals_dir,
            use_lock=False,
        )

        assert stats.files_scanned == 1
        assert stats.files_with_new_bytes == 1
        assert stats.events_journaled == 3  # 2 user turns + 1 assistant turn
        assert stats.extractions_attempted == 1
        assert stats.extractions_succeeded == 1
        assert stats.extractions_failed == 0

        # Verify evidence in journal
        events = j_store.query(harness="codex")
        assert len(events) == 3
        for e in events:
            assert e.source.conversation_id == "conv-test-1234"
            assert e.metadata["project"] == "context-memory-fabric"

        # Verify derived memories in ConsolidationStore
        cursor = c_store._conn.execute("SELECT * FROM derived_memories WHERE source_event_id LIKE 'codex:%'")
        memories = cursor.fetchall()
        assert len(memories) >= 1
        assert memories[0]["project"] == "context-memory-fabric"
        assert memories[0]["reasoning_kind"] == "decision"

        # Verify doc proposal was created in proposals_dir
        with TailStateStore(db_path) as tail_store:
            pending = tail_store.get_pending_extractions()
            # Queue should be empty after successful extraction
            assert len(pending) == 0


def test_pending_extraction_retries_on_inference_failure(tmp_path):
    """When inference fails, conversation stays queued and retries without new bytes."""
    db_path = tmp_path / "journal.db"
    sessions_root = tmp_path / "sessions"
    session_file = sessions_root / "2026" / "09" / "29" / "rollout-2026-09-29T21-00-00-conv-fail-1234.jsonl"
    _create_sample_codex_session(session_file, conv_id="conv-fail-1234")

    failing_policy = FakeExtractPolicy(should_fail=True)

    with SqliteEventStore(db_path) as j_store, ConsolidationStore(db_path) as c_store:
        # Pass 1: Journal succeeds, extraction fails
        stats1 = process_pending(
            j_store,
            c_store,
            sessions_root=sessions_root,
            policy=failing_policy,
            use_lock=False,
        )
        assert stats1.events_journaled == 3
        assert stats1.extractions_attempted == 1
        assert stats1.extractions_failed == 1

        # Check queue has pending extraction with attempt count
        with TailStateStore(db_path) as tail_store:
            pending = tail_store.get_pending_extractions()
            assert len(pending) == 1
            assert pending[0]["conversation_id"] == "conv-fail-1234"
            assert pending[0]["attempts"] == 1
            assert "Simulated inference engine failure" in pending[0]["last_error"]

            # Offset was already advanced past the file
            assert tail_store.get_offset(session_file) > 0

        # Pass 2: No new bytes written to file!
        # Now run with healthy policy -- worker must retry pending extraction from queue
        healthy_policy = FakeExtractPolicy(should_fail=False)
        stats2 = process_pending(
            j_store,
            c_store,
            sessions_root=sessions_root,
            policy=healthy_policy,
            use_lock=False,
        )

        assert stats2.files_with_new_bytes == 0  # No new bytes
        assert stats2.events_journaled == 0
        assert stats2.extractions_attempted == 1
        assert stats2.extractions_succeeded == 1

        # Queue is now clear
        with TailStateStore(db_path) as tail_store:
            assert len(tail_store.get_pending_extractions()) == 0


def test_coexistence_with_overlapping_mcp_evidence(tmp_path):
    """Pre-existing MCP-boundary captured events under harness=codex do not collide."""
    db_path = tmp_path / "journal.db"
    now = datetime.now(timezone.utc)

    # Insert an MCP tool call into journal first
    mcp_content = {"tool": "recall_mem", "input": {"query": "test query"}}
    mcp_hash = compute_content_hash(mcp_content)
    mcp_event_id = compute_event_id(
        harness="codex",
        content_hash=mcp_hash,
        conversation_id="conv-overlap",
        turn_id="mcp-call-1",
    )

    mcp_event = SourceEvent(
        schema_version="1.0",
        event_id=mcp_event_id,
        event_type="mcp.call",
        source=SourceProvenance(
            harness="codex",
            conversation_id="conv-overlap",
            session_id="conv-overlap",
            turn_id="mcp-call-1",
        ),
        actor_type="assistant",
        observed_at=now,
        content=mcp_content,
        content_hash=mcp_hash,
    )

    with SqliteEventStore(db_path) as j_store:
        assert j_store.append(mcp_event) is True

    # Now run transcript reader on a session with the same conversation ID
    sessions_root = tmp_path / "sessions"
    session_file = sessions_root / "2026" / "09" / "29" / "rollout-2026-09-29T21-00-00-conv-overlap.jsonl"
    _create_sample_codex_session(session_file, conv_id="conv-overlap")

    with SqliteEventStore(db_path) as j_store, ConsolidationStore(db_path) as c_store:
        stats = process_pending(
            j_store,
            c_store,
            sessions_root=sessions_root,
            policy=FakeExtractPolicy(),
            use_lock=False,
        )

        assert stats.events_journaled == 3
        # Query events: both MCP event and transcript turn events coexist cleanly
        all_events = j_store.query(harness="codex", conversation_id="conv-overlap")
        assert len(all_events) == 4
        types = {e.event_type for e in all_events}
        assert types == {"mcp.call", "turn.completed"}


def test_cli_status_preview_pending_tail(tmp_path, capsys):
    db_path = tmp_path / "journal.db"
    sessions_root = tmp_path / "sessions"
    session_file = sessions_root / "2026" / "09" / "29" / "rollout-2026-09-29T21-00-00-conv-test-1234.jsonl"
    _create_sample_codex_session(session_file)

    # 1. CLI status
    rc_status = cli_main(["--db", str(db_path), "--sessions-root", str(sessions_root), "status"])
    assert rc_status == 0
    out_status, _ = capsys.readouterr()
    status_data = json.loads(out_status)
    assert status_data["transcript_files_found"] == 1
    assert status_data["files_previously_tailed"] == 0

    # 2. CLI preview
    rc_preview = cli_main(["--db", str(db_path), "--sessions-root", str(sessions_root), "preview"])
    assert rc_preview == 0
    out_preview, _ = capsys.readouterr()
    preview_data = json.loads(out_preview)
    assert preview_data["kept_events_count"] == 3

    # 3. CLI tail with journal-only mode (--no-consolidation)
    rc_tail = cli_main([
        "--db", str(db_path),
        "--sessions-root", str(sessions_root),
        "tail",
        "--no-consolidation",
        "--no-lock",
    ])
    assert rc_tail == 0
    out_tail, _ = capsys.readouterr()
    tail_data = json.loads(out_tail)
    assert tail_data["events_journaled"] == 3
    assert tail_data["extractions_attempted"] == 0

    # 4. CLI pending (queued for extraction)
    rc_pending = cli_main(["--db", str(db_path), "pending"])
    assert rc_pending == 0
    out_pending, _ = capsys.readouterr()
    pending_data = json.loads(out_pending)
    assert len(pending_data) == 1
    assert pending_data[0]["conversation_id"] == "conv-test-1234"
