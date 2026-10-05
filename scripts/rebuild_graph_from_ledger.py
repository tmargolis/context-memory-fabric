"""Rebuild a FalkorDB graph by replaying its promotions ledger.

Disaster recovery for the 2026-09-12 FalkorDB persistence incident (see
docs history / memory `cmf-falkordb-persistence-incident`): a container
recreate destroyed every graph, but `imports/journal/journal.db`'s
`promotions` + `derived_memories` tables recorded exactly what had been
written into each one. This replays a source graph's succeeded promotions
into a NEW target graph via the same `promote_reviewed` path production
promotion runs use (idempotent per (memory_id, graph_name), so it is safe
to re-run after an interruption).

Deliberately does NOT reuse the original episode names or write into the
source graph name again -- PromotionStore's idempotency key is
(memory_id, graph_name), so replaying into the same name reports every
row as already_promoted and produces an empty graph with a clean-looking
ledger.

--batch-size (added 2026-09-13, MS7b): chunks the replay and prints a
timestamped progress line after each chunk, so a long run (e.g. a slow
model against graphiti's real extraction pipeline) gives live visibility
instead of one report at the very end. Idempotency is unaffected --
promote_reviewed already skips anything already succeeded for
(memory_id, target_graph), per-batch or not, so re-running (or resuming
a run that used a different batch size) never re-promotes or duplicates
an episode.

Usage:
    python scripts/rebuild_graph_from_ledger.py \\
        --source-graph mem-fabric-local \\
        --target-graph mem-fabric-local-restore-20260912 \\
        --limit 10          # calibration batch; omit for the full run
        --batch-size 25      # progress line every 25 episodes

--project / --memory-ids-file (added 2026-09-28, MS4e): replay a fixed
subset instead of the whole ledger, so several extraction variants (set via
CMF_EXTRACTION_PROFILE / CMF_EPISODE_BODY_REASONING in the environment) can
be compared on exactly the same episodes. The file is one memory_id per
line; blank lines and `#` comments are ignored. Both filters keep the
ledger's promoted_at order.

FALKORDB_DATABASE in the environment is overridden for the duration of
this process -- it does not touch your .env file or a running MCP server.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-graph", required=True, help="Graph name the ledger's promotions.graph_name recorded, e.g. mem-fabric-local")
    parser.add_argument("--target-graph", required=True, help="New graph name to write into (must differ from --source-graph)")
    parser.add_argument("--limit", type=int, default=None, help="Cap on how many memory_ids to replay this run (omit for all)")
    parser.add_argument("--inter-call-delay", type=float, default=3.5, help="Seconds between remember() calls (default 3.5, matches production)")
    parser.add_argument("--project", default=None, help="Only replay episodes whose derived_memories.project matches (e.g. jspace)")
    parser.add_argument("--memory-ids-file", default=None, help="Only replay the memory_ids listed in this file, one per line")
    parser.add_argument("--batch-size", type=int, default=None, help="Print a timestamped progress line after every N episodes (omit for one report at the end)")
    return parser.parse_args()


def _read_memory_ids(path: str) -> set[str]:
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")}


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


async def _run(args: argparse.Namespace) -> int:
    if args.source_graph == args.target_graph:
        print("--source-graph and --target-graph must differ (idempotency key would collide).", file=sys.stderr)
        return 2

    # Must be set before the first Graphiti/FalkorDB call (server.providers.memory_graphiti
    # reads it lazily via os.getenv, so this is sufficient -- no need to touch .env).
    os.environ["FALKORDB_DATABASE"] = args.target_graph

    from server.consolidation.promotion import PromotionStore, format_promotion_report, promote_reviewed
    from server.consolidation.store import ConsolidationStore
    from server.journal.store import SqliteEventStore
    from server.memory import remember

    with ConsolidationStore() as consolidation_store, \
         SqliteEventStore() as journal_store, \
         PromotionStore() as promotion_store:

        rows = promotion_store._conn.execute(
            "SELECT memory_id FROM promotions WHERE graph_name = ? AND status = 'succeeded' "
            "ORDER BY promoted_at ASC",
            (args.source_graph,),
        ).fetchall()
        memory_ids = [r["memory_id"] for r in rows]

        if args.memory_ids_file:
            wanted = _read_memory_ids(args.memory_ids_file)
            missing = wanted - set(memory_ids)
            if missing:
                print(f"{len(missing)} memory_id(s) in {args.memory_ids_file} have no succeeded promotion "
                      f"into {args.source_graph!r}; e.g. {sorted(missing)[0]}", file=sys.stderr)
                return 2
            memory_ids = [m for m in memory_ids if m in wanted]

        if args.project:
            in_project = {
                r["memory_id"] for r in consolidation_store.get_derived_memories(memory_ids)
                if r["project"] == args.project
            }
            memory_ids = [m for m in memory_ids if m in in_project]

        if not memory_ids:
            print(f"No succeeded promotions found for graph_name={args.source_graph!r}.", file=sys.stderr)
            return 1

        if args.limit is not None:
            memory_ids = memory_ids[: args.limit]

        chunk_size = args.batch_size or len(memory_ids)
        from server.core.config import extraction_profile_from_env
        from server.consolidation.promotion import episode_body_includes_reasoning
        print(f"[{_ts()}] Extraction profile {extraction_profile_from_env()!r}, "
              f"Reasoning line in body: {episode_body_includes_reasoning()}", flush=True)
        print(f"[{_ts()}] Replaying {len(memory_ids)} episode(s) from {args.source_graph!r} into "
              f"{args.target_graph!r} (already-promoted ones are skipped automatically, "
              f"batch size {chunk_size})...", flush=True)

        started = time.monotonic()
        total_already, total_promoted, total_failed, any_stopped_early = 0, 0, 0, False

        for start in range(0, len(memory_ids), chunk_size):
            chunk = memory_ids[start:start + chunk_size]
            result = await promote_reviewed(
                consolidation_store=consolidation_store,
                journal_store=journal_store,
                promotion_store=promotion_store,
                remember_fn=remember,
                memory_ids=chunk,
                dry_run=False,
                graph_name=args.target_graph,
                inter_call_delay=args.inter_call_delay,
            )
            total_already += result["already_promoted"]
            total_promoted += len(result["promoted"])
            total_failed += len(result["failed"])
            any_stopped_early = any_stopped_early or result["stopped_early"]

            done = min(start + chunk_size, len(memory_ids))
            elapsed = time.monotonic() - started
            rate = elapsed / done if done else 0
            print(f"[{_ts()}] {done}/{len(memory_ids)} processed  "
                  f"(promoted {total_promoted}, already-promoted {total_already}, failed {total_failed})  "
                  f"{elapsed:.0f}s elapsed, {rate:.1f}s/episode, "
                  f"~{rate * (len(memory_ids) - done) / 60:.0f} min remaining", flush=True)

            if result["failed"]:
                for f in result["failed"]:
                    print(f"    FAILED {f['memory_id']}: {f['error']}", flush=True)
            if result["stopped_early"]:
                print(f"[{_ts()}] Stopped early (rate limiter) -- remaining episodes untouched, safe to resume "
                      f"by re-running this same command.", flush=True)
                break

        elapsed = time.monotonic() - started

    print(f"\n[{_ts()}] DONE. Promoted {total_promoted}, already-promoted {total_already}, "
          f"failed {total_failed}, out of {len(memory_ids)} requested.")
    print(f"Elapsed: {elapsed:.1f}s ({elapsed / max(len(memory_ids), 1):.1f}s/episode)")
    return 0 if not total_failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_parse_args())))
