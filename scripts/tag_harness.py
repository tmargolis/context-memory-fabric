"""Tag a FalkorDB graph's Episodic nodes with a harness label.

Companion to scripts/tag_projects.py, same rationale: `:Episodic` nodes get
a second (third, counting `:Episodic` itself) label naming the harness that
produced them (`Gemini`, `ChatGPT`, `Claude`, `Claude_Code`, ...) -- a
friendly per-harness colour/filter in the FalkorDB Browser, same idea as
the project label.

Found missing entirely for claude_code (2026-09-23, DelayedVideoTablet
review): gemini-*/chatgpt-*/claude-* (non-code) episodes are 100% labeled
(191/191, 150/150, 124/124), but claude_code-* episodes were 0/79. There is
no prior committed script that ever set these -- whatever tagged the other
three harnesses predates the claude_code adapter (MS4b, 2026-09-18) and was
apparently a one-off, not a repeatable pass. This script is that missing
repeatable pass, and (as of the same day) `server/consolidation/promotion.py`
also applies it automatically per-episode at promotion time (see
server/consolidation/graph_tagging.py), so this script is now only needed
for a backfill/repair pass over episodes promoted before that was wired in,
or after a graph rebuild (scripts/rebuild_graph_from_ledger.py).

Source of truth is the same as tag_projects.py: the journal ledger
(imports/journal/journal.db) -- promotions.graph_name/episode_name joined
through derived_memories.source_event_id to events.harness -- not anything
already in FalkorDB.

Idempotent: only ever adds a label (SET e:`Label`), never removes one, so
re-running is always safe and a partially-tagged graph converges to
fully-tagged on the next run.

Usage:
    python scripts/tag_harness.py --graph mem-fabric-local
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from server.consolidation.graph_tagging import harness_label_for


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph", required=True, help="FalkorDB graph name to tag, e.g. mem-fabric-local")
    parser.add_argument(
        "--ledger-graph",
        default=None,
        help="graph_name to read the ledger under, if different from --graph (see scripts/tag_projects.py's module docstring for why this can happen).",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    from server.consolidation.promotion import PromotionStore

    falkor_host = os.getenv("FALKORDB_HOST", "localhost")
    falkor_port = int(os.getenv("FALKORDB_PORT", "6379"))
    falkor_password = os.getenv("FALKORDB_PASSWORD") or None

    ledger_graph = args.ledger_graph or args.graph
    driver = FalkorDriver(host=falkor_host, port=falkor_port, password=falkor_password, database=args.graph)

    with PromotionStore() as promotion_store:
        rows = promotion_store._conn.execute(
            "SELECT p.episode_name AS episode_name, e.harness AS harness "
            "FROM promotions p "
            "JOIN derived_memories d ON d.memory_id = p.memory_id "
            "JOIN events e ON e.event_id = d.source_event_id "
            "WHERE p.graph_name = ? AND p.status = 'succeeded'",
            (ledger_graph,),
        ).fetchall()

    if not rows:
        print(f"No succeeded promotions found for graph_name={ledger_graph!r}.", file=sys.stderr)
        return 1

    print(f"Tagging harness label on up to {len(rows)} episode(s) in {args.graph!r}...")
    tagged = 0
    skipped_no_label = 0
    missing: list[str] = []
    by_label: dict[str, int] = {}
    for row in rows:
        label = harness_label_for(row["harness"])
        if not label:
            skipped_no_label += 1
            continue
        cypher = f"MATCH (e:Episodic {{name: $name}}) SET e:`{label}` RETURN e.uuid AS uuid"
        result = await driver.execute_query(cypher, name=row["episode_name"])
        recs = result[0] if result and isinstance(result[0], list) else (result or [])
        if not recs:
            missing.append(row["episode_name"])
        else:
            tagged += 1
            by_label[label] = by_label.get(label, 0) + 1

    print(f"Tagged {tagged}/{len(rows)} episodes.")
    if skipped_no_label:
        print(f"Skipped {skipped_no_label} episode(s) with no resolvable harness label.", file=sys.stderr)
    for label, count in sorted(by_label.items(), key=lambda kv: -kv[1]):
        print(f"  {label}: {count}")
    if missing:
        print(f"\n{len(missing)} episode(s) not found in the graph (ledger says promoted, graph disagrees):", file=sys.stderr)
        for name in missing[:20]:
            print(f"  - {name}", file=sys.stderr)
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more", file=sys.stderr)

    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_parse_args())))
