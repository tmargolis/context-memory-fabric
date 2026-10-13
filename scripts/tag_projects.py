"""Tag a FalkorDB graph's Episodic/Entity nodes with their project.

CMF-local-only post-promotion pass -- Graphiti never reads any of this (see
docs/FALKORDB-QUERIES.md Section H2). Recreating a graph, whether by a fresh
promotion run or a ledger replay (scripts/rebuild_graph_from_ledger.py),
only ever writes `:Episodic` and `:Entity` nodes, so this must be re-run
after either one.

As of 2026-09-23, `server/consolidation/promotion.py` also tags each
episode's project (and harness label -- see scripts/tag_harness.py)
automatically, inline, right after every successful promotion (see
server/consolidation/graph_tagging.py) -- so day-to-day review-and-promote
no longer depends on remembering to run this script. This full-graph pass
is still the right tool for a repair/backfill: a fresh graph rebuild, a
`derived_memories.project` value corrected retroactively, or anything
promoted before the inline tagging was wired in.

Per episode, this sets:
  - `e.project`          plain string property, the raw project id exactly
                          as recorded in derived_memories.project (e.g.
                          "my-project"). For exact-match filters.
  - a direct label        a friendly, Cypher-safe slug -- underscores, no
                          dashes -- with NO "ep_" prefix. DISPLAY_OVERRIDES
                          below renames specific project ids to a nicer
                          label (a deployment-specific map in
                          server/consolidation/graph_tagging.py, empty by
                          default -- add an entry to give a project id a
                          different label).

Per entity: `n.project`, a plain list of the distinct raw project ids among
the episodes that `[:MENTIONS]` it (an entity can legitimately belong to
more than one project, unlike an episode). Replaced the `(:Project {name})`
hub-node-and-`[:IN_PROJECT]`-edge layer on 2026-10-13: retrieval never read
it (`server/context.py` / `server/retrieval_expansion.py` match only typed
`MENTIONS`/`RELATES_TO`), it covered only entities (episodes always used
the plain property above), and a property is the cheaper filter
(`WHERE 'x' IN n.project`, no traversal) if project ever becomes a ranking
signal. No orphan-episode fallback is needed either way: an episode with
zero extracted entities still has its own `e.project` property, so it was
never actually disconnected from "its project" -- only from the hub *node*,
which no longer exists to be disconnected from.

Source of truth for which episode belongs to which project is the journal
ledger (imports/journal/journal.db): promotions.graph_name/episode_name
joined to derived_memories.project -- not anything already in FalkorDB.
Entity project membership is then derived from the graph's own MENTIONS
edges plus the e.project property this script just set, not the ledger.

Idempotent and self-healing: every run recomputes every entity's `project`
list from scratch (cleared first, not unioned forward), so it stays correct
even if an episode's entity count changes between runs (e.g. re-extraction)
or an episode's project was corrected retroactively.

Usage:
    python scripts/tag_projects.py --graph mem-fabric-local

If the target graph was ever rebuilt from the ledger and then renamed (see
scripts/rebuild_graph_from_ledger.py), the ledger's promotions.graph_name
still says the *old* pre-rename name (e.g. "mem-fabric-local-restore-
20260912"), not the name the graph now has in FalkorDB -- a redis-level
RENAME doesn't touch this SQLite ledger. Pass --ledger-graph to read
promotions/episode names from that old name while still writing into
--graph. Getting this wrong doesn't corrupt anything -- MATCH on a
nonexistent episode_name just tags nothing -- but it silently leaves
renamed/retried episodes (renumbered `<harness>-<project>-NNN` on replay)
untagged. Confirm which name is authoritative with:
    sqlite3 imports/journal/journal.db \\
      "SELECT graph_name, count(*) FROM promotions WHERE status='succeeded' GROUP BY graph_name;"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from server.consolidation.graph_tagging import project_label_for as _label_for


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--graph", required=True, help="FalkorDB graph name to tag, e.g. mem-fabric-local")
    parser.add_argument(
        "--ledger-graph",
        default=None,
        help="graph_name to read the ledger under, if different from --graph (see module docstring). Defaults to --graph.",
    )
    return parser.parse_args()


async def _tag_episodes(driver, rows) -> tuple[int, list[str]]:
    """Set e.project + the project label on each Episodic node. Returns (tagged_count, missing_episode_names)."""
    tagged = 0
    missing: list[str] = []
    for row in rows:
        episode_name = row["episode_name"]
        project = row["project"]
        label = _label_for(project)

        # Cypher can't parameterize a label name; `label` only ever comes
        # from _label_for()'s [a-z0-9_]+ output above, so string-building
        # it into the query is safe.
        cypher = f"MATCH (e:Episodic {{name: $name}}) SET e.project = $project, e:`{label}` RETURN e.uuid AS uuid"
        result = await driver.execute_query(cypher, name=episode_name, project=project)
        recs = result[0] if result and isinstance(result[0], list) else (result or [])
        if not recs:
            missing.append(episode_name)
        else:
            tagged += 1
    return tagged, missing


async def _reset_entity_projects(driver) -> int:
    """Clear every Entity.project so the recompute below starts from scratch."""
    result = await driver.execute_query(
        "MATCH (n:Entity) WHERE n.project IS NOT NULL REMOVE n.project RETURN count(n) AS n"
    )
    recs = result[0] if result and isinstance(result[0], list) else (result or [])
    return recs[0]["n"] if recs else 0


async def _link_entities_to_projects(driver) -> dict[str, int]:
    """Each entity's `project` becomes the sorted list of distinct raw project ids
    among the episodes that `[:MENTIONS]` it.

    Reads e.project directly off the graph (set by _tag_episodes above), not the
    ledger -- this only needs to know what's actually mentioned, not what was promoted.
    """
    rows_result = await driver.execute_query(
        "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) WHERE e.project IS NOT NULL "
        "WITH n, collect(DISTINCT e.project) AS projects "
        "SET n.project = projects "
        "RETURN n.uuid AS entity_uuid, projects"
    )
    rows = rows_result[0] if rows_result and isinstance(rows_result[0], list) else (rows_result or [])

    by_project: dict[str, int] = {}
    for row in rows:
        for project in row["projects"]:
            label = _label_for(project)
            by_project[label] = by_project.get(label, 0) + 1
    return by_project


async def _run(args: argparse.Namespace) -> int:
    from graphiti_core.driver.falkordb_driver import FalkorDriver

    from server.consolidation.promotion import PromotionStore


    ledger_graph = args.ledger_graph or args.graph
    from server.core.falkordb_conn import connection_params
    driver = FalkorDriver(**connection_params(), database=args.graph)

    with PromotionStore() as promotion_store:
        rows = promotion_store._conn.execute(
            "SELECT p.episode_name AS episode_name, d.project AS project "
            "FROM promotions p JOIN derived_memories d ON d.memory_id = p.memory_id "
            "WHERE p.graph_name = ? AND p.status = 'succeeded' AND d.project IS NOT NULL",
            (ledger_graph,),
        ).fetchall()

    if not rows:
        print(f"No succeeded promotions with a project found for graph_name={ledger_graph!r}.", file=sys.stderr)
        return 1

    print(f"Tagging {len(rows)} episode(s) in {args.graph!r}...")
    tagged, missing = await _tag_episodes(driver, rows)
    print(f"Tagged {tagged}/{len(rows)} episodes.")
    if missing:
        print(f"\n{len(missing)} episode(s) not found in the graph (ledger says promoted, graph disagrees):", file=sys.stderr)
        for name in missing[:20]:
            print(f"  - {name}", file=sys.stderr)
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more", file=sys.stderr)

    reset = await _reset_entity_projects(driver)
    if reset:
        print(f"\nCleared project on {reset} entity/entities before recomputing.")

    by_project = await _link_entities_to_projects(driver)
    print(f"\nSet entity.project via MENTIONS, touching {len(by_project)} project(s):")
    for label, count in sorted(by_project.items(), key=lambda kv: -kv[1]):
        print(f"  {label}: {count}")

    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_parse_args())))
