"""MS6b exit gate — live round-trip proof for explain_graph / correct_memory / delete_memory.

The unit tests in tests/test_ms6b_governance.py exercise these three
functions against a hand-rolled FakeDriver/FakeGraphiti — they prove the
logic (ledger updates, audit rows, dry-run no-ops) but never call real
Graphiti or FalkorDB. This script is the missing live check: it proves the
Cypher in graph_explain.py/correction.py actually matches what Graphiti
writes, and that remove_episode/add_episode behave the way correct_memory
assumes.

Deliberately isolated from production:
  - Runs against its own FalkorDB graph (--graph-name, default
    'cmf-ms6b-exit-gate') via create_graphiti(graph_name=...), which takes
    an explicit name over FALKORDB_DATABASE — so it never touches
    mem-fabric-local regardless of what .env points at.
  - Uses a scratch SQLite file (a tempfile, or --db) for PromotionStore /
    ReviewStore — never imports/journal/journal.db.
  - Never calls remember()/promote_reviewed — those always resolve the
    *configured* graph. This script calls graphiti.add_episode() directly
    (same call remember() makes) so no production code path is bypassed,
    just its graph-selection default.

Usage:
    uv run python scripts/ms6b_exit_gate.py [--graph-name NAME] [--cleanup]

Requires real LLM + embedding + FalkorDB credentials configured (same as
any `remember()` call) — this issues one real extraction call. Prints
PASS/FAIL per step; exits non-zero on the first failure.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile

from graphiti_core.nodes import EpisodeType

from server.consolidation.promotion import PromotionStore
from server.providers.memory_graphiti import EXTRACTION_INSTRUCTIONS, close_graphiti, create_graphiti
from server.review.correction import correct_memory, delete_memory
from server.review.graph_explain import explain_graph
from server.review.store import ReviewStore

MEMORY_ID = "ms6b-exit-gate-smoketest"
ORIGINAL_CONTENT = "MS6b exit gate: the original, soon-to-be-corrected statement."
CORRECTED_CONTENT = "MS6b exit gate: the corrected statement, re-issued by correct_memory."


def _ok(label: str) -> None:
    print(f"  PASS — {label}")


def _fail(label: str, detail: str) -> None:
    print(f"  FAIL — {label}: {detail}")
    sys.exit(1)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph-name", default="cmf-ms6b-exit-gate",
                         help="Scratch FalkorDB graph — never the production graph (default: %(default)s)")
    parser.add_argument("--db", type=Path, default=None,
                         help="Scratch journal.db path (default: a fresh tempfile, discarded after the run)")
    parser.add_argument("--cleanup", action="store_true",
                         help="Wipe the scratch graph's nodes when done (leaves the graph name itself)")
    args = parser.parse_args()

    if args.graph_name in ("mem-fabric-local", "mem-fabric-gemini", "mem-fabric-local-glm"):
        _fail("graph-name safety check", f"{args.graph_name!r} is a real production/rollback graph — refusing")

    tmp_dir = None
    db_path = args.db
    if db_path is None:
        tmp_dir = tempfile.TemporaryDirectory()
        db_path = Path(tmp_dir.name) / "ms6b_exit_gate.db"

    print(f"Scratch graph: {args.graph_name!r}   Scratch db: {db_path}")
    graphiti = create_graphiti(graph_name=args.graph_name)
    prom = PromotionStore(db_path=db_path)
    rev = ReviewStore(db_path=db_path)

    try:
        # ------------------------------------------------------------
        # Step 0 — seed: one real episode in the scratch graph, and a
        # PromotionStore row pointing at it (what promote_reviewed would
        # have written, without going through the full review pipeline).
        # ------------------------------------------------------------
        print("\n[0] Seeding one real episode into the scratch graph...")
        episode_name = "ms6b-exit-gate-001"
        ref_time = datetime.now(timezone.utc)
        await graphiti.add_episode(
            name=episode_name,
            episode_body=ORIGINAL_CONTENT,
            source_description="MS6b exit gate smoke test",
            reference_time=ref_time,
            source=EpisodeType.text,
            custom_extraction_instructions=EXTRACTION_INSTRUCTIONS,
        )
        prom.record_success(MEMORY_ID, episode_name, args.graph_name)
        _ok("episode added to scratch graph; PromotionStore seeded")

        # ------------------------------------------------------------
        # Step 1 — explain_graph() finds it for real.
        # ------------------------------------------------------------
        print("\n[1] explain_graph()...")
        result = await explain_graph(prom, MEMORY_ID, graphiti, graph_name=args.graph_name)
        if result is None:
            _fail("explain_graph", "returned None for a memory PromotionStore says is promoted")
        if not result["found_in_graph"]:
            _fail("explain_graph", "found_in_graph=False — episode not found in the graph right after add_episode")
        if result["episode_content"] != ORIGINAL_CONTENT:
            _fail("explain_graph", f"episode_content mismatch: {result['episode_content']!r}")
        _ok(f"found_in_graph=True, {result['entity_count']} entities, {result['edge_count']} edges")

        # ------------------------------------------------------------
        # Step 2 — correct_memory(): dry run must not touch the graph.
        # ------------------------------------------------------------
        print("\n[2] correct_memory() dry run...")
        dry = await correct_memory(
            prom, rev, MEMORY_ID, CORRECTED_CONTENT, graphiti,
            reviewer="exit-gate", reason="MS6b exit gate", graph_name=args.graph_name, dry_run=True,
        )
        if not dry.get("dry_run"):
            _fail("correct_memory dry run", f"expected dry_run=True, got {dry}")
        recheck = await explain_graph(prom, MEMORY_ID, graphiti, graph_name=args.graph_name)
        if recheck["episode_content"] != ORIGINAL_CONTENT:
            _fail("correct_memory dry run", "graph content changed despite dry_run=True")
        _ok("dry run reported the change, wrote nothing")

        # ------------------------------------------------------------
        # Step 3 — correct_memory(): apply, then verify via explain_graph.
        # ------------------------------------------------------------
        print("\n[3] correct_memory() --apply...")
        applied = await correct_memory(
            prom, rev, MEMORY_ID, CORRECTED_CONTENT, graphiti,
            reviewer="exit-gate", reason="MS6b exit gate", graph_name=args.graph_name, dry_run=False,
        )
        if applied.get("dry_run") is not False or "new_episode_name" not in applied:
            _fail("correct_memory apply", f"unexpected result: {applied}")
        post_correct = await explain_graph(prom, MEMORY_ID, graphiti, graph_name=args.graph_name)
        if post_correct["episode_content"] != CORRECTED_CONTENT:
            _fail("correct_memory apply", f"graph still shows old content: {post_correct['episode_content']!r}")
        if post_correct["episode_name"] != applied["new_episode_name"]:
            _fail("correct_memory apply", "PromotionStore did not follow the re-issued episode name")
        old_gone = await graphiti.driver.execute_query(
            "MATCH (e:Episodic {name: $name}) RETURN e.uuid AS uuid", name=applied["old_episode_name"]
        )
        if (old_gone[0] if old_gone and isinstance(old_gone[0], list) else old_gone):
            _fail("correct_memory apply", "old episode still present in the graph after remove_episode")
        _ok(f"old episode removed, new episode {applied['new_episode_name']!r} carries the corrected content")

        # ------------------------------------------------------------
        # Step 4 — delete_memory(): apply, then verify graph + ledger.
        # ------------------------------------------------------------
        print("\n[4] delete_memory() --apply...")
        deleted = await delete_memory(
            prom, rev, MEMORY_ID, graphiti,
            reviewer="exit-gate", reason="MS6b exit gate cleanup", graph_name=args.graph_name, dry_run=False,
        )
        if deleted.get("dry_run") is not False:
            _fail("delete_memory apply", f"unexpected result: {deleted}")
        if prom.is_promoted(MEMORY_ID, args.graph_name):
            _fail("delete_memory apply", "PromotionStore still reports this memory_id as promoted")
        gone = await graphiti.driver.execute_query(
            "MATCH (e:Episodic {name: $name}) RETURN e.uuid AS uuid", name=applied["new_episode_name"]
        )
        if (gone[0] if gone and isinstance(gone[0], list) else gone):
            _fail("delete_memory apply", "episode still present in the graph after remove_episode")
        _ok("episode removed from the graph, PromotionStore row cleared")

        # ------------------------------------------------------------
        # Step 5 — audit trail: two .note() rows, reviews table untouched.
        # ------------------------------------------------------------
        print("\n[5] Audit trail...")
        audit = rev.audit_for(MEMORY_ID)
        actions = [a["action"] for a in audit]
        if actions != ["correct_memory", "delete_memory"]:
            _fail("audit trail", f"expected [correct_memory, delete_memory], got {actions}")
        if rev.get(MEMORY_ID) is not None:
            _fail("audit trail", ".note() wrote to `reviews` — it must not touch verdict state")
        _ok("both actions audited via .note(); no review verdict written")

        print("\nALL STEPS PASSED.")

        if args.cleanup:
            print(f"\nCleaning up scratch graph {args.graph_name!r}...")
            await graphiti.driver.execute_query("MATCH (n) DETACH DELETE n")
            print("  done.")
        else:
            print(f"\nScratch graph {args.graph_name!r} left in place (pass --cleanup to wipe it).")

    finally:
        prom.close()
        rev.close()
        await close_graphiti()
        if tmp_dir is not None:
            tmp_dir.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
