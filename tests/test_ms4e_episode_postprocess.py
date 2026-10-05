"""MS4e — what CMF does after add_episode(): debris filter, project entity, episode embedding.

Fakes throughout: a driver that answers the two lookups these steps run and
records writes, and Graphiti's EntityNode/EpisodicEdge `save` patched out.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from graphiti_core.edges import EpisodicEdge
from graphiti_core.nodes import EntityNode

from server.providers import episode_postprocess as pp
from server.providers.entity_filter import prune_debris_entities
from server.review import projects

SD = "Promoted from claude_code via extract@1.3 | project=jspace | evidence=2"


class FakeDriver:
    def __init__(self, existing_entity_uuid=None, other_mentions=None):
        self.existing_entity_uuid = existing_entity_uuid
        self.other_mentions = other_mentions or {}
        self.queries: list[tuple[str, dict]] = []

    async def execute_query(self, query, **params):
        self.queries.append((query, params))
        if "toLower(n.name)" in query:
            return [[{"uuid": self.existing_entity_uuid}] if self.existing_entity_uuid else []]
        if "count(x) AS others" in query:
            return [[{"others": self.other_mentions.get(params["uuid"], 0)}]]
        return [[]]


class FakeEmbedder:
    async def create(self, input_data):
        return [0.25, 0.5]


def _graphiti(driver):
    return SimpleNamespace(driver=driver, embedder=FakeEmbedder())


def _result(nodes=()):
    return SimpleNamespace(episode=SimpleNamespace(uuid="ep-1", group_id="_"), nodes=list(nodes))


def _node(uuid, name, labels=("Entity",)):
    return SimpleNamespace(uuid=uuid, name=name, labels=list(labels))


_TAXONOMY_DIR = tempfile.TemporaryDirectory()
_TAXONOMY = Path(_TAXONOMY_DIR.name) / "taxonomy.json"
_TAXONOMY.write_text(json.dumps({"rules": [], "display_names": {"jspace": "J-Space"}}))


def _env(**values):
    keys = ("CMF_EXTRACTION_PROFILE", "CMF_ENTITY_DEBRIS_FILTER", "CMF_EMBED_EPISODES")
    env = {k: v for k, v in os.environ.items() if k not in keys}
    env["CMF_TAXONOMY"] = str(_TAXONOMY)
    env.update(values)
    return mock.patch.dict(os.environ, env, clear=True)


class _TaxonomyEnv:
    """A fixed taxonomy, so the project display name doesn't depend on the local file."""

    def setUp(self):
        self._taxonomy_env = _env()
        self._taxonomy_env.start()
        projects.clear_cache()

    def tearDown(self):
        self._taxonomy_env.stop()
        projects.clear_cache()


class TestProjectEntity(_TaxonomyEnv, unittest.IsolatedAsyncioTestCase):
    async def test_skipped_when_extraction_already_has_it(self):
        driver = FakeDriver()
        with mock.patch.object(EpisodicEdge, "save") as edge_save:
            name = await pp.ensure_project_entity(_graphiti(driver), _result([_node("n1", "J-Space")]), SD)
        self.assertEqual(name, "J-Space")
        edge_save.assert_not_called()
        self.assertEqual(driver.queries, [])

    async def test_reuses_existing_entity_case_insensitively(self):
        driver = FakeDriver(existing_entity_uuid="old-uuid")
        with mock.patch.object(EpisodicEdge, "save", autospec=True) as edge_save, \
             mock.patch.object(EntityNode, "save") as node_save:
            await pp.ensure_project_entity(_graphiti(driver), _result(), SD)
        node_save.assert_not_called()
        edge = edge_save.call_args.args[0]
        self.assertEqual((edge.source_node_uuid, edge.target_node_uuid), ("ep-1", "old-uuid"))

    async def test_creates_typed_entity_when_missing(self):
        driver = FakeDriver()
        with mock.patch.object(EpisodicEdge, "save", autospec=True) as edge_save, \
             mock.patch.object(EntityNode, "save", autospec=True) as node_save:
            await pp.ensure_project_entity(_graphiti(driver), _result(), SD)
        node = node_save.call_args.args[0]
        self.assertEqual(node.name, "J-Space")
        self.assertIn("Workstream", node.labels)
        self.assertEqual(node.name_embedding, [0.25, 0.5])
        self.assertEqual(edge_save.call_args.args[0].target_node_uuid, node.uuid)

    async def test_no_project_means_nothing(self):
        self.assertIsNone(await pp.ensure_project_entity(_graphiti(FakeDriver()), _result(), "x | project=misc"))
        self.assertIsNone(await pp.ensure_project_entity(_graphiti(FakeDriver()), _result(), None))


