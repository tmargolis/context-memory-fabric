"""MS7b Phase 2 & 4 -- unit tests for the deterministic (non-LLM, non-graph)
parts of the wiki-entity-layer scripts: heading cleaning and hierarchy
parsing (build_wiki_sections.py) and IDF-weighted retention scoring
(sweep_wiki_graph.py). The LLM-calling and FalkorDB-writing parts of
build_wiki_entities.py / seed_wiki_graph.py / sweep_wiki_graph.py were
validated live against the real corpus and a real graph during the MS7b
working session instead -- there is no local model or FalkorDB fixture in
this test environment to exercise them against in CI.
"""

from __future__ import annotations

import unittest

from scripts.build_wiki_sections import _clean_heading, _norm, parse_note
from scripts.sweep_wiki_graph import _phrase_df, idf_score


class TestCleanHeading(unittest.TestCase):
    def test_strips_numbering_prefix(self):
        self.assertEqual(_clean_heading("3A. Install Docker Desktop"), "Install Docker Desktop")
        self.assertEqual(_clean_heading("Step 2 — Re-verify the original four"), "Re-verify the original four")
        self.assertEqual(_clean_heading("Phase 9 — Start the Gateway"), "Start the Gateway")

    def test_strips_dated_prefix(self):
        self.assertEqual(_clean_heading("June 4, 2026 — Board Zoom Meeting"), "Board Zoom Meeting")

    def test_strips_dated_parenthetical(self):
        self.assertEqual(_clean_heading("Apple Vision Pro Status (May 2026)"), "Apple Vision Pro Status")

    def test_leaves_headings_with_nothing_to_strip_unchanged(self):
        self.assertEqual(
            _clean_heading("Why Gemma 4 12B is especially suitable artistically"),
            "Why Gemma 4 12B is especially suitable artistically",
        )

    def test_strips_markdown_emphasis_but_keeps_non_dated_parentheticals(self):
        # Only a *dated* parenthetical is stripped (see test_strips_dated_parenthetical) --
        # "(Architectural Keystone)" carries real meaning and must survive.
        self.assertEqual(_clean_heading("**Boundary PEP** (Architectural Keystone)"), "Boundary PEP (Architectural Keystone)")


class TestParseNote(unittest.TestCase):
    def test_hierarchy_lede_and_wikilinks(self):
        text = (
            "# Top\n"
            "intro text, not counted as any section's body\n"
            "## Child One\n"
            "This section mentions [[Interlock]] and has some words in its opening.\n"
            "### Grandchild\n"
            "A nested section with its own body and a link to [[EV-Charging|the EV matter]].\n"
            "## Child Two\n"
            "No links here.\n"
        )
        notes_by_stem = {"interlock": ["WIKI/projects/Interlock.md"], "ev charging": ["WIKI/Household/EV-Charging.md"]}
        sections, referenced = parse_note("WIKI/test-note.md", text, notes_by_stem)

        by_label = {s["heading_clean"]: s for s in sections}
        self.assertEqual(set(by_label), {"Top", "Child One", "Grandchild", "Child Two"})

        top, child1, grandchild, child2 = (by_label[k] for k in ("Top", "Child One", "Grandchild", "Child Two"))
        self.assertIsNone(top["parent_id"])
        self.assertEqual(child1["parent_id"], top["section_id"])
        self.assertEqual(grandchild["parent_id"], child1["section_id"])
        # Child Two is a sibling of Child One under Top, not a child of Grandchild --
        # the level-based stack must have popped back to Top's level.
        self.assertEqual(child2["parent_id"], top["section_id"])

        self.assertIn("Interlock", child1["lede"])
        self.assertEqual(child1["wikilinks"][0]["resolved_note_path"], "WIKI/projects/Interlock.md")
        self.assertEqual(grandchild["wikilinks"][0]["target_raw"], "EV-Charging")
        self.assertEqual(grandchild["wikilinks"][0]["resolved_note_path"], "WIKI/Household/EV-Charging.md")
        self.assertEqual(referenced, {"WIKI/projects/Interlock.md", "WIKI/Household/EV-Charging.md"})
        self.assertEqual(child2["wikilinks"], [])

    def test_unresolved_wikilink_keeps_target_with_null_resolution(self):
        text = "## A Section\nLinks to [[Some Note That Does Not Exist]].\n"
        sections, referenced = parse_note("WIKI/x.md", text, {})
        self.assertEqual(sections[0]["wikilinks"][0]["resolved_note_path"], None)
        self.assertEqual(referenced, set())

    def test_boilerplate_flag(self):
        text = "## Sources\nSome bibliography.\n## A Real Topic\nReal content.\n"
        sections, _ = parse_note("WIKI/x.md", text, {})
        by_label = {s["heading_clean"]: s for s in sections}
        self.assertTrue(by_label["Sources"]["is_boilerplate"])
        self.assertFalse(by_label["A Real Topic"]["is_boilerplate"])

    def test_boilerplate_sections_keep_their_own_wikilinks(self):
        """A dropped-from-decomposition Sources section must not also drop
        its own bibliography links -- those still deserve a REFERENCES edge."""
        notes_by_stem = {"some ref": ["WIKI/Some-Ref.md"]}
        text = "## Sources\nSee [[Some Ref]] for background.\n"
        sections, referenced = parse_note("WIKI/x.md", text, notes_by_stem)
        self.assertTrue(sections[0]["is_boilerplate"])
        self.assertEqual(sections[0]["wikilinks"][0]["resolved_note_path"], "WIKI/Some-Ref.md")
        self.assertIn("WIKI/Some-Ref.md", referenced)


