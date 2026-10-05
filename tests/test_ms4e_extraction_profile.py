"""MS4e — extraction profiles reach every add_episode() call site.

No real Graphiti/FalkorDB: remember() is driven against a recording fake via
a patched get_graphiti_for_operation, and correct_memory() against the MS6b
governance suite's FakeGraphiti. The ontology is checked against graphiti's
own validators, since a bad type model only fails at add_episode() time.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from graphiti_core.helpers import validate_excluded_entity_types
from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types

from server.core.config import load_config
from server.providers import memory_graphiti
from server.providers.extraction_profile import (
    ENTITY_TYPES,
    EXCLUDED_ENTITY_TYPES,
    LEGACY_INSTRUCTIONS,
    RECALL_ENTITY_TYPES,
    RECALL_INSTRUCTIONS,
    SELECTIVE_INSTRUCTIONS,
    extraction_kwargs,
)
from server.review import projects
from server.review.correction import correct_memory
from tests.test_ms6b_governance import DEFAULT_REVIEWER, GRAPH, FakeDriver, FakeGraphiti, MS6bBase

# Labels CMF itself puts on graph nodes (server.consolidation.graph_tagging,
# scripts/seed_wiki_graph.py) plus Graphiti's own. An entity type named like
# one of these would merge extracted entities into CMF's structural nodes'
# label space — `Project` above all, which IN_PROJECT edges point at.
_RESERVED_LABELS = {"Entity", "Episodic", "Community", "Saga", "Project", "Note", "Section"}


def _env(profile: str | None):
    env = {k: v for k, v in os.environ.items() if k != "CMF_EXTRACTION_PROFILE"}
    if profile is not None:
        env["CMF_EXTRACTION_PROFILE"] = profile
    return mock.patch.dict(os.environ, env, clear=True)


class TestExtractionKwargs(unittest.TestCase):
    def test_legacy_is_the_pre_ms4e_instructions_only(self):
        self.assertEqual(extraction_kwargs("legacy"), {"custom_extraction_instructions": LEGACY_INSTRUCTIONS})
        self.assertIs(memory_graphiti.EXTRACTION_INSTRUCTIONS, LEGACY_INSTRUCTIONS)

    def test_selective_has_no_ontology(self):
        self.assertEqual(extraction_kwargs("selective"), {"custom_extraction_instructions": SELECTIVE_INSTRUCTIONS})

    def test_typed_adds_ontology_and_excludes_generic_entity(self):
        kw = extraction_kwargs("typed")
        self.assertEqual(kw["custom_extraction_instructions"], SELECTIVE_INSTRUCTIONS)
        self.assertIs(kw["entity_types"], ENTITY_TYPES)
        self.assertEqual(kw["excluded_entity_types"], ["Entity"])

    def test_unknown_profile_raises(self):
        with self.assertRaises(ValueError):
            extraction_kwargs("strict")

    def test_env_default_is_legacy(self):
        # load_config() re-reads .env, which may set a profile (production
        # does); the default under test is the code's, not the operator's.
        with _env(None), mock.patch("server.core.config.load_dotenv"):
            self.assertEqual(extraction_kwargs(), extraction_kwargs("legacy"))
            self.assertEqual(load_config().extraction_profile, "legacy")

    def test_env_selects_profile_case_insensitively(self):
        with _env(" Typed "):
            self.assertEqual(extraction_kwargs(), extraction_kwargs("typed"))
            self.assertEqual(load_config().extraction_profile, "typed")

    def test_env_typo_fails_fast(self):
        with _env("selectve"):
            with self.assertRaises(ValueError):
                extraction_kwargs()
            with self.assertRaises(ValueError):
                load_config()

    def test_selective_instructions_name_the_prompt_input_correctly(self):
        # remember() sends EpisodeType.text, whose prompt calls the input <TEXT>.
        self.assertIn("TEXT", SELECTIVE_INSTRUCTIONS)
        self.assertNotIn("CURRENT MESSAGE", SELECTIVE_INSTRUCTIONS)
        self.assertNotIn("should be rare", SELECTIVE_INSTRUCTIONS)


class TestOntology(unittest.TestCase):
    ONTOLOGIES = {"typed": ENTITY_TYPES, "typed-recall": RECALL_ENTITY_TYPES}

    def test_passes_graphiti_validators(self):
        for profile, types in self.ONTOLOGIES.items():
            with self.subTest(profile=profile):
                self.assertTrue(validate_entity_types(types))
                self.assertTrue(validate_excluded_entity_types(EXCLUDED_ENTITY_TYPES, types))

    def test_type_names_do_not_collide_with_cmf_labels(self):
        for profile, types in self.ONTOLOGIES.items():
            with self.subTest(profile=profile):
                self.assertEqual(set(types) & _RESERVED_LABELS, set())

    def test_types_are_docstring_only(self):
        # A field would cost an attribute-extraction LLM call per node.
        for profile, types in self.ONTOLOGIES.items():
            for name, model in types.items():
                with self.subTest(profile=profile, name=name):
                    self.assertEqual(model.model_fields, {})
                    self.assertTrue((model.__doc__ or "").strip())

    def test_recall_adds_format_and_topic(self):
        self.assertTrue({"Format", "Topic"} <= set(RECALL_ENTITY_TYPES))


class TestTypedRecallProject(unittest.TestCase):
    """typed-recall names the episode's own project, read from source_description."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = Path(self._tmp.name) / "taxonomy.json"
        path.write_text(json.dumps({
            "rules": [["3d", "\\b3d\\b"]],
            "display_names": {"3d": "3D", "career-navigator-dev": "Career Navigator"},
        }))
        self._env = mock.patch.dict(os.environ, {"CMF_TAXONOMY": str(path)})
        self._env.start()
        projects.clear_cache()

    def tearDown(self):
        self._env.stop()
        projects.clear_cache()
        self._tmp.cleanup()

    def _instructions(self, source_description):
        return extraction_kwargs("typed-recall", source_description)["custom_extraction_instructions"]

    def test_display_name_from_taxonomy(self):
        text = self._instructions("Promoted from claude_code via extract@1.3 | project=career-navigator-dev | evidence=2")
        self.assertTrue(text.startswith(RECALL_INSTRUCTIONS))
        self.assertIn("'Career Navigator' project", text)

    def test_slug_fallback_without_display_name(self):
        self.assertIn("'ev charging' project", self._instructions("x | project=ev-charging"))

    def test_no_project_or_misc_adds_nothing(self):
        for sd in (None, "Context Memory Fabric MCP", "x | project=misc | y"):
            with self.subTest(source_description=sd):
                self.assertEqual(self._instructions(sd), RECALL_INSTRUCTIONS)

    def test_other_profiles_ignore_the_project(self):
        for profile in ("legacy", "selective", "typed"):
            with self.subTest(profile=profile):
                self.assertEqual(
                    extraction_kwargs(profile, "x | project=3d"), extraction_kwargs(profile)
                )


