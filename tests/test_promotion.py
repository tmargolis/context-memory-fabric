"""Tests for the auto_accepted -> Graphiti promotion step (server.consolidation.promotion).

remember() is faked throughout — no real Gemini/FalkorDB dependency. This
tests the promotion bookkeeping (idempotency, dry-run, partial-failure
isolation, rate-limit-exhaustion handling), not Graphiti behavior itself.
"""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from server.consolidation.promotion import PromotionStore, promote_auto_accepted
from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.core.rate_limiter import GeminiQuotaExhaustedError
from server.journal.store import SqliteEventStore
from server.policies.protocols import ExtractionCategory, ExtractionResult


def make_event(event_id: str, harness: str = "chatgpt") -> SourceEvent:
    return SourceEvent(
        schema_version="1.0",
        event_id=event_id,
        event_type="turn.completed",
        source=SourceProvenance(harness=harness, conversation_id="conv-1", turn_id=event_id),
        observed_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        content={"text": "Decided to use SQLite for the journal."},
        content_hash="sha256:deadbeef",
        actor_type="user",
    )


def make_auto_accepted_result() -> ExtractionResult:
    return ExtractionResult(
        category=ExtractionCategory.EPISODIC,
        statement="Decided to use SQLite for the journal.",
        reason="test fixture",
        confidence=0.9,
        event_date=datetime(2026, 3, 1, tzinfo=timezone.utc),
        date_precision=DatePrecision.DAY,
    )


class TestPromoteAutoAccepted(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "journal.db"
        self.journal = SqliteEventStore(db_path=self.db_path)
        self.consolidation = ConsolidationStore(db_path=self.db_path)
        self.promotion_store = PromotionStore(db_path=self.db_path)
        self.remembered_calls = []

    def tearDown(self):
        self.journal.close()
        self.consolidation.close()
        self.promotion_store.close()
        self.tmp_dir.cleanup()

    def _seed_auto_accepted(self, event_id: str) -> str:
        event = make_event(event_id)
        self.journal.append(event)
        result = make_auto_accepted_result()
        memory_id = f"{event_id}::heuristic-pattern@1.0"
        self.consolidation.record_consolidation(
            job_id=f"job:{memory_id}",
            memory_id=memory_id,
            source_event_id=event_id,
            policy_name="heuristic-pattern",
            policy_version="1.0",
            result=result,
            approval_state="auto_accepted",
            supersedes=None,
        )
        return memory_id

    async def _fake_remember(self, content, name=None, source_description="", reference_time=None):
        self.remembered_calls.append({"content": content, "name": name, "reference_time": reference_time})
        return {"status": "success", "name": name, "reference_time": (reference_time or datetime.now(timezone.utc)).isoformat()}

    async def test_dry_run_previews_without_calling_remember(self):
        self._seed_auto_accepted("evt-1")
        result = await promote_auto_accepted(
            self.consolidation, self.journal, self.promotion_store, self._fake_remember, dry_run=True
        )
        self.assertEqual(result["eligible_this_run"], 1)
        self.assertEqual(len(result["promoted_preview"]), 1)
        self.assertEqual(self.remembered_calls, [])

    async def test_commits_and_records_success(self):
        memory_id = self._seed_auto_accepted("evt-2")
        result = await promote_auto_accepted(
            self.consolidation, self.journal, self.promotion_store, self._fake_remember, dry_run=False, inter_call_delay=0
        )
        self.assertEqual(len(result["promoted"]), 1)
        self.assertEqual(len(self.remembered_calls), 1)
        self.assertTrue(self.promotion_store.is_promoted(memory_id))

    async def test_second_run_is_idempotent(self):
        self._seed_auto_accepted("evt-3")
        await promote_auto_accepted(self.consolidation, self.journal, self.promotion_store, self._fake_remember, dry_run=False, inter_call_delay=0)
        self.assertEqual(len(self.remembered_calls), 1)

        result2 = await promote_auto_accepted(
            self.consolidation, self.journal, self.promotion_store, self._fake_remember, dry_run=False, inter_call_delay=0
        )
        self.assertEqual(result2["already_promoted"], 1)
        self.assertEqual(result2["eligible_this_run"], 0)
        self.assertEqual(len(self.remembered_calls), 1)  # not called again

    async def test_only_auto_accepted_rows_are_considered(self):
        event = make_event("evt-4")
        self.journal.append(event)
        result = make_auto_accepted_result()
        self.consolidation.record_consolidation(
            job_id="job:evt-4::x@1.0",
            memory_id="evt-4::x@1.0",
            source_event_id="evt-4",
            policy_name="x",
            policy_version="1.0",
            result=result,
            approval_state="queued_for_review",
            supersedes=None,
        )
        result = await promote_auto_accepted(
            self.consolidation, self.journal, self.promotion_store, self._fake_remember, dry_run=True
        )
        self.assertEqual(result["candidates_considered"], 0)

    async def test_failure_on_one_row_does_not_block_others(self):
        self._seed_auto_accepted("evt-ok")
        self._seed_auto_accepted("evt-bad")

        async def flaky_remember(content, name=None, source_description="", reference_time=None):
            if "evt-bad" in source_description:
                raise RuntimeError("simulated failure")
            self.remembered_calls.append(content)
            return {"status": "success", "name": name, "reference_time": datetime.now(timezone.utc).isoformat()}

        result = await promote_auto_accepted(
            self.consolidation, self.journal, self.promotion_store, flaky_remember, dry_run=False, inter_call_delay=0
        )
        # One succeeds, one fails — both attempted, neither silently dropped.
        self.assertEqual(len(result["promoted"]) + len(result["failed"]), 2)

    async def test_rate_limit_exhaustion_stops_early_without_losing_remaining_candidates(self):
        self._seed_auto_accepted("evt-5")
        self._seed_auto_accepted("evt-6")

        async def exhausted_remember(content, name=None, source_description="", reference_time=None):
            raise GeminiQuotaExhaustedError("no headroom")

        result = await promote_auto_accepted(
            self.consolidation, self.journal, self.promotion_store, exhausted_remember, dry_run=False, inter_call_delay=0
        )
        self.assertTrue(result["stopped_early"])
        self.assertEqual(len(result["promoted"]), 0)
        self.assertEqual(len(result["failed"]), 0)
        # Neither candidate got a terminal (succeeded/failed) record — both
        # remain eligible for a later retry.
        self.assertFalse(self.promotion_store.is_promoted("evt-5::heuristic-pattern@1.0"))

    async def test_limit_caps_candidates_attempted(self):
        self._seed_auto_accepted("evt-7")
        self._seed_auto_accepted("evt-8")
        self._seed_auto_accepted("evt-9")
        result = await promote_auto_accepted(
            self.consolidation, self.journal, self.promotion_store, self._fake_remember, dry_run=False, inter_call_delay=0, limit=1
        )
        self.assertEqual(result["eligible_this_run"], 1)
        self.assertEqual(len(self.remembered_calls), 1)


if __name__ == "__main__":
    unittest.main()
