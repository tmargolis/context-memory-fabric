"""Drop the `:Project` hub-node layer, replace it with `entity.project` (B08, 2026-10-13).

Decision: `:Project`/`IN_PROJECT` never earned their place as graph
structure -- retrieval (server/context.py, server/retrieval_expansion.py)
never read either, the hub nodes carried no property besides `{name}`, and
episodes already used a plain `e.project` string the whole time (only
entities used the edge+hub form). See docs/plan-active.md B08 and
scripts/tag_projects.py's module docstring for the full writeup.

This script performs the one-time cutover on an existing graph:
  1. Set `n.project` (sorted list) on every Entity, from its mentioning
     episodes' `e.project` -- the same computation `tag_projects.py` now
     does, pulled out here as a standalone migration step (because the
     DELETE below needs to run exactly once, not on every repair-pass
     re-run the way `tag_projects.py` is idempotent).
  2. Delete every `(:Project)` node and (what's left of) `[:IN_PROJECT]`.

Idempotent in the sense that re-running it on an already-migrated graph is a
no-op (step 1 recomputes the same property, step 2 finds nothing to delete),
but it is NOT meant to be part of the regular repair rotation the way
`tag_projects.py` is -- run `tag_projects.py` for ongoing backfills.

Safety, mirroring scripts/retag_promoted_projects.py:
  - dry run by default; --apply writes;
  - --rehearsal copies --source into a migration-rehearsal-* graph first;
  - a non-rehearsal graph needs --backup-graph or --make-backup;
  - Redis SAVE before and after the write.

    uv run python scripts/migrate_project_hubs_to_property.py --graph migration-rehearsal-20261013 --rehearsal --apply
    uv run python scripts/migrate_project_hubs_to_property.py --graph mem-fabric-local --make-backup --apply
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

STAMP = "pre-projecthub-migration-20261013"


def _redis():
    from server.core.falkordb_conn import redis_client
    return redis_client()


def _graph(name: str):
    from server.core.falkordb_conn import falkordb_client
    return falkordb_client().select_graph(name)


def counts(g) -> dict[str, int]:
    return {
        "nodes": g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
        "edges": g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
        "project_hubs": g.ro_query("MATCH (p:Project) RETURN count(p)").result_set[0][0],
        "in_project_edges": g.ro_query("MATCH ()-[r:IN_PROJECT]->() RETURN count(r)").result_set[0][0],
        "entities_with_project": g.ro_query("MATCH (n:Entity) WHERE n.project IS NOT NULL RETURN count(n)").result_set[0][0],
    }


def wait_bgsave(r) -> None:
    while r.info("persistence").get("rdb_bgsave_in_progress"):
        time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", required=True)
    ap.add_argument("--source", default="mem-fabric-local", help="graph a --rehearsal copy is made from")
    ap.add_argument("--rehearsal", action="store_true", help="GRAPH.COPY --source into --graph (must not exist)")
    ap.add_argument("--backup-graph", default=None)
    ap.add_argument("--make-backup", action="store_true", help=f"GRAPH.COPY --graph to <graph>.{STAMP} first")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    r = _redis()

    if args.rehearsal:
        if not args.graph.startswith("migration-rehearsal-"):
            raise SystemExit("--rehearsal graphs must be named migration-rehearsal-*")
        if r.exists(args.graph):
            raise SystemExit(f"ABORT: {args.graph} already exists")
        if args.apply:
            wait_bgsave(r)
            r.execute_command("GRAPH.COPY", args.source, args.graph)
            print(f"copied {args.source} -> {args.graph}")
        else:
            print(f"(dry run) would copy {args.source} -> {args.graph}")
            args.graph = args.source  # inspect the source read-only

    scratch = args.rehearsal or args.graph.startswith("migration-rehearsal-")
    g = _graph(args.graph)
    before = counts(g)
    print(f"{args.graph} before: {before}")

    if not args.apply:
        print("DRY RUN -- nothing written.")
        return 0

    if not scratch:
        backup = args.backup_graph
        if args.make_backup:
            backup = f"{args.graph}.{STAMP}"
            if r.exists(backup):
                raise SystemExit(f"ABORT: backup graph {backup} already exists")
            wait_bgsave(r)
            r.execute_command("GRAPH.COPY", args.graph, backup)
            print(f"backed up {args.graph} -> {backup}")
        if not backup:
            raise SystemExit("refusing: non-rehearsal migration needs --backup-graph or --make-backup")
        b = counts(_graph(backup))
        if (b["nodes"], b["edges"]) != (before["nodes"], before["edges"]):
            raise SystemExit(f"ABORT: backup {backup} counts {b} != target {before}")
        wait_bgsave(r)
        r.execute_command("SAVE")
        print("Redis SAVE done (pre-write)")

    tagged = g.query(
        "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) WHERE e.project IS NOT NULL "
        "WITH n, collect(DISTINCT e.project) AS projects "
        "SET n.project = projects RETURN count(n)"
    ).result_set[0][0]
    print(f"set entity.project on {tagged} entities")

    deleted_edges = g.query("MATCH ()-[r:IN_PROJECT]->() DELETE r RETURN count(r)").result_set[0][0]
    deleted_hubs = g.query("MATCH (p:Project) DELETE p RETURN count(p)").result_set[0][0]
    print(f"deleted {deleted_edges} IN_PROJECT edges, {deleted_hubs} Project hub nodes")

    after = counts(g)
    print(f"{args.graph} after:  {after}")
    ok = (
        after["project_hubs"] == 0
        and after["in_project_edges"] == 0
        and after["entities_with_project"] >= before["entities_with_project"]
        and after["nodes"] == before["nodes"] - deleted_hubs
    )
    if not scratch:
        wait_bgsave(r)
        r.execute_command("SAVE")
        print("Redis SAVE done (post-write)")
    print("VERIFY OK" if ok else "VERIFY FAILED")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
