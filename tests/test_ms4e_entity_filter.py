"""MS4e — deterministic debris filter (server.providers.entity_filter).

The classifier is pure; prune_debris_entities / filter_debris_after_add run
against a fake driver that answers the "other mentions" count per uuid and
records deletes. No FalkorDB.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest import mock

from server.providers.entity_filter import (
    debris_filter_enabled,
    filter_debris_after_add,
    noise_category,
    prune_debris_entities,
)


class TestNoiseCategoryRefinements(unittest.TestCase):
    def test_kept_by_todds_review(self):
        # Formats, techniques and roles are entities (2026-09-28 review); pairs
        # joined by a slash are names, not paths.
        for name in ["JSON", "Markdown", "parquet", "safetensors", "k-means", "PCA", "teacher-forcing",
                     "board director", "staff", "PyTorch/MPS", "Thai/English", "Stage Manager", "3D",
                     "EV charging station", "collaborative research studio"]:
            with self.subTest(name=name):
                self.assertIsNone(noise_category(name))

    def test_dropped_by_todds_review(self):
        cases = {"50 episodes": "number_or_measure", "Phase 6": "local_label",
                 "Phase 6 step 1": "local_label", "step 2": "local_label",
                 "notes/Trace schema questions - emotionmatrix.md": "file_or_path",
                 "/api/run": "file_or_path", "categories_*.jsonl": "file_or_path",
                 "~/Library/LaunchAgents": "file_or_path"}
        for name, category in cases.items():
            with self.subTest(name=name):
                self.assertEqual(noise_category(name), category)


class FakeDriver:
    def __init__(self, other_mentions: dict[str, int]):
        self.other_mentions = other_mentions
        self.deleted: list[str] = []

    async def execute_query(self, query, **params):
        if "DETACH DELETE" in query:
            self.deleted.append(params["uuid"])
            return [[]]
        if params["uuid"] not in self.other_mentions:
            return [[]]  # node already gone
        return [[{"others": self.other_mentions[params["uuid"]]}]]


def _node(uuid, name):
    return SimpleNamespace(uuid=uuid, name=name)


class TestPrune(unittest.IsolatedAsyncioTestCase):
    async def test_deletes_only_debris_this_episode_alone_mentions(self):
        driver = FakeDriver({"n1": 0, "n2": 0, "n3": 2, "n4": 0})
        nodes = [_node("n1", "qa_dump.json"), _node("n2", "J-Space"),
                 _node("n3", "layer 40"), _node("n4", "Phase 6 step 1")]
        removed = await prune_debris_entities(driver, "ep-1", nodes)
        self.assertEqual(removed, ["qa_dump.json", "Phase 6 step 1"])
        self.assertEqual(driver.deleted, ["n1", "n4"])  # layer 40 predates this episode: kept

    async def test_missing_node_is_skipped(self):
        driver = FakeDriver({})
        self.assertEqual(await prune_debris_entities(driver, "ep-1", [_node("gone", "x.py")]), [])
        self.assertEqual(driver.deleted, [])


class TestFilterSwitch(unittest.IsolatedAsyncioTestCase):
    def _env(self, value):
        env = {k: v for k, v in os.environ.items() if k != "CMF_ENTITY_DEBRIS_FILTER"}
        if value is not None:
            env["CMF_ENTITY_DEBRIS_FILTER"] = value
        return mock.patch.dict(os.environ, env, clear=True)

    def _result(self):
        return SimpleNamespace(episode=SimpleNamespace(uuid="ep-1"), nodes=[_node("n1", "jspace20.js")])

    async def test_off_by_default(self):
        driver = FakeDriver({"n1": 0})
        with self._env(None):
            self.assertFalse(debris_filter_enabled())
            self.assertEqual(await filter_debris_after_add(SimpleNamespace(driver=driver), self._result(), "ep"), [])
        self.assertEqual(driver.deleted, [])

    async def test_on_prunes(self):
        driver = FakeDriver({"n1": 0})
        with self._env("1"):
            removed = await filter_debris_after_add(SimpleNamespace(driver=driver), self._result(), "ep")
        self.assertEqual(removed, ["jspace20.js"])

    async def test_none_result_is_a_no_op(self):
        with self._env("1"):
            self.assertEqual(await filter_debris_after_add(SimpleNamespace(driver=None), None, "ep"), [])

    def test_garbage_value_fails_fast(self):
        with self._env("sometimes"):
            with self.assertRaises(ValueError):
                debris_filter_enabled()


if __name__ == "__main__":
    unittest.main()
