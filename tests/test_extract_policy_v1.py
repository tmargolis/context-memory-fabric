"""ExtractPolicyV1 -- unit and pipeline-level tests for the new
episode+doc-proposal extraction policy (docs/plan-active.md, "Wiki->doc
rename and doc-proposal extraction", 2026-09-19).

The model call is faked throughout, same convention as
tests/test_ms3_5_reasoning_episodes.py -- nothing here needs a real LLM.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from server.consolidation.pipeline import run_reasoning_consolidation
from server.consolidation.store import ConsolidationStore
from server.consolidation.threads import ThreadIndex
from server.core.models import DatePrecision, SourceEvent, SourceProvenance
from server.journal.identity import compute_content_hash
from server.journal.store import SqliteEventStore
from server.policies.extract_v1 import EXTRACT_POLICY_VERSION, ExtractPolicyV1
from server.policies.protocols import ExtractionCategory, PolicyContext
from server.policies.reasoning_episode_v1 import ReasoningEpisodePolicyV1
from server.proposals import list_proposals

BASE = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def ev(event_id, text, actor_type="user", conversation_id="A", harness="chatgpt", minute_offset=0):
    content = {"text": text}
    return SourceEvent(
        schema_version="1.0",
        event_id=event_id,
        event_type="turn.completed",
        source=SourceProvenance(harness=harness, conversation_id=conversation_id, turn_id=event_id),
        actor_type=actor_type,
        observed_at=BASE + timedelta(minutes=minute_offset),
        content=content,
        content_hash=compute_content_hash(content),
        date_precision=DatePrecision.NONE,
    )


class _AlwaysGrant:
    def reserve(self, estimated_calls=None, now=None):
        return "fake-model"


class FakeModel:
    """Returns canned {episodes, doc_proposals} JSON based on prompt markers."""

    def __init__(self):
        self.calls = []

    def __call__(self, model: str, prompt: str) -> str:
        self.calls.append(prompt)
        if "EPISODE-ONLY-MARKER" in prompt:
            return json.dumps(
                {
                    "episodes": [
                        {
                            "reasoning_kind": "decision",
                            "statement": "Chose SQLite for the journal.",
                            "thread_key": "journal-backend",
                            "confidence": 0.9,
                            "turn_numbers": [1],
                        }
                    ],
                    "doc_proposals": [],
                }
            )
        if "DOC-ONLY-MARKER" in prompt:
            return json.dumps(
                {
                    "episodes": [],
                    "doc_proposals": [
                        {
                            "target_path": "projects/cmf/extract-policy.md",
                            "proposed_content": "# Extract Policy\n\nHow ExtractPolicyV1 works.\n",
                            "rationale": "durable reference material about the pipeline's own design",
                            "statement": "Documents how ExtractPolicyV1 splits episodes from doc proposals.",
                            "turn_numbers": [1],
                        }
                    ],
                }
            )
        if "MIXED-MARKER" in prompt:
            return json.dumps(
                {
                    "episodes": [
                        {
                            "reasoning_kind": "finding",
                            "statement": "Confirmed the split works in one call.",
                            "thread_key": "extract-policy-v1",
                            "confidence": 0.8,
                            "turn_numbers": [1],
                        }
                    ],
                    "doc_proposals": [
                        {
                            "target_path": "projects/cmf/extract-policy.md",
                            "proposed_content": "# Extract Policy\n\nUpdated.\n",
                            "rationale": "durable reference material",
                            "statement": "Doc summary.",
                            "turn_numbers": [1],
                        }
                    ],
                }
            )
        if "MALFORMED-DOC-MARKER" in prompt:
            return json.dumps(
                {
                    "episodes": [],
                    "doc_proposals": [
                        {"target_path": "", "proposed_content": "", "rationale": "", "statement": "missing everything"},
                        {
                            "target_path": "projects/cmf/valid.md",
                            "proposed_content": "# Valid\n",
                            "rationale": "this one is well-formed",
                            "statement": "the valid one",
                            "turn_numbers": [1],
                        },
                    ],
                }
            )
        return json.dumps({"episodes": [], "doc_proposals": []})


class TestExtractPolicyV1Unit(unittest.TestCase):
    """Policy-level: evaluate_window() alone, no pipeline/store involved."""

    def setUp(self):
        self.model = FakeModel()
        self.policy = ExtractPolicyV1(generate_fn=self.model, rate_limiter=_AlwaysGrant())

    def _window(self, marker: str):
        return [ev("e1", f"{marker} let's think this through", "user")]

    def test_identity_is_distinct_from_reasoning_episode_policy(self):
        self.assertEqual(self.policy.name, "extract")
        self.assertEqual(self.policy.version, EXTRACT_POLICY_VERSION)
        self.assertNotEqual(self.policy.name, ReasoningEpisodePolicyV1.name)

    def test_episode_only_reply_yields_one_episodic_candidate(self):
        out = self.policy.evaluate_window(self._window("EPISODE-ONLY-MARKER"), PolicyContext())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].category, ExtractionCategory.EPISODIC)
        self.assertEqual(out[0].reasoning_kind, "decision")
        self.assertEqual(self.policy.doc_proposals_total, 0)
        self.assertEqual(self.policy.episodes_total, 1)

    def test_doc_only_reply_yields_one_durable_candidate(self):
        out = self.policy.evaluate_window(self._window("DOC-ONLY-MARKER"), PolicyContext())
        self.assertEqual(len(out), 1)
        candidate = out[0]
        self.assertEqual(candidate.category, ExtractionCategory.DURABLE_CANDIDATE)
        self.assertEqual(candidate.target_path, "projects/cmf/extract-policy.md")
        self.assertIn("ExtractPolicyV1", candidate.proposed_content)
        self.assertTrue(candidate.rationale)
        self.assertEqual(self.policy.doc_proposals_total, 1)
        self.assertEqual(self.policy.episodes_total, 0)

    def test_mixed_reply_yields_both_shapes_from_one_call(self):
        out = self.policy.evaluate_window(self._window("MIXED-MARKER"), PolicyContext())
        self.assertEqual(len(out), 2)
        self.assertEqual(len(self.model.calls), 1)  # one model call produced both
        categories = {c.category for c in out}
        self.assertEqual(categories, {ExtractionCategory.EPISODIC, ExtractionCategory.DURABLE_CANDIDATE})

    def test_malformed_doc_proposal_dropped_without_crashing(self):
        out = self.policy.evaluate_window(self._window("MALFORMED-DOC-MARKER"), PolicyContext())
        # the empty-fields spec is dropped; the well-formed one survives
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].target_path, "projects/cmf/valid.md")

    def test_prompt_includes_doc_instructions(self):
        self.policy.evaluate_window(self._window("EPISODE-ONLY-MARKER"), PolicyContext())
        prompt = self.model.calls[0]
        self.assertIn("DOC PROPOSAL", prompt)
        self.assertIn("doc_proposals", prompt)


class TestExtractPolicyV1Pipeline(unittest.TestCase):
    """Pipeline-level: run_reasoning_consolidation() actually routes a
    DURABLE_CANDIDATE item to create_doc_proposal(), and an EPISODIC item
    still lands in derived_memories, from the same run.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.journal_path = Path(self._tmp.name) / "journal.db"
        self.cons_path = Path(self._tmp.name) / "consolidation.db"
        self.wiki_root = Path(self._tmp.name) / "wiki"
        self.wiki_root.mkdir()
        self.proposals_dir = Path(self._tmp.name) / "doc-proposals"
        self.model = FakeModel()

    def tearDown(self):
        self._tmp.cleanup()

    def _consolidate(self, **kw):
        with SqliteEventStore(self.journal_path) as j, ConsolidationStore(self.cons_path) as c:
            ti = ThreadIndex(self.cons_path)
            policy = ExtractPolicyV1(generate_fn=self.model, rate_limiter=_AlwaysGrant())
            try:
                return run_reasoning_consolidation(
                    j, c, policy, thread_index=ti,
                    wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
                    **kw,
                )
            finally:
                ti.close()

    def test_doc_proposal_written_to_store_not_derived_memories(self):
        with SqliteEventStore(self.journal_path) as j:
            for i in range(4):
                j.append(ev(f"d{i}", f"DOC-ONLY-MARKER turn {i}", "user" if i % 2 == 0 else "assistant", minute_offset=i))

        stats = self._consolidate(triage=False, min_window_events=1)
        self.assertEqual(stats["doc_proposals_created"], 1)
        self.assertEqual(stats["doc_proposals_failed"], 0)
        self.assertEqual(stats["episodes_created"], 0)

        proposals = list_proposals(proposals_dir=self.proposals_dir)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].target_path, "projects/cmf/extract-policy.md")
        self.assertEqual(proposals[0].operation, "create")

        with ConsolidationStore(self.cons_path) as c:
            rows = c.query_derived_memories()
        self.assertEqual(len(rows), 0)  # a doc proposal never becomes a derived_memories row

    def test_mixed_window_writes_both_an_episode_and_a_doc_proposal(self):
        with SqliteEventStore(self.journal_path) as j:
            for i in range(4):
                j.append(ev(f"m{i}", f"MIXED-MARKER turn {i}", "user" if i % 2 == 0 else "assistant", minute_offset=i))

        stats = self._consolidate(triage=False, min_window_events=1)
        self.assertEqual(stats["episodes_created"], 1)
        self.assertEqual(stats["doc_proposals_created"], 1)

        with ConsolidationStore(self.cons_path) as c:
            rows = c.query_derived_memories()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["policy_name"], "extract")
        self.assertEqual(rows[0]["policy_version"], EXTRACT_POLICY_VERSION)

        proposals = list_proposals(proposals_dir=self.proposals_dir)
        self.assertEqual(len(proposals), 1)

    def test_reasoning_episode_policy_still_never_creates_doc_proposals(self):
        """Regression guard: the old policy shares run_reasoning_consolidation's
        new branch but never emits DURABLE_CANDIDATE, so it must be a no-op
        for it -- confirms Phase 2 didn't change Phase 1's policy's behavior.
        """
        with SqliteEventStore(self.journal_path) as j:
            for i in range(4):
                j.append(ev(f"r{i}", f"MIXED-MARKER turn {i}", "user" if i % 2 == 0 else "assistant", minute_offset=i))

        with SqliteEventStore(self.journal_path) as j, ConsolidationStore(self.cons_path) as c:
            ti = ThreadIndex(self.cons_path)
            policy = ReasoningEpisodePolicyV1(generate_fn=self.model, rate_limiter=_AlwaysGrant())
            try:
                stats = run_reasoning_consolidation(
                    j, c, policy, thread_index=ti,
                    wiki_root=self.wiki_root, proposals_dir=self.proposals_dir,
                    triage=False, min_window_events=1,
                )
            finally:
                ti.close()

        # ReasoningEpisodePolicyV1's own prompt has no doc_proposals key, so
        # the fake model's MIXED-MARKER branch still returns one (ignored)
        # since the policy's _to_episode/_parse_episodes only ever reads
        # "episodes" -- doc_proposals_created must stay 0 regardless.
        self.assertEqual(stats["doc_proposals_created"], 0)
        self.assertEqual(list_proposals(proposals_dir=self.proposals_dir), [])


if __name__ == "__main__":
    unittest.main()
