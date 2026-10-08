"""edit_memory's new_content path re-extracts the episode's facts (MS9 task 7).

Before this, new_content only overwrote Episodic.content, so the facts
Graphiti extracted from the old text stayed behind (backlog: "edit_memory
doesn't re-run fact extraction"). These tests seed nodes directly with Cypher
in the cmf_test graph and fake the extraction step, so no LLM is called.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid as uuid_mod

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.adapters.codex.transcript_reader import WorkerLock
from server.memory import close_graphiti, edit_memory, get_graphiti
import server.providers.memory_graphiti as mg
from server.providers.episode_retract import retract_episode

NOW = "2026-09-16T21:19:59+00:00"


def _rows(result):
    return result[0] if result and isinstance(result[0], list) else (result or [])


class _FakeGraphiti:
    """Stands in for Graphiti in _reextract_episode: add_episode writes a new
    Episodic node plus one fact for it, or raises when `fail` is set."""

    def __init__(self, driver, fail=False):
        self.driver = driver
        self.fail = fail
        self.calls = []

    async def add_episode(self, *, name, episode_body, source_description, reference_time, **_):
        self.calls.append(name)
        new_uuid = f"t-new-{uuid_mod.uuid4().hex}"
        await self.driver.execute_query(
            "CREATE (e:Episodic {uuid: $u, name: $n, content: $c, valid_at: $v, group_id: 'test', "
            "source: 'text', source_description: $sd})",
            u=new_uuid, n=name, c=episode_body, v=reference_time.isoformat(), sd=source_description,
        )
        if self.fail:
            raise RuntimeError("extraction failed")
        ent = f"t-ent-{uuid_mod.uuid4().hex}"
        await self.driver.execute_query(
            "MATCH (e:Episodic {uuid: $eu}) CREATE (n:Entity {uuid: $nu, name: 'BRAVO', summary: '', group_id: 'test'}) "
            "CREATE (e)-[:MENTIONS]->(n) CREATE (n)-[:RELATES_TO {uuid: $ru, fact: 'test value is BRAVO', episodes: [$eu]}]->(n)",
            eu=new_uuid, nu=ent, ru=f"t-fact-{uuid_mod.uuid4().hex}",
        )
        return SimpleNamespace(episode=SimpleNamespace(uuid=new_uuid))


class TestEditMemoryReextract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = get_graphiti().driver
        self.tag = uuid_mod.uuid4().hex[:8]
        self.tmp = Path(tempfile.mkdtemp(prefix="cmf-edit-"))
        self.ledger = self.tmp / "edit_ledger.jsonl"
        self.journal_db = self.tmp / "journal.db"
        os.environ["CMF_SPARK_LOCK_IGNORE_EXTERNAL"] = "1"
        # Episode A (the edit target) and B (a bystander). Fact f_only is
        # supported by A alone; f_shared by A and B. Entity X is mentioned
        # only by A; Y by both.
        self.a, self.b = f"t-epA-{self.tag}", f"t-epB-{self.tag}"
        self.x, self.y = f"t-entX-{self.tag}", f"t-entY-{self.tag}"
        self.name_a = f"t_edit_target_{self.tag}"
        q = self.driver.execute_query
        await q("CREATE (:Episodic {uuid: $u, name: $n, content: 'test value is ALPHA', valid_at: $v, "
                "group_id: 'test', source: 'text', source_description: 'test'})", u=self.a, n=self.name_a, v=NOW)
        await q("CREATE (:Episodic {uuid: $u, name: $n, content: 'unrelated', valid_at: $v, "
                "group_id: 'test', source: 'text', source_description: 'test'})", u=self.b, n=f"t_bystander_{self.tag}", v=NOW)
        await q("CREATE (:Entity {uuid: $x, name: 'ALPHA', summary: '', group_id: 'test'}), "
                "(:Entity {uuid: $y, name: 'TestThing', summary: '', group_id: 'test'})", x=self.x, y=self.y)
        await q("MATCH (a:Episodic {uuid: $a}), (b:Episodic {uuid: $b}), (x:Entity {uuid: $x}), (y:Entity {uuid: $y}) "
                "CREATE (a)-[:MENTIONS]->(x), (a)-[:MENTIONS]->(y), (b)-[:MENTIONS]->(y), "
                "(y)-[:RELATES_TO {uuid: $f1, fact: 'test value is ALPHA', episodes: [$a]}]->(x), "
                "(y)-[:RELATES_TO {uuid: $f2, fact: 'TestThing exists', episodes: [$a, $b]}]->(y)",
                a=self.a, b=self.b, x=self.x, y=self.y, f1=f"t-f1-{self.tag}", f2=f"t-f2-{self.tag}")

    async def asyncTearDown(self):
        await self.driver.execute_query(
            "MATCH (n) WHERE n.uuid STARTS WITH 't-' AND (n.uuid ENDS WITH $t OR n.name STARTS WITH 't_' "
            "OR n.name = 'BRAVO') DETACH DELETE n", t=self.tag)
        await self.driver.execute_query("MATCH (n:Entity {name: 'BRAVO'}) DETACH DELETE n")
        os.environ.pop("CMF_SPARK_LOCK_IGNORE_EXTERNAL", None)
        await close_graphiti()

    async def _facts(self):
        return {r["fact"]: list(r["episodes"]) for r in _rows(await self.driver.execute_query(
            "MATCH ()-[r:RELATES_TO]->() WHERE r.uuid STARTS WITH 't-' RETURN r.fact AS fact, r.episodes AS episodes"))}

    async def _episodes(self, name):
        return _rows(await self.driver.execute_query(
            "MATCH (e:Episodic {name: $n}) RETURN e.uuid AS uuid, e.content AS content", n=name))

    async def _edit(self, fake, **kw):
        with mock.patch.object(mg, "get_graphiti_for_operation", return_value=(fake, "test")), \
             mock.patch.object(mg, "postprocess_episode", mock.AsyncMock(return_value={})), \
             mock.patch("server.consolidation.graph_tagging.tag_promoted_episode", mock.AsyncMock()):
            return await edit_memory(
                target_query=kw.pop("target", self.a), format_for_mcp=False,
                ledger_path=self.ledger, journal_db=self.journal_db, **kw)

    def _ledger_events(self):
        return [json.loads(line)["event"] for line in self.ledger.read_text().splitlines()]

    async def test_retract_deletes_exclusive_and_detaches_shared(self):
        rec = await retract_episode(self.driver, self.a)
        self.assertEqual([f["fact"] for f in rec["facts_deleted"]], ["test value is ALPHA"])
        self.assertEqual([f["fact"] for f in rec["facts_detached"]], ["TestThing exists"])
        self.assertEqual(rec["orphaned_entities"], ["ALPHA"])
        self.assertEqual(await self._facts(), {"TestThing exists": [self.b]})
        self.assertEqual(await self._episodes(self.name_a), [])
        ents = _rows(await self.driver.execute_query(
            "MATCH (n:Entity) WHERE n.uuid IN [$x, $y] RETURN n.name AS name", x=self.x, y=self.y))
        self.assertEqual([e["name"] for e in ents], ["TestThing"])

    async def test_retract_preview_writes_nothing(self):
        before = await self._facts()
        rec = await retract_episode(self.driver, self.a, apply=False)
        self.assertEqual(len(rec["facts_deleted"]), 1)
        self.assertEqual(await self._facts(), before)
        self.assertEqual(len(await self._episodes(self.name_a)), 1)

    async def test_new_content_replaces_facts_and_keeps_name(self):
        fake = _FakeGraphiti(self.driver)
        res = await self._edit(fake, new_content="test value is BRAVO")
        self.assertEqual(res["reextraction"]["status"], "done")
        eps = await self._episodes(self.name_a)
        self.assertEqual(len(eps), 1)  # renamed back, no duplicate name
        self.assertNotEqual(eps[0]["uuid"], self.a)
        self.assertEqual(eps[0]["content"], "test value is BRAVO")
        facts = await self._facts()
        self.assertNotIn("test value is ALPHA", facts)
        self.assertEqual(facts["TestThing exists"], [self.b])
        self.assertEqual(facts["test value is BRAVO"], [eps[0]["uuid"]])
        self.assertTrue(fake.calls[0].startswith(f"{self.name_a}__edit-pending-"))
        self.assertEqual(self._ledger_events(), ["started", "done"])

    async def test_failed_extraction_keeps_old_episode(self):
        before = await self._facts()
        with self.assertRaises(RuntimeError):
            await self._edit(_FakeGraphiti(self.driver, fail=True), new_content="test value is BRAVO")
        eps = await self._episodes(self.name_a)
        self.assertEqual([(e["uuid"], e["content"]) for e in eps], [(self.a, "test value is ALPHA")])
        pending = _rows(await self.driver.execute_query(
            "MATCH (e:Episodic) WHERE e.name STARTS WITH $p RETURN e.uuid", p=f"{self.name_a}__edit-pending"))
        self.assertEqual(pending, [])
        self.assertEqual(await self._facts(), before)
        self.assertEqual(self._ledger_events(), ["started", "failed"])

    async def test_refuses_more_than_one_episode(self):
        with self.assertRaises(ValueError):
            await self._edit(_FakeGraphiti(self.driver), target=f"t_", new_content="x")
        self.assertEqual(len(await self._episodes(self.name_a)), 1)
        self.assertFalse(self.ledger.exists())

    async def test_refuses_when_spark_busy(self):
        lock = WorkerLock(self.tmp / "spark_job.lock")
        self.assertTrue(lock.acquire())
        try:
            with self.assertRaises(RuntimeError):
                await self._edit(_FakeGraphiti(self.driver), new_content="test value is BRAVO", new_name="renamed")
        finally:
            lock.release()
        eps = await self._episodes(self.name_a)
        self.assertEqual(eps[0]["content"], "test value is ALPHA")
        self.assertFalse(self.ledger.exists())

    async def test_dry_run_previews_without_writing(self):
        before = await self._facts()
        res = await self._edit(_FakeGraphiti(self.driver), new_content="test value is BRAVO", dry_run=True)
        self.assertEqual(res["reextraction"]["status"], "dry_run")
        self.assertEqual([f["fact"] for f in res["reextraction"]["retract"]["facts_deleted"]], ["test value is ALPHA"])
        self.assertEqual(await self._facts(), before)
        self.assertEqual((await self._episodes(self.name_a))[0]["uuid"], self.a)

    async def test_background_returns_queued_then_completes(self):
        fake = _FakeGraphiti(self.driver)
        with mock.patch.object(mg, "get_graphiti_for_operation", return_value=(fake, "test")), \
             mock.patch.object(mg, "postprocess_episode", mock.AsyncMock(return_value={})), \
             mock.patch("server.consolidation.graph_tagging.tag_promoted_episode", mock.AsyncMock()):
            res = await edit_memory(target_query=self.a, new_content="test value is BRAVO", format_for_mcp=False,
                                    background=True, ledger_path=self.ledger, journal_db=self.journal_db)
            self.assertEqual(res["reextraction"]["status"], "queued")
            for task in list(mg._BACKGROUND_EDIT_TASKS):
                await task
        self.assertEqual((await self._episodes(self.name_a))[0]["content"], "test value is BRAVO")
        self.assertEqual(self._ledger_events(), ["started", "done"])
        # The background task released the Spark slot.
        lock = WorkerLock(self.tmp / "spark_job.lock")
        self.assertTrue(lock.acquire())
        lock.release()

    async def test_registry_sync_reaches_the_configured_registry(self):
        """The default registry path used to resolve under server/ and never
        exist, so sync was skipped. Tests get a temp path via conftest."""
        reg = mg.default_import_registry_path()
        self.assertNotEqual(reg.resolve(), (Path(mg._REPO_ROOT) / "imports/state/import_registry.json").resolve())
        reg.write_text(json.dumps({"records": {"fp1": {
            "episode_name": self.name_a, "text": "test value is ALPHA", "reference_time": NOW}}}))
        await self._edit(_FakeGraphiti(self.driver), new_reference_time="2026-09-17")
        rec = json.loads(reg.read_text())["records"]["fp1"]
        self.assertTrue(rec["reference_time"].startswith("2026-09-17"))

    async def test_date_only_edit_keeps_no_llm_path(self):
        fake = _FakeGraphiti(self.driver)
        res = await self._edit(fake, new_reference_time="2026-09-17")
        self.assertIsNone(res["reextraction"])
        self.assertEqual(fake.calls, [])
        self.assertEqual((await self._episodes(self.name_a))[0]["uuid"], self.a)


if __name__ == "__main__":
    unittest.main()