class TestIdfScoring(unittest.TestCase):
    def test_common_word_scores_low(self):
        # "table" appears in 90 of 100 notes -- a near-ubiquitous word
        score = idf_score("table", note_count=100, word_df={"table": 90})
        self.assertLess(score, 1.0)

    def test_rare_specific_name_scores_high(self):
        score = idf_score("Kotlin", note_count=100, word_df={"kotlin": 2})
        self.assertGreater(score, 2.5)

    def test_unseen_word_scores_maximally(self):
        score = idf_score("Nonexistent Term", note_count=100, word_df={})
        self.assertGreater(score, idf_score("Kotlin", note_count=100, word_df={"kotlin": 2}))

    def test_multiword_phrase_uses_rarest_word(self):
        # "Kotlin" is rare (df=2) but "code" is common (df=80); the phrase
        # "Kotlin code" should score as rare as its rarest word (Kotlin),
        # not get penalized down to "code"'s commonness.
        df = _phrase_df(_norm("Kotlin code"), 100, {"kotlin": 2, "code": 80})
        self.assertEqual(df, 2)


class TestEntityBuildCheckpoint(unittest.TestCase):
    """scripts/build_wiki_entities.py's resume logic -- added after a real
    SSH-tunnel drop interrupted a live run mid-corpus during the MS7b
    working session (2026-09-13). Exercises _load_checkpoint() against a
    synthetic prior-run output; the network-calling half of build() is not
    testable without a live model and is validated live instead."""

    def test_resume_reconstructs_done_ids_and_entities(self):
        import json
        import tempfile
        from pathlib import Path as _Path
        from scripts.build_wiki_entities import _load_checkpoint

        prior = {
            "entities": {
                "kotlin": {"name": "Kotlin", "type": "language", "sections": ["s1", "s2"]},
            },
            "processed_section_ids_no_entities": ["s3"],
            "stoplist_filtered_mentions": 4,
        }
        with tempfile.TemporaryDirectory() as d:
            p = _Path(d) / "checkpoint.json"
            p.write_text(json.dumps(prior))
            entities, done_ids, stoplist_hits = _load_checkpoint(p)

        self.assertEqual(done_ids, {"s1", "s2", "s3"})
        self.assertEqual(entities["kotlin"]["name"], "Kotlin")
        self.assertEqual(stoplist_hits, 4)

    def test_missing_checkpoint_is_empty_state(self):
        from pathlib import Path as _Path
        from scripts.build_wiki_entities import _load_checkpoint
        entities, done_ids, stoplist_hits = _load_checkpoint(_Path("/nonexistent/path.json"))
        self.assertEqual((entities, done_ids, stoplist_hits), ({}, set(), 0))

    def test_corrupt_checkpoint_is_treated_as_empty_not_a_crash(self):
        import tempfile
        from pathlib import Path as _Path
        from scripts.build_wiki_entities import _load_checkpoint
        with tempfile.TemporaryDirectory() as d:
            p = _Path(d) / "checkpoint.json"
            p.write_text("{not valid json")
            entities, done_ids, stoplist_hits = _load_checkpoint(p)
        self.assertEqual((entities, done_ids, stoplist_hits), ({}, set(), 0))


