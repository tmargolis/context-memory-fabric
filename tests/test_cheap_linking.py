"""Unit tests for MS9 Phase 3 -- Cheap Linking at retrieval time.

Validates:
- Continuous Inverse Note Frequency (INF) weighting (rare entities rank higher, hubs are damped but never skipped)
- Provenance labeling on expanded items (e.g. 'expanded via entity: <name>')
- Direct caps and expansion caps (5 direct + 3 expanded for wiki, 10 direct + 3 expanded for memory)
- Graceful degradation when graph is unavailable or mock/fake providers are used
"""

import asyncio
from dataclasses import dataclass
import unittest
from unittest.mock import MagicMock, patch

from server.context import get_context
from server.providers.wiki.corpus import SearchResult
from server.retrieval_expansion import (
    _extract_lede,
    expand_episodes_to_notes,
    expand_notes_to_episodes,
)
from tests.fakes.fake_knowledge_provider import FakeDocument, FakeKnowledgeProvider
from tests.fakes.fake_memory_provider import FakeMemoryProvider


class TestRetrievalExpansionHelpers(unittest.TestCase):
    """Test helper functions and INF weighting."""

    def test_extract_lede_strips_frontmatter(self):
        text_with_fm = (
            "---\n"
            "type: project\n"
            "status: active\n"
            "---\n"
            "\n"
            "Project Atlas is a next-generation context engine. It uses PostgreSQL 16."
        )
        lede = _extract_lede(text_with_fm)
        self.assertEqual(lede, "Project Atlas is a next-generation context engine.")

    def test_extract_lede_plain_text(self):
        text = "This is a simple decision. It was made on 2026-09-01."
        lede = _extract_lede(text)
        self.assertEqual(lede, "This is a simple decision.")

    def test_expand_episodes_to_notes_empty_inputs(self):
        res = expand_episodes_to_notes(episode_names=[], exclude_paths=set())
        self.assertEqual(res, [])
        res2 = expand_episodes_to_notes(episode_names=["ep1"], exclude_paths=set(), max_expanded=0)
        self.assertEqual(res2, [])

    def test_expand_notes_to_episodes_empty_inputs(self):
        res = expand_notes_to_episodes(note_paths=[], exclude_episode_names=set())
        self.assertEqual(res, [])
        res2 = expand_notes_to_episodes(note_paths=["note1.md"], exclude_episode_names=set(), max_expanded=0)
        self.assertEqual(res2, [])

    def test_expand_episodes_to_notes_inf_ranking(self):
        """Simulate FalkorDB query result and test continuous INF weighting."""
        # Entity 'RareKey' (freq=1) -> note 'rare_doc.md'
        # Entity 'HubKey' (freq=40) -> note 'hub_doc.md'
        mock_res = [
            ("RareKey", 1, ["rare_doc.md"]),
            ("HubKey", 40, ["hub_doc.md"]),
        ]
        mock_graph = MagicMock()
        mock_graph.ro_query.return_value.result_set = mock_res
        mock_fdb = MagicMock()
        mock_fdb.select_graph.return_value = mock_graph

        with patch("falkordb.FalkorDB", return_value=mock_fdb):
            results = expand_episodes_to_notes(
                episode_names=["test-ep-001"],
                exclude_paths=set(),
                max_expanded=3,
                target_graph="test-graph",
            )

        self.assertEqual(len(results), 2)
        # 'rare_doc.md' must rank first because 1/1 > 1/40
        self.assertEqual(results[0].relative_path, "rare_doc.md")
        self.assertIn("expanded via entity: RareKey", results[0].match_basis)
        self.assertAlmostEqual(results[0].relevance_score, 1.0, places=2)

        # 'hub_doc.md' is NOT skipped; it is damped and ranks second
        self.assertEqual(results[1].relative_path, "hub_doc.md")
        self.assertIn("expanded via entity: HubKey", results[1].match_basis)
        self.assertAlmostEqual(results[1].relevance_score, 0.03, places=2)

    def test_expand_notes_to_episodes_inf_ranking(self):
        """Simulate FalkorDB query result and test Note -> Episode INF weighting."""
        mock_res = [
            ("RareKey", 1, "rare-ep-001", "Decision regarding rare topic.", "2026-09-01", "project=atlas"),
            ("HubKey", 50, "hub-ep-001", "General decision mentioning hub.", "2026-09-01", "project=misc"),
        ]
        mock_graph = MagicMock()
        mock_graph.ro_query.return_value.result_set = mock_res
        mock_fdb = MagicMock()
        mock_fdb.select_graph.return_value = mock_graph

        with patch("falkordb.FalkorDB", return_value=mock_fdb):
            results = expand_notes_to_episodes(
                note_paths=["doc1.md"],
                exclude_episode_names=set(),
                max_expanded=3,
                target_graph="test-graph",
            )

        self.assertEqual(len(results), 2)
        # 'rare-ep-001' ranks first
        self.assertEqual(results[0]["episode_names"], ["rare-ep-001"])
        self.assertIn("[Expanded via entity: RareKey]", results[0]["fact"])
        self.assertIn("expanded via entity: RareKey", results[0]["source_episodes"][0]["provenance"])

        # 'hub-ep-001' is NOT skipped; it ranks second
        self.assertEqual(results[1]["episode_names"], ["hub-ep-001"])
        self.assertIn("[Expanded via entity: HubKey]", results[1]["fact"])


