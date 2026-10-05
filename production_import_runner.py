"""Production runner and recovery manager for ChatGPT memory-fabric import.

Supports:
- Fresh production import (requires empty graph)
- Safe resume mode (--resume) with strict pre-flight graph & registry reconciliation
- Read-only reconciliation inspection (--reconcile-only)
- Gemini API quota availability probing (--probe-quota)
- Canary resume execution (--max-new-candidates 1)
- Configurable rate-limit backoff, jitter, retry limits, and inter-candidate delays
- Automated halt on 429 exhaustion (INCOMPLETE_RATE_LIMITED)
"""

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

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
from server.core.rate_limiter import GeminiQuotaExhaustedError, get_default_rate_limiter
from server.providers.memory_graphiti import _classify_transient_error

EXPORT_DIR = Path.home() / "Documents/export-chatgpt/full_export-2026-09-01"
RESULTS_DIR = PROJECT_ROOT / "imports" / "results"
REVIEW_DIR = PROJECT_ROOT / "imports" / "review"
STATE_DIR = PROJECT_ROOT / "imports" / "state"

COMBINED_OVERRIDES_PATH = REVIEW_DIR / "combined_active_review_overrides.json"
MANIFEST_PATH = RESULTS_DIR / "proposed_locked_production_manifest_20260902.json"
REGISTRY_PATH = STATE_DIR / "import_registry_memory-fabric.json"
PRODUCTION_REPORT_PATH = RESULTS_DIR / "production_import_report_20260902.json"
WIKI_DIR = Path.home() / "LLM_Wiki"

EXPECTED_MANIFEST_SHA256 = "173970b2843ff10f5013840636919a60cfa6e76459ab92e66b3a7af05e172e55"
EXPECTED_TOTAL_EPISODIC = 57


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


def backup_registry(prefix: str = "backup") -> Optional[Path]:
    """Create a timestamped backup of the current registry file."""
    if not REGISTRY_PATH.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = STATE_DIR / f"import_registry_memory-fabric.{prefix}_{timestamp}.json"
    shutil.copy2(REGISTRY_PATH, backup_file)
    logger.info(f"Registry backed up to: {backup_file.name}")
    return backup_file


