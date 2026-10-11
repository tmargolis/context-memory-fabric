"""B04: edit_memory target lookup (found 2026-09-21, fixed 2026-10-10).

All 8 journaled failures were dry runs with no change requested. edit_memory
reported only nodes that would change, so a lookup always said "No matching
episodes" even when the query matched. One query was recall_mem fact text,
which only a fact contains. Nodes are seeded straight into the test graph
(tests/conftest.py: cmf_test) with Cypher; no LLM calls.
"""

import unittest
import uuid as uuid_mod
from datetime import datetime, timezone

from server.memory import close_graphiti, edit_memory, get_graphiti
from server.providers.memory_graphiti import format_edit_memory_results_for_mcp


class TestEditMemoryLookup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = get_graphiti().driver
        tag = uuid_mod.uuid4().hex[:10]
        self.tag = tag
        self.ep_direct = f"t-ep-direct-{tag}"
        self.ep_fact = f"t-ep-fact-{tag}"
        self.ent_a, self.ent_b, self.edge = f"t-ent-a-{tag}", f"t-ent-b-{tag}", f"t-edge-{tag}"
        self.fact = f"Widget {tag} ships with the quiet fan profile"
        now = datetime.now(timezone.utc).isoformat()
        q = self.driver.execute_query
        await q("CREATE (e:Episodic {uuid: $u, name: $n, content: $c, valid_at: $now, group_id: 'test', "
                "source: 'text', source_description: 'test'})",
                u=self.ep_direct, n=f"lookup-direct-{tag}", c=f"Kickoff notes for project lookup-{tag}.", now=now)
        await q("CREATE (e:Episodic {uuid: $u, name: $n, content: $c, valid_at: $now, group_id: 'test', "
                "source: 'text', source_description: 'test'})",
                u=self.ep_fact, n=f"lookup-fact-{tag}", c="Unrelated wording the fact was extracted from.", now=now)
        for u in (self.ent_a, self.ent_b):
            await q("CREATE (n:Entity {uuid: $u, name: $u, summary: '', group_id: 'test'})", u=u)
        await q("MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
                "CREATE (a)-[:RELATES_TO {uuid: $r, name: 'HAS', fact: $f, episodes: [$ep], valid_at: $now}]->(b)",
                a=self.ent_a, b=self.ent_b, r=self.edge, f=self.fact, ep=self.ep_fact, now=now)

    async def asyncTearDown(self):
        await self.driver.execute_query(
            "MATCH (n) WHERE n.uuid IN $u DETACH DELETE n",
            u=[self.ep_direct, self.ep_fact, self.ent_a, self.ent_b],
        )
        await close_graphiti()

    async def _episode(self, uuid):
        rows, _, _ = await self.driver.execute_query(
            "MATCH (e:Episodic {uuid: $u}) RETURN e.name AS name, e.content AS content", u=uuid)
        return rows[0]

    async def test_lookup_without_changes_lists_direct_matches(self):
        res = await edit_memory(target_query=f"lookup-direct-{self.tag}", format_for_mcp=False)
        self.assertEqual(res["mode"], "preview")
        self.assertEqual([e["uuid"] for e in res["direct_episodes"]], [self.ep_direct])
        md = await edit_memory(target_query=f"lookup-direct-{self.tag}")
        self.assertIn("Memory Match Preview", md)
        self.assertIn(self.ep_direct, md)
        self.assertNotIn("No matching", md)

    async def test_lookup_ignores_dry_run_false_and_writes_nothing(self):
        before = await self._episode(self.ep_direct)
        res = await edit_memory(target_query=f"lookup-direct-{self.tag}", dry_run=False, format_for_mcp=False)
        self.assertEqual(res["mode"], "preview")
        self.assertEqual(await self._episode(self.ep_direct), before)

    async def test_blank_change_fields_still_mean_lookup(self):
        before = await self._episode(self.ep_direct)
        res = await edit_memory(target_query=f"lookup-direct-{self.tag}", new_name="", new_content="  ",
                                dry_run=False, format_for_mcp=False)
        self.assertEqual(res["mode"], "preview")
        self.assertEqual(await self._episode(self.ep_direct), before)

    async def test_fact_text_finds_its_episode_as_a_candidate(self):
        res = await edit_memory(target_query=self.fact, format_for_mcp=False)
        self.assertEqual(res["direct_episodes"], [])
        self.assertEqual([(c["uuid"], c["facts"]) for c in res["fact_candidates"]], [(self.ep_fact, [self.fact])])

    async def test_fact_candidates_are_never_written(self):
        res = await edit_memory(target_query=self.fact, new_name="renamed", dry_run=False, format_for_mcp=False)
        self.assertEqual(res["matched_episodes"], [])
        self.assertEqual([c["uuid"] for c in res["fact_candidates"]], [self.ep_fact])
        self.assertEqual((await self._episode(self.ep_fact))["name"], f"lookup-fact-{self.tag}")
        md = await edit_memory(target_query=self.fact, new_name="renamed", dry_run=True)
        self.assertIn("nothing was changed", md)
        self.assertIn(self.ep_fact, md)

    async def test_retargeting_the_candidate_by_uuid_edits_it(self):
        res = await edit_memory(target_query=self.ep_fact, new_name=f"lookup-fact-{self.tag}-fixed",
                                dry_run=False, format_for_mcp=False)
        self.assertEqual([e["uuid"] for e in res["matched_episodes"]], [self.ep_fact])
        self.assertEqual((await self._episode(self.ep_fact))["name"], f"lookup-fact-{self.tag}-fixed")

    async def test_nothing_matches(self):
        md = await edit_memory(target_query=f"no-such-thing-{self.tag}")
        self.assertIn("Nothing matches", md)


class TestEditReportMessages(unittest.TestCase):
    def test_direct_match_with_no_effective_change_is_not_called_no_match(self):
        md = format_edit_memory_results_for_mcp("x", True, [], [], [], [], None,
                                                direct_match_counts={"episodes": 1, "entities": 2})
        self.assertIn("matched 1 episode(s) and 2 entity(ies) directly", md)
        self.assertNotIn("No matching", md)

    def test_plain_no_match_message_is_unchanged(self):
        md = format_edit_memory_results_for_mcp("x", True, [], [], [], [])
        self.assertIn("No matching episodes, entities, or facts found", md)


if __name__ == "__main__":
    unittest.main()
