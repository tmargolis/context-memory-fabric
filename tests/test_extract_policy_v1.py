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
from server.policies.extract import (
    EXTRACT_POLICY_VERSION,
    ExtractPolicyV1,
    _canonicalize_doc_project_folder,
    _ensure_frontmatter,
    _normalize_target_path,
)
from server.policies.protocols import ExtractionCategory, PolicyContext
from server.policies.reasoning_episode import ReasoningEpisodePolicyV1
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
        self.assertEqual(candidate.target_path, "WIKI/projects/cmf/extract-policy.md")
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
        self.assertEqual(out[0].target_path, "WIKI/projects/cmf/valid.md")

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
        self.assertEqual(proposals[0].target_path, "WIKI/projects/cmf/extract-policy.md")
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


class TestTargetPathNormalization(unittest.TestCase):
    """_normalize_target_path is the deterministic backstop for a model that
    ignores the prompt's WIKI/projects/ instruction -- 2026-09-19, the doc-
    path IA follow-up (the first two real doc proposals landed at
    LLM_WIKI_PATH/projects/falkordb/, outside WIKI/ entirely)."""

    def test_bare_path_gets_full_prefix(self):
        self.assertEqual(_normalize_target_path("projects/falkordb/setup.md"), "WIKI/projects/falkordb/setup.md")

    def test_missing_projects_segment_gets_full_prefix(self):
        self.assertEqual(_normalize_target_path("WIKI/falkordb/setup.md"), "WIKI/projects/falkordb/setup.md")

    def test_already_correct_path_is_unchanged(self):
        self.assertEqual(
            _normalize_target_path("WIKI/projects/Context-Memory-Fabric/Setup.md"),
            "WIKI/projects/Context-Memory-Fabric/Setup.md",
        )

    def test_no_folder_at_all_still_lands_under_wiki_projects(self):
        self.assertEqual(_normalize_target_path("setup.md"), "WIKI/projects/setup.md")

    def test_case_insensitive_prefix_stripping(self):
        self.assertEqual(_normalize_target_path("Wiki/Projects/x/y.md"), "WIKI/projects/x/y.md")


class TestDocProjectFolderCanonicalization(unittest.TestCase):
    """_canonicalize_doc_project_folder is the deterministic backstop for a
    model that picks an existing-but-wrong project folder -- 2026-09-23,
    the DelayedVideoTablet review (the model proposed WIKI/projects/AI-Tools/
    for a DelayedVideoTablet conversation; structurally valid, so
    _normalize_target_path's prefix check alone can't catch it)."""

    def test_wrong_existing_folder_is_corrected(self):
        out = _canonicalize_doc_project_folder(
            "WIKI/projects/AI-Tools/Battery-Optimization.md",
            "DelayedVideoTablet",
            existing_folders=["AI-Tools", "Context-Memory-Fabric", "DelayedVideoTablet"],
        )
        self.assertEqual(out, "WIKI/projects/DelayedVideoTablet/Battery-Optimization.md")

    def test_already_correct_folder_is_unchanged(self):
        out = _canonicalize_doc_project_folder(
            "WIKI/projects/DelayedVideoTablet/Battery-Optimization.md",
            "DelayedVideoTablet",
            existing_folders=["DelayedVideoTablet"],
        )
        self.assertEqual(out, "WIKI/projects/DelayedVideoTablet/Battery-Optimization.md")

    def test_matches_existing_folder_case_and_punctuation_insensitively(self):
        # "jspace" (project_slug's raw directory name) should resolve to the
        # real "J-Space" folder, not mint a new "Jspace" duplicate.
        out = _canonicalize_doc_project_folder(
            "WIKI/projects/Misc/Notes.md",
            "jspace",
            existing_folders=["J-Space", "Context-Memory-Fabric"],
        )
        self.assertEqual(out, "WIKI/projects/J-Space/Notes.md")

    def test_genuinely_new_project_uses_project_string_as_is(self):
        out = _canonicalize_doc_project_folder(
            "WIKI/projects/Misc/Notes.md",
            "AstroAlert",
            existing_folders=["Context-Memory-Fabric"],
        )
        self.assertEqual(out, "WIKI/projects/AstroAlert/Notes.md")

    def test_short_path_with_no_rest_segment_is_handled(self):
        out = _canonicalize_doc_project_folder(
            "WIKI/projects/AI-Tools",
            "DelayedVideoTablet",
            existing_folders=["DelayedVideoTablet"],
        )
        self.assertEqual(out, "WIKI/projects/DelayedVideoTablet")


