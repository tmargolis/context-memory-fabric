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
    _semantic_episode_name,
    default_tier,
    promote_reviewed,
    tier1_review_queue,
)
from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.core.rate_limiter import GeminiQuotaExhaustedError
from server.journal.identity import compute_content_hash
from server.journal.store import SqliteEventStore
from server.policies.reasoning_episode_v1 import REASONING_POLICY_VERSION
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


class _FakeRateLimiter:
    """A rate limiter test double whose wait is fixed and near-instant, so
    tests exercise the real wait-and-retry loop in promote_reviewed
    without actually sleeping or touching the real, process-global
    persisted ledger (imports/state/gemini_rate_limiter_state.json)."""

    def __init__(self, wait: float):
        self.wait = wait
        self.calls = 0

    def seconds_until_headroom(self, *args, **kwargs) -> float:
        self.calls += 1
        return self.wait


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
            policy_name="reasoning-episode", policy_version=REASONING_POLICY_VERSION,
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

    async def test_source_description_carries_the_ms6_project_label(self):
        """MS6's taxonomy (server.review.projects) tags derived_memories.project;
        that label rides along as BM25-searchable text in source_description —
        deliberately NOT Graphiti's group_id, which is a hard partition
        boundary and would fragment entity resolution across projects."""
        self.journal.append(ev("e0"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        self.cons._conn.execute(
            "UPDATE derived_memories SET project = 'openclaw' WHERE memory_id = ?",
            ("r:0::reasoning-episode@0.2",),
        )
        self.cons._conn.commit()  # left open, this holds the writer lock and
        # blocks PromotionStore's separate connection to the same file below
        await promote_reviewed(self.cons, self.journal, self.prom, self.ok_remember,
                                ["r:0::reasoning-episode@0.2"], dry_run=False, inter_call_delay=0)
        self.assertIn("project=openclaw", self.calls[0]["source_description"])

    async def test_source_description_omits_project_when_unset(self):
        self.journal.append(ev("e0"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        await promote_reviewed(self.cons, self.journal, self.prom, self.ok_remember,
                                ["r:0::reasoning-episode@0.2"], dry_run=False, inter_call_delay=0)
        self.assertNotIn("project=", self.calls[-1]["source_description"])

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

    async def test_quota_exhaustion_stops_clean_when_wait_disabled(self):
        # wait_through_rate_limit=False restores the pre-fix behavior:
        # stop the whole batch immediately rather than sleep through it.
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
            wait_through_rate_limit=False,
        )
        self.assertTrue(run["stopped_early"])
        self.assertEqual(len(run["promoted"]), 1)
        # the 2 unpromoted are still eligible on a later run
        again = await promote_reviewed(
            self.cons, self.journal, self.prom, self.ok_remember,
            [f"r:{i}::reasoning-episode@0.2" for i in range(3)], dry_run=True,
        )
        self.assertEqual(again["eligible_this_run"], 2)

    async def test_default_waits_through_an_rpm_stall_and_retries_the_same_episode(self):
        # The real bug this fixes: a 283-episode run hit local rate-limit
        # exhaustion (a conservative estimate, not the account's real
        # dashboard limit) and the old code abandoned the rest of the
        # batch rather than waiting the ~60s an RPM wall actually needs.
        for i in range(2):
            self.journal.append(ev(f"e{i}"))
            self._reason_row(f"r:{i}::reasoning-episode@0.2", episode(evidence=[f"e{i}"]))

        attempts = {"e0": 0}

        async def flaky(**kw):
            name = kw["name"]
            if "r:0" in kw.get("source_description", "") and attempts["e0"] < 2:
                attempts["e0"] += 1
                raise GeminiQuotaExhaustedError("no headroom")
            self.calls.append(kw)
            return {}

        fake_limiter = _FakeRateLimiter(wait=0.001)
        run = await promote_reviewed(
            self.cons, self.journal, self.prom, flaky,
            ["r:0::reasoning-episode@0.2", "r:1::reasoning-episode@0.2"],
            dry_run=False, inter_call_delay=0, rate_limiter=fake_limiter,
        )
        # r:0 stalled twice and STILL succeeded — never skipped, never
        # counted as failed, no early stop.
        self.assertFalse(run["stopped_early"])
        self.assertEqual(len(run["promoted"]), 2)
        self.assertEqual(len(run["failed"]), 0)
        self.assertEqual(run["quota_stalls"], 2)
        self.assertGreater(run["waited_seconds"], 0)
        self.assertEqual(fake_limiter.calls, 2)

    async def test_waits_through_a_real_api_429_that_survives_remembers_own_retries(self):
        # This is the actual gap a real 283-episode run exposed:
        # stopped_early stayed False the whole run (the LOCAL ledger never
        # pre-emptively blocked — it believed there was headroom) while 21
        # episodes still failed on genuine 429/RESOURCE_EXHAUSTED responses
        # from Google, because that exception is a plain Exception from the
        # Gemini SDK, never a GeminiQuotaExhaustedError, so the old code
        # routed it straight to record_failure() with no retry at all.
        self.journal.append(ev("e0"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        attempts = {"n": 0}

        async def real_api_429_then_ok(**kw):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RuntimeError("429 RESOURCE_EXHAUSTED. You exceeded your current quota.")
            self.calls.append(kw)
            return {}

        import server.consolidation.promotion as promotion_mod
        original = promotion_mod._API_TRANSIENT_ERROR_BACKOFF_SECONDS
        promotion_mod._API_TRANSIENT_ERROR_BACKOFF_SECONDS = 0.001
        try:
            run = await promote_reviewed(
                self.cons, self.journal, self.prom, real_api_429_then_ok,
                ["r:0::reasoning-episode@0.2"], dry_run=False, inter_call_delay=0,
            )
        finally:
            promotion_mod._API_TRANSIENT_ERROR_BACKOFF_SECONDS = original

        self.assertFalse(run["stopped_early"])
        self.assertEqual(len(run["promoted"]), 1)
        self.assertEqual(len(run["failed"]), 0)
        self.assertEqual(run["api_stalls"], 1)
        self.assertEqual(attempts["n"], 2)

    async def test_a_genuinely_unrelated_error_is_not_retried_as_transient(self):
        # A real bug (bad statement content, a schema mismatch, ...) must
        # still fail fast — only the specific transient-shaped messages
        # should ever trigger a wait.
        self.journal.append(ev("e0"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))

        async def broken(**kw):
            raise ValueError("content cannot be empty")

        run = await promote_reviewed(
            self.cons, self.journal, self.prom, broken,
            ["r:0::reasoning-episode@0.2"], dry_run=False, inter_call_delay=0,
        )
        self.assertEqual(len(run["failed"]), 1)
        self.assertEqual(run["api_stalls"], 0)

    async def test_stops_and_reports_when_a_single_wait_exceeds_the_cap(self):
        # A pathological case (or a genuine RPD wall many hours away) must
        # still terminate rather than hang forever.
        self.journal.append(ev("e0"))
        self._reason_row("r:0::reasoning-episode@0.2", episode(evidence=["e0"]))

        async def always_exhausted(**kw):
            raise GeminiQuotaExhaustedError("no headroom")

        # The cap is only checked BEFORE each wait, so the first stall
        # still sleeps in full before the (now-exceeded) budget stops the
        # next one — keep both numbers small so the test itself is fast.
        fake_limiter = _FakeRateLimiter(wait=0.05)
        run = await promote_reviewed(
            self.cons, self.journal, self.prom, always_exhausted,
            ["r:0::reasoning-episode@0.2"], dry_run=False, inter_call_delay=0,
            rate_limiter=fake_limiter, max_single_wait_seconds=0.03,
        )
        self.assertTrue(run["stopped_early"])
        self.assertEqual(len(run["promoted"]), 0)
        self.assertEqual(len(run["failed"]), 0, "a quota stall is not a failure, even when capped out")


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


class TestSemanticEpisodeNames(MS36Base):
    async def test_harness_project_sequence_names(self):
        for i in range(3):
            self.journal.append(ev(f"e{i}"))
        # two chatgpt/astrophotography, one claude/condo
        self._reason_row("reason:c1:chatgpt:x:y:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        self._reason_row("reason:c1:chatgpt:x:y:1::reasoning-episode@0.2", episode(kind="plan", evidence=["e1"]))
        self._reason_row("reason:c2:claude:x:y:0::reasoning-episode@0.2", episode(evidence=["e2"]))
        for mid, proj in (
            ("reason:c1:chatgpt:x:y:0::reasoning-episode@0.2", "astrophotography"),
            ("reason:c1:chatgpt:x:y:1::reasoning-episode@0.2", "astrophotography"),
            ("reason:c2:claude:x:y:0::reasoning-episode@0.2", "condo"),
        ):
            self.cons._conn.execute("UPDATE derived_memories SET project=? WHERE memory_id=?", (proj, mid))
        self.cons._conn.commit()

        ids = [
            "reason:c1:chatgpt:x:y:0::reasoning-episode@0.2",
            "reason:c1:chatgpt:x:y:1::reasoning-episode@0.2",
            "reason:c2:claude:x:y:0::reasoning-episode@0.2",
        ]
        await promote_reviewed(self.cons, self.journal, self.prom, self.ok_remember,
                               ids, dry_run=False, inter_call_delay=0)
        self.assertEqual(
            [c["name"] for c in self.calls],
            ["chatgpt-astrophotography-001", "chatgpt-astrophotography-002", "claude-condo-001"],
        )

    async def test_sequence_continues_across_runs(self):
        self.journal.append(ev("e0")); self.journal.append(ev("e1"))
        self._reason_row("reason:c1:chatgpt:x:y:0::reasoning-episode@0.2", episode(evidence=["e0"]))
        self._reason_row("reason:c1:chatgpt:x:y:1::reasoning-episode@0.2", episode(kind="plan", evidence=["e1"]))
        for mid in ("reason:c1:chatgpt:x:y:0::reasoning-episode@0.2", "reason:c1:chatgpt:x:y:1::reasoning-episode@0.2"):
            self.cons._conn.execute("UPDATE derived_memories SET project='obsidian' WHERE memory_id=?", (mid,))
        self.cons._conn.commit()

        await promote_reviewed(self.cons, self.journal, self.prom, self.ok_remember,
                               ["reason:c1:chatgpt:x:y:0::reasoning-episode@0.2"], dry_run=False, inter_call_delay=0)
        await promote_reviewed(self.cons, self.journal, self.prom, self.ok_remember,
                               ["reason:c1:chatgpt:x:y:1::reasoning-episode@0.2"], dry_run=False, inter_call_delay=0)
        self.assertEqual([c["name"] for c in self.calls],
                         ["chatgpt-obsidian-001", "chatgpt-obsidian-002"])

    def test_missing_project_falls_back_to_misc(self):
        name = _semantic_episode_name("reason:c1:gemini:apps:x:y:0::reasoning-episode@0.2", None, self.prom, "g")
        self.assertEqual(name, "gemini-misc-001")


if __name__ == "__main__":
    unittest.main()