if __name__ == "__main__":
    unittest.main()


class TestDuplicateGrouping(unittest.TestCase):
    """scripts/sweep_wiki_graph.py's _group_and_pick_canonical -- the
    group_id-mismatch merge fix found 2026-09-13 during the MS7b replay
    (two byte-identical "Anthropic" entities never resolved to each other
    because Graphiti's dedup search is scoped by group_id, and the wiki
    seed used a different one than episode replay defaults to)."""

    def _rec(self, uuid, name, source=None, created_at="2026-01-01"):
        return {"uuid": uuid, "name": name, "source": source, "created_at": created_at}

    def test_no_duplicates_returns_empty(self):
        from scripts.sweep_wiki_graph import _group_and_pick_canonical
        recs = [self._rec("u1", "Anthropic"), self._rec("u2", "OpenAI")]
        self.assertEqual(_group_and_pick_canonical(recs), [])

    def test_wiki_sourced_member_is_always_canonical(self):
        from scripts.sweep_wiki_graph import _group_and_pick_canonical
        recs = [
            self._rec("u1", "Anthropic", source=None, created_at="2026-01-01"),
            self._rec("u2", "Anthropic", source="wiki", created_at="2026-06-01"),
        ]
        groups = _group_and_pick_canonical(recs)
        self.assertEqual(len(groups), 1)
        canonical, duplicates = groups[0]
        self.assertEqual(canonical["uuid"], "u2")
        self.assertEqual([d["uuid"] for d in duplicates], ["u1"])

    def test_no_wiki_member_picks_earliest_created(self):
        from scripts.sweep_wiki_graph import _group_and_pick_canonical
        recs = [
            self._rec("u1", "Foo", created_at="2026-05-01"),
            self._rec("u2", "Foo", created_at="2026-01-01"),
            self._rec("u3", "Foo", created_at="2026-03-01"),
        ]
        canonical, duplicates = _group_and_pick_canonical(recs)[0]
        self.assertEqual(canonical["uuid"], "u2")
        self.assertEqual({d["uuid"] for d in duplicates}, {"u1", "u3"})

    def test_unicode_whitespace_variant_groups_together(self):
        """The NVIDIA Spark case: a non-breaking space makes two names
        byte-different but visually identical -- _norm() must still group them."""
        from scripts.sweep_wiki_graph import _group_and_pick_canonical
        recs = [
            self._rec("u1", "NVIDIA Spark", source="wiki"),
            self._rec("u2", "NVIDIA\xa0Spark", source=None),  # non-breaking space
        ]
        groups = _group_and_pick_canonical(recs)
        self.assertEqual(len(groups), 1)
        canonical, duplicates = groups[0]
        self.assertEqual(canonical["uuid"], "u1")
        self.assertEqual(duplicates[0]["uuid"], "u2")

    def test_case_only_variant_groups_together(self):
        from scripts.sweep_wiki_graph import _group_and_pick_canonical
        recs = [self._rec("u1", "Wiki", source="wiki"), self._rec("u2", "WIKI")]
        groups = _group_and_pick_canonical(recs)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0]["uuid"], "u1")

    def test_multiple_independent_groups(self):
        from scripts.sweep_wiki_graph import _group_and_pick_canonical
        recs = [
            self._rec("u1", "Anthropic", source="wiki"), self._rec("u2", "Anthropic"),
            self._rec("u3", "LinkedIn", source="wiki"), self._rec("u4", "LinkedIn"),
            self._rec("u5", "Unrelated"),
        ]
        groups = _group_and_pick_canonical(recs)
        self.assertEqual(len(groups), 2)
        canonicals = {g[0]["uuid"] for g in groups}
        self.assertEqual(canonicals, {"u1", "u3"})