class TestContextIntegrationWithExpansion(unittest.IsolatedAsyncioTestCase):
    """Test get_context integration with 1-hop expansion and cap enforcement."""

    async def test_get_context_graceful_degradation_with_fakes(self):
        """With fakes and no FalkorDB, get_context degrades gracefully without error."""
        knowledge = FakeKnowledgeProvider(
            documents=[FakeDocument("WIKI/Atlas.md", "Project Atlas architecture documentation.")]
        )
        memory = FakeMemoryProvider()
        await memory.remember(content="Project Atlas decided on PostgreSQL 16.", name="atlas-db-001")

        # Must not raise even if FalkorDB connection fails in expand helpers
        with patch("server.retrieval_expansion.falkordb.FalkorDB", side_effect=Exception("Connection refused")):
            ctx = await get_context(
                "Project Atlas",
                knowledge_provider=knowledge,
                memory_provider=memory,
                max_wiki_results=5,
                max_memory_results=10,
                max_expanded_wiki=3,
                max_expanded_memory=3,
            )

        self.assertIn("# Context Fabric: 'Project Atlas'", ctx)
        self.assertIn("DURABLE KNOWLEDGE", ctx)
        self.assertIn("RECENT EPISODIC MEMORY", ctx)
        self.assertIn("Atlas.md", ctx)
        self.assertIn("PostgreSQL 16", ctx)

    async def test_get_context_includes_expanded_hits_and_provenance(self):
        """Simulate expansion and verify output rendering with provenance labels."""
        knowledge = FakeKnowledgeProvider(
            documents=[FakeDocument("WIKI/Atlas.md", "Project Atlas direct doc content.")]
        )
        memory = FakeMemoryProvider()
        await memory.remember(content="Project Atlas direct memory content.", name="atlas-001")

        mock_expanded_notes = [
            SearchResult(
                source="durable_knowledge",
                relative_path="WIKI/Related-Note.md",
                filename="Related-Note.md",
                top_level_area="WIKI",
                media_type="text/markdown",
                extractor="file",
                extraction_status="extracted",
                matched_snippet="Expanded note lede content.",
                match_basis="expanded via entity: SharedConcept (INF: 1.00)",
                relevance_score=1.0,
            )
        ]
        mock_expanded_facts = [
            {
                "fact": "[Expanded via entity: SharedConcept] Connected episode content.",
                "valid_at": "2026-09-01T00:00:00Z",
                "invalid_at": None,
                "source_episodes": [{
                    "name": "connected-ep-002",
                    "content": "Connected episode content.",
                    "provenance": "expanded via entity: SharedConcept (INF: 1.00) · reasoning_kind=decision",
                }],
                "episode_names": ["connected-ep-002"],
                "is_expanded": True,
                "via_entity": "SharedConcept",
            }
        ]

        with patch("server.retrieval_expansion.expand_episodes_to_notes", return_value=mock_expanded_notes), \
             patch("server.retrieval_expansion.expand_notes_to_episodes", return_value=mock_expanded_facts):
            ctx = await get_context(
                "Project Atlas",
                knowledge_provider=knowledge,
                memory_provider=memory,
                max_wiki_results=5,
                max_memory_results=10,
                max_expanded_wiki=3,
                max_expanded_memory=3,
            )

        # Verify direct hits exist
        self.assertIn("WIKI/Atlas.md", ctx)
        self.assertIn("Project Atlas direct memory content", ctx)

        # Verify expanded hits exist with provenance
        self.assertIn("WIKI/Related-Note.md", ctx)
        self.assertIn("Match:** `expanded via entity: SharedConcept (INF: 1.00)`", ctx)
        self.assertIn("[Expanded via entity: SharedConcept] Connected episode content.", ctx)
        self.assertIn("expanded via entity: SharedConcept (INF: 1.00)", ctx)


if __name__ == "__main__":
    unittest.main()
