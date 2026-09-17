"""MS7b Phase 0 — enriched episode content sent to remember().

Before this change, both promotion call sites sent `row["statement"]`
alone; `derived_memories.reason` (which carries the reasoning-episode
policy's driving question and rationale, packed as
"Q: ... | why: ... | alt: ... | status=... | thread=...") never reached
the graph. `enriched_episode_content()` is the fix; these tests cover its
unit behaviour, and `TestPromoteReviewedSendsEnrichedContent` confirms the
real call site actually uses it end to end.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from server.consolidation.promotion import PromotionStore, enriched_episode_content, promote_reviewed
from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision
from server.journal.store import SqliteEventStore
from server.policies.reasoning_episode_v1 import REASONING_POLICY_VERSION
from server.policies.protocols import ExtractionCategory, ExtractionResult, ReasoningEpisode

BASE = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _row(**overrides) -> sqlite3.Row:
    """A derived_memories-shaped sqlite3.Row, built the same way
    ConsolidationStore actually produces one (row_factory=sqlite3.Row over
    a real query), so this exercises the same `row["x"]` / `row.keys()`
    access the production call sites use."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    fields = {
        "statement": "Investigated why the NAS still showed bad permissions after a sync.",
        "reason": None,
    }
    fields.update(overrides)
    cols = ", ".join(fields.keys())
    placeholders = ", ".join(["?"] * len(fields))
    conn.execute(f"CREATE TABLE t ({', '.join(f'{k} TEXT' for k in fields)})")
    conn.execute(f"INSERT INTO t ({cols}) VALUES ({placeholders})", list(fields.values()))
    row = conn.execute(f"SELECT {cols} FROM t").fetchone()
    conn.close()
    return row


class TestEnrichedEpisodeContent(unittest.TestCase):
    def test_all_fields_present_appends_question_and_rationale(self):
        row = _row(
            reason=(
                "reasoning_kind=investigation | Q: Why does the NAS still show bad permissions? "
                "| why: Observed permissions post-sync and isolated it to SMB/QNAP behaviour. "
                "| alt: sync script bug; NAS filesystem override | status=resolved | thread=nas-sync"
            )
        )
        out = enriched_episode_content(row)
        self.assertTrue(out.startswith(row["statement"]))
        self.assertIn("Driving question: Why does the NAS still show bad permissions?", out)
        self.assertIn("Reasoning: Observed permissions post-sync and isolated it to SMB/QNAP behaviour.", out)

    def test_alternatives_are_never_included(self):
        row = _row(
            reason=(
                "reasoning_kind=decision | Q: Which NAS to buy? | why: Compared cost and RAM. "
                "| alt: QNAP TS-264; Synology DS224+ | status=resolved"
            )
        )
        out = enriched_episode_content(row)
        self.assertNotIn("QNAP TS-264", out)
        self.assertNotIn("Synology", out)
        self.assertNotIn("alt:", out)

    def test_missing_reason_falls_back_to_statement(self):
        row = _row(reason=None)
        self.assertEqual(enriched_episode_content(row), row["statement"])

    def test_reason_without_reason_column_falls_back_to_statement(self):
        """Defensive: a row shaped without a `reason` column at all (not
        just NULL) must not raise."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE t (statement TEXT)")
        conn.execute("INSERT INTO t (statement) VALUES (?)", ("Just a statement.",))
        row = conn.execute("SELECT statement FROM t").fetchone()
        conn.close()
        self.assertEqual(enriched_episode_content(row), "Just a statement.")

    def test_unfamiliar_reason_shape_falls_back_gracefully(self):
        """heuristic_v1's ExtractionResult.reason has no Q:/why: structure
        at all (see record_consolidation) -- must not raise, must not
        fabricate a driving question or rationale from unrelated text."""
        row = _row(reason="x")
        self.assertEqual(enriched_episode_content(row), row["statement"])

    def test_question_only_no_rationale(self):
        row = _row(reason="reasoning_kind=hypothesis | Q: Is the cache stale? | status=open")
        out = enriched_episode_content(row)
        self.assertIn("Driving question: Is the cache stale?", out)
        self.assertNotIn("Reasoning:", out)

    def test_empty_string_reason_falls_back(self):
        row = _row(reason="")
        self.assertEqual(enriched_episode_content(row), row["statement"])


class TestPromoteReviewedSendsEnrichedContent(unittest.IsolatedAsyncioTestCase):
    """End-to-end: promote_reviewed's real call site must actually use
    enriched_episode_content(), not row["statement"] directly."""

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

    async def test_enriched_content_reaches_remember_fn(self):
        mid = "r:0::reasoning-episode@0.2"
        self.cons.record_reasoning_episode(
            job_id=f"job:{mid}",
            memory_id=mid,
            episode=ReasoningEpisode(
                category=ExtractionCategory.EPISODIC,
                reasoning_kind="investigation",
                statement="Investigated the NAS permissions issue.",
                driving_question="Why does the NAS still show bad permissions after a sync?",
                rationale="Isolated it to SMB/QNAP filesystem behaviour, not the sync script.",
                alternatives="sync script bug; NAS filesystem override",
                confidence=0.8,
                evidence_event_ids=["e0"],
                event_date=BASE,
                date_precision=DatePrecision.DAY,
            ),
            policy_name="reasoning-episode",
            policy_version=REASONING_POLICY_VERSION,
            approval_state="queued_for_review",
            supersedes=None,
        )

        await promote_reviewed(self.cons, self.journal, self.prom, self.ok_remember, [mid], dry_run=False, inter_call_delay=0)

        self.assertEqual(len(self.calls), 1)
        content = self.calls[0]["content"]
        self.assertIn("Investigated the NAS permissions issue.", content)
        self.assertIn("Driving question: Why does the NAS still show bad permissions after a sync?", content)
        self.assertIn("Reasoning: Isolated it to SMB/QNAP filesystem behaviour, not the sync script.", content)
        self.assertNotIn("sync script bug", content)  # alternatives excluded

    async def test_heuristic_row_without_qa_shape_still_promotes_statement(self):
        """promote_auto_accepted's rows (heuristic_v1) don't carry the
        Q:/why: reason shape; enriched_episode_content must degrade to the
        statement rather than raise, on that path too."""
        from server.consolidation.promotion import promote_auto_accepted

        mid = "h:0::heuristic-pattern@1.2"
        self.journal.append.__self__  # no-op; source_event not required for auto_accepted path's harness lookup fallback
        self.cons.record_consolidation(
            job_id=f"job:{mid}",
            memory_id=mid,
            source_event_id="missing-event",
            policy_name="heuristic-pattern",
            policy_version="1.2",
            result=ExtractionResult(
                category=ExtractionCategory.AMBIGUOUS,
                statement="Noted a preference for dark mode.",
                reason="matched pattern: preference-keyword",
                confidence=0.4,
            ),
            approval_state="auto_accepted",
            supersedes=None,
        )

        result = await promote_auto_accepted(
            self.cons, self.journal, self.prom, self.ok_remember, dry_run=False, inter_call_delay=0
        )

        self.assertEqual(len(result["failed"]), 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["content"], "Noted a preference for dark mode.")


if __name__ == "__main__":
    unittest.main()
