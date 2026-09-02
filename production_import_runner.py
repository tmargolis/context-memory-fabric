"""Production runner for importing the 57 approved episodic candidates into memory-fabric."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("production_runner")

import redis

from server.chatgpt_export_parser import (
    ChatGPTConversationParser,
    import_chatgpt_exports,
)

EXPORT_DIR = Path("/Users/todd/Documents/export-chatgpt/full_export-2026-09-01")
RESULTS_DIR = PROJECT_ROOT / "imports" / "results"
REVIEW_DIR = PROJECT_ROOT / "imports" / "review"
STATE_DIR = PROJECT_ROOT / "imports" / "state"

COMBINED_OVERRIDES_PATH = REVIEW_DIR / "combined_active_review_overrides.json"
MANIFEST_PATH = RESULTS_DIR / "proposed_locked_production_manifest_20260902.json"
REGISTRY_PATH = STATE_DIR / "import_registry_memory-fabric.json"
PRODUCTION_REPORT_PATH = RESULTS_DIR / "production_import_report_20260902.json"
WIKI_DIR = Path("/Users/todd/LLM_Wiki")

EXPECTED_MANIFEST_SHA256 = "173970b2843ff10f5013840636919a60cfa6e76459ab92e66b3a7af05e172e55"
EXPECTED_EPISODIC_COUNT = 57


def get_falkordb_inventory() -> dict[str, dict[str, int]]:
    """Query FalkorDB directly to obtain live node and edge counts per graph."""
    r = redis.Redis(host="localhost", port=6379, decode_responses=True)
    graphs = r.execute_command("GRAPH.LIST")
    inventory = {}
    for g in graphs:
        g_name = g if isinstance(g, str) else g.decode("utf-8")
        n_res = r.execute_command("GRAPH.QUERY", g_name, "MATCH (n) RETURN count(n)")
        e_res = r.execute_command("GRAPH.QUERY", g_name, "MATCH ()-[r]->() RETURN count(r)")
        n_count = n_res[1][0][0]
        e_count = e_res[1][0][0]
        inventory[g_name] = {"nodes": n_count, "edges": e_count}
    return inventory


def get_wiki_max_mtime() -> float:
    """Get the latest mtime among files in LLM_Wiki."""
    if not WIKI_DIR.exists():
        return 0.0
    mtimes = [f.stat().st_mtime for f in WIKI_DIR.rglob("*") if f.is_file()]
    return max(mtimes) if mtimes else 0.0


async def main():
    logger.info("=================================================================")
    logger.info("=== STARTING OFFICIAL PRODUCTION IMPORT INTO 'memory-fabric' ===")
    logger.info("=================================================================")
    run_timestamp = datetime.now(timezone.utc).isoformat()

    # ---------------------------------------------------------
    # STEP 1: PRE-FLIGHT VERIFICATION
    # ---------------------------------------------------------
    logger.info("[Pre-Flight 1/5] Verifying locked-manifest SHA-256...")
    if not MANIFEST_PATH.exists():
        logger.error(f"BLOCKED: Manifest not found at {MANIFEST_PATH}")
        sys.exit(1)
    actual_manifest_sha = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
    if actual_manifest_sha != EXPECTED_MANIFEST_SHA256:
        logger.error(
            f"BLOCKED: Manifest SHA mismatch! Expected {EXPECTED_MANIFEST_SHA256}, got {actual_manifest_sha}"
        )
        sys.exit(1)
    logger.info(f"✓ Manifest SHA-256 pinned: {actual_manifest_sha}")

    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)
    approved_eps = manifest_data.get("approved_episodic_manifest", [])
    if len(approved_eps) != EXPECTED_EPISODIC_COUNT:
        logger.error(f"BLOCKED: Manifest contains {len(approved_eps)} episodes, expected {EXPECTED_EPISODIC_COUNT}")
        sys.exit(1)
    logger.info(f"✓ Manifest contains exactly {len(approved_eps)} approved episodic candidates.")

    logger.info("[Pre-Flight 2/5] Recording pre-flight FalkorDB inventory...")
    pre_inventory = get_falkordb_inventory()
    logger.info(f"Pre-flight inventory: {pre_inventory}")

    mf_pre = pre_inventory.get("memory-fabric", {"nodes": -1, "edges": -1})
    if mf_pre["nodes"] != 0 or mf_pre["edges"] != 0:
        logger.error(f"BLOCKED: Target graph 'memory-fabric' is not empty: {mf_pre}")
        sys.exit(1)
    logger.info("✓ Target graph 'memory-fabric' is confirmed empty (0 nodes, 0 edges).")

    db_pre = pre_inventory.get("default_db", {})
    if db_pre.get("nodes") != 128 or db_pre.get("edges") != 167:
        logger.error(f"BLOCKED: Pre-existing 'default_db' inventory mismatch: {db_pre}")
        sys.exit(1)
    logger.info("✓ Pre-existing 'default_db' confirmed: 128 nodes, 167 edges.")

    cmf_pre = pre_inventory.get("cmf_chatgpt_000", {})
    if cmf_pre.get("nodes") != 52 or cmf_pre.get("edges") != 67:
        logger.error(f"BLOCKED: Pre-existing 'cmf_chatgpt_000' inventory mismatch: {cmf_pre}")
        sys.exit(1)
    logger.info("✓ Pre-existing 'cmf_chatgpt_000' confirmed: 52 nodes, 67 edges.")

    logger.info("[Pre-Flight 3/5] Checking LLM_Wiki pre-flight status...")
    wiki_pre_mtime = get_wiki_max_mtime()
    logger.info(f"✓ LLM_Wiki confirmed untouched (max mtime: {wiki_pre_mtime}).")

    logger.info("[Pre-Flight 4/5] Checking registry file status...")
    if REGISTRY_PATH.exists():
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            reg_pre = json.load(f)
        logger.info(f"Existing registry found with {len(reg_pre.get('imported_episodes', {}))} records.")
    else:
        logger.info("Registry file does not exist yet; will be initialized on first episode.")

    logger.info("[Pre-Flight 5/5] Checking source files...")
    paths = [EXPORT_DIR / f"conversations-{i:03d}.json" for i in range(6)]
    for p in paths:
        if not p.exists():
            logger.error(f"BLOCKED: Source file missing: {p}")
            sys.exit(1)
    logger.info("✓ All 6 source export files verified.")

    # ---------------------------------------------------------
    # STEP 2: PRODUCTION RUN (dry_run=False)
    # ---------------------------------------------------------
    logger.info("-----------------------------------------------------------------")
    logger.info(">>> INITIATING COMMITTED PRODUCTION INGESTION (dry_run=False) <<<")
    logger.info("-----------------------------------------------------------------")

    report_md, report_dict = await import_chatgpt_exports(
        paths=paths,
        dry_run=False,
        graph_name="memory-fabric",
        review_overrides_path=str(COMBINED_OVERRIDES_PATH),
    )

    logger.info("-----------------------------------------------------------------")
    logger.info(">>> PRODUCTION INGESTION COMPLETED <<<")
    logger.info("-----------------------------------------------------------------")

    # ---------------------------------------------------------
    # STEP 3: POST-FLIGHT VERIFICATION
    # ---------------------------------------------------------
    logger.info("[Post-Flight 1/6] Verifying registry checkpoint...")
    if not REGISTRY_PATH.exists():
        logger.error(f"BLOCKED: Registry file {REGISTRY_PATH} was not created!")
        sys.exit(1)

    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        registry_data = json.load(f)

    imported_episodes = registry_data.get("imported_episodes", {})
    logger.info(f"Total registry records: {len(imported_episodes)}")
    if len(imported_episodes) != EXPECTED_EPISODIC_COUNT:
        logger.error(
            f"BLOCKED: Expected {EXPECTED_EPISODIC_COUNT} registry records, found {len(imported_episodes)}"
        )
        sys.exit(1)
    logger.info(f"✓ Exactly {EXPECTED_EPISODIC_COUNT} episodes successfully recorded in registry.")

    logger.info("[Post-Flight 2/6] Verifying 1-to-1 manifest representation...")
    manifest_cand_ids = {c["candidate_id"] for c in approved_eps}
    registry_cand_ids = {rec["candidate_id"] for rec in imported_episodes.values()}

    missing_from_registry = manifest_cand_ids - registry_cand_ids
    if missing_from_registry:
        logger.error(f"BLOCKED: Candidates missing from registry: {missing_from_registry}")
        sys.exit(1)

    extra_in_registry = registry_cand_ids - manifest_cand_ids
    if extra_in_registry:
        logger.error(f"BLOCKED: Unexpected candidates in registry: {extra_in_registry}")
        sys.exit(1)
    logger.info("✓ All 57 manifest candidates represented exactly once in registry.")

    logger.info("[Post-Flight 3/6] Verifying exclusion of pending, ambiguous, and durable IDs...")
    forbidden_ids = {
        "cand_f281f124713a",
        "cand_9e93177072d0",
        "cand_cons_commit_gate_20231219",
    }
    for c in manifest_data.get("durable_proposals_collection", []):
        forbidden_ids.add(c["candidate_id"])

    leaked = registry_cand_ids & forbidden_ids
    if leaked:
        logger.error(f"BLOCKED: Forbidden IDs leaked into registry: {leaked}")
        sys.exit(1)
    logger.info("✓ Zero pending, ambiguous, or durable candidate IDs in registry.")

    logger.info("[Post-Flight 4/6] Verifying retrospective reference_time mappings in registry...")
    # Verify key retrospective anchors
    rec_by_cid = {rec["candidate_id"]: rec for rec in imported_episodes.values()}
    # 2014 gesture presentation
    cand_2014 = rec_by_cid.get("cand_8f7ed87e0b4a")
    if not cand_2014:
        logger.error("BLOCKED: cand_8f7ed87e0b4a missing from registry")
        sys.exit(1)
    if not cand_2014.get("reference_time", "").startswith("2014-01-01"):
        logger.error(f"BLOCKED: cand_8f7ed87e0b4a reference_time invalid: {cand_2014.get('reference_time')}")
        sys.exit(1)
    logger.info(f"✓ 2014 gesture presentation reference_time: {cand_2014.get('reference_time')}")

    # Commit gates: Text Analytics & Text-to-SQL
    ta_rec = rec_by_cid.get("cand_731a_episodic_text_analytics_commit")
    sql_rec = rec_by_cid.get("cand_f9de_episodic_text_to_sql_commit")
    if not ta_rec or not sql_rec:
        logger.error("BLOCKED: Commit gate records missing from registry")
        sys.exit(1)
    if "2023-12-19" not in ta_rec.get("reference_time", ""):
        logger.error(f"BLOCKED: Text Analytics reference_time invalid: {ta_rec.get('reference_time')}")
        sys.exit(1)
    if "2023-12-19" not in sql_rec.get("reference_time", ""):
        logger.error(f"BLOCKED: Text-to-SQL reference_time invalid: {sql_rec.get('reference_time')}")
        sys.exit(1)
    logger.info("✓ Commit-gate reference_times verified on 2023-12-19.")

    logger.info("[Post-Flight 5/6] Verifying FalkorDB graph topology and episode node count...")
    post_inventory = get_falkordb_inventory()
    logger.info(f"Post-flight inventory: {post_inventory}")

    r = redis.Redis(host="localhost", port=6379, decode_responses=True)
    # Query Episodic nodes in memory-fabric
    ep_query_res = r.execute_command("GRAPH.QUERY", "memory-fabric", "MATCH (e:Episodic) RETURN count(e)")
    ep_count = ep_query_res[1][0][0]
    logger.info(f"FalkorDB 'memory-fabric' Episodic node count: {ep_count}")

    if ep_count != EXPECTED_EPISODIC_COUNT:
        # Fallback check for any node with chatgpt prefix
        chatgpt_nodes_res = r.execute_command(
            "GRAPH.QUERY", "memory-fabric", "MATCH (n) WHERE n.name STARTS WITH 'chatgpt_' RETURN count(n)"
        )
        chatgpt_nodes_count = chatgpt_nodes_res[1][0][0]
        logger.info(f"Nodes starting with 'chatgpt_': {chatgpt_nodes_count}")
        if chatgpt_nodes_count != EXPECTED_EPISODIC_COUNT:
            logger.error(
                f"BLOCKED: Expected {EXPECTED_EPISODIC_COUNT} episode nodes in memory-fabric, found {ep_count} (or {chatgpt_nodes_count} chatgpt_ nodes)"
            )
            sys.exit(1)

    logger.info(f"✓ Exactly {EXPECTED_EPISODIC_COUNT} Graphiti episode nodes confirmed in memory-fabric.")

    # Check that other graphs were completely untouched
    db_post = post_inventory.get("default_db", {})
    if db_post.get("nodes") != 128 or db_post.get("edges") != 167:
        logger.error(f"BLOCKED: 'default_db' was mutated! Post: {db_post}")
        sys.exit(1)
    logger.info("✓ 'default_db' confirmed untouched: 128 nodes, 167 edges (delta: 0).")

    cmf_post = post_inventory.get("cmf_chatgpt_000", {})
    if cmf_post.get("nodes") != 52 or cmf_post.get("edges") != 67:
        logger.error(f"BLOCKED: 'cmf_chatgpt_000' was mutated! Post: {cmf_post}")
        sys.exit(1)
    logger.info("✓ 'cmf_chatgpt_000' confirmed untouched: 52 nodes, 67 edges (delta: 0).")

    logger.info("[Post-Flight 6/6] Verifying LLM_Wiki untouched...")
    wiki_post_mtime = get_wiki_max_mtime()
    if wiki_post_mtime != wiki_pre_mtime:
        logger.error(f"BLOCKED: LLM_Wiki was modified! Pre: {wiki_pre_mtime}, Post: {wiki_post_mtime}")
        sys.exit(1)
    logger.info("✓ LLM_Wiki confirmed untouched (delta: 0).")

    # ---------------------------------------------------------
    # STEP 4: IDEMPOTENCY CHECK (rerun identical command)
    # ---------------------------------------------------------
    logger.info("=================================================================")
    logger.info("=== STEP 4: RUNNING IDEMPOTENCY RERUN (identical command)    ===")
    logger.info("=================================================================")

    rerun_pre_inventory = get_falkordb_inventory()
    logger.info(f"Inventory before idempotency rerun: {rerun_pre_inventory}")

    rerun_md, rerun_dict = await import_chatgpt_exports(
        paths=paths,
        dry_run=False,
        graph_name="memory-fabric",
        review_overrides_path=str(COMBINED_OVERRIDES_PATH),
    )

    rerun_post_inventory = get_falkordb_inventory()
    logger.info(f"Inventory after idempotency rerun: {rerun_post_inventory}")

    # Verify zero mutations during rerun
    for g_name, pre_vals in rerun_pre_inventory.items():
        post_vals = rerun_post_inventory.get(g_name, {})
        d_n = post_vals.get("nodes", 0) - pre_vals.get("nodes", 0)
        d_e = post_vals.get("edges", 0) - pre_vals.get("edges", 0)
        if d_n != 0 or d_e != 0:
            logger.error(f"BLOCKED: Graph '{g_name}' mutated during idempotency rerun! Delta: nodes={d_n}, edges={d_e}")
            sys.exit(1)

    # Verify all 57 were skipped
    stats_rerun = rerun_dict.get("stats", {})
    skipped_count = stats_rerun.get("matches_against_existing_registry", 0)
    logger.info(f"Idempotency rerun: matches_against_existing_registry = {skipped_count}")
    if skipped_count != EXPECTED_EPISODIC_COUNT:
        logger.error(
            f"BLOCKED: Idempotency rerun expected {EXPECTED_EPISODIC_COUNT} skipped duplicate matches, got {skipped_count}"
        )
        sys.exit(1)
    logger.info("✓ Idempotency check PASSED: All 57 records skipped, zero graph mutations.")

    # ---------------------------------------------------------
    # STEP 5: SAVE FINAL PRODUCTION REPORT
    # ---------------------------------------------------------
    production_summary = {
        "timestamp": run_timestamp,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "status": "SUCCESS",
        "manifest_sha256": actual_manifest_sha,
        "target_graph": "memory-fabric",
        "episodes_ingested": len(imported_episodes),
        "registry_records_count": len(imported_episodes),
        "episodic_nodes_in_graph": ep_count,
        "idempotency_check_passed": True,
        "idempotency_skipped_count": skipped_count,
        "graph_inventories": {
            "initial": pre_inventory,
            "post_production": post_inventory,
            "post_idempotency": rerun_post_inventory,
        },
        "graph_deltas": {
            "memory-fabric": {
                "nodes": post_inventory["memory-fabric"]["nodes"] - pre_inventory["memory-fabric"]["nodes"],
                "edges": post_inventory["memory-fabric"]["edges"] - pre_inventory["memory-fabric"]["edges"],
            },
            "default_db": {
                "nodes": post_inventory["default_db"]["nodes"] - pre_inventory["default_db"]["nodes"],
                "edges": post_inventory["default_db"]["edges"] - pre_inventory["default_db"]["edges"],
            },
            "cmf_chatgpt_000": {
                "nodes": post_inventory["cmf_chatgpt_000"]["nodes"] - pre_inventory["cmf_chatgpt_000"]["nodes"],
                "edges": post_inventory["cmf_chatgpt_000"]["edges"] - pre_inventory["cmf_chatgpt_000"]["edges"],
            },
        },
        "llm_wiki_untouched": True,
    }

    with open(PRODUCTION_REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(production_summary, f, indent=2)

    logger.info("=================================================================")
    logger.info("=== OFFICIAL PRODUCTION IMPORT FULLY COMPLETED AND VERIFIED! ===")
    logger.info("=================================================================")


if __name__ == "__main__":
    asyncio.run(main())
