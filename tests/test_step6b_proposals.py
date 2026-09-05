"""Comprehensive test suite for Phase 1 Step 6B: Wiki Update Proposals & Semantic Boundary.

Validates:
- Update proposal generation (diff, hashes, persistence, original unchanged)
- Create proposal generation (new file preview, target file not created)
- Path traversal rejection
- Binary target rejection
- Search isolation (proposals do not enter search_wiki)
- Process persistence across fresh store instances
- Full 5-way semantic contract (remember, recall, search_wiki, propose_wiki_update, get_context)
"""

import hashlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.context import get_context
from server.corpus import CorpusAsset, ExtractionStatus
from server.mcp import app, get_context as mcp_get_context, propose_wiki_update as mcp_propose_wiki_update, recall as mcp_recall, remember as mcp_remember, search_wiki as mcp_search_wiki
from server.memory import close_graphiti
from server.proposals import (
    WikiProposal,
    create_wiki_proposal,
    format_proposal_for_mcp,
    get_proposal,
    list_proposals,
    validate_target_path,
)
from server.wiki import CorpusScanner, CorpusSearchEngine, search_corpus


class TestStep6BProposals(unittest.IsolatedAsyncioTestCase):
    """Test suite for Wiki Update Proposals and Semantic Boundaries."""

    def setUp(self):
        self.temp_wiki = tempfile.TemporaryDirectory()
        self.wiki_root = Path(self.temp_wiki.name)

        self.temp_proposals = tempfile.TemporaryDirectory()
        self.proposals_dir = Path(self.temp_proposals.name) / "wiki-proposals"

        # Populate a minimal synthetic Wiki
        wiki_dir = self.wiki_root / "WIKI"
        wiki_dir.mkdir(parents=True, exist_ok=True)
        self.project_md = wiki_dir / "project.md"
        self.initial_content = "# Project Atlas\nProject Atlas uses PostgreSQL."
        self.project_md.write_text(self.initial_content, encoding="utf-8")

        images_dir = self.wiki_root / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        (images_dir / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR...")

    def tearDown(self):
        self.temp_wiki.cleanup()
        self.temp_proposals.cleanup()

    async def asyncTearDown(self):
        await close_graphiti()

    def test_update_proposal_semantics(self):
        """Verify update proposal records hashes, generates diff, and leaves Wiki untouched."""
        proposed_text = "# Project Atlas\nProject Atlas uses PostgreSQL and Redis."
        rationale = "Adding Redis for caching layer."

        initial_sha = hashlib.sha256(self.initial_content.encode("utf-8")).hexdigest()
        proposed_sha = hashlib.sha256(proposed_text.encode("utf-8")).hexdigest()

        proposal = create_wiki_proposal(
            target_path="WIKI/project.md",
            proposed_content=proposed_text,
            rationale=rationale,
            source_context="Session 42 discussion",
            wiki_root=self.wiki_root,
            proposals_dir=self.proposals_dir,
        )

        # 1. Verify proposal fields
        self.assertTrue(proposal.proposal_id.startswith("prop_"))
        self.assertEqual(proposal.status, "pending_review")
        self.assertEqual(proposal.operation, "update")
        self.assertEqual(proposal.target_path, "WIKI/project.md")
        self.assertEqual(proposal.current_sha256, initial_sha)
        self.assertEqual(proposal.proposed_sha256, proposed_sha)
        self.assertEqual(proposal.rationale, rationale)
        self.assertEqual(proposal.source_context, "Session 42 discussion")

        # 2. Verify diff contents
        self.assertIn("+Project Atlas uses PostgreSQL and Redis.", proposal.unified_diff)
        self.assertIn("-Project Atlas uses PostgreSQL.", proposal.unified_diff)

        # 3. CRITICAL: Verify the original file was NOT modified
        actual_content = self.project_md.read_text(encoding="utf-8")
        self.assertEqual(actual_content, self.initial_content, "Target file in LLM_Wiki must not be modified!")

    def test_create_proposal_semantics(self):
        """Verify create proposal handles new files without creating them on disk."""
        target_path = "WIKI/new-project.md"
        proposed_text = "# New Project\nInitial draft for review."
        rationale = "New project documentation."

        proposal = create_wiki_proposal(
            target_path=target_path,
            proposed_content=proposed_text,
            rationale=rationale,
            wiki_root=self.wiki_root,
            proposals_dir=self.proposals_dir,
        )

        # 1. Verify proposal metadata
        self.assertEqual(proposal.operation, "create")
        self.assertIsNone(proposal.current_sha256)
        self.assertEqual(proposal.target_path, target_path)
        self.assertIn("+Initial draft for review.", proposal.unified_diff)

        # 2. CRITICAL: Verify new file does NOT exist in LLM_Wiki
        created_file = self.wiki_root / target_path
        self.assertFalse(created_file.exists(), "Target file must not be created on disk during proposal!")

    def test_path_traversal_rejection(self):
        """Verify path traversal attempts are rejected."""
        traversals = [
            "../../outside.md",
            "../secret.txt",
            "/etc/passwd",
            "WIKI/../../../root.md",
            ".git/config",
            "_lint_reports/report.md",
        ]
        for t in traversals:
            with self.assertRaises(ValueError, msg=f"Should reject traversal: '{t}'"):
                validate_target_path(t, self.wiki_root)

    def test_binary_target_rejection(self):
        """Verify proposals targeting binary/media formats are rejected."""
        binaries = [
            "images/diagram.png",
            "documents/sample.pdf",
            "audio/recording.mp3",
            "data/file.bin",
        ]
        for b in binaries:
            with self.assertRaises(ValueError, msg=f"Should reject binary format: '{b}'"):
                validate_target_path(b, self.wiki_root)

    def test_search_isolation(self):
        """Verify proposal content never leaks into durable knowledge search results."""
        secret_token = "SecretProposalTokenNotInWiki999"
        create_wiki_proposal(
            target_path="WIKI/project.md",
            proposed_content=f"# Project Atlas\nContains {secret_token}",
            rationale="Testing search isolation",
            wiki_root=self.wiki_root,
            proposals_dir=self.proposals_dir,
        )

        # Search the synthetic wiki
        results = search_corpus(secret_token, root_path=self.wiki_root)
        self.assertEqual(len(results), 0, "Proposals must NOT be returned in search_wiki / search_corpus results!")

    def test_process_persistence(self):
        """Verify proposals persist to disk and can be retrieved by a fresh reader instance."""
        prop = create_wiki_proposal(
            target_path="WIKI/project.md",
            proposed_content="# Project Atlas\nUpdated content",
            rationale="Persistence test",
            wiki_root=self.wiki_root,
            proposals_dir=self.proposals_dir,
        )

        # Retrieve proposal from a fresh listing / read
        loaded_prop = get_proposal(prop.proposal_id, proposals_dir=self.proposals_dir)
        self.assertIsNotNone(loaded_prop)
        self.assertEqual(loaded_prop.proposal_id, prop.proposal_id)
        self.assertEqual(loaded_prop.rationale, "Persistence test")

        all_props = list_proposals(proposals_dir=self.proposals_dir)
        self.assertIn(prop.proposal_id, [p.proposal_id for p in all_props])

    async def test_full_five_tool_mcp_contract(self):
        """Verify the complete 5-tool semantic contract."""
        # 1. MCP Tool Registration
        tools = await app.list_tools()
        tool_names = {t.name for t in tools}
        expected_tools = {
            "remember", "recall", "search_wiki", "get_context", "propose_wiki_update",
            "import_memories", "import_chatgpt_exports", "edit_memory", "reconcile_memories",
            "capture_note", "capture_health",  # MS4a MCP-boundary capture
            "promote_auto_accepted_memories",
        }
        self.assertEqual(tool_names, expected_tools, f"Expected exactly {expected_tools}, got {tool_names}")

        # 2. Direct Episodic Write
        rem_res = await mcp_remember(
            content="Project Orion selected SQLite for edge execution on August 31, 2026.",
            name="step6b_orion_sqlite_decision",
            source_description="Semantic boundary test",
        )
        self.assertIn("Memory stored successfully", rem_res)

        # 3. Direct Episodic Read
        recall_res = await mcp_recall(query="Project Orion database", max_results=3)
        self.assertIn("SQLite", recall_res)

        # 4. Canonical Durable Read
        wiki_res = await mcp_search_wiki(query="Personal-Context-Service", max_results=3)
        self.assertIn("Personal-Context-Service", wiki_res)

        # 5. Durable Write Proposal (Persistent Proposal Only, No Canonical Mutation)
        prop_res = await mcp_propose_wiki_update(
            target_path="WIKI/projects/Personal-Context-Service.md",
            proposed_content="# Proposed Personal Context Service Update\nSynthetic proposal content.",
            rationale="Test proposal via MCP tool",
        )
        self.assertIn("Wiki Update Proposal Generated", prop_res)
        self.assertIn("NOT", prop_res)
        self.assertIn("modified `LLM_Wiki`", prop_res)

        # 6. Combined Read
        context_res = await mcp_get_context(topic="Project Atlas", max_wiki_results=3, max_memory_results=3)
        self.assertIn("## 📚 DURABLE KNOWLEDGE", context_res)
        self.assertIn("## 🧠 RECENT EPISODIC MEMORY", context_res)
        self.assertIn("## ⚖️ INTERPRETATION & CONFLICT GUIDANCE", context_res)


if __name__ == "__main__":
    unittest.main()