class TestFrontmatterBackstop(unittest.TestCase):
    """_ensure_frontmatter guarantees every applied doc has the vault's
    expected YAML block, regardless of model compliance. `created_date` is
    always the ground truth (the conversation's own date) -- see 1.5's
    changelog note: the model can't be trusted to fill in `created`/
    `updated` itself, since 1.4 let it write "today's date" for `created`,
    landing every applied doc on the review day instead of when the
    underlying conversation happened."""

    def test_missing_frontmatter_is_injected(self):
        out = _ensure_frontmatter(
            "# My Page\n\nBody text.",
            target_path="WIKI/projects/Context-Memory-Fabric/My-Page.md",
            harness="claude_code",
            created_date="2026-08-28",
        )
        self.assertTrue(out.startswith("---\n"))
        self.assertIn("title: My Page", out)
        self.assertIn("created: 2026-08-28", out)
        self.assertIn("source: claude_code", out)
        self.assertIn("- Context-Memory-Fabric", out)
        self.assertIn("# My Page\n\nBody text.", out)

    def test_created_is_always_the_conversation_date_not_the_models_value(self):
        # The model was told (pre-1.5) to write "today's date" for `created`
        # -- confirmed wrong in every doc applied for finances/AstroAlert/
        # DelayedVideoTablet (2026-09-24). Even when the model supplies a
        # created/updated pair, `created` must be forced to the real
        # conversation date, not trusted.
        content = (
            "---\ntitle: Custom\ncreated: 2026-09-23\nupdated: 2026-09-23\nstatus: active\n"
            "source: claude_code\ntags:\n  - x\n---\n\n# Custom\n"
        )
        out = _ensure_frontmatter(
            content, target_path="WIKI/projects/x/y.md", harness="claude_code", created_date="2026-08-28"
        )
        self.assertIn("created: 2026-08-28", out)
        self.assertNotIn("created: 2026-09-23", out)
        self.assertIn("title: Custom", out)  # other model-supplied values preserved
        self.assertIn("- x", out)

    def test_updated_is_always_todays_date_not_the_models_value(self):
        content = (
            "---\ntitle: Custom\ncreated: 2026-08-28\nupdated: 2020-01-02\nstatus: active\n"
            "source: claude_code\ntags:\n  - x\n---\n\n# Custom\n"
        )
        out = _ensure_frontmatter(
            content, target_path="WIKI/projects/x/y.md", harness="claude_code", created_date="2026-08-28"
        )
        today_str = datetime.now(timezone.utc).date().isoformat()
        self.assertIn(f"updated: {today_str}", out)
        self.assertNotIn("updated: 2020-01-02", out)

    def test_legacy_date_key_is_renamed_to_created_and_forced_to_conversation_date(self):
        content = "---\ntitle: Custom\ndate: 2020-01-01\n---\n\n# Custom\n"
        out = _ensure_frontmatter(
            content, target_path="WIKI/projects/x/y.md", harness="claude_code", created_date="2026-08-28"
        )
        self.assertNotIn("date:", out)
        self.assertIn("created: 2026-08-28", out)
        self.assertIn("title: Custom", out)
        self.assertIn("# Custom\n", out)

    def test_incomplete_frontmatter_is_filled_in_not_trusted_as_is(self):
        # Real case found 2026-09-23 (DelayedVideoTablet review): a proposal
        # had a --- block with title/status/source/tags but no date/created/
        # updated at all -- the old all-or-nothing check trusted it as-is.
        content = "---\ntitle: Custom\nstatus: active\nsource: claude_code\ntags:\n  - x\n---\n\n# Custom\n"
        out = _ensure_frontmatter(
            content, target_path="WIKI/projects/x/y.md", harness="claude_code", created_date="2026-08-28"
        )
        self.assertIn("title: Custom", out)  # model's own value preserved
        self.assertIn("created: 2026-08-28", out)
        self.assertIn("updated:", out)
        self.assertIn("# Custom\n", out)

    def test_created_date_with_timestamp_is_normalized_to_date_only(self):
        content = "# Custom\n\nPage text."
        out = _ensure_frontmatter(
            content, target_path="WIKI/projects/x/y.md", harness="claude_code", created_date="2026-08-28T14:29:00Z"
        )
        self.assertIn("created: 2026-08-28\n", out)
        self.assertNotIn("2026-08-28T", out)