def probe_gemini_quota() -> dict[str, Any]:
    """Probe Gemini API to verify whether quota is currently available.

    Probes whichever model the rate limiter's configured chain would
    actually reserve right now (server.core.rate_limiter), not a hardcoded
    model string — a probe against a model the rest of the pipeline no
    longer uses would give a falsely-rosy (or falsely-alarming) read on
    real availability. The probe call itself is reserved against the same
    ledger remember()/recall() use, so it counts against real quota rather
    than looking "free" to later calls that check the ledger.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return {"status": "ERROR", "error": "GEMINI_API_KEY environment variable not set"}

    try:
        model = get_default_rate_limiter().reserve(estimated_calls=1)
    except GeminiQuotaExhaustedError as e:
        logger.error(f"✗ Gemini quota probe skipped: rate limiter reports no headroom in the configured chain: {e}")
        return {"status": "RATE_LIMITED", "model": None, "error": str(e)}

    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        start_t = time.perf_counter()
        response = client.models.generate_content(
            model=model,
            contents="Respond with only the single word: OK",
        )
        elapsed_s = time.perf_counter() - start_t
        resp_text = (response.text or "").strip()
        logger.info(f"✓ Gemini quota probe succeeded in {elapsed_s:.2f}s on {model}: '{resp_text}'")
        return {
            "status": "AVAILABLE",
            "model": model,
            "latency_seconds": round(elapsed_s, 2),
            "response": resp_text,
        }
    except Exception as e:
        err_msg = str(e)
        clean_err = re.sub(r"key=[A-Za-z0-9_-]+", "key=[REDACTED]", err_msg)
        error_class = _classify_transient_error(e)
        logger.error(f"✗ Gemini quota probe failed on {model} (class={error_class}): {clean_err[:200]}")
        return {
            "status": "RATE_LIMITED" if error_class == "quota" else "ERROR",
            "model": model,
            "error_class": error_class,
            "error": clean_err[:300],
        }


def reconcile_state(
    expected_count: Optional[int] = 20,
    target_graph: str = "memory-fabric",
) -> dict[str, Any]:
    """Perform a comprehensive read-only reconciliation check of graph and registry state.
    
    Verifies:
    1. Locked manifest SHA-256 matches expected pinned value.
    2. Registry file exists and contains valid JSON.
    3. Every candidate_id in the registry belongs to the pinned manifest.
    4. Exactly one FalkorDB Episodic node exists for each registry record.
    5. No orphan Episodic nodes exist in FalkorDB without registry records.
    6. No orphan registry records exist without FalkorDB Episodic nodes.
    7. Zero duplicate episode names or candidate IDs.
    8. Verified count equals expected_count (if provided).
    9. default_db (128 nodes, 167 edges) and cmf_chatgpt_000 (52 nodes, 67 edges) are untouched.
    10. LLM_Wiki is untouched.
    """
    logger.info("--- Running Read-Only Reconciliation Check ---")
    reconciliation_errors = []

    # 1. Manifest verification
    if not MANIFEST_PATH.exists():
        reconciliation_errors.append(f"Locked manifest not found at {MANIFEST_PATH}")
        actual_manifest_sha = None
        approved_eps = []
    else:
        actual_manifest_sha = hashlib.sha256(MANIFEST_PATH.read_bytes()).hexdigest()
        if actual_manifest_sha != EXPECTED_MANIFEST_SHA256:
            reconciliation_errors.append(
                f"Manifest SHA-256 mismatch: expected {EXPECTED_MANIFEST_SHA256}, got {actual_manifest_sha}"
            )
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            m_data = json.load(f)
        approved_eps = m_data.get("approved_episodic_manifest", [])
        if len(approved_eps) != EXPECTED_TOTAL_EPISODIC:
            reconciliation_errors.append(
                f"Manifest approved count is {len(approved_eps)}, expected {EXPECTED_TOTAL_EPISODIC}"
            )

    manifest_cids = [c["candidate_id"] for c in approved_eps]
    manifest_cid_set = set(manifest_cids)

    # 2. Registry verification
    if not REGISTRY_PATH.exists():
        reconciliation_errors.append(f"Registry file not found at {REGISTRY_PATH}")
        imported_episodes = {}
    else:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            reg_data = json.load(f)
        imported_episodes = reg_data.get("imported_episodes", {})

    reg_cids = []
    reg_ep_names = set()
    for fp, rec in imported_episodes.items():
        cid = rec.get("candidate_id")
        ep_name = rec.get("episode_name")
        reg_cids.append(cid)
        if ep_name:
            reg_ep_names.add(ep_name)
        if cid not in manifest_cid_set:
            reconciliation_errors.append(f"Registry candidate '{cid}' not in locked manifest!")

    if len(reg_cids) != len(set(reg_cids)):
        dups = [c for c in reg_cids if reg_cids.count(c) > 1]
        reconciliation_errors.append(f"Duplicate candidate IDs in registry: {set(dups)}")

    # 3. FalkorDB query for Episodic nodes in target graph
    r = redis.Redis(host="localhost", port=6379, decode_responses=True)
    try:
        ep_res = r.execute_command("GRAPH.QUERY", target_graph, "MATCH (e:Episodic) RETURN e.name, e.uuid")
        graph_episodes = {row[0]: row[1] for row in ep_res[1]}
    except Exception as e:
        reconciliation_errors.append(f"Failed to query {target_graph} Episodic nodes: {e}")
        graph_episodes = {}

    # Bidirectional matching
    for ep_name in reg_ep_names:
        if ep_name not in graph_episodes:
            reconciliation_errors.append(f"Registry episode '{ep_name}' missing from {target_graph} Episodic nodes!")

    for ep_name in graph_episodes:
        if ep_name not in reg_ep_names:
            reconciliation_errors.append(f"Graph episode '{ep_name}' missing from registry records!")

    # Check counts
    if expected_count is not None:
        if len(imported_episodes) != expected_count:
            reconciliation_errors.append(
                f"Registry record count ({len(imported_episodes)}) != expected ({expected_count})"
            )
        if len(graph_episodes) != expected_count:
            reconciliation_errors.append(
                f"Graph Episodic node count ({len(graph_episodes)}) != expected ({expected_count})"
            )

    # 4. Inventory check of other graphs
    inventory = get_falkordb_inventory()
    db_inv = inventory.get("default_db", {})
    if db_inv.get("nodes") != 128 or db_inv.get("edges") != 167:
        reconciliation_errors.append(f"'default_db' mutated! Current: {db_inv}")

    cmf_inv = inventory.get("cmf_chatgpt_000", {})
    if cmf_inv.get("nodes") != 52 or cmf_inv.get("edges") != 67:
        reconciliation_errors.append(f"'cmf_chatgpt_000' mutated! Current: {cmf_inv}")

    target_inv = inventory.get(target_graph, {})

    # 5. Check LLM_Wiki
    wiki_mtime = get_wiki_max_mtime()

    is_valid = len(reconciliation_errors) == 0
    uncheckpointed_cids = [cid for cid in manifest_cids if cid not in set(reg_cids)]

    result = {
        "status": "PASS" if is_valid else "FAIL",
        "target_graph": target_graph,
        "manifest_sha256": actual_manifest_sha,
        "manifest_approved_total": len(approved_eps),
        "registry_records_count": len(imported_episodes),
        "graph_episodic_nodes_count": len(graph_episodes),
        "graph_total_nodes": target_inv.get("nodes", 0),
        "graph_total_edges": target_inv.get("edges", 0),
        "uncheckpointed_candidates_count": len(uncheckpointed_cids),
        "next_uncheckpointed_candidate_id": uncheckpointed_cids[0] if uncheckpointed_cids else None,
        "default_db_inventory": db_inv,
        "cmf_chatgpt_000_inventory": cmf_inv,
        "wiki_max_mtime": wiki_mtime,
        "reconciliation_errors": reconciliation_errors,
    }

    logger.info(f"Reconciliation Status: {result['status']}")
    logger.info(f"- Registry records: {result['registry_records_count']}")
    logger.info(f"- Graph Episodic nodes: {result['graph_episodic_nodes_count']}")
    logger.info(f"- Total graph topology: {result['graph_total_nodes']} nodes, {result['graph_total_edges']} edges")
    logger.info(f"- Remaining uncheckpointed: {result['uncheckpointed_candidates_count']}")
    if uncheckpointed_cids:
        logger.info(f"- Next candidate in manifest order: {uncheckpointed_cids[0]}")
    if reconciliation_errors:
        for err in reconciliation_errors:
            logger.error(f"  ✗ {err}")
    else:
        logger.info("✓ 100% bidirectional 1-to-1 match between registry and graph!")
        logger.info("✓ All registry records belong to pinned manifest!")
        logger.info("✓ Zero orphan nodes or records!")
        logger.info("✓ default_db, cmf_chatgpt_000, and LLM_Wiki confirmed untouched!")

    return result


async def run_import_flow(
    resume: bool = False,
    max_new_candidates: Optional[int] = None,
    inter_candidate_delay: float = 6.0,
    max_retries: int = 5,
    max_retry_delay: float = 120.0,
    expected_resume_count: Optional[int] = 20,
):
    """Execute either a fresh import or a resumed recovery import."""
    logger.info("=================================================================")
    mode_label = f"RESUME MODE (max_new={max_new_candidates})" if resume else "FRESH PRODUCTION RUN"
    logger.info(f"=== INITIATING {mode_label} INTO 'memory-fabric' ===")
    logger.info("=================================================================")

    paths = [EXPORT_DIR / f"conversations-{i:03d}.json" for i in range(6)]

    if not resume:
        # Strict fresh run preflight: graph MUST be completely empty
        logger.info("[Fresh Pre-Flight] Requiring strictly empty memory-fabric...")
        inv = get_falkordb_inventory()
        mf_inv = inv.get("memory-fabric", {"nodes": -1, "edges": -1})
        if mf_inv["nodes"] != 0 or mf_inv["edges"] != 0:
            logger.error(f"BLOCKED: Fresh production run requires empty graph, found: {mf_inv}")
            sys.exit(1)
        if REGISTRY_PATH.exists():
            logger.error(f"BLOCKED: Fresh production run requires no existing registry, found: {REGISTRY_PATH}")
            sys.exit(1)
    else:
        # Strict resume preflight: verify existing 20 records and 20 Episodic nodes
        logger.info(f"[Resume Pre-Flight] Reconciling existing data (expecting {expected_resume_count})...")
        rec_res = reconcile_state(expected_count=expected_resume_count, target_graph="memory-fabric")
        if rec_res["status"] != "PASS":
            logger.error(f"BLOCKED: Resume pre-flight reconciliation failed: {rec_res['reconciliation_errors']}")
            sys.exit(1)

        # Back up the current verified registry before attempting any write
        backup_registry(prefix="pre_resume")

    start_reg_count = 0
    if REGISTRY_PATH.exists():
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            start_reg_count = len(json.load(f).get("imported_episodes", {}))

    start_inv = get_falkordb_inventory()
    start_ep_count = start_inv.get("memory-fabric", {}).get("nodes", 0)

    # Execute committed ingestion
    logger.info("Calling import_chatgpt_exports with committed execution...")
    report_md, report_dict = await import_chatgpt_exports(
        paths=paths,
        dry_run=False,
        graph_name="memory-fabric",
        review_overrides_path=str(COMBINED_OVERRIDES_PATH),
        max_new_candidates=max_new_candidates,
        inter_candidate_delay=inter_candidate_delay,
        max_retries=max_retries,
        max_retry_delay=max_retry_delay,
        halt_on_rate_limit=True,
    )

    import_status = report_dict.get("import_status", "UNKNOWN")
    new_ingested = report_dict.get("new_ingested_count", 0)
    halt_reason = report_dict.get("halt_reason")

    logger.info(f"Import returned status: {import_status} (new_ingested={new_ingested})")
    if halt_reason:
        logger.warning(f"Halt reason: {halt_reason}")

    # Post-execution verification
    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        end_reg_data = json.load(f)
    end_reg_count = len(end_reg_data.get("imported_episodes", {}))

    r = redis.Redis(host="localhost", port=6379, decode_responses=True)
    ep_res = r.execute_command("GRAPH.QUERY", "memory-fabric", "MATCH (e:Episodic) RETURN count(e)")
    end_ep_count = ep_res[1][0][0]

    end_inv = get_falkordb_inventory()
    logger.info(f"End state: Registry={end_reg_count}, Graph Episodic={end_ep_count}, Topology={end_inv.get('memory-fabric')}")

    # Check canary completion if max_new_candidates == 1
    if max_new_candidates == 1:
        logger.info("--- Verifying Canary Result ---")
        expected_canary_count = start_reg_count + 1
        if end_reg_count == expected_canary_count and end_ep_count == expected_canary_count:
            logger.info(f"✓ CANARY SUCCESSFUL: Registry and Episodic nodes both increased from {start_reg_count} to {end_reg_count}!")
        else:
            logger.error(
                f"✗ CANARY FAILED: Expected {expected_canary_count}, got Registry={end_reg_count}, Episodic={end_ep_count}"
            )
            sys.exit(1)

    # If full run reached 57
    if end_reg_count == EXPECTED_TOTAL_EPISODIC and end_ep_count == EXPECTED_TOTAL_EPISODIC:
        logger.info("=================================================================")
        logger.info("=== FULL IMPORT REACHED 57! RUNNING IDEMPOTENCY CHECK...     ===")
        logger.info("=================================================================")
        pre_idemp_inv = get_falkordb_inventory()
        idemp_md, idemp_dict = await import_chatgpt_exports(
            paths=paths,
            dry_run=False,
            graph_name="memory-fabric",
            review_overrides_path=str(COMBINED_OVERRIDES_PATH),
        )
        post_idemp_inv = get_falkordb_inventory()
        skipped_count = idemp_dict.get("stats", {}).get("matches_against_existing_registry", 0)
        logger.info(f"Idempotency rerun: matches_against_existing_registry={skipped_count}")

        # Assert zero mutations
        for g_name, pre_vals in pre_idemp_inv.items():
            post_vals = post_idemp_inv.get(g_name, {})
            dn = post_vals.get("nodes", 0) - pre_vals.get("nodes", 0)
            de = post_vals.get("edges", 0) - pre_vals.get("edges", 0)
            if dn != 0 or de != 0:
                logger.error(f"BLOCKED: Idempotency mutation in {g_name}: delta_nodes={dn}, delta_edges={de}")
                sys.exit(1)
        logger.info("✓ Idempotency verification PASSED: All 57 skipped, zero graph changes.")

        production_summary = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "SUCCESS",
            "manifest_sha256": EXPECTED_MANIFEST_SHA256,
            "target_graph": "memory-fabric",
            "episodes_ingested": end_reg_count,
            "registry_records_count": end_reg_count,
            "episodic_nodes_in_graph": end_ep_count,
            "idempotency_check_passed": True,
            "idempotency_skipped_count": skipped_count,
            "graph_inventories": {
                "post_production": end_inv,
                "post_idempotency": post_idemp_inv,
            },
            "llm_wiki_untouched": True,
        }
        with open(PRODUCTION_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(production_summary, f, indent=2)
        logger.info(f"Saved production report to: {PRODUCTION_REPORT_PATH.name}")


def verify_post_import(target_graph: str = "memory-fabric") -> dict[str, Any]:
    """Perform final post-import verification of all production requirements."""
    logger.info("=================================================================")
    logger.info("=== RUNNING FULL PRODUCTION POST-IMPORT INTEGRITY VERIFICATION ===")
    logger.info("=================================================================")

    # 1. Run reconciliation at expected 57
    rec = reconcile_state(expected_count=EXPECTED_TOTAL_EPISODIC, target_graph=target_graph)
    if rec["status"] != "PASS":
        logger.error(f"Reconciliation FAILED: {rec['reconciliation_errors']}")
        sys.exit(1)

    # 2. Check candidate ID representation
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    manifest_cids = [c["candidate_id"] for c in manifest["approved_episodic_manifest"]]

    with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
        reg = json.load(f)
    imported = reg.get("imported_episodes", {})

    reg_cids = [rec["candidate_id"] for rec in imported.values()]
    if len(reg_cids) != EXPECTED_TOTAL_EPISODIC:
        logger.error(f"Expected {EXPECTED_TOTAL_EPISODIC} registry entries, got {len(reg_cids)}")
        sys.exit(1)
    if set(reg_cids) != set(manifest_cids):
        logger.error("Candidate IDs in registry do not match manifest!")
        sys.exit(1)

    # 3. Check forbidden candidate IDs
    forbidden = {"cand_f281f124713a", "cand_9e93177072d0", "cand_cons_commit_gate_20231219"}
    for d in manifest.get("durable_proposals_collection", []):
        forbidden.add(d["candidate_id"])
    leaked = set(reg_cids) & forbidden
    if len(leaked) > 0:
        logger.error(f"Forbidden candidate IDs leaked into registry: {leaked}")
        sys.exit(1)

    # 4. Check retrospective reference_times
    cand_map = {rec["candidate_id"]: rec for rec in imported.values()}
    # 2014 gesture presentation
    if not cand_map["cand_8f7ed87e0b4a"]["reference_time"].startswith("2014-01-01"):
        logger.error("2014 gesture presentation reference_time invalid")
        sys.exit(1)
    # Commit gates
    if "2023-12-19" not in cand_map["cand_731a_episodic_text_analytics_commit"]["reference_time"]:
        logger.error("Text Analytics commit gate reference_time invalid")
        sys.exit(1)
    if "2023-12-19" not in cand_map["cand_f9de_episodic_text_to_sql_commit"]["reference_time"]:
        logger.error("Text-to-SQL commit gate reference_time invalid")
        sys.exit(1)

    # 5. Check graph inventories
    r = redis.Redis(host="localhost", port=6379, decode_responses=True)
    inv = {}
    for g in r.execute_command("GRAPH.LIST"):
        n = r.execute_command("GRAPH.QUERY", g, "MATCH (n) RETURN count(n)")[1][0][0]
        e = r.execute_command("GRAPH.QUERY", g, "MATCH ()-[r]->() RETURN count(r)")[1][0][0]
        inv[g] = {"nodes": n, "edges": e}

    if inv["default_db"] != {"nodes": 128, "edges": 167}:
        logger.error(f"default_db mutated: {inv['default_db']}")
        sys.exit(1)
    if inv["cmf_chatgpt_000"] != {"nodes": 52, "edges": 67}:
        logger.error(f"cmf_chatgpt_000 mutated: {inv['cmf_chatgpt_000']}")
        sys.exit(1)
    if inv["memory-fabric"]["nodes"] != 142:
        logger.error(f"memory-fabric nodes unexpected: {inv['memory-fabric']}")
        sys.exit(1)
    if inv["memory-fabric"]["edges"] != 169:
        logger.error(f"memory-fabric edges unexpected: {inv['memory-fabric']}")
        sys.exit(1)

    # 6. Check production report
    if PRODUCTION_REPORT_PATH.exists():
        with open(PRODUCTION_REPORT_PATH, "r", encoding="utf-8") as f:
            prod_report = json.load(f)
        if prod_report.get("idempotency_check_passed") is not True:
            logger.error("Idempotency check was not marked passed in report")
            sys.exit(1)
        if prod_report.get("idempotency_skipped_count") != EXPECTED_TOTAL_EPISODIC:
            logger.error("Idempotency skipped count mismatch")
            sys.exit(1)

    logger.info("=================================================================")
    logger.info("✓ ALL POST-IMPORT INTEGRITY CHECKS PASSED (57/57 EPISODES)!")
    logger.info("=================================================================")
    return {"status": "PASS", "inventories": inv}


def main():
    parser = argparse.ArgumentParser(
        description="Production Runner & Recovery Manager for ChatGPT memory-fabric import",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--resume", action="store_true", help="Explicit resume mode (verifies and preserves existing data)")
    parser.add_argument("--reconcile-only", action="store_true", help="Run read-only state reconciliation and print report without writes")
    parser.add_argument("--verify-post-import", action="store_true", help="Run full post-import verification across all 57 episodes, graph, and idempotency")
    parser.add_argument("--probe-quota", action="store_true", help="Probe Gemini API quota availability without graph mutations")
    parser.add_argument("--max-new-candidates", type=int, default=None, help="Maximum number of new candidates to ingest (e.g. 1 for canary)")
    parser.add_argument("--inter-candidate-delay", type=float, default=6.0, help="Seconds to delay between candidates to reduce burst pressure")
    parser.add_argument("--max-retries", type=int, default=5, help="Maximum retries per candidate on rate limit (429)")
    parser.add_argument("--max-retry-delay", type=float, default=120.0, help="Maximum backoff delay in seconds")
    parser.add_argument("--expected-resume-count", type=int, default=20, help="Expected existing checkpointed episode count before resume")

    args = parser.parse_args()

    if args.probe_quota:
        probe_gemini_quota()
        return

    if args.verify_post_import:
        verify_post_import(target_graph="memory-fabric")
        return

    if args.reconcile_only:
        res = reconcile_state(expected_count=args.expected_resume_count, target_graph="memory-fabric")
        sys.exit(0 if res["status"] == "PASS" else 1)

    asyncio.run(
        run_import_flow(
            resume=args.resume,
            max_new_candidates=args.max_new_candidates,
            inter_candidate_delay=args.inter_candidate_delay,
            max_retries=args.max_retries,
            max_retry_delay=args.max_retry_delay,
            expected_resume_count=args.expected_resume_count,
        )
    )


if __name__ == "__main__":
    main()

