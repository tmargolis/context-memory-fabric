"""MS6b — governance: explain() into Graphiti, correct_memory, delete_memory.

No real FalkorDB/Graphiti. `FakeDriver`/`FakeGraphiti` are hand-rolled test
doubles matching just enough of graphiti_core's shape (`driver.execute_query`
returning `[[rows...]]`, `remove_episode`/`add_episode` coroutines) for these
three MS6b modules, which is all any of them touch — see
server/providers/memory_graphiti.py for the real Graphiti-backed
implementation these mirror.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from server.consolidation.promotion import PromotionStore
from server.review.correction import correct_memory, delete_memory
from server.review.graph_explain import explain_graph
from server.review.store import ReviewStore

GRAPH = "test-graph"
DEFAULT_REVIEWER = "todd"


class FakeDriver:
    def __init__(self, episode=None, entities=None, edges=None):
        self.episode = episode
        self.entities = entities or []
        self.edges = edges or []
        self.calls: list[tuple[str, dict]] = []

    async def execute_query(self, query, **params):
        self.calls.append((query, params))
        if "MENTIONS" in query:
            return [self.entities]
        if "RELATES_TO" in query:
            return [self.edges]
        if "Episodic {name:" in query:
            return [[self.episode] if self.episode else []]
        raise AssertionError(f"unexpected query: {query}")


class FakeGraphiti:
    def __init__(self, driver: FakeDriver):
        self.driver = driver
        self.removed: list[str] = []
        self.added: list[dict] = []

    async def remove_episode(self, uuid):
        self.removed.append(uuid)

    async def add_episode(self, **kw):
        self.added.append(kw)


class MS6bBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "journal.db"
        self.prom = PromotionStore(db_path=self.db)
        self.rev = ReviewStore(db_path=self.db)

    def tearDown(self):
        self.prom.close()
        self.rev.close()
        self._tmp.cleanup()

    def _promote(self, memory_id="r:0::reasoning-episode@0.2", episode_name="claude-cmf-001"):
        self.prom.record_success(memory_id, episode_name, GRAPH)
        return memory_id, episode_name


class TestExplainGraph(MS6bBase):
    async def test_none_when_never_promoted(self):
        result = await explain_graph(self.prom, "no-such-memory", FakeGraphiti(FakeDriver()), graph_name=GRAPH)
        self.assertIsNone(result)

    async def test_found_in_graph_false_when_ledger_ahead_of_graph(self):
        memory_id, _ = self._promote()
        driver = FakeDriver(episode=None)  # ledger says promoted; graph has nothing
        result = await explain_graph(self.prom, memory_id, FakeGraphiti(driver), graph_name=GRAPH)
        self.assertFalse(result["found_in_graph"])
        self.assertEqual(result["entities"], [])
        self.assertEqual(result["edges"], [])

    async def test_full_walk_returns_entities_and_edges(self):
        memory_id, episode_name = self._promote()
        driver = FakeDriver(
            episode={"uuid": "ep-1", "name": episode_name, "content": "Chose SQLite for the journal.",
                     "valid_at": "2026-05-01T00:00:00+00:00", "source_description": "reasoning_kind=decision"},
            entities=[{"uuid": "n-1", "name": "SQLite", "summary": "embedded database"}],
            edges=[{"uuid": "e-1", "source": "CMF", "fact": "CMF uses SQLite for the journal",
                    "target": "SQLite", "valid_at": "2026-05-01T00:00:00+00:00", "invalid_at": None}],
        )
        result = await explain_graph(self.prom, memory_id, FakeGraphiti(driver), graph_name=GRAPH)
        self.assertTrue(result["found_in_graph"])
        self.assertEqual(result["episode_uuid"], "ep-1")
        self.assertEqual(result["entity_count"], 1)
        self.assertEqual(result["edge_count"], 1)
        self.assertEqual(result["entities"][0]["name"], "SQLite")
        self.assertEqual(result["edges"][0]["fact"], "CMF uses SQLite for the journal")


class TestCorrectMemory(MS6bBase):
    async def test_error_when_not_promoted(self):
        result = await correct_memory(
            self.prom, self.rev, "unpromoted", "new text", FakeGraphiti(FakeDriver()),
            reviewer=DEFAULT_REVIEWER, reason="fix", graph_name=GRAPH,
        )
        self.assertIn("error", result)

    async def test_dry_run_makes_no_graph_or_ledger_changes(self):
        memory_id, episode_name = self._promote()
        driver = FakeDriver(episode={"uuid": "ep-1", "valid_at": "2026-05-01T00:00:00+00:00",
                                      "content": "old statement", "source_description": "x"})
        graphiti = FakeGraphiti(driver)
        result = await correct_memory(
            self.prom, self.rev, memory_id, "corrected statement", graphiti,
            reviewer=DEFAULT_REVIEWER, reason="fix", graph_name=GRAPH, dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(graphiti.removed, [])
        self.assertEqual(graphiti.added, [])
        self.assertEqual(self.prom.get(memory_id, GRAPH)["episode_name"], episode_name)

    async def test_apply_removes_readds_updates_ledger_and_audits(self):
        memory_id, old_name = self._promote()
        driver = FakeDriver(episode={"uuid": "ep-1", "valid_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                                      "content": "old statement", "source_description": "reasoning_kind=decision"})
        graphiti = FakeGraphiti(driver)
        result = await correct_memory(
            self.prom, self.rev, memory_id, "corrected statement", graphiti,
            reviewer=DEFAULT_REVIEWER, reason="reviewer caught a factual error", graph_name=GRAPH, dry_run=False,
        )
        self.assertFalse(result["dry_run"])
        self.assertEqual(graphiti.removed, ["ep-1"])
        self.assertEqual(len(graphiti.added), 1)
        added = graphiti.added[0]
        self.assertEqual(added["episode_body"], "corrected statement")
        self.assertEqual(added["reference_time"], datetime(2026, 5, 1, tzinfo=timezone.utc))

        # Ledger now points at the new episode name, still promoted.
        row = self.prom.get(memory_id, GRAPH)
        self.assertEqual(row["episode_name"], result["new_episode_name"])
        self.assertNotEqual(row["episode_name"], old_name)
        self.assertTrue(self.prom.is_promoted(memory_id, GRAPH))

        # Audited without touching a review verdict.
        audit = self.rev.audit_for(memory_id)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "correct_memory")
        self.assertIsNone(self.rev.get(memory_id))  # .note() never writes `reviews`

    async def test_noop_when_content_unchanged(self):
        memory_id, _ = self._promote()
        driver = FakeDriver(episode={"uuid": "ep-1", "valid_at": "2026-05-01T00:00:00+00:00",
                                      "content": "same statement", "source_description": "x"})
        graphiti = FakeGraphiti(driver)
        result = await correct_memory(
            self.prom, self.rev, memory_id, "same statement", graphiti,
            reviewer=DEFAULT_REVIEWER, reason="fix", graph_name=GRAPH, dry_run=False,
        )
        self.assertIn("note", result)
        self.assertEqual(graphiti.removed, [])
        self.assertEqual(graphiti.added, [])


class TestDeleteMemory(MS6bBase):
    async def test_error_when_not_promoted(self):
        result = await delete_memory(
            self.prom, self.rev, "unpromoted", FakeGraphiti(FakeDriver()),
            reviewer=DEFAULT_REVIEWER, reason="bad extraction", graph_name=GRAPH,
        )
        self.assertIn("error", result)

    async def test_dry_run_makes_no_changes(self):
        memory_id, episode_name = self._promote()
        driver = FakeDriver(episode={"uuid": "ep-1"})
        result = await delete_memory(
            self.prom, self.rev, memory_id, FakeGraphiti(driver),
            reviewer=DEFAULT_REVIEWER, reason="bad extraction", graph_name=GRAPH, dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertTrue(self.prom.is_promoted(memory_id, GRAPH))

    async def test_apply_removes_from_graph_and_clears_ledger(self):
        memory_id, episode_name = self._promote()
        driver = FakeDriver(episode={"uuid": "ep-1"})
        graphiti = FakeGraphiti(driver)
        result = await delete_memory(
            self.prom, self.rev, memory_id, graphiti,
            reviewer=DEFAULT_REVIEWER, reason="promoted in error", graph_name=GRAPH, dry_run=False,
        )
        self.assertFalse(result["dry_run"])
        self.assertEqual(graphiti.removed, ["ep-1"])
        self.assertFalse(self.prom.is_promoted(memory_id, GRAPH))
        self.assertIsNone(self.prom.get(memory_id, GRAPH))

        audit = self.rev.audit_for(memory_id)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "delete_memory")

    async def test_apply_clears_ledger_even_if_episode_already_absent(self):
        memory_id, _ = self._promote()
        driver = FakeDriver(episode=None)  # graph rebuilt from a different snapshot
        graphiti = FakeGraphiti(driver)
        result = await delete_memory(
            self.prom, self.rev, memory_id, graphiti,
            reviewer=DEFAULT_REVIEWER, reason="cleanup", graph_name=GRAPH, dry_run=False,
        )
        self.assertFalse(result["dry_run"])
        self.assertEqual(graphiti.removed, [])
        self.assertFalse(self.prom.is_promoted(memory_id, GRAPH))


if __name__ == "__main__":
    unittest.main()
