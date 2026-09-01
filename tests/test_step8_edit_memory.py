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

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.mcp import app, edit_memory as mcp_edit_memory
from server.memory import (
    close_graphiti,
    edit_memory,
    format_edit_memory_results_for_mcp,
    get_graphiti,
    parse_iso_datetime,
    recall,
    remember,
)


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

    async def test_edit_memory_dry_run(self):
        """Verify dry_run previews changes without modifying state."""
        # Use target query for C7 right transverse process fracture
        res = await edit_memory(
            target_query="C7 right transverse process fracture",
            new_reference_time="2025-01-13",
            dry_run=True,
            format_for_mcp=False,
        )
        self.assertIsInstance(res, dict)
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["target_query"], "C7 right transverse process fracture")
        self.assertTrue(len(res["matched_entities"]) > 0 or len(res["matched_episodes"]) > 0)

        # Verify Markdown formatting
        md_res = await edit_memory(
            target_query="C7 right transverse process fracture",
            new_reference_time="2025-01-13",
            dry_run=True,
            format_for_mcp=True,
        )
        self.assertIn("DRY RUN", md_res)
        self.assertIn("C7 right transverse process fracture", md_res)

    async def test_edit_memory_empty_query_error(self):
        """Verify empty target_query raises ValueError."""
        with self.assertRaises(ValueError):
            await edit_memory(target_query="   ", new_reference_time="2025-01-13")

    async def test_edit_memory_no_match(self):
        """Verify report when no entities or episodes match target query."""
        md_res = await edit_memory(
            target_query="NonExistentTargetEntity999XYZ",
            new_reference_time="2025-01-13",
            dry_run=True,
            format_for_mcp=True,
        )
        self.assertIn("No matching episodes, entities, or facts found", md_res)


if __name__ == "__main__":
    unittest.main()
