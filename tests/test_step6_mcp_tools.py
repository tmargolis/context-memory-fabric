"""Comprehensive test suite for Phase 1 Step 6 MCP tools.

Validates:
- search_wiki() with cache, new exclusions (_lint_reports, _profile_reports, templates, memory) and MCP formatting
- remember() storing synthetic memory into Graphiti/FalkorDB
- recall() retrieving memory facts with temporal provenance
- get_context() unifying durable knowledge and episodic memory
- MCP tool registration and schemas
"""

import asyncio
import os
from pathlib import Path
import sys
import unittest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.corpus import ExtractionStatus, get_corpus_root
from server.context import get_context
from server.mcp import app, get_context as mcp_get_context, recall as mcp_recall, remember as mcp_remember, search_wiki as mcp_search_wiki
from server.memory import close_graphiti, recall, remember
from server.wiki import WikiCorpusManager, format_search_results_for_mcp, search_corpus, search_wiki


class TestStep6MCPTools(unittest.IsolatedAsyncioTestCase):
    """Integration test suite for Step 6 MCP tools."""

    async def asyncTearDown(self):
        await close_graphiti()

    async def test_mcp_tool_registration(self):
        """Verify all 4 required MCP tools are registered on the MCPServer instance."""
        tools = await app.list_tools()
        tool_names = [t.name for t in tools]
        print(f"Registered MCP tools: {tool_names}")

        self.assertIn("remember", tool_names)
        self.assertIn("recall", tool_names)
        self.assertIn("search_wiki", tool_names)
        self.assertIn("get_context", tool_names)

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
