"""scripts/backfill_threadmerge_project.py against temp-dir SQLite stores.

Builds the pre-b28446a state for real: window episodes written with a
project, merged by `_merge_tier1_by_thread` without one. No model, no graph.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.backfill_threadmerge_project import main, parse_overrides, plan
from server.consolidation.pipeline import _merge_tier1_by_thread
from server.consolidation.store import ConsolidationStore
from server.policies.protocols import ExtractionCategory, ReasoningEpisode
from server.review.actions import approve_episode
from server.review.store import ReviewStore

BASE = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
POLICY, VERSION = "extract", "1.6"


def _ep(i: int, thread: str) -> ReasoningEpisode:
    return ReasoningEpisode(
        category=ExtractionCategory.EPISODIC,
        reasoning_kind="decision",
        statement=f"statement {i}",
        confidence=0.9,
        evidence_event_ids=[f"ev-{thread}-{i}"],
        thread_key=thread,
        event_date=BASE + timedelta(minutes=i),
    )


class TestBackfillThreadmergeProject(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.db = self.root / "journal.db"
        self.cs = ConsolidationStore(db_path=self.db)
        self.rev = ReviewStore(db_path=self.db)
        self.ep_dir = self.cs._episode_proposals_dir
        self.doc_dir = self.root / "doc-proposals"
        self.doc_dir.mkdir()

    def tearDown(self):
        self.cs.close()
        self.rev.close()
        self._tmp.cleanup()

    def _conversation(self, conv: str, thread: str, child_projects: list) -> str:
        """Write window episodes with the given projects, then merge them the old (project-less) way."""
        episodes = []
        for i, project in enumerate(child_projects):
            mid = f"reason:{conv}:w{i}::{POLICY}@{VERSION}"
            self.cs.mark_running(f"job:{mid}", f"ev-{thread}-{i}", POLICY, VERSION)
            self.cs.record_reasoning_episode(
                job_id=f"job:{mid}", memory_id=mid, episode=_ep(i, thread), policy_name=POLICY,
                policy_version=VERSION, approval_state="queued_for_review", supersedes=None,
                conversation_id=conv, harness="claude_cowork", project=project,
            )
            episodes.append((mid, _ep(i, thread)))
        _merge_tier1_by_thread(
            consolidation_store=self.cs, review_store=self.rev, conv_key=conv, harness="claude_cowork",
            policy_name=POLICY, policy_version=VERSION, episodes=episodes,
        )
        return f"reason:threadmerge:{conv}:{thread}::{POLICY}@{VERSION}"

    def _doc(self, conv: str, project=None) -> Path:
        path = self.doc_dir / f"prop_x_{conv}.json"
        path.write_text(json.dumps({"proposal_id": path.stem, "status": "pending_review",
                                    "source_conversation_id": conv, "source_project": project}))
        return path

    def _run(self, *extra: str) -> None:
        main(["--db", str(self.db), "--episode-dir", str(self.ep_dir), "--doc-dir", str(self.doc_dir),
              "--bak-dir", str(self.root / "bak"), *extra])

    def _project(self, memory_id: str):
        return self.cs.get_derived_memory(memory_id)["project"]

    def _mirror(self, memory_id: str) -> dict:
        for path in (self.ep_dir / "tier1").glob("*.json"):
            data = json.loads(path.read_text())
            if data["memory_id"] == memory_id:
                return data
        raise AssertionError(f"no pending mirror for {memory_id}")

    def test_merge_takes_its_childrens_project(self):
        merged = self._conversation("conv-a", "t", ["proj-alpha", "proj-alpha"])
        self.assertIsNone(self._project(merged))
        self._run("--apply")
        self.assertEqual(self._project(merged), "proj-alpha")
        self.assertEqual(self._mirror(merged)["project"], "proj-alpha")
        self.assertTrue(list((self.root / "bak").glob("journal-*.db")))

    def test_dry_run_writes_nothing(self):
        merged = self._conversation("conv-a", "t", ["p", "p"])
        self._run()
        self.assertIsNone(self._project(merged))
        self.assertIsNone(self._mirror(merged)["project"])
        self.assertFalse((self.root / "bak").exists())

    def test_disagreeing_children_are_left_alone(self):
        merged = self._conversation("conv-a", "t", ["p", "q"])
        with sqlite3.connect(self.db) as conn:
            p = plan(conn, self.ep_dir, self.doc_dir, {})
        self.assertEqual(p["updates"], [])
        self.assertEqual(p["conflicts"], [(merged, ["p", "q"])])

    def test_no_project_anywhere_needs_an_override(self):
        merged = self._conversation("conv-b", "t", [None, None])
        doc = self._doc("conv-b")
        self._run("--apply")
        self.assertIsNone(self._project(merged))

        self._run("--apply", "--override", "conv-b=proj-alpha")
        self.assertEqual(self._project(merged), "proj-alpha")
        self.assertEqual(self._mirror(merged)["project"], "proj-alpha")
        self.assertEqual(json.loads(doc.read_text())["source_project"], "proj-alpha")

    def test_override_never_replaces_an_existing_project(self):
        merged = self._conversation("conv-a", "t", ["p", "p"])
        doc = self._doc("conv-a", project="p")
        self._run("--apply", "--override", "conv-a=other")
        self.assertEqual(self._project(merged), "p")
        self.assertEqual(json.loads(doc.read_text())["source_project"], "p")

    def test_reviewed_episodes_are_never_touched(self):
        merged = self._conversation("conv-b", "t", [None, None])
        approve_episode(self.rev, merged, reviewer="test")
        self._run("--apply", "--override", "conv-b=p")
        self.assertIsNone(self._project(merged))

    def test_parse_overrides_rejects_malformed(self):
        self.assertEqual(parse_overrides(["c=p"]), {"c": "p"})
        with self.assertRaises(SystemExit):
            parse_overrides(["no-equals"])


if __name__ == "__main__":
    unittest.main()
