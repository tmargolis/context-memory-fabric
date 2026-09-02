"""Comprehensive test suite for native ChatGPT Conversation Export Parser (Step 9).

Validates:
- Loading and schema validation of native ChatGPT conversations-*.json files
- Active branch reconstruction from current_node backwards to root (chronological)
- Discarded / regenerated branch counting
- Source Authority: USER messages as primary evidence; ASSISTANT as context disambiguation only
- Four-way classification: episodic, durable_candidate, ambiguous, non_memory
- Exact observed_at preservation from user message create_time (UTC ISO)
- Separation of event_date from observed_at
- Filtering out generic non-memory queries (generic coding, trivia, DALL-E)
- Skipping is_do_not_remember conversations
- Stable origin IDs (src_chatgpt_...)
- Administrative MCP tool boundary invocation via app.call_tool("import_chatgpt_exports", ...)
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.chatgpt_exporter import (
    ChatGPTConversationParser,
    NativeCandidateCategory,
    NativeCandidateClassifier,
    NativeExportStats,
    NativeMemoryCandidate,
    import_chatgpt_exports,
)
from server.mcp import app


def build_synthetic_chatgpt_export() -> list[dict]:
    """Construct synthetic native ChatGPT export data structure with branched nodes."""
    return [
        # Conversation 1: Branching with decisions and assistant context
        {
            "id": "conv_synthetic_001",
            "title": "Database Architecture & Decision",
            "create_time": 1756684800.0,  # 2025-09-01T00:00:00Z
            "update_time": 1756688400.0,
            "is_do_not_remember": False,
            "current_node": "node_user_3",  # Active branch tip
            "mapping": {
                # Root node
                "node_root": {
                    "id": "node_root",
                    "parent": None,
                    "children": ["node_user_1"],
                    "message": None,
                },
                # Turn 1: User personal statement with explicit past date
                "node_user_1": {
                    "id": "node_user_1",
                    "parent": "node_root",
                    "children": ["node_asst_1a", "node_asst_1b"],
                    "message": {
                        "id": "msg_u1",
                        "author": {"role": "user"},
                        "create_time": 1756684800.0,
                        "content": {"content_type": "text", "parts": ["I sustained a C7 fracture during a ski accident on 2025-01-13."]},
                    },
                },
                # Discarded branch: Regenerated assistant turn
                "node_asst_1a": {
                    "id": "node_asst_1a",
                    "parent": "node_user_1",
                    "children": [],
                    "message": {
                        "id": "msg_a1a",
                        "author": {"role": "assistant"},
                        "create_time": 1756684810.0,
                        "content": {"content_type": "text", "parts": ["Draft response that was regenerated..."]},
                    },
                },
                # Active assistant turn
                "node_asst_1b": {
                    "id": "node_asst_1b",
                    "parent": "node_user_1",
                    "children": ["node_user_2"],
                    "message": {
                        "id": "msg_a1b",
                        "author": {"role": "assistant"},
                        "create_time": 1756684820.0,
                        "content": {"content_type": "text", "parts": ["Option A is PostgreSQL 16. Option B is DuckDB."]},
                    },
                },
                # Turn 2: User referential response using assistant context
                "node_user_2": {
                    "id": "node_user_2",
                    "parent": "node_asst_1b",
                    "children": ["node_asst_2"],
                    "message": {
                        "id": "msg_u2",
                        "author": {"role": "user"},
                        "create_time": 1756684900.0,
                        "content": {"content_type": "text", "parts": ["Let's go with the first option for Project Atlas."]},
                    },
                },
                "node_asst_2": {
                    "id": "node_asst_2",
                    "parent": "node_user_2",
                    "children": ["node_user_3"],
                    "message": {
                        "id": "msg_a2",
                        "author": {"role": "assistant"},
                        "create_time": 1756684920.0,
                        "content": {"content_type": "text", "parts": ["Great choice. PostgreSQL 16 configured."]},
                    },
                },
                # Turn 3: User durable biography statement
                "node_user_3": {
                    "id": "node_user_3",
                    "parent": "node_asst_2",
                    "children": [],
                    "message": {
                        "id": "msg_u3",
                        "author": {"role": "user"},
                        "create_time": 1756685000.0,
                        "content": {"content_type": "text", "parts": ["I live in Chicago and work as a software architect."]},
                    },
                },
            },
        },
        # Conversation 2: Generic Non-Memory Queries (Generic C++ code, DALL-E, and Trivia)
        {
            "id": "conv_synthetic_002",
            "title": "Generic Code & DALL-E",
            "create_time": 1684098520.0,
            "update_time": 1684098600.0,
            "is_do_not_remember": False,
            "current_node": "node_g_u2",
            "mapping": {
                "node_g_root": {
                    "id": "node_g_root",
                    "parent": None,
                    "children": ["node_g_u1"],
                    "message": None,
                },
                "node_g_u1": {
                    "id": "node_g_u1",
                    "parent": "node_g_root",
                    "children": ["node_g_a1"],
                    "message": {
                        "id": "msg_gu1",
                        "author": {"role": "user"},
                        "create_time": 1684098520.0,
                        "content": {"content_type": "text", "parts": ["write this function in c++ instead of in python and use only native libraries"]},
                    },
                },
                "node_g_a1": {
                    "id": "node_g_a1",
                    "parent": "node_g_u1",
                    "children": ["node_g_u2"],
                    "message": {
                        "id": "msg_ga1",
                        "author": {"role": "assistant"},
                        "create_time": 1684098540.0,
                        "content": {"content_type": "text", "parts": ["Here is the C++ code..."]},
                    },
                },
                "node_g_u2": {
                    "id": "node_g_u2",
                    "parent": "node_g_a1",
                    "children": [],
                    "message": {
                        "id": "msg_gu2",
                        "author": {"role": "user"},
                        "create_time": 1684098580.0,
                        "content": {"content_type": "text", "parts": ["image of iconic fall mountain valley"]},
                    },
                },
            },
        },
        # Conversation 3: is_do_not_remember = True (Should be skipped)
        {
            "id": "conv_synthetic_003",
            "title": "Private Incognito Chat",
            "create_time": 1756684800.0,
            "update_time": 1756684900.0,
            "is_do_not_remember": True,
            "current_node": "node_p_u1",
            "mapping": {
                "node_p_root": {"id": "node_p_root", "parent": None, "children": ["node_p_u1"], "message": None},
                "node_p_u1": {
                    "id": "node_p_u1",
                    "parent": "node_p_root",
                    "children": [],
                    "message": {
                        "id": "msg_pu1",
                        "author": {"role": "user"},
                        "create_time": 1756684800.0,
                        "content": {"content_type": "text", "parts": ["I decided to invest in solar on 2026-05-01."]},
                    },
                },
            },
        },
    ]


class TestChatGPTNativeExporter(unittest.IsolatedAsyncioTestCase):
    """Test suite for native ChatGPT export parser and classifier."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_file = Path(self.temp_dir) / "conversations-000.json"
        with open(self.export_file, "w", encoding="utf-8") as f:
            json.dump(build_synthetic_chatgpt_export(), f, indent=2)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_active_path_traversal_and_branch_discarding(self):
        """Verify active path traces current_node backwards to root and counts discarded nodes."""
        data = build_synthetic_chatgpt_export()
        conv1 = data[0]
        mapping = conv1["mapping"]
        current_node_id = conv1["current_node"]

        active_path, discarded = ChatGPTConversationParser.extract_active_path(mapping, current_node_id)
        
        # In conv1, total nodes = 7, discarded node_asst_1a = 1, active = 6
        self.assertEqual(discarded, 1)
        self.assertEqual(len(active_path), 6)

        # Check chronological sequence: root -> user1 -> asst1b -> user2 -> asst2 -> user3
        node_ids = [n["id"] for n in active_path]
        self.assertEqual(
            node_ids,
            ["node_root", "node_user_1", "node_asst_1b", "node_user_2", "node_asst_2", "node_user_3"],
        )

    def test_user_vs_assistant_authority_and_classification(self):
        """Verify USER statements establish facts while ASSISTANT statements provide context only."""
        data = build_synthetic_chatgpt_export()
        conv1 = data[0]

        # Turn 1: Ski accident with explicit date
        cand1 = NativeCandidateClassifier.classify_turn(
            user_text="I sustained a C7 fracture during a ski accident on 2025-01-13.",
            user_msg_id="msg_u1",
            user_create_time=1756684800.0,
            prev_assistant_text=None,
            prev_assistant_msg_id=None,
            conversation_id="conv_1",
            conversation_title="Accident",
        )
        self.assertEqual(cand1.category, NativeCandidateCategory.EPISODIC)
        self.assertEqual(cand1.event_date, "2025-01-13")
        self.assertEqual(cand1.event_date_precision, "day")
        self.assertEqual(cand1.observed_at, "2025-09-01T00:00:00+00:00")
        self.assertNotEqual(cand1.observed_at, cand1.event_date)  # Decoupled!

        # Turn 2: Referential decision with assistant context
        cand2 = NativeCandidateClassifier.classify_turn(
            user_text="Let's go with the first option for Project Atlas.",
            user_msg_id="msg_u2",
            user_create_time=1756684900.0,
            prev_assistant_text="Option A is PostgreSQL 16. Option B is DuckDB.",
            prev_assistant_msg_id="msg_a1b",
            conversation_id="conv_1",
            conversation_title="Database Decision",
        )
        self.assertEqual(cand2.category, NativeCandidateCategory.EPISODIC)
        self.assertIn("msg_a1b", cand2.assistant_context_message_ids)
        self.assertIn("Option A is PostgreSQL 16", cand2.context_notes)

        # Turn 3: Biography durable candidate
        cand3 = NativeCandidateClassifier.classify_turn(
            user_text="I live in Chicago and work as a software architect.",
            user_msg_id="msg_u3",
            user_create_time=1756685000.0,
            prev_assistant_text=None,
            prev_assistant_msg_id=None,
            conversation_id="conv_1",
            conversation_title="Bio",
        )
        self.assertEqual(cand3.category, NativeCandidateCategory.DURABLE_CANDIDATE)

    def test_generic_non_memory_filtering(self):
        """Verify generic coding requests, DALL-E prompts, and trivia are classified as NON_MEMORY."""
        cand_code = NativeCandidateClassifier.classify_turn(
            user_text="write this function in c++ instead of in python and use only native libraries",
            user_msg_id="msg_c1",
            user_create_time=1684098520.0,
            prev_assistant_text=None,
            prev_assistant_msg_id=None,
            conversation_id="conv_2",
            conversation_title="C++ Function",
        )
        self.assertEqual(cand_code.category, NativeCandidateCategory.NON_MEMORY)

        cand_dalle = NativeCandidateClassifier.classify_turn(
            user_text="image of iconic fall mountain valley",
            user_msg_id="msg_c2",
            user_create_time=1684098580.0,
            prev_assistant_text=None,
            prev_assistant_msg_id=None,
            conversation_id="conv_2",
            conversation_title="Image",
        )
        self.assertEqual(cand_dalle.category, NativeCandidateCategory.NON_MEMORY)

        cand_trivia = NativeCandidateClassifier.classify_turn(
            user_text="Explain why there was a drop in birth rate in Japan in 1966",
            user_msg_id="msg_c3",
            user_create_time=1684098600.0,
            prev_assistant_text=None,
            prev_assistant_msg_id=None,
            conversation_id="conv_2",
            conversation_title="Trivia",
        )
        self.assertEqual(cand_trivia.category, NativeCandidateCategory.NON_MEMORY)

    async def test_full_export_dry_run_and_reporting(self):
        """Verify running import_chatgpt_exports on synthetic export file."""
        report_md, report_data = await import_chatgpt_exports(
            paths=[str(self.export_file)],
            dry_run=True,
            results_dir=Path(self.temp_dir) / "results",
        )

        self.assertIn("DRY RUN (No Graphiti writes)", report_md)
        self.assertIn("- **Total Conversations:** 3", report_md)
        self.assertIn("- **Branch Messages Discarded:** 1", report_md)

        stats = report_data["stats"]
        self.assertEqual(stats["files_processed"], 1)
        self.assertEqual(stats["total_conversations"], 3)
        self.assertEqual(stats["branch_messages_discarded"], 1)
        self.assertEqual(stats["episodic_count"], 2)
        self.assertEqual(stats["durable_candidate_count"], 1)
        self.assertEqual(stats["non_memory_count"], 2)

    async def test_mcp_tool_boundary_call(self):
        """Verify calling import_chatgpt_exports over the MCP protocol boundary."""
        tools = await app.list_tools()
        tool_dict = {t.name: t for t in tools}
        self.assertIn("import_chatgpt_exports", tool_dict)

        tool = tool_dict["import_chatgpt_exports"]
        self.assertEqual(tool.title, "Import Native ChatGPT Conversation Exports")

        mcp_res = await app.call_tool(
            "import_chatgpt_exports",
            arguments={"paths": [str(self.export_file)], "dry_run": True},
        )
        self.assertFalse(mcp_res.is_error)
        self.assertGreater(len(mcp_res.content), 0)
        text_out = mcp_res.content[0].text
        self.assertIn("Native ChatGPT Conversation Export Report", text_out)
        self.assertIn("DRY RUN", text_out)


if __name__ == "__main__":
    unittest.main()
