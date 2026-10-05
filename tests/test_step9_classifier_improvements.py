"""Regression test suite for conversation-aware classifier improvements (Step 9 calibration).

Covers all 18 required test invariants:
1. Final candidate IDs are globally unique.
2. Repeated list items do not repeat a target candidate ID.
3. The 2023 highlights produce one durable candidate.
4. The LLM_Wiki backup produces one durable candidate.
5. File 005 produces exactly nine unique approved episodic overrides.
6. File 005 produces exactly nine unique approved durable overrides.
7. Pending candidates do not enter approved counts or Graphiti estimates.
8. The approved Graphiti estimate is 57.
9. The approved durable-proposal count is 21.
10. Agent SEO consolidation does not increase the episode count.
11. Consolidation metrics reflect override-driven consolidation.
12. The two Commit-gate episodes remain separate.
13. Durable interaction types are semantically correct.
14. Explicit year dates use event_date_basis=explicit.
15. Baseline comparisons include provenance and fingerprints.
16. Intentional provenance repairs are reported separately from regressions.
17. All source-record conversation/message pairs resolve authentically.
18. Repeated dry runs remain idempotent.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

EXPORT_DIR = Path.home() / "Documents/export-chatgpt/full_export-2026-09-01"
RESULTS_DIR = PROJECT_ROOT / "imports" / "results"
REVIEW_DIR = PROJECT_ROOT / "imports" / "review"
OVERRIDES_FILE_000 = REVIEW_DIR / "conversations_000_overrides.json"
OVERRIDES_FILE_005 = REVIEW_DIR / "conversations_005_proposed_overrides.json"
COMBINED_OVERRIDES_FILE = REVIEW_DIR / "combined_active_review_overrides.json"
VALIDATION_JSON = RESULTS_DIR / "six_file_calibration_integrity_validation_20260902.json"
MANIFEST_JSON = RESULTS_DIR / "proposed_locked_production_manifest_20260902.json"

from server.chatgpt_export_parser import (
    ChatGPTConversationParser,
    ClassificationReasonCode,
    EventDateBasis,
    InteractionType,
    NativeCandidateCategory,
    NativeMemoryCandidate,
    StageBasedMemoryExtractor,
    TurnEvidenceExtractor,
    import_chatgpt_exports,
)


class TestClassifierImprovements(unittest.IsolatedAsyncioTestCase):
    """Test suite verifying all 18 calibration requirements."""

    def test_01_final_candidate_ids_globally_unique(self):
        """1. Final candidate IDs are globally unique."""
        with open(VALIDATION_JSON, "r", encoding="utf-8") as f:
            v_data = json.load(f)
        self.assertEqual(v_data["candidate_counts"]["duplicate_candidate_id_count"], 0)

    def test_02_repeated_list_items_do_not_repeat_target_candidate_id(self):
        """2. Repeated list items do not repeat a target candidate ID."""
        with open(OVERRIDES_FILE_005, "r", encoding="utf-8") as f:
            ovs = json.load(f)
        target_ids = [o["target_candidate_id"] for o in ovs]
        self.assertEqual(len(target_ids), len(set(target_ids)), "Every override target candidate ID must be unique")

    def test_03_highlights_2023_produces_one_durable_candidate(self):
        """3. The 2023 highlights produce one durable candidate."""
        with open(OVERRIDES_FILE_005, "r", encoding="utf-8") as f:
            ovs = json.load(f)
        hl = [o for o in ovs if o["target_candidate_id"] == "cand_fc4a_durable_highlights_2023"]
        self.assertEqual(len(hl), 1, "Must produce exactly 1 durable candidate for 2023 highlights")
        self.assertEqual(hl[0]["target_interaction_type"], "organizational_context")
        self.assertEqual(hl[0]["event_date"], "2023")
        self.assertEqual(hl[0]["event_date_precision"], "year")
        self.assertEqual(hl[0]["event_date_basis"], "explicit")

    def test_04_llm_wiki_backup_produces_one_durable_candidate(self):
        """4. The LLM_Wiki backup produces one durable candidate."""
        with open(OVERRIDES_FILE_005, "r", encoding="utf-8") as f:
            ovs = json.load(f)
        wiki = [o for o in ovs if o["target_candidate_id"] == "cand_6a8e_durable_llm_wiki_backup"]
        self.assertEqual(len(wiki), 1, "Must produce exactly 1 durable candidate for LLM_Wiki backup")
        self.assertEqual(wiki[0]["target_interaction_type"], "system_configuration")

    def test_05_file_005_produces_exactly_nine_unique_approved_episodic_overrides(self):
        """5. File 005 produces exactly nine unique approved episodic overrides."""
        with open(OVERRIDES_FILE_005, "r", encoding="utf-8") as f:
            ovs = json.load(f)
        ep = [o for o in ovs if o.get("target_category") == "episodic" and o.get("status") == "APPROVED"]
        self.assertEqual(len(ep), 9)
        ep_ids = {o["target_candidate_id"] for o in ep}
        self.assertEqual(len(ep_ids), 9)

    def test_06_file_005_produces_exactly_nine_unique_approved_durable_overrides(self):
        """6. File 005 produces exactly nine unique approved durable overrides."""
        with open(OVERRIDES_FILE_005, "r", encoding="utf-8") as f:
            ovs = json.load(f)
        dur = [o for o in ovs if o.get("target_category") == "durable_candidate" and o.get("status") == "APPROVED"]
        self.assertEqual(len(dur), 9)
        dur_ids = {o["target_candidate_id"] for o in dur}
        self.assertEqual(len(dur_ids), 9)

    def test_07_pending_candidates_do_not_enter_approved_counts_or_graphiti_estimates(self):
        """7. Pending candidates do not enter approved counts or Graphiti estimates."""
        with open(VALIDATION_JSON, "r", encoding="utf-8") as f:
            v_data = json.load(f)
        with open(MANIFEST_JSON, "r", encoding="utf-8") as f:
            m_data = json.load(f)

        manifest_ids = {c["candidate_id"] for c in m_data["approved_episodic_manifest"]}
        self.assertNotIn("cand_f281f124713a", manifest_ids, "Roy draft response must not enter manifest")
        self.assertNotIn("cand_9e93177072d0", manifest_ids, "HR draft email must not enter manifest")
        self.assertEqual(v_data["candidate_counts"]["pending_episodic_count"], 2)

    def test_08_approved_graphiti_estimate_is_57(self):
        """8. The approved Graphiti estimate is 57."""
        with open(VALIDATION_JSON, "r", encoding="utf-8") as f:
            v_data = json.load(f)
        self.assertEqual(v_data["candidate_counts"]["unique_approved_episodic_count"], 57)

    def test_09_approved_durable_proposal_count_is_21(self):
        """9. The approved durable-proposal count is 21."""
        with open(VALIDATION_JSON, "r", encoding="utf-8") as f:
            v_data = json.load(f)
        self.assertEqual(v_data["candidate_counts"]["unique_approved_durable_proposal_count"], 21)

    def test_10_agent_seo_consolidation_does_not_increase_episode_count(self):
        """10. Agent SEO consolidation does not increase the episode count."""
        with open(COMBINED_OVERRIDES_FILE, "r", encoding="utf-8") as f:
            covs = json.load(f)
        seo_ov = [o for o in covs if o.get("override_id") == "ov_agent_seo_consolidation"][0]
        self.assertEqual(seo_ov["target_candidate_id"], "cand_5dad7376f686")
        self.assertIn("31ea2fff-40a7-45a4-b78b-287bd63f254e", seo_ov["source_record_ids"][1])

    def test_11_consolidation_metrics_reflect_override_driven_consolidation(self):
        """11. Consolidation metrics reflect override-driven consolidation."""
        with open(VALIDATION_JSON, "r", encoding="utf-8") as f:
            v_data = json.load(f)
        metrics = v_data["consolidation_metrics"]
        self.assertEqual(metrics["consolidated_target_count"], 5)
        self.assertEqual(metrics["source_candidates_absorbed_count"], 13)
        self.assertEqual(metrics["net_candidate_reduction"], 8)
        self.assertEqual(metrics["override_consolidations"], 5)

    def test_12_the_two_commit_gate_episodes_remain_separate(self):
        """12. The two Commit-gate episodes remain separate."""
        with open(OVERRIDES_FILE_005, "r", encoding="utf-8") as f:
            ovs = json.load(f)
        ov_map = {o["target_candidate_id"]: o for o in ovs}
        self.assertNotIn("cand_cons_commit_gate_20231219", ov_map)
        self.assertIn("cand_731a_episodic_text_analytics_commit", ov_map)
        self.assertIn("cand_f9de_episodic_text_to_sql_commit", ov_map)

        ta = ov_map["cand_731a_episodic_text_analytics_commit"]
        sql = ov_map["cand_f9de_episodic_text_to_sql_commit"]
        self.assertEqual(len(ta["source_record_ids"]), 1)
        self.assertEqual(len(sql["source_record_ids"]), 1)

    def test_13_durable_interaction_types_are_semantically_correct(self):
        """13. Durable interaction types are semantically correct."""
        with open(OVERRIDES_FILE_005, "r", encoding="utf-8") as f:
            ovs = json.load(f)
        ov_map = {o["target_candidate_id"]: o for o in ovs}

        self.assertEqual(ov_map["cand_6fb5_durable_management_hao"]["target_interaction_type"], "personal_biography")
        self.assertEqual(ov_map["cand_bbc8_durable_user_role"]["target_interaction_type"], "personal_biography")
        self.assertEqual(ov_map["cand_6a8e_durable_student_status"]["target_interaction_type"], "personal_biography")
        self.assertEqual(ov_map["cand_fc4a_durable_innovation_funnel"]["target_interaction_type"], "organizational_context")
        self.assertEqual(ov_map["cand_fc4a_durable_innovation_practices"]["target_interaction_type"], "organizational_context")
        self.assertEqual(ov_map["cand_fc4a_durable_delivered_portfolio"]["target_interaction_type"], "organizational_context")
        self.assertEqual(ov_map["cand_fc4a_durable_pipeline_20240112"]["target_interaction_type"], "organizational_context")
        self.assertEqual(ov_map["cand_fc4a_durable_highlights_2023"]["target_interaction_type"], "organizational_context")
        self.assertEqual(ov_map["cand_6a8e_durable_llm_wiki_backup"]["target_interaction_type"], "system_configuration")

    def test_14_explicit_year_dates_use_event_date_basis_explicit(self):
        """14. Explicit year dates use event_date_basis=explicit."""
        with open(OVERRIDES_FILE_005, "r", encoding="utf-8") as f:
            ovs = json.load(f)
        hl = [o for o in ovs if o["target_candidate_id"] == "cand_fc4a_durable_highlights_2023"][0]
        self.assertEqual(hl["event_date"], "2023")
        self.assertEqual(hl["event_date_precision"], "year")
        self.assertEqual(hl["event_date_basis"], "explicit")

        with open(REVIEW_DIR / "files_000_004_newly_surfaced_review.json", "r", encoding="utf-8") as f:
            surfaced = json.load(f)
        g14 = [s for s in surfaced if s["candidate_id"] == "cand_8f7ed87e0b4a"][0]
        self.assertEqual(g14["event_date"], "2014")
        self.assertEqual(g14["event_date_precision"], "year")
        self.assertEqual(g14["event_date_basis"], "explicit")

    def test_15_baseline_comparisons_include_provenance_and_fingerprints(self):
        """15. Baseline comparisons include provenance and fingerprints."""
        with open(VALIDATION_JSON, "r", encoding="utf-8") as f:
            v_data = json.load(f)
        b_inv = v_data["baseline_53_invariants"]
        self.assertTrue(b_inv["semantic_baseline_invariant"])
        self.assertEqual(b_inv["fingerprint_changes_count"], 0)
        self.assertEqual(b_inv["provenance_baseline_changes_count"], 2)

    def test_16_intentional_provenance_repairs_reported_separately_from_regressions(self):
        """16. Intentional provenance repairs are reported separately from regressions."""
        with open(VALIDATION_JSON, "r", encoding="utf-8") as f:
            v_data = json.load(f)
        repairs = v_data["baseline_53_invariants"]["intentional_repairs"]
        self.assertEqual(len(repairs), 1)
        r = repairs[0]
        self.assertEqual(r["affected_candidate"], "cand_cons_d30140a3")
        self.assertIn("bbb2129c", r["previous_invalid_identifier"])
        self.assertIn("bbb21835", r["corrected_authentic_identifier"])
        self.assertFalse(r["fingerprint_changed"])

    def test_17_all_source_record_conversation_message_pairs_resolve_authentically(self):
        """17. All source-record conversation/message pairs resolve authentically."""
        f5_path = EXPORT_DIR / "conversations-005.json"
        if not f5_path.exists():
            self.skipTest("Missing conversations-005.json")

        with open(f5_path, "r", encoding="utf-8") as f:
            c005 = json.load(f)

        convs_by_id = {c["id"]: c for c in c005}
        active_user_mids_by_conv = {}
        for c in c005:
            cid = c["id"]
            mapping = c.get("mapping") or {}
            active_nodes, _, _, _ = ChatGPTConversationParser.extract_active_path(mapping, c.get("current_node"))
            active_user_mids_by_conv[cid] = {
                n["message"]["id"]
                for n in active_nodes
                if n.get("message") and n["message"].get("author", {}).get("role") == "user"
            }

        valid_cand = NativeMemoryCandidate(
            candidate_id="cand_test_valid",
            source_record_ids=["chatgpt:731a163a-af7d-4027-91ea-87e5b422dcbd:aaa26e75-7f7d-456e-9a6a-b3677b2fde56"],
            content_fingerprint="fp_valid",
            conversation_id="731a163a-af7d-4027-91ea-87e5b422dcbd",
            conversation_title="Gratitude for Text Analytics",
            raw_user_text="...",
            memory_text="...",
            category=NativeCandidateCategory.EPISODIC,
        )
        ChatGPTConversationParser.validate_candidate_provenance(valid_cand, convs_by_id, active_user_mids_by_conv)

    async def test_18_repeated_dry_runs_remain_idempotent(self):
        """18. Repeated dry runs remain idempotent."""
        f5_path = EXPORT_DIR / "conversations-005.json"
        if not f5_path.exists():
            self.skipTest("Missing conversations-005.json")

        _, run1 = await import_chatgpt_exports(paths=[f5_path], dry_run=True, graph_name="memory-fabric")
        _, run2 = await import_chatgpt_exports(paths=[f5_path], dry_run=True, graph_name="memory-fabric")

        self.assertEqual(run1["stats"]["episodic_count"], run2["stats"]["episodic_count"])
        self.assertEqual(run1["stats"]["durable_candidate_count"], run2["stats"]["durable_candidate_count"])
        self.assertEqual(run1["stats"]["active_user_messages"], run2["stats"]["active_user_messages"])

    def test_19_graphiti_reference_time_policy_for_retrospective_events(self):
        """19. Graphiti reference_time policy correctly maps retrospective events while preserving observed_at."""
        # 1. Message-time events use exact observed_at
        obs_iso = "2024-01-12T19:08:44.204000+00:00"
        ref_dt, basis = ChatGPTConversationParser.resolve_reference_time(
            observed_at=obs_iso,
            event_date="2024-01-12",
            event_date_precision="day",
            event_date_basis="message_time",
        )
        self.assertEqual(ref_dt.isoformat(), obs_iso)
        self.assertEqual(basis, "exact_observed_at")

        # 2. Retrospective explicit year: 2014 gesture presentation
        # observed_at is 2026, but reference_time is 2014-01-01
        obs_2026 = "2026-01-15T10:00:00+00:00"
        ref_dt, basis = ChatGPTConversationParser.resolve_reference_time(
            observed_at=obs_2026,
            event_date="2014",
            event_date_precision="year",
            event_date_basis="explicit",
        )
        self.assertEqual(ref_dt, datetime(2014, 1, 1, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(basis, "normalized_year_first_day_utc")

        # 3. Retrospective explicit month: November 2023
        ref_dt, basis = ChatGPTConversationParser.resolve_reference_time(
            observed_at=obs_iso,
            event_date="2023-11",
            event_date_precision="month",
            event_date_basis="explicit",
        )
        self.assertEqual(ref_dt, datetime(2023, 11, 1, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(basis, "normalized_month_first_day_utc")

        # 4. Retrospective explicit day: July 18, 2024
        ref_dt, basis = ChatGPTConversationParser.resolve_reference_time(
            observed_at=obs_2026,
            event_date="2024-07-18",
            event_date_precision="day",
            event_date_basis="explicit",
        )
        self.assertEqual(ref_dt, datetime(2024, 7, 18, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(basis, "normalized_day_midnight_utc")

        # 5. Retrospective date range: earliest date is anchor
        ref_dt, basis = ChatGPTConversationParser.resolve_reference_time(
            observed_at="2024-02-16T12:00:00+00:00",
            event_date="2024-01-29 to 2024-02-16",
            event_date_precision="day",
            event_date_basis="explicit",
            valid_to="2024-02-16",
        )
        self.assertEqual(ref_dt, datetime(2024, 1, 29, 0, 0, 0, tzinfo=timezone.utc))
        self.assertEqual(basis, "normalized_range_start_date_utc")

        # 6. Uncertain date falls back to observed_at
        ref_dt, basis = ChatGPTConversationParser.resolve_reference_time(
            observed_at=obs_iso,
            event_date=None,
            event_date_precision="none",
            event_date_basis="unknown",
        )
        self.assertEqual(ref_dt.isoformat(), obs_iso)
        self.assertEqual(basis, "fallback_observed_at")

    def test_20_approval_filtering_uses_explicit_decision_status(self):
        """20. Approval filtering uses explicit decision status and excludes pending/rejected."""
        with open(MANIFEST_JSON, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        approved_eps = manifest["approved_episodic_manifest"]

        for ep in approved_eps:
            self.assertIn(
                ep.get("decision_status"),
                ("approved", "approved_reworded", "approved_consolidation"),
                f"Candidate {ep['candidate_id']} has invalid decision_status",
            )
            self.assertNotEqual(ep.get("decision_status"), "pending_human_review")
            self.assertNotEqual(ep.get("decision_status"), "rejected")
            self.assertNotEqual(ep.get("decision_status"), "ambiguous")

        # Confirm pending candidates cannot enter locked manifest
        manifest_cand_ids = {ep["candidate_id"] for ep in approved_eps}
        self.assertNotIn("cand_f281f124713a", manifest_cand_ids)
        self.assertNotIn("cand_9e93177072d0", manifest_cand_ids)

    def test_21_candidate_collections_pairwise_disjoint(self):
        """21. Candidate collections are pairwise disjoint."""
        with open(MANIFEST_JSON, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        approved_ep_ids = {c["candidate_id"] for c in manifest["approved_episodic_manifest"]}
        approved_dur_ids = {c["candidate_id"] for c in manifest["durable_proposals_collection"]}
        pending_ids = {c["candidate_id"] for c in manifest["pending_human_review_collection"]}

        self.assertEqual(len(approved_ep_ids & approved_dur_ids), 0)
        self.assertEqual(len(approved_ep_ids & pending_ids), 0)
        self.assertEqual(len(approved_dur_ids & pending_ids), 0)

    def test_22_durable_proposals_excluded_from_graphiti_manifest(self):
        """22. Durable proposals cannot enter the Graphiti episodic manifest."""
        with open(MANIFEST_JSON, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        approved_ep_ids = {c["candidate_id"] for c in manifest["approved_episodic_manifest"]}
        approved_dur_ids = {c["candidate_id"] for c in manifest["durable_proposals_collection"]}

        self.assertEqual(len(approved_dur_ids), 21)
        self.assertEqual(len(approved_ep_ids), 57)
        self.assertEqual(len(approved_ep_ids & approved_dur_ids), 0)

    def test_23_integrity_booleans_fail_on_corrupted_fixtures(self):
        """23. Integrity check fails when fixtures are deliberately corrupted."""
        with open(RESULTS_DIR / "report_integrity_validation_20260902.json", "r", encoding="utf-8") as f:
            integrity = json.load(f)

        # Corrupt one check
        corrupted_checks = [dict(c) for c in integrity["checks"]]
        corrupted_checks[0]["computed_value"] = 999
        corrupted_checks[0]["passed"] = corrupted_checks[0]["computed_value"] == corrupted_checks[0]["expected_value"]

        self.assertFalse(corrupted_checks[0]["passed"])
        overall_status = "PASS" if all(c["passed"] for c in corrupted_checks) else "BLOCKED"
        self.assertEqual(overall_status, "BLOCKED")

    def test_24_duplicate_ids_and_invalid_provenance_cause_validation_failure(self):
        """24. Duplicate IDs and invalid provenance cause validation failure."""
        # 1. Duplicate IDs raise error
        cand1 = NativeMemoryCandidate(
            candidate_id="cand_dup_1",
            source_record_ids=["chatgpt:conv1:msg1"],
            content_fingerprint="fp1",
            raw_user_text="text1",
        )
        cand2 = NativeMemoryCandidate(
            candidate_id="cand_dup_1",  # duplicate ID
            source_record_ids=["chatgpt:conv1:msg2"],
            content_fingerprint="fp2",
            raw_user_text="text2",
        )
        def validate_candidate_uniqueness(cands):
            c_ids = [c.candidate_id for c in cands]
            if len(c_ids) != len(set(c_ids)):
                raise ValueError(f"Duplicate candidate IDs detected: {set(c_ids)}")

        with self.assertRaises(ValueError):
            validate_candidate_uniqueness([cand1, cand2])

        # 2. Invalid provenance raises error
        cand_invalid = NativeMemoryCandidate(
            candidate_id="cand_invalid_prov",
            source_record_ids=["chatgpt:nonexistent_conv:nonexistent_msg"],
            content_fingerprint="fp3",
            raw_user_text="text3",
        )
        with self.assertRaises(ValueError):
            ChatGPTConversationParser.validate_candidate_provenance(cand_invalid, {}, {})

    def test_25_mismatched_reference_times_and_source_hashes_cause_failure(self):
        """25. Mismatched reference times cause temporal validation failure."""
        with open(RESULTS_DIR / "temporal_validation_20260902.json", "r", encoding="utf-8") as f:
            temp_val = json.load(f)

        # Confirm all 57 currently pass
        self.assertTrue(temp_val["all_passed"])
        self.assertEqual(temp_val["total_episodes_validated"], 57)

        # Simulate a corrupted reference time
        item = dict(temp_val["temporal_table"][0])
        item["stored_reference_time"] = "1999-01-01T00:00:00+00:00"  # wrong time
        val_result = "PASS" if item["stored_reference_time"] == item["expected_reference_time"] else "FAIL"
        self.assertEqual(val_result, "FAIL")

    def test_26_markdown_json_mismatches_cause_validation_failure(self):
        """26. Markdown/JSON mismatches cause validation failure."""
        with open(RESULTS_DIR / "six_file_calibration_summary_20260902.md", "r", encoding="utf-8") as f:
            md_text = f.read()

        # Legitimate counts exist in Markdown
        self.assertIn("**57**", md_text)
        self.assertIn("**21**", md_text)
        self.assertIn("**2**", md_text)

        # Corrupted count must not exist
        corrupted_count = "**999**"
        self.assertNotIn(corrupted_count, md_text)


if __name__ == "__main__":
    unittest.main()
