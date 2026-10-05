"""episode-proposals/: file mirror for staged reasoning episodes
(Backlog, "Proposal-directory housekeeping," 2026-09-18).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision
from server.episode_proposals import (
    _mirror_filename,
    list_episode_mirrors,
    read_episode_mirror,
    tier_for_reasoning_kind,
)
from server.policies.protocols import ExtractionCategory, ReasoningEpisode
from server.review.actions import approve_episode, reject_episode
from server.review.store import ReviewStore

BASE = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _episode(kind="decision", statement="Chose SQLite for the journal.", evidence=("e0",)):
    return ReasoningEpisode(
        category=ExtractionCategory.EPISODIC,
        reasoning_kind=kind,
        statement=statement,
        confidence=0.9,
        evidence_event_ids=list(evidence),
        event_date=BASE,
        date_precision=DatePrecision.DAY,
        thread_key="cmf-journal-backend",
        rationale="the journal must survive a crash mid-write",
        driving_question="which store backs the journal?",
        status="resolved",
    )


class TestTierSplit(unittest.TestCase):
    def test_tier1_kinds(self):
        for kind in ("decision", "plan", "retrospective", "rejected_alternative"):
            self.assertEqual(tier_for_reasoning_kind(kind), "tier1")

    def test_tier2_kinds(self):
        for kind in ("investigation", "hypothesis", "experiment", "finding"):
            self.assertEqual(tier_for_reasoning_kind(kind), "tier2")


class TestEpisodeMirror(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "journal.db"
        self.cons = ConsolidationStore(db_path=self.db_path)
        self.rev = ReviewStore(db_path=self.db_path)
        # ConsolidationStore/ReviewStore derive the mirror dir from db_path
        self.mirror_root = self.db_path.parent / "episode-proposals"

    def tearDown(self):
        self.cons.close()
        self.rev.close()
        self._tmp.cleanup()

    def _stage(self, memory_id, kind="decision", policy_name="reasoning-episode"):
        self.cons.record_reasoning_episode(
            job_id=f"job:{memory_id}", memory_id=memory_id, episode=_episode(kind=kind),
            policy_name=policy_name, policy_version="0.2",
            approval_state="queued_for_review", supersedes=None,
        )

    def test_staging_writes_mirror_at_tier_root(self):
        self._stage("m1", kind="decision")
        path = self.mirror_root / "tier1" / _mirror_filename("m1")
        self.assertTrue(path.exists())
        data = json.loads(path.read_text())
        self.assertEqual(data["memory_id"], "m1")
        self.assertEqual(data["reasoning_kind"], "decision")
        self.assertEqual(data["tier"], "tier1")
        self.assertEqual(data["statement"], "Chose SQLite for the journal.")
        self.assertEqual(data["approval_state"], "queued_for_review")

    def test_tier2_kind_goes_to_tier2(self):
        self._stage("m2", kind="investigation")
        self.assertTrue((self.mirror_root / "tier2" / _mirror_filename("m2")).exists())

    def test_cowork_live_v1_policy_also_mirrored(self):
        """record_reasoning_episode() has no policy_name filter -- heuristic-
        pattern physically can't reach it (different method), so anything
        that does reach it gets mirrored, offline windower or cowork_live_v1."""
        self._stage("m3", kind="decision", policy_name="cowork_live_v1")
        data = json.loads((self.mirror_root / "tier1" / _mirror_filename("m3")).read_text())
        self.assertEqual(data["policy_name"], "cowork_live_v1")

    def test_approve_moves_to_approved_subfolder(self):
        self._stage("m4", kind="decision")
        approve_episode(self.rev, "m4", reviewer="todd", reason="looks right")

        filename = _mirror_filename("m4")
        self.assertFalse((self.mirror_root / "tier1" / filename).exists())
        moved = self.mirror_root / "tier1" / "approved" / filename
        self.assertTrue(moved.exists())
        # content must match location, not just the old staged value (found
        # 2026-09-18: a file could sit in a terminal folder while its own
        # approval_state field still said queued_for_review)
        data = json.loads(moved.read_text())
        self.assertEqual(data["approval_state"], "approved")
        self.assertEqual(data["reviewer"], "todd")
        self.assertEqual(data["review_reason"], "looks right")
        self.assertIn("reviewed_at", data)

    def test_reject_moves_to_rejected_subfolder(self):
        self._stage("m5", kind="investigation")
        reject_episode(self.rev, "m5", reviewer="todd", reason="not durable enough")

        filename = _mirror_filename("m5")
        self.assertFalse((self.mirror_root / "tier2" / filename).exists())
        moved = self.mirror_root / "tier2" / "rejected" / filename
        self.assertTrue(moved.exists())
        data = json.loads(moved.read_text())
        self.assertEqual(data["approval_state"], "rejected")
        self.assertEqual(data["review_reason"], "not durable enough")

    def test_long_memory_id_does_not_exceed_filename_limit(self):
        """Real production bug (2026-09-18): a windowed episode's memory_id
        can embed long composite event ids and exceed a filesystem's
        filename length limit."""
        long_id = "reason:" + ("gemini:apps:" + "a" * 64 + ":response:" + "b" * 64) * 2 + "::reasoning-episode@0.2"
        self.assertGreater(len(long_id), 255)
        self._stage(long_id, kind="decision")  # must not raise
        self.assertTrue((self.mirror_root / "tier1" / _mirror_filename(long_id)).exists())

    def test_approve_unknown_memory_id_is_a_safe_noop(self):
        """A memory_id with no mirror (e.g. heuristic-pattern, never
        mirrored) must not raise when reviewed."""
        approve_episode(self.rev, "no-such-memory-id")  # must not raise

    def test_custom_db_path_isolates_the_mirror_dir(self):
        """A caller-supplied db_path (every isolated test, including this
        one) mirrors to a sibling directory next to it, not the real
        project's episode-proposals/ -- checked structurally (against the
        temp dir's own path) rather than by calling
        get_episode_proposals_dir() with no override, which would create
        the real project directory as a side effect."""
        self.assertEqual(self.cons._episode_proposals_dir, self.mirror_root)
        self.assertEqual(self.mirror_root.parent, self.db_path.parent)


class TestProductionJournalPathMirrorsToProjectRoot(unittest.TestCase):
    def test_explicit_default_journal_path_is_treated_as_production(self):
        """Regression (2026-10-03): the codex worker passed DEFAULT_JOURNAL_PATH
        explicitly and its mirrors landed next to the db, not in the review dir."""
        from server.consolidation.store import DEFAULT_JOURNAL_PATH
        store = ConsolidationStore.__new__(ConsolidationStore)  # no connection to the real db
        with patch("sqlite3.connect") as connect:
            connect.return_value = MagicMock()
            ConsolidationStore.__init__(store, DEFAULT_JOURNAL_PATH)
        self.assertIsNone(store._episode_proposals_dir)


class TestEpisodeMirrorReaders(unittest.TestCase):
    """list_episode_mirrors()/read_episode_mirror() -- the read side the new
    list_episode_proposals/get_episode_proposal MCP tools sit on top of."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "journal.db"
        self.cons = ConsolidationStore(db_path=self.db_path)
        self.rev = ReviewStore(db_path=self.db_path)
        self.mirror_root = self.db_path.parent / "episode-proposals"

    def tearDown(self):
        self.cons.close()
        self.rev.close()
        self._tmp.cleanup()

    def _stage(self, memory_id, kind="decision", policy_name="reasoning-episode"):
        self.cons.record_reasoning_episode(
            job_id=f"job:{memory_id}", memory_id=memory_id, episode=_episode(kind=kind),
            policy_name=policy_name, policy_version="0.2",
            approval_state="queued_for_review", supersedes=None,
        )

    def test_read_by_memory_id(self):
        self._stage("r1", kind="decision")
        data = read_episode_mirror("r1", base_dir=self.mirror_root)
        self.assertIsNotNone(data)
        self.assertEqual(data["memory_id"], "r1")

    def test_read_missing_returns_none(self):
        self.assertIsNone(read_episode_mirror("no-such-id", base_dir=self.mirror_root))

    def test_list_covers_both_tiers_by_default(self):
        self._stage("r2", kind="decision")   # tier1
        self._stage("r3", kind="finding")    # tier2
        items = list_episode_mirrors(base_dir=self.mirror_root)
        ids = {d["memory_id"] for d in items}
        self.assertEqual(ids, {"r2", "r3"})

    def test_list_filters_by_tier(self):
        self._stage("r4", kind="decision")
        self._stage("r5", kind="finding")
        tier1_only = list_episode_mirrors(tier="tier1", base_dir=self.mirror_root)
        self.assertEqual({d["memory_id"] for d in tier1_only}, {"r4"})

    def test_list_finds_items_moved_to_approved_or_rejected(self):
        self._stage("r6", kind="decision")
        self._stage("r7", kind="decision")
        approve_episode(self.rev, "r6")
        reject_episode(self.rev, "r7")

        approved = list_episode_mirrors(approval_state="approved", base_dir=self.mirror_root)
        rejected = list_episode_mirrors(approval_state="rejected", base_dir=self.mirror_root)
        self.assertEqual({d["memory_id"] for d in approved}, {"r6"})
        self.assertEqual({d["memory_id"] for d in rejected}, {"r7"})

    def test_read_after_reject_reflects_new_status(self):
        """The bug found (2026-09-18): a mirror moved to rejected/ but
        still reading 'queued_for_review'. Covered end-to-end via the
        public read path here, not just move_episode_mirror() directly."""
        self._stage("r8", kind="decision")
        reject_episode(self.rev, "r8", reason="not durable")
        data = read_episode_mirror("r8", base_dir=self.mirror_root)
        self.assertEqual(data["approval_state"], "rejected")
        self.assertEqual(data["review_reason"], "not durable")


if __name__ == "__main__":
    unittest.main()
