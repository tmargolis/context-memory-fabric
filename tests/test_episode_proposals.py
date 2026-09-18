"""episode-proposals/: file mirror for staged reasoning episodes
(Backlog, "Proposal-directory housekeeping," 2026-09-18).
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision
from server.episode_proposals import _mirror_filename, tier_for_reasoning_kind
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
        approve_episode(self.rev, "m4")

        filename = _mirror_filename("m4")
        self.assertFalse((self.mirror_root / "tier1" / filename).exists())
        self.assertTrue((self.mirror_root / "tier1" / "approved" / filename).exists())

    def test_reject_moves_to_rejected_subfolder(self):
        self._stage("m5", kind="investigation")
        reject_episode(self.rev, "m5")

        filename = _mirror_filename("m5")
        self.assertFalse((self.mirror_root / "tier2" / filename).exists())
        self.assertTrue((self.mirror_root / "tier2" / "rejected" / filename).exists())

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


if __name__ == "__main__":
    unittest.main()
