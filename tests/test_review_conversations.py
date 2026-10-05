"""list_review_conversations() and the conversation_id filters on
list_episode_mirrors()/list_proposals() (docs/plan-active.md, "review by
conversation", 2026-09-19).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from server.consolidation.store import ConsolidationStore
from server.core.models import DatePrecision
from server.episode_proposals import list_episode_mirrors
from server.policies.protocols import ExtractionCategory, ReasoningEpisode
from server.proposals import create_doc_proposal, list_proposals
from server.review.conversations import format_review_conversations, list_review_conversations

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
    )


class TestConversationGroupedReview(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "journal.db"
        self.cons = ConsolidationStore(db_path=self.db_path)
        # ConsolidationStore derives the episode mirror dir from db_path
        self.episode_mirror_dir = self.db_path.parent / "episode-proposals"
        self.doc_proposals_dir = Path(self._tmp.name) / "doc-proposals"
        self.wiki_root = Path(self._tmp.name) / "wiki"
        self.wiki_root.mkdir()

    def tearDown(self):
        self.cons.close()
        self._tmp.cleanup()

    def _stage_episode(self, memory_id, kind, conversation_id, harness="claude_code"):
        self.cons.record_reasoning_episode(
            job_id=f"job:{memory_id}", memory_id=memory_id, episode=_episode(kind=kind),
            policy_name="extract", policy_version="1.0",
            approval_state="queued_for_review", supersedes=None,
            conversation_id=conversation_id, harness=harness,
        )

    def _create_doc(self, target_path, conversation_id, harness="claude_code"):
        return create_doc_proposal(
            target_path=target_path,
            proposed_content="# Doc\n\nContent.\n",
            rationale="durable reference material",
            source_conversation_id=conversation_id,
            source_harness=harness,
            wiki_root=self.wiki_root,
            proposals_dir=self.doc_proposals_dir,
        )

    def test_episode_mirror_carries_conversation_id(self):
        self._stage_episode("m1", "decision", conversation_id="conv-a")
        items = list_episode_mirrors(base_dir=self.episode_mirror_dir)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["conversation_id"], "conv-a")
        self.assertEqual(items[0]["harness"], "claude_code")

    def test_episode_mirror_without_conversation_id_defaults_none(self):
        self.cons.record_reasoning_episode(
            job_id="job:m2", memory_id="m2", episode=_episode(),
            policy_name="extract", policy_version="1.0",
            approval_state="queued_for_review", supersedes=None,
        )
        items = list_episode_mirrors(base_dir=self.episode_mirror_dir)
        self.assertIsNone(items[0]["conversation_id"])

    def test_list_episode_mirrors_filters_by_conversation_id(self):
        self._stage_episode("m1", "decision", conversation_id="conv-a")
        self._stage_episode("m2", "plan", conversation_id="conv-b")
        items = list_episode_mirrors(conversation_id="conv-a", base_dir=self.episode_mirror_dir)
        self.assertEqual([i["memory_id"] for i in items], ["m1"])

    def test_doc_proposal_carries_source_conversation_id(self):
        p = self._create_doc("projects/x.md", conversation_id="conv-a")
        self.assertEqual(p.source_conversation_id, "conv-a")
        self.assertEqual(p.source_harness, "claude_code")
        self.assertFalse(p.source_conversation_id_inferred)

    def test_list_proposals_filters_by_conversation_id(self):
        self._create_doc("projects/x.md", conversation_id="conv-a")
        self._create_doc("projects/y.md", conversation_id="conv-b")
        props = list_proposals(proposals_dir=self.doc_proposals_dir, conversation_id="conv-a")
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0].target_path, "projects/x.md")

    def test_list_review_conversations_aggregates_both_sources(self):
        self._stage_episode("m1", "decision", conversation_id="conv-a")  # tier1
        self._stage_episode("m2", "investigation", conversation_id="conv-a")  # tier2
        self._stage_episode("m3", "plan", conversation_id="conv-b")  # tier1
        self._create_doc("projects/x.md", conversation_id="conv-a")
        self._create_doc("projects/y.md", conversation_id="conv-a")

        conversations = list_review_conversations(
            episode_base_dir=self.episode_mirror_dir, doc_proposals_dir=self.doc_proposals_dir
        )
        by_id = {c["conversation_id"]: c for c in conversations}
        self.assertEqual(by_id["conv-a"]["episodes_tier1"], 1)
        self.assertEqual(by_id["conv-a"]["episodes_tier2"], 1)
        self.assertEqual(by_id["conv-a"]["doc_proposals"], 2)
        self.assertEqual(by_id["conv-b"]["episodes_tier1"], 1)
        self.assertEqual(by_id["conv-b"]["doc_proposals"], 0)

    def test_list_review_conversations_sorted_busiest_first(self):
        self._stage_episode("m1", "plan", conversation_id="conv-quiet")
        self._stage_episode("m2", "decision", conversation_id="conv-busy")
        self._stage_episode("m3", "plan", conversation_id="conv-busy")
        self._create_doc("projects/z.md", conversation_id="conv-busy")

        conversations = list_review_conversations(
            episode_base_dir=self.episode_mirror_dir, doc_proposals_dir=self.doc_proposals_dir
        )
        self.assertEqual(conversations[0]["conversation_id"], "conv-busy")

    def test_items_with_no_conversation_id_excluded_from_buckets(self):
        self.cons.record_reasoning_episode(
            job_id="job:m1", memory_id="m1", episode=_episode(),
            policy_name="extract", policy_version="1.0",
            approval_state="queued_for_review", supersedes=None,
        )  # no conversation_id
        conversations = list_review_conversations(
            episode_base_dir=self.episode_mirror_dir, doc_proposals_dir=self.doc_proposals_dir
        )
        self.assertEqual(conversations, [])
        # but it's still reachable unfiltered
        self.assertEqual(len(list_episode_mirrors(base_dir=self.episode_mirror_dir)), 1)

    def test_inferred_doc_proposals_flagged_in_aggregate(self):
        p = self._create_doc("projects/x.md", conversation_id="conv-a")
        # simulate a backfilled (inferred) link, same as the one-time
        # timestamp-correlation backfill would produce
        from server.proposals import _save_proposal
        p.source_conversation_id_inferred = True
        _save_proposal(p, self.doc_proposals_dir)

        conversations = list_review_conversations(
            episode_base_dir=self.episode_mirror_dir, doc_proposals_dir=self.doc_proposals_dir
        )
        self.assertEqual(conversations[0]["doc_proposals_inferred"], 1)

    def test_format_review_conversations_renders_table(self):
        self._stage_episode("m1", "decision", conversation_id="conv-a")
        conversations = list_review_conversations(
            episode_base_dir=self.episode_mirror_dir, doc_proposals_dir=self.doc_proposals_dir
        )
        text = format_review_conversations(conversations)
        self.assertIn("conv-a", text)
        self.assertIn("claude_code", text)

    def test_format_review_conversations_empty(self):
        self.assertEqual(format_review_conversations([]), "No conversations with pending items found.")

    def test_tier2_only_conversations_excluded_by_default(self):
        self._stage_episode("m1", "investigation", conversation_id="conv-quiet")  # tier2 only
        self._stage_episode("m2", "decision", conversation_id="conv-busy")  # tier1
        conversations = list_review_conversations(
            episode_base_dir=self.episode_mirror_dir, doc_proposals_dir=self.doc_proposals_dir
        )
        self.assertEqual([c["conversation_id"] for c in conversations], ["conv-busy"])

    def test_include_tier2_only_true_shows_everything(self):
        self._stage_episode("m1", "investigation", conversation_id="conv-quiet")
        conversations = list_review_conversations(
            include_tier2_only=True,
            episode_base_dir=self.episode_mirror_dir, doc_proposals_dir=self.doc_proposals_dir,
        )
        self.assertEqual([c["conversation_id"] for c in conversations], ["conv-quiet"])

    def test_filter_by_harness(self):
        self._stage_episode("m1", "decision", conversation_id="conv-a", harness="claude_code")
        self._stage_episode("m2", "decision", conversation_id="conv-b", harness="chatgpt")
        conversations = list_review_conversations(
            harness="claude_code",
            episode_base_dir=self.episode_mirror_dir, doc_proposals_dir=self.doc_proposals_dir,
        )
        self.assertEqual([c["conversation_id"] for c in conversations], ["conv-a"])

    def test_filter_by_policy_name(self):
        self.cons.record_reasoning_episode(
            job_id="job:m1", memory_id="m1", episode=_episode(kind="decision"),
            policy_name="reasoning-episode", policy_version="0.3",
            approval_state="queued_for_review", supersedes=None,
            conversation_id="conv-a", harness="claude_code",
        )
        self._stage_episode("m2", "decision", conversation_id="conv-b")  # policy_name="extract"
        conversations = list_review_conversations(
            policy_name="extract",
            episode_base_dir=self.episode_mirror_dir, doc_proposals_dir=self.doc_proposals_dir,
        )
        self.assertEqual([c["conversation_id"] for c in conversations], ["conv-b"])

    def test_harness_filter_also_applies_to_doc_proposals(self):
        self._create_doc("projects/x.md", conversation_id="conv-a", harness="claude_code")
        self._create_doc("projects/y.md", conversation_id="conv-b", harness="chatgpt")
        conversations = list_review_conversations(
            harness="claude_code",
            episode_base_dir=self.episode_mirror_dir, doc_proposals_dir=self.doc_proposals_dir,
        )
        self.assertEqual([c["conversation_id"] for c in conversations], ["conv-a"])


if __name__ == "__main__":
    unittest.main()