class _RecordingGraphiti:
    def __init__(self):
        self.calls: list[dict] = []

    async def add_episode(self, **kw):
        self.calls.append(kw)


class TestRememberPassesProfile(unittest.IsolatedAsyncioTestCase):
    async def _remember_under(self, profile: str) -> dict:
        fake = _RecordingGraphiti()
        with _env(profile), mock.patch.object(
            memory_graphiti, "get_graphiti_for_operation", return_value=(fake, "fake-model")
        ):
            await memory_graphiti.remember(
                "Chose FalkorDB for the graph.", name="t-001",
                reference_time=datetime(2026, 9, 28, tzinfo=timezone.utc),
            )
        self.assertEqual(len(fake.calls), 1)
        return fake.calls[0]

    async def test_each_profile_reaches_add_episode(self):
        for profile in ("legacy", "selective", "typed", "typed-recall"):
            with self.subTest(profile=profile):
                call = await self._remember_under(profile)
                for key, value in extraction_kwargs(profile).items():
                    self.assertEqual(call[key], value)
                self.assertEqual("entity_types" in call, profile in ("typed", "typed-recall"))


class TestCorrectMemoryPassesProfile(MS6bBase):
    async def test_correction_uses_the_same_profile_as_remember(self):
        memory_id, _ = self._promote(seed_journal=True)
        driver = FakeDriver(episode={"uuid": "ep-1", "valid_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                                      "content": "old statement", "source_description": "reasoning_kind=decision"})
        graphiti = FakeGraphiti(driver)
        with _env("typed"):
            await correct_memory(
                self.prom, self.rev, self.cs, memory_id, "corrected statement", graphiti,
                reviewer=DEFAULT_REVIEWER, reason="fix", graph_name=GRAPH, dry_run=False,
            )
        self.assertEqual(len(graphiti.added), 1)
        added = graphiti.added[0]
        self.assertEqual(added["custom_extraction_instructions"], SELECTIVE_INSTRUCTIONS)
        self.assertIs(added["entity_types"], ENTITY_TYPES)
        self.assertEqual(added["excluded_entity_types"], ["Entity"])


if __name__ == "__main__":
    unittest.main()