class TestPostprocess(_TaxonomyEnv, unittest.IsolatedAsyncioTestCase):
    async def test_embeds_episode_content(self):
        driver = FakeDriver()
        with _env(CMF_EXTRACTION_PROFILE="legacy"):
            report = await pp.postprocess_episode(
                _graphiti(driver), _result(), episode_name="e", content="hello", source_description=SD)
        self.assertTrue(report["embedded"])
        query, params = driver.queries[-1]
        self.assertIn("content_embedding = vecf32($vec)", query)
        self.assertEqual(params, {"uuid": "ep-1", "vec": [0.25, 0.5]})
        self.assertIsNone(report["project_entity"])  # legacy never adds one

    async def test_embedding_can_be_turned_off(self):
        driver = FakeDriver()
        with _env(CMF_EMBED_EPISODES="0"):
            report = await pp.postprocess_episode(
                _graphiti(driver), _result(), episode_name="e", content="x", source_description=None)
        self.assertFalse(report["embedded"])
        self.assertEqual(driver.queries, [])

    async def test_typed_recall_adds_project_and_runs_filter(self):
        driver = FakeDriver(existing_entity_uuid="proj")
        nodes = [_node("n1", "qa_dump.json"), _node("n2", "PCA")]
        with _env(CMF_EXTRACTION_PROFILE="typed-recall", CMF_ENTITY_DEBRIS_FILTER="1"), \
             mock.patch.object(EpisodicEdge, "save"):
            report = await pp.postprocess_episode(
                _graphiti(driver), _result(nodes), episode_name="e", content="x", source_description=SD)
        self.assertEqual(report["debris_removed"], ["qa_dump.json"])
        self.assertEqual(report["project_entity"], "J-Space")
        self.assertEqual(report["errors"], [])

    async def test_a_failing_step_is_reported_not_raised(self):
        class Broken(FakeEmbedder):
            async def create(self, input_data):
                raise RuntimeError("embedder down")
        g = SimpleNamespace(driver=FakeDriver(), embedder=Broken())
        with _env(CMF_EXTRACTION_PROFILE="legacy"):
            report = await pp.postprocess_episode(g, _result(), episode_name="e", content="x", source_description=None)
        self.assertFalse(report["embedded"])
        self.assertIn("embedder down", report["errors"][0])

    async def test_none_result_is_a_no_op(self):
        report = await pp.postprocess_episode(
            _graphiti(FakeDriver()), None, episode_name="e", content="x", source_description=SD)
        self.assertEqual(report, {"debris_removed": [], "project_entity": None, "embedded": False, "errors": []})


class TestIdentifierExemption(unittest.IsolatedAsyncioTestCase):
    async def test_identifier_typed_as_software_is_kept_files_are_not(self):
        driver = FakeDriver()
        nodes = [
            _node("n1", "huggingface_hub", ("Entity", "Software")),
            _node("n2", "qa_dump.json", ("Entity", "Software")),
            _node("n3", "workspace_dump", ("Entity", "Topic")),
        ]
        removed = await prune_debris_entities(driver, "ep-1", nodes)
        self.assertEqual(removed, ["qa_dump.json", "workspace_dump"])


if __name__ == "__main__":
    unittest.main()
