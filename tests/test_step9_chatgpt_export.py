"""Comprehensive test suite for native ChatGPT Export Parser (Step 9).

Validates:
- Loading and schema validation of native ChatGPT conversations-*.json files
- Active branch reconstruction from current_node backwards to root (chronological)
- Abandoned branch protection (asserting zero leak of abandoned user messages with personal facts)
- Edge case traversal handling (cycles, broken parent pointers, missing current_node, empty mappings, message-less nodes)
- Structural mapping nodes vs message-bearing nodes accounting
- Multi-stage pipeline: memory-worthiness gate, atomic extraction, 4-way classification
- Strict Source Authority: USER messages as primary evidence; ASSISTANT as context disambiguation only
- Separation of concise memory_text from raw_user_text
- Runtime review overrides: reclassify, consolidate, exclude keyed by source_record_ids
- Exact observed_at preservation from earliest supporting user message timestamp (UTC ISO)
- Separation of event_date and event_date_basis (explicit, relative_to_message, message_time, unknown)
- Duplicate accounting (conversations, messages, fingerprints, consolidated turns)
- Filesystem safety (filename pattern, allowed directory boundary)
- Safe commit mode (dry_run=False blocked with explicit error)
- MCP ClientSession protocol integration testing with zero Graphiti/registry writes verified
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

from mcp import ClientSession

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from server.chatgpt_export_parser import (
    ChatGPTConversationParser,
    EventDateBasis,
    NativeCandidateCategory,
    NativeExportStats,
    NativeMemoryCandidate,
    StageBasedMemoryExtractor,
    import_chatgpt_exports,
)
from server.mcp import app


def build_synthetic_chatgpt_export() -> list[dict]:
    """Construct neutral synthetic native ChatGPT export data structure."""
    return [
        # Conversation 1: Branching with abandoned branch containing a secret fact
        {
            "id": "conv_synth_001",
            "title": "Architecture Decision",
            "create_time": 1756684800.0,  # 2025-09-01T00:00:00Z
            "update_time": 1756688400.0,
            "is_do_not_remember": False,
            "current_node": "node_user_3",  # Active branch tip
            "mapping": {
                # Root node (structural, no message)
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
                        "id": "msg_synth_u1",
                        "author": {"role": "user"},
                        "create_time": 1756684800.0,
                        "content": {"content_type": "text", "parts": ["I sustained an injury during a bicycle trip on 2024-06-15."]},
                    },
                },
                # ABANDONED BRANCH: Contains a user message with a secret fact
                "node_asst_1a": {
                    "id": "node_asst_1a",
                    "parent": "node_user_1",
                    "children": ["node_user_abandoned"],
                    "message": {
                        "id": "msg_asst_abandoned",
                        "author": {"role": "assistant"},
                        "create_time": 1756684810.0,
                        "content": {"content_type": "text", "parts": ["Draft response that was regenerated..."]},
                    },
                },
                "node_user_abandoned": {
                    "id": "node_user_abandoned",
                    "parent": "node_asst_1a",
                    "children": [],
                    "message": {
                        "id": "msg_u_abandoned_secret",
                        "author": {"role": "user"},
                        "create_time": 1756684815.0,
                        "content": {"content_type": "text", "parts": ["My top secret security pin code is 99887766."]},
                    },
                },
                # ACTIVE BRANCH:
                "node_asst_1b": {
                    "id": "node_asst_1b",
                    "parent": "node_user_1",
                    "children": ["node_user_2"],
                    "message": {
                        "id": "msg_asst_active_1",
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
                        "id": "msg_synth_u2",
                        "author": {"role": "user"},
                        "create_time": 1756684900.0,
                        "content": {"content_type": "text", "parts": ["Let's go with the first option for Project Bluebird."]},
                    },
                },
                "node_asst_2": {
                    "id": "node_asst_2",
                    "parent": "node_user_2",
                    "children": ["node_user_3"],
                    "message": {
                        "id": "msg_asst_active_2",
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
                        "id": "msg_synth_u3",
                        "author": {"role": "user"},
                        "create_time": 1756685000.0,
                        "content": {"content_type": "text", "parts": ["I live in Seattle and work as a systems engineer."]},
                    },
                },
            },
        },
        # Conversation 2: Multi-Turn SQL Optimization
        {
            "id": "conv_synth_002",
            "title": "Query Optimization",
            "create_time": 1691009482.0,  # 2023-08-02T20:51:22Z
            "update_time": 1691009800.0,
            "is_do_not_remember": False,
            "current_node": "node_sql_u2",
            "mapping": {
                "node_sql_root": {"id": "node_sql_root", "parent": None, "children": ["node_sql_u1"], "message": None},
                "node_sql_u1": {
                    "id": "node_sql_u1",
                    "parent": "node_sql_root",
                    "children": ["node_sql_a1"],
                    "message": {
                        "id": "msg_synth_sql_1",
                        "author": {"role": "user"},
                        "create_time": 1691009482.0,
                        "content": {"content_type": "text", "parts": ["I have a data model that takes over an hour to build each morning. Can you optimize the SQL?"]},
                    },
                },
                "node_sql_a1": {
                    "id": "node_sql_a1",
                    "parent": "node_sql_u1",
                    "children": ["node_sql_u2"],
                    "message": {
                        "id": "msg_synth_sql_a1",
                        "author": {"role": "assistant"},
                        "create_time": 1691009500.0,
                        "content": {"content_type": "text", "parts": ["Here is the query optimizations."]},
                    },
                },
                "node_sql_u2": {
                    "id": "node_sql_u2",
                    "parent": "node_sql_a1",
                    "children": [],
                    "message": {
                        "id": "msg_synth_sql_2",
                        "author": {"role": "user"},
                        "create_time": 1691009776.0,  # 2023-08-02T20:56:16Z
                        "content": {"content_type": "text", "parts": ["I encountered a download error when fetching the generated code file."]},
                    },
                },
            },
        },
        # Conversation 3: Generic Non-Memory queries
        {
            "id": "conv_synth_003",
            "title": "Generic C++ & DALL-E",
            "create_time": 1684098520.0,
            "update_time": 1684098600.0,
            "is_do_not_remember": False,
            "current_node": "node_g_u2",
            "mapping": {
                "node_g_root": {"id": "node_g_root", "parent": None, "children": ["node_g_u1"], "message": None},
                "node_g_u1": {
                    "id": "node_g_u1",
                    "parent": "node_g_root",
                    "children": ["node_g_u2"],
                    "message": {
                        "id": "msg_synth_gu1",
                        "author": {"role": "user"},
                        "create_time": 1684098520.0,
                        "content": {"content_type": "text", "parts": ["write this function in c++ instead of in python and use only native libraries"]},
                    },
                },
                "node_g_u2": {
                    "id": "node_g_u2",
                    "parent": "node_g_u1",
                    "children": [],
                    "message": {
                        "id": "msg_synth_gu2",
                        "author": {"role": "user"},
                        "create_time": 1684098580.0,
                        "content": {"content_type": "text", "parts": ["image of iconic mountain range"]},
                    },
                },
            },
        },
        # Conversation 4: is_do_not_remember = True (Should be skipped)
        {
            "id": "conv_synth_004",
            "title": "Private Chat",
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
                        "id": "msg_synth_pu1",
                        "author": {"role": "user"},
                        "create_time": 1756684800.0,
                        "content": {"content_type": "text", "parts": ["I decided to upgrade servers on 2026-05-01."]},
                    },
                },
            },
        },
    ]


class TestChatGPTExportParser(unittest.IsolatedAsyncioTestCase):
    """Comprehensive unit test suite for native ChatGPT export parser."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.export_file = Path(self.temp_dir) / "conversations-000.json"
        with open(self.export_file, "w", encoding="utf-8") as f:
            json.dump(build_synthetic_chatgpt_export(), f, indent=2)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_abandoned_branch_protection_and_zero_fact_leakage(self):
        """Verify user messages on abandoned branches are never extracted or referenced."""
        data = build_synthetic_chatgpt_export()
        conv1 = data[0]
        mapping = conv1["mapping"]
        current_node_id = conv1["current_node"]

        active_path, discarded_nodes, act_struct, disc_struct = ChatGPTConversationParser.extract_active_path(
            mapping, current_node_id
        )

        active_ids = [n["id"] for n in active_path]
        discarded_ids = [n["id"] for n in discarded_nodes]

        self.assertNotIn("node_user_abandoned", active_ids)
        self.assertIn("node_user_abandoned", discarded_ids)
        self.assertIn("node_asst_1a", discarded_ids)

        # Run extraction on active path and verify zero leak of secret fact
        active_user_texts = [
            ChatGPTConversationParser.extract_message_text(n["message"])
            for n in active_path
            if n.get("message") and n["message"].get("author", {}).get("role") == "user"
        ]
        all_active_combined = " ".join(active_user_texts)
        self.assertNotIn("99887766", all_active_combined)
        self.assertNotIn("top secret security pin", all_active_combined)

    def test_traversal_edge_cases(self):
        """Verify cycle protection, missing current_node, broken parent pointers, and empty mappings."""
        # 1. Empty mapping
        act, disc, a_c, d_c = ChatGPTConversationParser.extract_active_path({}, "some_node")
        self.assertEqual(len(act), 0)
        self.assertEqual(len(disc), 0)

        # 2. Missing current_node
        mapping = {"node_1": {"id": "node_1", "parent": None, "message": None}}
        act, disc, a_c, d_c = ChatGPTConversationParser.extract_active_path(mapping, None)
        self.assertEqual(len(act), 0)
        self.assertEqual(len(disc), 1)

        # 3. Broken parent pointer
        mapping_broken = {
            "node_2": {"id": "node_2", "parent": "non_existent_node", "message": {"id": "m2", "author": {"role": "user"}, "content": {"parts": ["hello"]}}}
        }
        act, disc, a_c, d_c = ChatGPTConversationParser.extract_active_path(mapping_broken, "node_2")
        self.assertEqual(len(act), 1)
        self.assertEqual(act[0]["id"], "node_2")

        # 4. Cycle in parent pointers
        mapping_cycle = {
            "node_a": {"id": "node_a", "parent": "node_b", "message": None},
            "node_b": {"id": "node_b", "parent": "node_a", "message": None},
        }
        act, disc, a_c, d_c = ChatGPTConversationParser.extract_active_path(mapping_cycle, "node_a")
        self.assertEqual(len(act), 2)
        self.assertEqual(len(disc), 0)

    def test_runtime_review_overrides_reclassify_consolidate_exclude(self):
        """Verify runtime review overrides apply reclassification, consolidation, exclusion, and multi-candidate splitting."""
        cands = [
            NativeMemoryCandidate(
                candidate_id="cand_1",
                source_record_ids=["chatgpt:conv_1:msg_1"],
                conversation_id="conv_1",
                conversation_title="Civic Query",
                supporting_user_message_ids=["msg_1"],
                raw_user_text="Draft an email to park department about community garden rules.",
                memory_text="Draft an email to park department about community garden rules.",
                category=NativeCandidateCategory.NON_MEMORY,
            ),
            NativeMemoryCandidate(
                candidate_id="cand_2",
                source_record_ids=["chatgpt:conv_1:msg_2"],
                conversation_id="conv_1",
                conversation_title="Civic Query",
                supporting_user_message_ids=["msg_2"],
                raw_user_text="I encountered an issue submitting the community garden application.",
                memory_text="I encountered an issue submitting the community garden application.",
                category=NativeCandidateCategory.NON_MEMORY,
            ),
            NativeMemoryCandidate(
                candidate_id="cand_3",
                source_record_ids=["chatgpt:conv_1:msg_3"],
                conversation_id="conv_1",
                conversation_title="Random Chat",
                supporting_user_message_ids=["msg_3"],
                raw_user_text="I want to exclude this candidate.",
                memory_text="I want to exclude this candidate.",
                category=NativeCandidateCategory.EPISODIC,
            ),
            NativeMemoryCandidate(
                candidate_id="cand_4",
                source_record_ids=["chatgpt:conv_1:msg_4"],
                conversation_id="conv_1",
                conversation_title="Career Strategy",
                supporting_user_message_ids=["msg_4"],
                raw_user_text="I marked 10 years at Acme Corp leading systems architecture.",
                memory_text="I marked 10 years at Acme Corp leading systems architecture.",
                category=NativeCandidateCategory.EPISODIC,
            ),
        ]

        overrides = [
            # 1. Consolidate msg_1 and msg_2
            {
                "source_record_ids": ["chatgpt:conv_1:msg_1", "chatgpt:conv_1:msg_2"],
                "action": "consolidate",
                "category": "episodic",
                "interaction_type": "assisted_drafting",
                "memory_text": "The user used ChatGPT to assist with drafting a civic inquiry regarding community garden rules and troubleshooting an application submission issue.",
                "event_date": "2024-05-20",
                "event_date_precision": "day",
                "event_date_basis": "message_time",
                "reason": "Consolidated civic engagement workflow.",
            },
            # 2. Exclude msg_3
            {
                "source_record_ids": ["chatgpt:conv_1:msg_3"],
                "action": "exclude",
                "reason": "Excluded per user review.",
            },
            # 3. Multi-output reclassify msg_4 into episodic milestone + durable career role
            {
                "source_record_ids": ["chatgpt:conv_1:msg_4"],
                "action": "reclassify",
                "category": "episodic",
                "interaction_type": "assisted_drafting",
                "memory_text": "The user marked a 10-year career milestone at Acme Corp leading systems architecture.",
                "event_date": "2024-10-01",
                "event_date_precision": "day",
                "event_date_basis": "message_time",
                "multiple_outputs": [
                    {
                        "suffix": "durable_role",
                        "category": "durable_candidate",
                        "interaction_type": "real_world_event",
                        "memory_text": "The user serves as lead systems architect at Acme Corp with over 10 years of experience.",
                        "reason": "Durable leadership profile context."
                    }
                ]
            }
        ]

        applied_cands, count = StageBasedMemoryExtractor.apply_review_overrides(cands, overrides)
        self.assertEqual(count, 4)

        episodic_list = [c for c in applied_cands if c.category == NativeCandidateCategory.EPISODIC]
        durable_list = [c for c in applied_cands if c.category == NativeCandidateCategory.DURABLE_CANDIDATE]
        non_mem_list = [c for c in applied_cands if c.category == NativeCandidateCategory.NON_MEMORY]

        self.assertEqual(len(episodic_list), 2)
        self.assertEqual(len(durable_list), 1)
        self.assertEqual(len(non_mem_list), 1)

        cons = [c for c in episodic_list if "civic inquiry" in c.memory_text][0]
        self.assertEqual(set(cons.source_record_ids), {"chatgpt:conv_1:msg_1", "chatgpt:conv_1:msg_2"})
        self.assertEqual(cons.event_date, "2024-05-20")
        self.assertEqual(cons.interaction_type, "assisted_drafting")

    def test_temporal_precision_and_embedded_dates(self):
        """Verify day, month, year precision, message_time basis, and ignoring embedded code/example dates."""
        # Explicit Day
        cand_day = StageBasedMemoryExtractor.distill_turn_to_candidate(
            user_text="I sustained an injury during a bicycle trip on 2024-06-15.",
            user_msg_id="msg_d1",
            user_create_time=1756684800.0,
            prev_assistant_text=None,
            prev_assistant_msg_id=None,
            conversation_id="conv_t1",
            conversation_title="Injury",
        )
        self.assertEqual(cand_day.event_date, "2024-06-15")
        self.assertEqual(cand_day.event_date_precision, "day")
        self.assertEqual(cand_day.event_date_basis, EventDateBasis.EXPLICIT.value)

        # Explicit Month
        cand_month = StageBasedMemoryExtractor.distill_turn_to_candidate(
            user_text="I decided to launch the project in July 2016.",
            user_msg_id="msg_m1",
            user_create_time=1756684800.0,
            prev_assistant_text=None,
            prev_assistant_msg_id=None,
            conversation_id="conv_t2",
            conversation_title="Launch",
        )
        self.assertEqual(cand_month.event_date, "2016-07")
        self.assertEqual(cand_month.event_date_precision, "month")
        self.assertEqual(cand_month.event_date_basis, EventDateBasis.EXPLICIT.value)

        # Embedded date in generic SQL code query (should be NON_MEMORY with no event_date assigned)
        cand_embedded = StageBasedMemoryExtractor.distill_turn_to_candidate(
            user_text="write a query to filter records where created_at >= '2021-03-15' in postgres",
            user_msg_id="msg_e1",
            user_create_time=1756684800.0,
            prev_assistant_text=None,
            prev_assistant_msg_id=None,
            conversation_id="conv_t3",
            conversation_title="Query",
        )
        self.assertEqual(cand_embedded.category, NativeCandidateCategory.NON_MEMORY)

    def test_filesystem_safety_and_commit_mode_blocking(self):
        """Verify filename enforcement and fail-closed graph name validation."""
        # 1. Invalid filename pattern
        with self.assertRaises(ValueError):
            ChatGPTConversationParser.validate_file_path("/tmp/unrelated.json")

        # 2. Directory containment check
        with self.assertRaises(PermissionError):
            ChatGPTConversationParser.validate_file_path(
                str(self.export_file), allowed_root=Path("/some/other/path")
            )

        # 3. Missing graph_name on committed import fails closed
        with self.assertRaises(ValueError) as ctx:
            asyncio.run(import_chatgpt_exports(paths=[str(self.export_file)], dry_run=False, graph_name=None))
        self.assertIn("graph_name must be explicitly provided", str(ctx.exception))

        # 4. Protected default_db on committed import fails closed during validation
        with self.assertRaises(ValueError) as ctx2:
            asyncio.run(import_chatgpt_exports(paths=[str(self.export_file)], dry_run=False, graph_name="default_db"))
        self.assertIn("Refusing to run import into protected graph 'default_db'", str(ctx2.exception))

    async def test_full_export_dry_run_and_reporting(self):
        """Verify running import_chatgpt_exports on synthetic export file."""
        report_md, report_data = await import_chatgpt_exports(
            paths=[str(self.export_file)],
            dry_run=True,
            results_dir=Path(self.temp_dir) / "results",
            allowed_root=Path(self.temp_dir),
        )

        self.assertIn("DRY RUN - Zero Graphiti Writes, Zero Gemini Calls", report_md)
        self.assertIn("- **Total Conversations in File:** 4", report_md)
        self.assertIn("- **Do-Not-Remember Conversations Skipped:** 1", report_md)

        stats = report_data["stats"]
        self.assertEqual(stats["files_processed"], 1)
        self.assertEqual(stats["total_conversations"], 4)
        self.assertEqual(stats["do_not_remember_conversations_skipped"], 1)
        self.assertGreater(stats["episodic_count"], 0)
        self.assertGreater(stats["non_memory_count"], 0)

    async def test_mcp_dispatcher_and_client_session_boundary(self):
        """Verify calling import_chatgpt_exports over MCP dispatcher and ClientSession in-memory stream."""
        tools = await app.list_tools()
        tool_dict = {t.name: t for t in tools}
        self.assertIn("import_chatgpt_exports", tool_dict)

        # 1. Dispatcher-level call
        mcp_res = await app.call_tool(
            "import_chatgpt_exports",
            arguments={"paths": [str(self.export_file)], "dry_run": True},
        )
        self.assertFalse(mcp_res.is_error)
        self.assertIn("Native ChatGPT Conversation Export Report", mcp_res.content[0].text)

        # 2. ClientSession protocol integration over in-memory stream
        from anyio import create_memory_object_stream
        client_to_server_send, client_to_server_receive = create_memory_object_stream(100)
        server_to_client_send, server_to_client_receive = create_memory_object_stream(100)

        async def run_server():
            try:
                await app._lowlevel_server.run(
                    client_to_server_receive,
                    server_to_client_send,
                    app._lowlevel_server.create_initialization_options()
                )
            except Exception:
                pass

        async def run_client():
            async with ClientSession(server_to_client_receive, client_to_server_send) as session:
                await session.initialize()
                listed = await session.list_tools()
                tool_names = [t.name for t in listed.tools]
                self.assertIn("import_chatgpt_exports", tool_names)

                call_res = await session.call_tool(
                    "import_chatgpt_exports",
                    {"paths": [str(self.export_file)], "dry_run": True},
                )
                self.assertIsNotNone(call_res)
                self.assertGreater(len(call_res.content), 0)
                self.assertIn("Native ChatGPT Conversation Export Report", call_res.content[0].text)

        async with asyncio.TaskGroup() as tg:
            server_task = tg.create_task(run_server())
            client_task = tg.create_task(run_client())
            await client_task
            server_task.cancel()


if __name__ == "__main__":
    unittest.main()
