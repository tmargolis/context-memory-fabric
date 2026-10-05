"""MS4e — thread merges stay within a text budget.

Drives `_merge_tier1_by_thread` directly against real (temp-dir) SQLite
stores: no model call, no graph. Before MS4e a thread merged without bound,
and one 74-turn thread became a 23.5K-char episode that Graphiti turned into
85 entities.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from server.consolidation.pipeline import (
    THREAD_MERGE_CHAR_BUDGET,
    _chunk_by_budget,
    _merge_tier1_by_thread,
)
from server.consolidation.store import ConsolidationStore
from server.policies.protocols import ExtractionCategory, ReasoningEpisode
from server.review.store import REJECTED, ReviewStore

BASE = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
CONV = "conv-1"
POLICY, VERSION = "extract", "1.6"
BASE_ID = f"reason:threadmerge:{CONV}:thread-viewer::{POLICY}@{VERSION}"


def _ep(i: int, size: int, thread: str = "thread-viewer") -> tuple[str, ReasoningEpisode]:
    return (
        f"reason:{CONV}:w{i}::{POLICY}@{VERSION}",
        ReasoningEpisode(
            category=ExtractionCategory.EPISODIC,
            reasoning_kind="decision",
            statement=f"s{i}:" + "x" * (size - len(f"s{i}:")),
            confidence=0.9 - i * 0.01,
            evidence_event_ids=[f"ev-{i}"],
            thread_key=thread,
            event_date=BASE + timedelta(minutes=i),
        ),
    )


class TestChunkByBudget(unittest.TestCase):
    def test_within_budget_is_one_chunk(self):
        group = [_ep(i, 500) for i in range(4)]
        self.assertEqual(_chunk_by_budget(group, 3000), [group])

    def test_splits_in_order_without_dropping(self):
        group = [_ep(i, 1000) for i in range(7)]
        chunks = _chunk_by_budget(group, 3000)
        self.assertEqual([len(c) for c in chunks], [3, 3, 1])
        self.assertEqual([p for c in chunks for p in c], group)

    def test_oversized_episode_gets_its_own_chunk(self):
        group = [_ep(0, 500), _ep(1, 5000), _ep(2, 500)]
        self.assertEqual([len(c) for c in _chunk_by_budget(group, 3000)], [1, 1, 1])

    def test_counts_driving_question_and_rationale(self):
        mid, ep = _ep(0, 100)
        ep = replace(ep, driving_question="q" * 1500, rationale="r" * 1500)
        self.assertEqual(len(_chunk_by_budget([(mid, ep), _ep(1, 100)], 3000)), 2)


class TestMergeBudget(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db = Path(self._tmp.name) / "journal.db"
        self.cs = ConsolidationStore(db_path=db)
        self.rev = ReviewStore(db_path=db)

    def tearDown(self):
        self.cs.close()
        self.rev.close()
        self._tmp.cleanup()

    def _merge(self, episodes, budget=None, project=None):
        return _merge_tier1_by_thread(
            consolidation_store=self.cs, review_store=self.rev, conv_key=CONV, harness="claude_code",
            policy_name=POLICY, policy_version=VERSION, episodes=episodes, char_budget=budget,
            project=project,
        )

    def test_default_budget(self):
        self.assertEqual(THREAD_MERGE_CHAR_BUDGET, 3000)

    def test_within_budget_keeps_unsuffixed_ids(self):
        group = [_ep(i, 400) for i in range(3)]
        stats = self._merge(group)
        self.assertEqual(stats, {"threads_merged": 1, "episodes_merged_away": 3})
        merged = self.cs.get_derived_memory(BASE_ID)
        self.assertIsNotNone(merged)
        self.assertTrue(merged["statement"].startswith("(1) s0:"))
        self.assertIsNone(self.cs.get_derived_memory(BASE_ID + "::part1"))

    def test_over_budget_splits_into_parts(self):
        group = [_ep(i, 1000) for i in range(7)]  # chunks of 3, 3, 1
        stats = self._merge(group)
        self.assertEqual(stats, {"threads_merged": 1, "episodes_merged_away": 6})
        self.assertIsNone(self.cs.get_derived_memory(BASE_ID))

        part1 = self.cs.get_derived_memory(BASE_ID + "::part1")
        part2 = self.cs.get_derived_memory(BASE_ID + "::part2")
        self.assertIsNone(self.cs.get_derived_memory(BASE_ID + "::part3"))  # singleton run stays unmerged
        self.assertIn("(1) s0:", part1["statement"])
        self.assertIn("(3) s2:", part1["statement"])
        self.assertIn("(1) s3:", part2["statement"])
        for row in (part1, part2):
            self.assertLessEqual(len(row["statement"]), 3000 + 50)  # "(n) " prefixes and "; " joins

        for i in range(6):
            self.assertEqual(self.rev.state_of(group[i][0]), REJECTED)
        self.assertNotEqual(self.rev.state_of(group[6][0]), REJECTED)

    def test_merged_episode_keeps_project(self):
        """Regression (2026-10-03): merges were written with project NULL."""
        self._merge([_ep(i, 1000) for i in range(7)], project="proj-alpha")
        for part in ("::part1", "::part2"):
            self.assertEqual(self.cs.get_derived_memory(BASE_ID + part)["project"], "proj-alpha")

    def test_rerun_is_idempotent(self):
        group = [_ep(i, 1000) for i in range(7)]
        self._merge(group)
        again = self._merge([_ep(i, 1000) for i in range(7)])
        self.assertEqual(again, {"threads_merged": 0, "episodes_merged_away": 0})

    def test_thread_merged_whole_before_ms4e_is_not_resplit(self):
        self._merge([_ep(i, 1000) for i in range(7)], budget=10**9)  # pre-MS4e: one unsuffixed merge
        self.assertIsNotNone(self.cs.get_derived_memory(BASE_ID))
        again = self._merge([_ep(i, 1000) for i in range(7)])
        self.assertEqual(again, {"threads_merged": 0, "episodes_merged_away": 0})
        self.assertIsNone(self.cs.get_derived_memory(BASE_ID + "::part1"))


if __name__ == "__main__":
    unittest.main()
