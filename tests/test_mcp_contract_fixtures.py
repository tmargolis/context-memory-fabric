"""MCP tool-contract drift detection.

Snapshots of all registered MCP tool schemas were recorded in
tests/fixtures/mcp_contracts/tool_schemas.json as part of the Milestone 0.5
baseline (see docs/adr/0001-four-layer-model.md and IMPLEMENTATION-PLAN.md).
This is the safety net for the Milestone 1 provider-interface refactor: it
must fail loudly if that refactor changes any tool's observable contract
(name, title, description, input schema, or annotations) without an
intentional, reviewed fixture update.

To intentionally update the fixture after a deliberate contract change,
regenerate tests/fixtures/mcp_contracts/tool_schemas.json and review the diff.
"""

import asyncio
import json
from pathlib import Path
import unittest

from server.mcp import app

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "mcp_contracts" / "tool_schemas.json"


class TestMCPContractFixtures(unittest.TestCase):
    """Verify live MCP tool schemas match the recorded baseline fixture."""

    @classmethod
    def setUpClass(cls):
        cls.expected = json.loads(FIXTURE_PATH.read_text())

        async def _collect():
            tools = await app.list_tools()
            return {
                t.name: {
                    "name": t.name,
                    "title": t.title,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "output_schema": getattr(t, "output_schema", None),
                    "annotations": t.annotations.model_dump() if t.annotations else None,
                }
                for t in tools
            }

        cls.actual = asyncio.run(_collect())

    def test_fixture_exists_and_is_non_empty(self):
        self.assertTrue(FIXTURE_PATH.exists())
        self.assertGreater(len(self.expected), 0)

    def test_no_tools_added_or_removed(self):
        self.assertEqual(
            set(self.expected.keys()),
            set(self.actual.keys()),
            "Registered MCP tool names have changed. If intentional, "
            "regenerate tests/fixtures/mcp_contracts/tool_schemas.json.",
        )

    def test_every_tool_contract_matches_fixture(self):
        for name in self.expected:
            with self.subTest(tool=name):
                self.assertEqual(
                    self.actual[name],
                    self.expected[name],
                    f"Tool '{name}' contract drifted from the recorded fixture. "
                    "If intentional, regenerate the fixture and review the diff.",
                )

    def test_known_tool_count(self):
        # Documents the current tool count so an accidental addition/removal
        # is visible in the diff even if test_no_tools_added_or_removed's
        # message is missed. Update deliberately alongside README.md/CLIENTS.md.
        # 11 as of Milestone 4a (added capture_note, capture_health).
        # 12 as of the auto_accepted promotion tool (promote_auto_accepted_memories).
        self.assertEqual(len(self.expected), 12)


if __name__ == "__main__":
    unittest.main()
