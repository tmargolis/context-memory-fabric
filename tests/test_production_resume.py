"""Unit and integration tests for production import recovery and resume mode.

Covers all 7 required recovery test scenarios:
1. Resume from 20/57 attempts only remaining 37 candidates.
2. Existing candidates are skipped without Graphiti/LLM calls.
3. An unmatched graph episode blocks resume preflight.
4. An unmatched registry record blocks resume preflight.
5. Sustained 429 halts the run instead of skipping ahead (INCOMPLETE_RATE_LIMITED).
6. Successful canary changes both counts from 20 to 21.
7. Repeated resume remains idempotent.
"""

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
from production_import_runner import (
    EXPECTED_MANIFEST_SHA256,
    EXPECTED_TOTAL_EPISODIC,
    reconcile_state,
)
from server.chatgpt_export_parser import (
    ChatGPTConversationParser,
    NativeCandidateCategory,
    NativeMemoryCandidate,
    import_chatgpt_exports,
)

MANIFEST_PATH = PROJECT_ROOT / "imports" / "results" / "proposed_locked_production_manifest_20260902.json"
REGISTRY_PATH = PROJECT_ROOT / "imports" / "state" / "import_registry_memory-fabric.json"


class TestProductionResume(unittest.IsolatedAsyncioTestCase):
    """Test suite covering safe resume and rate-limit recovery."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_01_resume_from_20_of_57_calculates_remaining(self):
        """1. Resume from 20/57 identifies exactly 37 remaining in manifest order."""
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            m_data = json.load(f)
        manifest_eps = m_data["approved_episodic_manifest"]
        self.assertEqual(len(manifest_eps), 57)

        backup_path = PROJECT_ROOT / "imports" / "state" / "import_registry_memory-fabric.backup_20260903_interrupted.json"
        with open(backup_path, "r", encoding="utf-8") as f:
            reg_data = json.load(f)
        imported = reg_data["imported_episodes"]
        self.assertEqual(len(imported), 20)

        reg_cids = {rec["candidate_id"] for rec in imported.values()}
        remaining = [c for c in manifest_eps if c["candidate_id"] not in reg_cids]

        self.assertEqual(len(remaining), 37)
        # First remaining candidate must be cand_9a63aba92761
        self.assertEqual(remaining[0]["candidate_id"], "cand_9a63aba92761")

    @patch("server.memory.get_graphiti")
    async def test_02_existing_candidates_skipped_without_graphiti_llm_calls(self, mock_get_graphiti):
        """2. Existing candidates are skipped without Graphiti/LLM calls."""
        mock_client = MagicMock()
        mock_client.add_episode = AsyncMock()
        mock_get_graphiti.return_value = mock_client

        # Create 2 test candidates
        c1 = NativeMemoryCandidate(
            candidate_id="cand_1",
            category=NativeCandidateCategory.EPISODIC,
            decision_status="approved",
            content_fingerprint="fp1",
            raw_user_text="text 1",
            memory_text="memory text 1",
            source_record_ids=["chatgpt:conv1:msg1"],
        )
        c2 = NativeMemoryCandidate(
            candidate_id="cand_2",
            category=NativeCandidateCategory.EPISODIC,
            decision_status="approved",
            content_fingerprint="fp2",
            raw_user_text="text 2",
            memory_text="memory text 2",
            source_record_ids=["chatgpt:conv1:msg2"],
        )

        reg_file = self.temp_path / "import_registry_test.json"
        # c1 is already checkpointed
        reg_file.write_text(json.dumps({
            "imported_episodes": {
                "fp1": {"candidate_id": "cand_1", "episode_uuid": "uuid1", "episode_name": "ep1"}
            }
        }))

        mock_ep = MagicMock()
        mock_ep.uuid = "uuid2"
        mock_client.add_episode.return_value = mock_ep

        # Mock apply_review_overrides to return [c1, c2]
        with patch("server.chatgpt_export_parser.StageBasedMemoryExtractor.apply_review_overrides", return_value=([c1, c2], 0)):
            f_dummy = self.temp_path / "conversations-000.json"
            f_dummy.write_text("[]")

            report_md, report_dict = await import_chatgpt_exports(
                paths=[f_dummy],
                dry_run=False,
                graph_name="test",
                results_dir=self.temp_path,
                state_dir=self.temp_path,
            )

        # c1 must be skipped, c2 ingested
        self.assertEqual(mock_client.add_episode.call_count, 1)
        call_kwargs = mock_client.add_episode.call_args.kwargs
        self.assertEqual(call_kwargs["episode_body"], "memory text 2")
        self.assertEqual(report_dict["stats"]["matches_against_existing_registry"], 1)

    @patch("production_import_runner.redis.Redis")
    def test_03_unmatched_graph_episode_blocks_resume(self, mock_redis_cls):
        """3. An unmatched graph episode blocks resume."""
        mock_r = MagicMock()
        mock_redis_cls.return_value = mock_r

        # Graph has an extra episode node not in registry
        mock_r.execute_command.side_effect = lambda cmd, *args: {
            ("GRAPH.QUERY", "memory-fabric", "MATCH (e:Episodic) RETURN e.name, e.uuid"): [
                None,
                [
                    ["chatgpt_cand_1_fp1", "uuid1"],
                    ["chatgpt_unmatched_extra_node", "uuid_extra"],
                ]
            ],
            ("GRAPH.LIST",): ["memory-fabric", "default_db", "cmf_chatgpt_000"],
            ("GRAPH.QUERY", "default_db", "MATCH (n) RETURN count(n)"): [None, [[128]]],
            ("GRAPH.QUERY", "default_db", "MATCH ()-[r]->() RETURN count(r)"): [None, [[167]]],
            ("GRAPH.QUERY", "cmf_chatgpt_000", "MATCH (n) RETURN count(n)"): [None, [[52]]],
            ("GRAPH.QUERY", "cmf_chatgpt_000", "MATCH ()-[r]->() RETURN count(r)"): [None, [[67]]],
            ("GRAPH.QUERY", "memory-fabric", "MATCH (n) RETURN count(n)"): [None, [[2]]],
            ("GRAPH.QUERY", "memory-fabric", "MATCH ()-[r]->() RETURN count(r)"): [None, [[0]]],
        }.get((cmd, *args), [None, [[0]]])

        # Registry only has cand_1
        mock_reg = {
            "imported_episodes": {
                "fp1": {
                    "candidate_id": "cand_cons_84c6745d",
                    "episode_name": "chatgpt_cand_1_fp1",
                }
            }
        }
        test_reg_path = self.temp_path / "reg.json"
        test_reg_path.write_text(json.dumps(mock_reg))

        with patch("production_import_runner.REGISTRY_PATH", test_reg_path):
            res = reconcile_state(expected_count=1, target_graph="memory-fabric")
            self.assertEqual(res["status"], "FAIL")
            self.assertTrue(any("missing from registry records" in err for err in res["reconciliation_errors"]))

    @patch("production_import_runner.redis.Redis")
    def test_04_unmatched_registry_record_blocks_resume(self, mock_redis_cls):
        """4. An unmatched registry record blocks resume."""
        mock_r = MagicMock()
        mock_redis_cls.return_value = mock_r

        # Graph has no nodes
        mock_r.execute_command.side_effect = lambda cmd, *args: {
            ("GRAPH.QUERY", "memory-fabric", "MATCH (e:Episodic) RETURN e.name, e.uuid"): [None, []],
            ("GRAPH.LIST",): ["memory-fabric", "default_db", "cmf_chatgpt_000"],
            ("GRAPH.QUERY", "default_db", "MATCH (n) RETURN count(n)"): [None, [[128]]],
            ("GRAPH.QUERY", "default_db", "MATCH ()-[r]->() RETURN count(r)"): [None, [[167]]],
            ("GRAPH.QUERY", "cmf_chatgpt_000", "MATCH (n) RETURN count(n)"): [None, [[52]]],
            ("GRAPH.QUERY", "cmf_chatgpt_000", "MATCH ()-[r]->() RETURN count(r)"): [None, [[67]]],
            ("GRAPH.QUERY", "memory-fabric", "MATCH (n) RETURN count(n)"): [None, [[0]]],
            ("GRAPH.QUERY", "memory-fabric", "MATCH ()-[r]->() RETURN count(r)"): [None, [[0]]],
        }.get((cmd, *args), [None, [[0]]])

        # Registry has a record
        mock_reg = {
            "imported_episodes": {
                "fp1": {
                    "candidate_id": "cand_cons_84c6745d",
                    "episode_name": "chatgpt_cand_1_fp1",
                }
            }
        }
        test_reg_path = self.temp_path / "reg.json"
        test_reg_path.write_text(json.dumps(mock_reg))

        with patch("production_import_runner.REGISTRY_PATH", test_reg_path):
            res = reconcile_state(expected_count=1, target_graph="memory-fabric")
            self.assertEqual(res["status"], "FAIL")
            self.assertTrue(any("missing from memory-fabric Episodic nodes" in err for err in res["reconciliation_errors"]))

    @patch("server.memory.get_graphiti")
    async def test_05_sustained_429_halts_run_instead_of_skipping_ahead(self, mock_get_graphiti):
        """5. Sustained 429 halts the run instead of skipping ahead."""
        mock_client = MagicMock()
        # Raise 429 RESOURCE_EXHAUSTED
        mock_client.add_episode = AsyncMock(side_effect=RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded"))
        mock_get_graphiti.return_value = mock_client

        c1 = NativeMemoryCandidate(
            candidate_id="cand_fail_429",
            category=NativeCandidateCategory.EPISODIC,
            decision_status="approved",
            content_fingerprint="fp1",
            raw_user_text="text 1",
            memory_text="memory text 1",
            source_record_ids=["chatgpt:conv1:msg1"],
        )
        c2 = NativeMemoryCandidate(
            candidate_id="cand_should_not_run",
            category=NativeCandidateCategory.EPISODIC,
            decision_status="approved",
            content_fingerprint="fp2",
            raw_user_text="text 2",
            memory_text="memory text 2",
            source_record_ids=["chatgpt:conv1:msg2"],
        )

        reg_file = self.temp_path / "import_registry_test.json"
        reg_file.write_text(json.dumps({"imported_episodes": {}}))

        with patch("server.chatgpt_export_parser.StageBasedMemoryExtractor.apply_review_overrides", return_value=([c1, c2], 0)):
            f_dummy = self.temp_path / "conversations-000.json"
            f_dummy.write_text("[]")

            report_md, report_dict = await import_chatgpt_exports(
                paths=[f_dummy],
                dry_run=False,
                graph_name="test",
                results_dir=self.temp_path,
                state_dir=self.temp_path,
                max_retries=2,
                max_retry_delay=0.01,
                inter_candidate_delay=0.01,
                halt_on_rate_limit=True,
            )

        # Verified behavior:
        # Candidate 1 retried 2 times then failed
        # Candidate 2 was NEVER attempted
        self.assertEqual(mock_client.add_episode.call_count, 2)
        self.assertEqual(report_dict["import_status"], "INCOMPLETE_RATE_LIMITED")
        self.assertIn("exhausted 2 rate-limit retries", report_dict["halt_reason"])

    @patch("server.memory.get_graphiti")
    async def test_06_successful_canary_changes_count_from_20_to_21(self, mock_get_graphiti):
        """6. Successful canary (--max-new-candidates 1) ingests exactly 1 candidate."""
        mock_client = MagicMock()
        mock_ep = MagicMock()
        mock_ep.uuid = "uuid_canary"
        mock_client.add_episode = AsyncMock(return_value=mock_ep)
        mock_get_graphiti.return_value = mock_client

        # Create 20 existing candidates and 2 new candidates
        existing_cands = [
            NativeMemoryCandidate(
                candidate_id=f"cand_exist_{i}",
                category=NativeCandidateCategory.EPISODIC,
                decision_status="approved",
                content_fingerprint=f"fp_exist_{i}",
                raw_user_text=f"text {i}",
                memory_text=f"memory text {i}",
                source_record_ids=[f"chatgpt:conv:msg{i}"],
            )
            for i in range(20)
        ]
        new_c1 = NativeMemoryCandidate(
            candidate_id="cand_canary_target",
            category=NativeCandidateCategory.EPISODIC,
            decision_status="approved",
            content_fingerprint="fp_new_1",
            raw_user_text="canary text",
            memory_text="canary memory text",
            source_record_ids=["chatgpt:conv:msg_canary"],
        )
        new_c2 = NativeMemoryCandidate(
            candidate_id="cand_next_remaining",
            category=NativeCandidateCategory.EPISODIC,
            decision_status="approved",
            content_fingerprint="fp_new_2",
            raw_user_text="next remaining text",
            memory_text="next memory text",
            source_record_ids=["chatgpt:conv:msg_next"],
        )

        # Seed registry with 20 existing
        reg_file = self.temp_path / "import_registry_test.json"
        reg_data = {
            "imported_episodes": {
                f"fp_exist_{i}": {
                    "candidate_id": f"cand_exist_{i}",
                    "episode_uuid": f"uuid_exist_{i}",
                    "episode_name": f"ep_{i}",
                }
                for i in range(20)
            }
        }
        reg_file.write_text(json.dumps(reg_data))

        all_cands = existing_cands + [new_c1, new_c2]
        with patch("server.chatgpt_export_parser.StageBasedMemoryExtractor.apply_review_overrides", return_value=(all_cands, 0)):
            f_dummy = self.temp_path / "conversations-000.json"
            f_dummy.write_text("[]")

            report_md, report_dict = await import_chatgpt_exports(
                paths=[f_dummy],
                dry_run=False,
                graph_name="test",
                results_dir=self.temp_path,
                state_dir=self.temp_path,
                max_new_candidates=1,
                inter_candidate_delay=0.01,
            )

        # Verify only 1 new candidate was ingested
        self.assertEqual(mock_client.add_episode.call_count, 1)
        self.assertEqual(report_dict["new_ingested_count"], 1)

        # Verify registry increased from 20 to 21
        updated_reg = json.loads(reg_file.read_text())["imported_episodes"]
        self.assertEqual(len(updated_reg), 21)
        self.assertIn("fp_new_1", updated_reg)
        self.assertNotIn("fp_new_2", updated_reg)

    @patch("server.memory.get_graphiti")
    async def test_07_repeated_resume_remains_idempotent(self, mock_get_graphiti):
        """7. Repeated resume skips all already imported candidates without graph changes."""
        mock_client = MagicMock()
        mock_client.add_episode = AsyncMock()
        mock_get_graphiti.return_value = mock_client

        # Seed registry with 3 candidates
        cands = [
            NativeMemoryCandidate(
                candidate_id=f"cand_{i}",
                category=NativeCandidateCategory.EPISODIC,
                decision_status="approved",
                content_fingerprint=f"fp_{i}",
                raw_user_text=f"text {i}",
                memory_text=f"memory text {i}",
                source_record_ids=[f"chatgpt:conv:msg{i}"],
            )
            for i in range(3)
        ]
        reg_file = self.temp_path / "import_registry_test.json"
        reg_data = {
            "imported_episodes": {
                f"fp_{i}": {
                    "candidate_id": f"cand_{i}",
                    "episode_uuid": f"uuid_{i}",
                    "episode_name": f"ep_{i}",
                }
                for i in range(3)
            }
        }
        reg_file.write_text(json.dumps(reg_data))

        with patch("server.chatgpt_export_parser.StageBasedMemoryExtractor.apply_review_overrides", return_value=(cands, 0)):
            f_dummy = self.temp_path / "conversations-000.json"
            f_dummy.write_text("[]")

            report_md, report_dict = await import_chatgpt_exports(
                paths=[f_dummy],
                dry_run=False,
                graph_name="test",
                results_dir=self.temp_path,
                state_dir=self.temp_path,
            )

        self.assertEqual(mock_client.add_episode.call_count, 0)
        self.assertEqual(report_dict["stats"]["matches_against_existing_registry"], 3)
        self.assertEqual(report_dict["new_ingested_count"], 0)



if __name__ == "__main__":
    unittest.main()
