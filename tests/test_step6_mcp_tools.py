"""Comprehensive test suite for Phase 1 Step 6 MCP tools.

Validates:
- All 5 MCP tools registered with titles, rich routing descriptions, and parameter annotations
- MCPServer instructions configured for LLM client routing
- Accurate read/write ToolAnnotations (hints)
- search_wiki() with cache, exclusions (_lint_reports, _profile_reports, templates, memory) and MCP formatting
- remember() storing synthetic memory into Graphiti/FalkorDB
- recall() retrieving memory facts with temporal provenance
- get_context() unifying durable knowledge and episodic memory
"""

import asyncio
import os
from pathlib import Path
import sys
import unittest
import pytest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.corpus import ExtractionStatus, get_corpus_root
from server.context import get_context
from server.mcp import (
    app,
    get_context as mcp_get_context,
    propose_wiki_update as mcp_propose_wiki_update,
    recall_mem as mcp_recall,
    remember as mcp_remember,
    search_wiki as mcp_search_wiki,
)
from server.memory import close_graphiti, remember
from server.wiki import WikiCorpusManager, format_search_results_for_mcp, search_corpus, search_wiki


class TestStep6MCPTools(unittest.IsolatedAsyncioTestCase):
    """Integration test suite for Step 6 MCP tools."""

    async def asyncTearDown(self):
        await close_graphiti()

    async def test_mcp_tool_registration_and_server_instructions(self):
        """Verify all 5 tools are registered, have server instructions, titles, and rich metadata."""
        # 1. Server Instructions
        self.assertIsNotNone(app.instructions)
        self.assertIn("default personal context layer for the user", app.instructions)
        self.assertIn("prefer get_context", app.instructions)
        self.assertIn("propose_wiki_update creates a proposal but does not modify canonical LLM_Wiki", app.instructions)

        # 2. Tool Listing
        tools = await app.list_tools()
        tool_dict = {t.name: t for t in tools}
        expected_tools = {
            "remember", "recall_mem", "search_wiki", "get_context", "propose_wiki_update",
            "import_memories", "import_chatgpt_exports", "edit_memory", "reconcile_memories",
            "capture_note", "capture_health",  # MS4a MCP-boundary capture
            "promote_auto_accepted_memories",
        }
        self.assertEqual(set(tool_dict.keys()), expected_tools)

        # 3. Titles & Annotations
        for name in expected_tools:
            tool = tool_dict[name]
            self.assertIsNotNone(tool.title, f"Tool {name} must have a title")
            self.assertTrue(len(tool.title) > 0)
            self.assertIsNotNone(tool.annotations, f"Tool {name} must have annotations")

        # 4. Check read-only vs write annotations
        self.assertTrue(tool_dict["get_context"].annotations.read_only_hint)
        self.assertTrue(tool_dict["search_wiki"].annotations.read_only_hint)
        self.assertTrue(tool_dict["recall_mem"].annotations.read_only_hint)
        self.assertFalse(tool_dict["remember"].annotations.read_only_hint)
        self.assertFalse(tool_dict["propose_wiki_update"].annotations.read_only_hint)
        self.assertFalse(tool_dict["import_memories"].annotations.read_only_hint)
        self.assertFalse(tool_dict["edit_memory"].annotations.read_only_hint)

        for name, tool in tool_dict.items():
            self.assertFalse(tool.annotations.destructive_hint, f"Tool {name} should not be marked destructive")

        # 5. Routing Keywords & Safety Contracts in Descriptions
        self.assertIn("DEFAULT personal-context retrieval tool", tool_dict["get_context"].description)
        self.assertIn("Where are we with", tool_dict["get_context"].description)

        self.assertIn("Durable/source-material retrieval", tool_dict["search_wiki"].description)
        self.assertIn("Find my notes on", tool_dict["search_wiki"].description)

        self.assertIn("Temporal/episodic retrieval", tool_dict["recall_mem"].description)
        self.assertIn("What did I decide", tool_dict["recall_mem"].description)

        self.assertIn("Direct episodic write", tool_dict["remember"].description)
        self.assertIn("Do NOT call this automatically for every casual chat message", tool_dict["remember"].description)

        self.assertIn("DOES NOT modify LLM_Wiki", tool_dict["propose_wiki_update"].description)
        self.assertIn("wiki-proposals/", tool_dict["propose_wiki_update"].description)

        # 6. Parameter descriptions. capture_health is a legitimate zero-argument
        # status tool (MS4a) — every other tool here takes at least one
        # parameter, but that was never a general requirement, just true of
        # every tool that happened to exist before it.
        zero_arg_tools = {"capture_health"}
        for name, tool in tool_dict.items():
            props = tool.input_schema.get("properties", {})
            if name in zero_arg_tools:
                continue
            self.assertTrue(len(props) > 0, f"Tool {name} should have input properties")
            for prop_name, prop_data in props.items():
                self.assertIn("description", prop_data, f"Param '{prop_name}' on tool '{name}' must have a description")

    async def test_search_wiki_mcp_formatting_and_exclusions(self):
        """Verify search_wiki returns formatted Markdown and respects new exclusions."""
        # 1. Search for a known term
        md_output = await mcp_search_wiki(query="Personal-Context-Service", max_results=3)
        self.assertIsInstance(md_output, str)
        self.assertIn("### Durable Knowledge Search Results", md_output)
        self.assertIn("Personal-Context-Service", md_output)

        # 2. Verify excluded directories are not returned in search results
        raw_results = search_corpus("vault", max_results=50)
        for r in raw_results:
            self.assertNotIn("_lint_reports", r.relative_path)
            self.assertNotIn("_profile_reports", r.relative_path)
            self.assertNotIn("templates", r.relative_path)
            self.assertNotIn("memory/", r.relative_path)

    @pytest.mark.live
    async def test_remember_and_recall_tools(self):
        """Verify remember and recall tools save and retrieve episodic facts."""
        synthetic_note = "Project Atlas selected PostgreSQL 16 on August 31, 2026 for its low latency."
        remember_resp = await mcp_remember(
            content=synthetic_note,
            name="phase1_step6_atlas_test_memory",
            source_description="Step 6 MCP test suite",
        )
        self.assertIn("Memory stored successfully", remember_resp)

        recall_resp = await mcp_recall(query="Project Atlas database", max_results=3)
        self.assertIsInstance(recall_resp, str)
        self.assertIn("### Episodic Memory Search Results", recall_resp)
        self.assertIn("PostgreSQL", recall_resp)

    @pytest.mark.live
    async def test_get_context_unification(self):
        """Verify get_context combines durable knowledge and episodic memory with distinct sections."""
        ctx = await mcp_get_context(topic="Project Atlas", max_wiki_results=3, max_memory_results=3)
        self.assertIsInstance(ctx, str)

        # Verify all major sections exist
        self.assertIn("# Context Fabric: 'Project Atlas'", ctx)
        self.assertIn("## 📚 DURABLE KNOWLEDGE", ctx)
        self.assertIn("## 🧠 RECENT EPISODIC MEMORY", ctx)
        self.assertIn("## ⚖️ INTERPRETATION & CONFLICT GUIDANCE", ctx)


if __name__ == "__main__":
    unittest.main()
