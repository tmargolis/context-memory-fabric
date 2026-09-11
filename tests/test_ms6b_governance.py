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
from server.consolidation.store import ConsolidationStore
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
        self.cs = ConsolidationStore(db_path=self.db)

    def tearDown(self):
        self.prom.close()
        self.rev.close()
        self.cs.close()
        self._tmp.cleanup()

    def _promote(self, memory_id="r:0::reasoning-episode@0.2", episode_name="claude-cmf-001", seed_journal=False):
        self.prom.record_success(memory_id, episode_name, GRAPH)
        if seed_journal:
            self._seed_journal_row(memory_id)
        return memory_id, episode_name

    def _seed_journal_row(self, memory_id, statement="old statement"):
        """Minimal `derived_memories` row — what `promote_reviewed` would have
        found already in place; `correct_memory` needs one to write a
        superseding row against."""
        self.cs._conn.execute(
            "INSERT INTO derived_memories (memory_id, source_event_id, policy_name, policy_version, "
            "category, statement, reason, confidence, event_date, date_precision, reasoning_kind, "
            "evidence_event_ids_json, approval_state, thread_key, project, created_at) VALUES "
            "(?, 'ev-1', 'reasoning-episode', '0.2', 'episodic', ?, 'why: test fixture', 0.9, "
            "'2026-05-01T00:00:00+00:00', 'day', 'decision', '[\"ev-1\"]', 'queued_for_review', "
            "'test-thread', 'test-project', '2026-05-01T00:00:00+00:00')",
            (memory_id, statement),
        )
        self.cs._conn.commit()


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
            self.prom, self.rev, self.cs, "unpromoted", "new text", FakeGraphiti(FakeDriver()),
            reviewer=DEFAULT_REVIEWER, reason="fix", graph_name=GRAPH,
        )
        self.assertIn("error", result)

    async def test_error_when_no_journal_row(self):
        # Promoted in the ledger but never seeded into derived_memories —
        # correct_memory has nothing to write a superseding row against.
        memory_id, _ = self._promote(seed_journal=False)
        driver = FakeDriver(episode={"uuid": "ep-1", "valid_at": "2026-05-01T00:00:00+00:00",
                                      "content": "old statement", "source_description": "x"})
        result = await correct_memory(
            self.prom, self.rev, self.cs, memory_id, "corrected statement", FakeGraphiti(driver),
            reviewer=DEFAULT_REVIEWER, reason="fix", graph_name=GRAPH,
        )
        self.assertIn("error", result)

    async def test_dry_run_makes_no_graph_or_ledger_changes(self):
        memory_id, episode_name = self._promote(seed_journal=True)
        driver = FakeDriver(episode={"uuid": "ep-1", "valid_at": "2026-05-01T00:00:00+00:00",
                                      "content": "old statement", "source_description": "x"})
        graphiti = FakeGraphiti(driver)
        result = await correct_memory(
            self.prom, self.rev, self.cs, memory_id, "corrected statement", graphiti,
            reviewer=DEFAULT_REVIEWER, reason="fix", graph_name=GRAPH, dry_run=True,
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(graphiti.removed, [])
        self.assertEqual(graphiti.added, [])
        self.assertEqual(self.prom.get(memory_id, GRAPH)["episode_name"], episode_name)
        self.assertEqual(self.cs.get_derived_memory(memory_id)["approval_state"], "queued_for_review")

    async def test_apply_removes_readds_updates_ledger_and_audits(self):
        memory_id, old_name = self._promote(seed_journal=True)
        driver = FakeDriver(episode={"uuid": "ep-1", "valid_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                                      "content": "old statement", "source_description": "reasoning_kind=decision"})
        graphiti = FakeGraphiti(driver)
        result = await correct_memory(
            self.prom, self.rev, self.cs, memory_id, "corrected statement", graphiti,
            reviewer=DEFAULT_REVIEWER, reason="reviewer caught a factual error", graph_name=GRAPH, dry_run=False,
        )
        self.assertFalse(result["dry_run"])
        new_memory_id = result["new_memory_id"]
        self.assertTrue(new_memory_id.startswith(f"{memory_id}::corrected-"))
        self.assertEqual(graphiti.removed, ["ep-1"])
        self.assertEqual(len(graphiti.added), 1)
        added = graphiti.added[0]
        self.assertEqual(added["episode_body"], "corrected statement")
        self.assertEqual(added["reference_time"], datetime(2026, 5, 1, tzinfo=timezone.utc))

        # Graph identity moved to the new memory_id — old one no longer promoted.
        self.assertFalse(self.prom.is_promoted(memory_id, GRAPH))
        new_row = self.prom.get(new_memory_id, GRAPH)
        self.assertEqual(new_row["episode_name"], result["new_episode_name"])
        self.assertNotEqual(new_row["episode_name"], old_name)
        self.assertTrue(self.prom.is_promoted(new_memory_id, GRAPH))

        # Journal: a new derivation superseding the old one, not an overwrite.
        old_journal = self.cs.get_derived_memory(memory_id)
        self.assertEqual(old_journal["approval_state"], "superseded_by_correction")
        self.assertEqual(old_journal["superseded_by"], new_memory_id)
        self.assertEqual(old_journal["statement"], "old statement")  # untouched
        new_journal = self.cs.get_derived_memory(new_memory_id)
        self.assertEqual(new_journal["statement"], "corrected statement")
        self.assertEqual(new_journal["supersedes"], memory_id)
        self.assertEqual(new_journal["approval_state"], "queued_for_review")
        self.assertEqual(new_journal["reasoning_kind"], "decision")  # carried over

        # The new memory_id gets its own review verdict, carried forward.
        new_review = self.rev.get(new_memory_id)
        self.assertEqual(new_review["review_state"], "approved")
        self.assertEqual(new_review["reviewer"], DEFAULT_REVIEWER)

        # Old memory_id: graph-mutation audit only, no review verdict written for it.
        audit = self.rev.audit_for(memory_id)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "correct_memory")
        self.assertIsNone(self.rev.get(memory_id))  # .note() never writes `reviews`
        # New memory_id: the review-verdict audit row from .record().
        new_audit = self.rev.audit_for(new_memory_id)
        self.assertEqual(len(new_audit), 1)
        self.assertEqual(new_audit[0]["action"], "correct_memory")

    async def test_noop_when_content_unchanged(self):
        memory_id, _ = self._promote(seed_journal=True)
        driver = FakeDriver(episode={"uuid": "ep-1", "valid_at": "2026-05-01T00:00:00+00:00",
                                      "content": "same statement", "source_description": "x"})
        graphiti = FakeGraphiti(driver)
        result = await correct_memory(
            self.prom, self.rev, self.cs, memory_id, "same statement", graphiti,
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