class TestWholeCorpusWikiGrounding(unittest.TestCase):
    """Whole-corpus wiki grounding (2026-09-24): tests for passing retrieved
    corpus docs to the model prompt, preserving existing doc paths, and
    pipeline-level whole-corpus search."""

    def test_relevant_wiki_docs_rendered_in_prompt(self):
        policy = ExtractPolicyV1(generate_fn=FakeModel())
        events = [ev("e1", "Discussing J-Space visualization")]
        context = PolicyContext(
            relevant_wiki_docs=[
                {
                    "target_path": "WIKI/projects/J-Space/J-Space visualization method and decisions.md",
                    "title": "J-Space visualization: method and decisions",
                    "snippet": "Working record of how J-Space visualizations are built...",
                }
            ]
        )
        prompt = policy._build_prompt(events, context)
        self.assertIn("--- EXISTING WIKI DOCUMENTATION (searched across entire corpus", prompt)
        self.assertIn("J-Space visualization method and decisions.md", prompt)

    def test_matched_existing_wiki_doc_path_is_preserved_verbatim(self):
        policy = ExtractPolicyV1(generate_fn=FakeModel())
        events = [
            SourceEvent(
                schema_version="1.0",
                event_id="e1",
                event_type="turn.completed",
                source=SourceProvenance(harness="claude_code", conversation_id="conv-1", turn_id="t1"),
                actor_type="user",
                observed_at=BASE,
                content={"text": "Let's update the doc"},
                content_hash="h1",
                date_precision=DatePrecision.NONE,
                metadata={"project_path": "/Users/mockuser/Dev/jspace"},
            )
        ]
        # Target an existing doc outside of a simple default canonicalization
        spec = {
            "target_path": "WIKI/projects/J-Space/J-Space visualization method and decisions.md",
            "proposed_content": "# Updated\n\nContent",
            "rationale": "Updating existing doc",
            "statement": "Updates J-Space visualization doc",
        }
        relevant_docs = [
            {
                "target_path": "WIKI/projects/J-Space/J-Space visualization method and decisions.md",
                "title": "J-Space visualization: method and decisions",
            }
        ]
        candidate = policy._to_doc_candidate(
            spec,
            ordered=events,
            existing_project_folders=["J-Space"],
            relevant_wiki_docs=relevant_docs,
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(
            candidate.target_path,
            "WIKI/projects/J-Space/J-Space visualization method and decisions.md",
        )

    def test_retrieve_relevant_wiki_docs_searches_whole_corpus(self):
        from server.consolidation.pipeline import _retrieve_relevant_wiki_docs

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            wiki_dir = root / "WIKI" / "projects" / "J-Space"
            wiki_dir.mkdir(parents=True)
            doc_file = wiki_dir / "J-Space-Analysis.md"
            doc_file.write_text("# J-Space Analysis\n\nDeep quarter L31-40 ignition notes.\n", encoding="utf-8")

            # Also a file in RAW/ outside of projects/
            raw_dir = root / "RAW"
            raw_dir.mkdir(parents=True)
            raw_file = raw_dir / "Trace-Notes.md"
            raw_file.write_text("# Raw Trace Notes\n\nTrace schema answers and notes.\n", encoding="utf-8")

            events = [ev("e1", "Investigating trace schema questions for J-Space")]
            docs = _retrieve_relevant_wiki_docs(
                window_events=events,
                project="J-Space",
                conversation_title="J-Space trace schema",
                wiki_root=root,
                max_results=5,
            )
            self.assertTrue(len(docs) > 0)
            paths = [d["target_path"] for d in docs]
            # Confirms retrieval finds matching docs across the corpus
            self.assertTrue(any("J-Space-Analysis.md" in p for p in paths))


if __name__ == "__main__":
    unittest.main()

