"""Comprehensive test suite for edit_memory MCP tool and memory correction engine.

Validates:
- Tool registration, titles, annotations, and parameters in server.mcp
- parse_iso_datetime date parsing across formats
- format_edit_memory_results_for_mcp formatting
- dry_run mode previewing changes without mutation
- edit_memory modifying episode nodes, entity nodes, and graph edges
- registry synchronization with imports/state/import_registry.json
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import pytest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.mcp import app, edit_memory as mcp_edit_memory
from server.memory import (
    close_graphiti,
    edit_memory,
    format_edit_memory_results_for_mcp,
    get_graphiti,
    parse_iso_datetime,
    reconcile_memories as reconcile_episodic_memories,
    remember,
)
from tests.fixtures import local as local_fixtures


class TestStep8EditMemory(unittest.IsolatedAsyncioTestCase):
    """Test suite for edit_memory tool."""

    async def asyncTearDown(self):
        await close_graphiti()

    def test_parse_iso_datetime(self):
        """Test parsing of various date formats into UTC datetime."""
        # YYYY-MM-DD
        dt1 = parse_iso_datetime("2025-01-13")
        self.assertEqual(dt1, datetime(2025, 1, 13, 0, 0, 0, tzinfo=timezone.utc))

        # ISO 8601 with Z
        dt2 = parse_iso_datetime("2025-01-13T15:30:00Z")
        self.assertEqual(dt2, datetime(2025, 1, 13, 15, 30, 0, tzinfo=timezone.utc))

        # ISO 8601 with offset
        dt3 = parse_iso_datetime("2025-01-13T10:00:00-05:00")
        self.assertEqual(dt3, datetime(2025, 1, 13, 15, 0, 0, tzinfo=timezone.utc))

        # Already datetime
        dt4 = parse_iso_datetime(datetime(2025, 1, 13, 0, 0, 0))
        self.assertEqual(dt4.tzinfo, timezone.utc)

        # Invalid
        with self.assertRaises(ValueError):
            parse_iso_datetime("invalid-date-string")

    async def test_mcp_tool_registration(self):
        """Verify edit_memory is registered in server.mcp with appropriate annotations."""
        tools = await app.list_tools()
        tool_dict = {t.name: t for t in tools}
        self.assertIn("edit_memory", tool_dict)

        tool = tool_dict["edit_memory"]
        self.assertEqual(tool.title, "Edit or Correct Episodic Memory")
        self.assertFalse(tool.annotations.read_only_hint)
        self.assertTrue(tool.annotations.idempotent_hint)
        self.assertFalse(tool.annotations.destructive_hint)

    @pytest.mark.live
    async def test_edit_memory_dry_run(self):
        """Verify dry_run previews changes without modifying state."""
        # Seed a synthetic fixture episode so this test is self-contained and
        # does not depend on incidental content already present in whichever
        # graph the suite is pointed at (see tests/conftest.py).
        await remember(
            content=(
                "On 2025-01-14, a synthetic test appliance was configured with "
                "firmware build 7-Q4-92 (fixture data, not a real record)."
            ),
            name="test_fixture_edit_memory_firmware_build",
            source_description="test_step8_edit_memory synthetic fixture",
        )

        res = await edit_memory(
            target_query="firmware build 7-Q4-92",
            new_reference_time="2025-01-13",
            dry_run=True,
            format_for_mcp=False,
        )
        self.assertIsInstance(res, dict)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["target_query"], "firmware build 7-Q4-92")
        self.assertTrue(len(res["matched_entities"]) > 0 or len(res["matched_episodes"]) > 0)

        # Verify Markdown formatting
        md_res = await edit_memory(
            target_query="firmware build 7-Q4-92",
            new_reference_time="2025-01-13",
            dry_run=True,
            format_for_mcp=True,
        )
        self.assertIn("DRY RUN", md_res)
        self.assertIn("firmware build 7-Q4-92", md_res)

    @pytest.mark.live
    async def test_edit_memory_dry_run__real_example(self):
        """Same behavior as above, against the original fixture content this
        test used to hardcode directly (medical-sounding, even though marked
        synthetic — no reason for that to sit in a public repo regardless).
        Gitignored (tests/fixtures/local/); skips itself when the fixture
        file isn't present, e.g. in CI or on another machine.
        """
        data = local_fixtures.load("edit_memory")
        if data is None:
            self.skipTest("tests/fixtures/local/edit_memory.local.json not present")
        case = data["dry_run_example"]

        await remember(
            content=case["content"],
            name=case["episode_name"],
            source_description="test_step8_edit_memory local fixture",
        )

        res = await edit_memory(
            target_query=case["target_query"],
            new_reference_time=case["new_reference_time"],
            dry_run=True,
            format_for_mcp=False,
        )
        self.assertIsInstance(res, dict)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["target_query"], case["target_query"])
        self.assertTrue(len(res["matched_entities"]) > 0 or len(res["matched_episodes"]) > 0)

    async def test_edit_memory_scopes_writes_to_directly_matched_nodes(self):
        """MS6b regression: a `new_summary` targeted at one entity by exact
        uuid must not overwrite an unrelated entity's summary just because
        they co-occur in the same episode. No LLM call needed — nodes are
        seeded directly via Cypher, not through remember()/extraction."""
        graphiti = get_graphiti()
        driver = graphiti.driver
        import uuid as uuid_mod
        from datetime import datetime, timezone

        target_uuid = f"test-target-{uuid_mod.uuid4().hex}"
        bystander_uuid = f"test-bystander-{uuid_mod.uuid4().hex}"
        episode_uuid = f"test-episode-{uuid_mod.uuid4().hex}"
        now = datetime.now(timezone.utc).isoformat()

        try:
            await driver.execute_query(
                "CREATE (n:Entity {uuid: $u, name: $name, summary: $s, group_id: 'test'})",
                u=target_uuid, name=f"TargetEntity-{target_uuid[:8]}", s="original target summary",
            )
            await driver.execute_query(
                "CREATE (n:Entity {uuid: $u, name: $name, summary: $s, group_id: 'test'})",
                u=bystander_uuid, name=f"BystanderEntity-{bystander_uuid[:8]}", s="original bystander summary",
            )
            await driver.execute_query(
                "CREATE (e:Episodic {uuid: $u, name: $name, content: 'test episode', "
                "valid_at: $now, group_id: 'test', source: 'text', source_description: 'test'})",
                u=episode_uuid, name=f"test-episode-{episode_uuid[:8]}", now=now,
            )
            # Both entities mentioned by the same episode — the shape that
            # previously caused the bystander to be swept into the write.
            await driver.execute_query(
                "MATCH (e:Episodic {uuid: $eu}), (n:Entity {uuid: $nu}) CREATE (e)-[:MENTIONS]->(n)",
                eu=episode_uuid, nu=target_uuid,
            )
            await driver.execute_query(
                "MATCH (e:Episodic {uuid: $eu}), (n:Entity {uuid: $nu}) CREATE (e)-[:MENTIONS]->(n)",
                eu=episode_uuid, nu=bystander_uuid,
            )

            result = await edit_memory(
                target_query=target_uuid,
                new_summary="corrected target summary",
                dry_run=False,
                format_for_mcp=False,
            )
            modified_uuids = {e["uuid"] for e in result["matched_entities"]}
            self.assertIn(target_uuid, modified_uuids)
            self.assertNotIn(bystander_uuid, modified_uuids)

            rows, _, _ = await driver.execute_query(
                "MATCH (n:Entity) WHERE n.uuid IN [$t, $b] RETURN n.uuid AS uuid, n.summary AS summary",
                t=target_uuid, b=bystander_uuid,
            )
            summaries = {r["uuid"]: r["summary"] for r in rows}
            self.assertEqual(summaries[target_uuid], "corrected target summary")
            self.assertEqual(summaries[bystander_uuid], "original bystander summary")
        finally:
            await driver.execute_query(
                "MATCH (n) WHERE n.uuid IN [$t, $b, $e] DETACH DELETE n",
                t=target_uuid, b=bystander_uuid, e=episode_uuid,
            )

    async def test_edit_memory_empty_query_error(self):
        """Verify empty target_query raises ValueError."""
        with self.assertRaises(ValueError):
            await edit_memory(target_query="   ", new_reference_time="2025-01-13")

    @pytest.mark.live
    async def test_reconcile_memories_registration_and_dry_run(self):
        """Verify reconcile_memories is registered in server.mcp and works with dry_run."""
        tools = await app.list_tools()
        tool_dict = {t.name: t for t in tools}
        self.assertIn("reconcile_memories", tool_dict)

        rec = {
            "candidate_ids": ["cand_test_1"],
            "action": "upsert_episode",
            "name": "Test Reconciled Memory",
            "content": "Test reconciled content regarding project milestones on 2026-05-15.",
            "event_date": "2026-05-15",
            "event_date_precision": "day",
            "observed_at": "2026-05-20",
        }

        # Dry run formatted
        md_res = await reconcile_episodic_memories(
            records=[rec],
            dry_run=True,
            format_for_mcp=True,
        )
        self.assertIn("DRY RUN", md_res)
        self.assertIn("Test Reconciled Memory", md_res)

        # Reject action dry run
        reject_rec = {
            "candidate_ids": ["cand_test_reject"],
            "action": "discard_candidate",
            "reason": "Test rejection reason.",
        }
        md_reject = await reconcile_episodic_memories(
            records=[reject_rec],
            dry_run=True,
            format_for_mcp=True,
        )
        self.assertIn("- **Candidates Discarded / Rejected:** 1", md_reject)
        self.assertIn("Test rejection reason.", md_reject)

    @pytest.mark.live
    async def test_mcp_boundary_protocol_calls(self):
        """Verify calling reconcile_memories, import_memories, and edit_memory over the MCP protocol boundary."""
        # 1. Reconcile memories tool call
        reconcile_args = {
            "records": [
                {
                    "candidate_ids": ["cand_mcp_1"],
                    "origin_ids": ["src_mcp_1"],
                    "action": "upsert_episode",
                    "name": "Protocol Test Episode",
                    "content": "Protocol test memory content on 2026-08-01.",
                    "event_date": "2026-08-01",
                    "event_date_precision": "day",
                    "observed_at": None,
                },
                {
                    "candidate_ids": ["cand_mcp_reject"],
                    "action": "discard_candidate",
                    "reason": "Conflated test memory.",
                }
            ],
            "dry_run": True,
        }
        rec_res = await app.call_tool("reconcile_memories", arguments=reconcile_args)
        self.assertFalse(rec_res.is_error)
        self.assertGreater(len(rec_res.content), 0)
        rec_text = rec_res.content[0].text
        self.assertIn("Historical Memory Reconciliation Report", rec_text)
        self.assertIn("Protocol Test Episode", rec_text)
        self.assertIn("Conflated test memory.", rec_text)

        # 2. Import memories tool call
        import_args = {
            "content": "- On 2026-08-15, configured FalkorDB memory limits.",
            "source": "chatgpt",
            "source_description": "MCP boundary test",
            "dry_run": True,
        }
        imp_res = await app.call_tool("import_memories", arguments=import_args)
        self.assertFalse(imp_res.is_error)
        self.assertGreater(len(imp_res.content), 0)
        imp_text = imp_res.content[0].text
        self.assertIn("Historical Memory Import Report", imp_text)
        self.assertIn("DRY RUN", imp_text)

        # 3. Edit memory tool call
        edit_args = {
            "target_query": "Protocol Test Episode",
            "new_reference_time": "2026-08-02",
            "dry_run": True,
        }
        edit_res = await app.call_tool("edit_memory", arguments=edit_args)
        self.assertFalse(edit_res.is_error)
        self.assertGreater(len(edit_res.content), 0)
        edit_text = edit_res.content[0].text
        self.assertIn("Memory Edit Report", edit_text)
        self.assertIn("DRY RUN", edit_text)


if __name__ == "__main__":
    unittest.main()


