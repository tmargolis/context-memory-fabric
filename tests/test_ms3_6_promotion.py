"""MS3.6 — reasoning-episode promotion + coverage-based auto-resolve.

remember() is faked; no Gemini/FalkorDB. Tests the bookkeeping: explicit
list-driven promotion, idempotency, per-row failure isolation, quota
clean-stop, tier routing, and the heuristic-backlog auto-resolve
(idempotent + reversible).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from server.consolidation.promotion import (
    PromotionStore,
    default_tier,
    promote_reviewed,
    tier1_review_queue,
)
from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.core.rate_limiter import GeminiQuotaExhaustedError
from server.journal.identity import compute_content_hash
from server.journal.store import SqliteEventStore
from server.policies.protocols import ExtractionCategory, ReasoningEpisode

BASE = datetime(2026, 5, 1, tzinfo=timezone.utc)


def ev(event_id, text="working through the backend choice", actor="user", harness="claude"):
    content = {"text": text}
    return SourceEvent(
        schema_version="1.0",
        event_id=event_id,
        event_type="turn.completed",
        source=SourceProvenance(harness=harness, conversation_id="c1", turn_id=event_id),
        observed_at=BASE,
        content=content,
        content_hash=compute_content_hash(content),
        actor_type=actor,
    )


def episode(kind="decision", evidence=("e0",), statement="Chose SQLite for the journal.", conf=0.9):
    return ReasoningEpisode(
        category=ExtractionCategory.EPISODIC,
        reasoning_kind=kind,
        statement=statement,
        confidence=conf,
        evidence_event_ids=list(evidence),
        event_date=BASE,
        date_precision=DatePrecision.DAY,
    )


class MS36Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        p = Path(self._tmp.name) / "journal.db"
        self.journal = SqliteEventStore(db_path=p)
        self.cons = ConsolidationStore(db_path=p)
        self.prom = PromotionStore(db_path=p)
        self.calls = []

    def tearDown(self):
        self.journal.close(); self.cons.close(); self.prom.close(); self._tmp.cleanup()

    async def ok_remember(self, **kw):
        self.calls.append(kw)
        return {"ok": True}

    def _reason_row(self, mid, epi):
        self.cons.record_reasoning_episode(
            job_id=f"job:{mid}", memory_id=mid, episode=epi,
            policy_name="reasoning-episode", policy_version="0.2",
            approval_state="queued_for_review", supersedes=None,
        )

    def _heuristic_row(self, mid, source_event_id, state="queued_for_review"):
        from server.policies.protocols import ExtractionResult
        self.cons.record_consolidation(
            job_id=f"job:{mid}", memory_id=mid, source_event_id=source_event_id,
            policy_name="heuristic-pattern", policy_version="1.2",
            result=ExtractionResult(category=ExtractionCategory.AMBIGUOUS, statement="a turn", reason="x", confidence=0.2),
            approval_state=state, supersedes=None,
        )


class TestDefaultTier(unittest.TestCase):
    def test_kind_routing(self):
        for k in ("decision", "plan", "retrospective", "rejected_alternative"):
            self.assertEqual(default_tier(k), 1)
        for k in ("investigation", "experiment", "hypothesis", "finding", None, ""):
            self.assertEqual(default_tier(k), 2)


class TestPromoteReviewed(MS36Base):
    async def test_list_driven_dry_run_then_commit_then_idempotent(self):
        self.journal.append(ev("e0")); self.journal.append(ev("e1"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        self._reason_row("r:1::reasoning-episode@0.2", episode(kind="plan", evidence=["e1"], statement="Plan the migration."))
        ids = ["r:0::reasoning-episode@0.2", "r:1::reasoning-episode@0.2"]

        dry = await promote_reviewed(self.cons, self.journal, self.prom, self.ok_remember, ids, dry_run=True)
        self.assertEqual(dry["eligible_this_run"], 2)
        self.assertEqual(len(self.calls), 0)
        self.assertEqual([p["memory_id"] for p in dry["promoted_preview"]], ids)

        run = await promote_reviewed(self.cons, self.journal, self.prom, self.ok_remember, ids, dry_run=False, inter_call_delay=0)
        self.assertEqual(len(run["promoted"]), 2)
        self.assertEqual(len(self.calls), 2)
        # metadata carried into remember()
        self.assertIn("reasoning_kind=decision", self.calls[0]["source_description"])
        self.assertIn("evidence=1 turn(s)", self.calls[0]["source_description"])

        again = await promote_reviewed(self.cons, self.journal, self.prom, self.ok_remember, ids, dry_run=False, inter_call_delay=0)
        self.assertEqual(again["already_promoted"], 2)
        self.assertEqual(again["eligible_this_run"], 0)
        self.assertEqual(len(self.calls), 2)  # no new calls

    async def test_not_found_ids_reported(self):
        self.journal.append(ev("e0"))
        self._reason_row("r:real::reasoning-episode@0.2", episode(evidence=["e0"]))
        run = await promote_reviewed(
            self.cons, self.journal, self.prom, self.ok_remember,
            ["r:real::reasoning-episode@0.2", "r:ghost::reasoning-episode@0.2"], dry_run=True,
        )
        self.assertEqual(run["not_found"], ["r:ghost::reasoning-episode@0.2"])
        self.assertEqual(run["eligible_this_run"], 1)

    async def test_one_failure_does_not_block_the_rest(self):
        for i in range(3):
            self.journal.append(ev(f"e{i}"))
            self._reason_row(f"r:{i}::reasoning-episode@0.2", episode(evidence=[f"e{i}"]))

        async def flaky(**kw):
            self.calls.append(kw)
            if "r:1::" in kw["source_description"]:
                raise RuntimeError("graph write failed")
            return {}

        run = await promote_reviewed(
            self.cons, self.journal, self.prom, flaky,
            [f"r:{i}::reasoning-episode@0.2" for i in range(3)], dry_run=False, inter_call_delay=0,
        )
        self.assertEqual(len(run["promoted"]), 2)
        self.assertEqual(len(run["failed"]), 1)
        self.assertIn("r:1::reasoning-episode@0.2", run["failed"][0]["memory_id"])

    async def test_quota_exhaustion_stops_clean(self):
        for i in range(3):
            self.journal.append(ev(f"e{i}"))
            self._reason_row(f"r:{i}::reasoning-episode@0.2", episode(evidence=[f"e{i}"]))
        n = {"i": 0}

        async def limited(**kw):
            if n["i"] >= 1:
                raise GeminiQuotaExhaustedError("no headroom")
            n["i"] += 1
            self.calls.append(kw)
            return {}

        run = await promote_reviewed(
            self.cons, self.journal, self.prom, limited,
            [f"r:{i}::reasoning-episode@0.2" for i in range(3)], dry_run=False, inter_call_delay=0,
        )
        self.assertTrue(run["stopped_early"])
        self.assertEqual(len(run["promoted"]), 1)
        # the 2 unpromoted are still eligible on a later run
        again = await promote_reviewed(
            self.cons, self.journal, self.prom, self.ok_remember,
            [f"r:{i}::reasoning-episode@0.2" for i in range(3)], dry_run=True,
        )
        self.assertEqual(again["eligible_this_run"], 2)


class TestTier1ReviewQueue(MS36Base):
    async def test_only_tier1_kinds_unpromoted_unrejected(self):
        self.journal.append(ev("e0"))
        self._reason_row("r:dec::reasoning-episode@0.2", episode(kind="decision", evidence=["e0"]))
        self._reason_row("r:inv::reasoning-episode@0.2", episode(kind="investigation", evidence=["e0"]))
        self._reason_row("r:pl::reasoning-episode@0.2", episode(kind="plan", evidence=["e0"]))
        q = tier1_review_queue(self.cons, self.prom)
        self.assertCountEqual([r["memory_id"] for r in q], ["r:dec::reasoning-episode@0.2", "r:pl::reasoning-episode@0.2"])

        await promote_reviewed(self.cons, self.journal, self.prom, self.ok_remember,
                               ["r:dec::reasoning-episode@0.2"], dry_run=False, inter_call_delay=0)
        q2 = tier1_review_queue(self.cons, self.prom)
        self.assertEqual([r["memory_id"] for r in q2], ["r:pl::reasoning-episode@0.2"])


class TestCoverageAutoResolve(MS36Base):
    def test_flips_only_covered_rows_idempotent_reversible(self):
        # events e0 (covered by a reasoning episode) and e1 (not)
        for i in range(2):
            self.journal.append(ev(f"e{i}"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        self._heuristic_row("h:0::heuristic-pattern@1.2", "e0")
        self._heuristic_row("h:1::heuristic-pattern@1.2", "e1")

        def state(mid):
            return self.cons.get_derived_memory(mid)["approval_state"]

        dry = self.cons.mark_superseded_by_reasoning(heuristic_version="1.2", dry_run=True)
        self.assertEqual(dry["covered_by_reasoning"], 1)
        self.assertEqual(dry["remaining_queued"], 1)
        self.assertEqual(state("h:0::heuristic-pattern@1.2"), "queued_for_review")  # dry run: unchanged

        run = self.cons.mark_superseded_by_reasoning(heuristic_version="1.2", dry_run=False)
        self.assertEqual(run["covered_by_reasoning"], 1)
        superseded = self.cons.query_derived_memories(approval_state="superseded_by_reasoning")
        self.assertEqual([r["memory_id"] for r in superseded], ["h:0::heuristic-pattern@1.2"])
        self.assertEqual(superseded[0]["superseded_by"], "r:0::reasoning-episode@0.2")
        self.assertEqual(state("h:1::heuristic-pattern@1.2"), "queued_for_review")  # not covered — untouched
        self.assertEqual(state("r:0::reasoning-episode@0.2"), "queued_for_review")  # reasoning row untouched

        # idempotent
        run2 = self.cons.mark_superseded_by_reasoning(heuristic_version="1.2", dry_run=False)
        self.assertEqual(run2["covered_by_reasoning"], 0)

        # reversible
        self.assertEqual(self.cons.revert_superseded_by_reasoning(), 1)
        self.assertEqual(state("h:0::heuristic-pattern@1.2"), "queued_for_review")
        self.assertIsNone(self.cons.get_derived_memory("h:0::heuristic-pattern@1.2")["superseded_by"])


if __name__ == "__main__":
    unittest.main()
