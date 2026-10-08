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
promoted before the inline tagging was wired in (found the same day: 68 of
79 claude_code episodes had zero IN_PROJECT edges because nothing had ever
tagged them, going all the way back to MS4b's original build).

Per episode, this sets:
  - `e.project`          plain string property, the raw project id exactly
                          as recorded in derived_memories.project (e.g.
                          "my-project"). For exact-match filters.
  - a direct label        a friendly, Cypher-safe slug -- underscores, no
                          dashes -- with NO "ep_" prefix (that prefix was
                          the pre-2026-09-12 convention; dropped at the
                          user's 2026-09-12 request). DISPLAY_OVERRIDES below
                          renames specific project ids to a nicer label
                          (a deployment-specific map in
                          server/consolidation/graph_tagging.py, empty by
                          default -- add an entry to give a project id a
                          different label).

`(:Project {name})` hub nodes -- always `:Project`, never `:Entity` -- link
primarily to `:Entity` nodes, not `:Episodic` (changed 2026-09-12 at the user's request:
entities are the more useful clustering target, and unlike episodes an
entity can legitimately belong to more than one project). This is
many-to-many by construction: an entity gets one `[:IN_PROJECT]` edge per
distinct project among the episodes that `[:MENTIONS]` it. Confirmed safe
against Graphiti -- graphiti_core's build_communities() and every query in
server/providers/memory_graphiti.py match explicit relationship types
(RELATES_TO, MENTIONS), never an untyped `-[r]-`, so an extra IN_PROJECT
edge on :Entity is invisible to all of them.

~25% of episodes have zero extracted entities (no `MENTIONS` edges at
all -- a known extraction gap, see the MS7 eval notes), and an
entity-only link would leave those completely disconnected from their
project's hub. So episodes with zero `MENTIONS` get a direct
`Episodic-[:IN_PROJECT]->Project` edge as a fallback (added 2026-09-12
after `gemini-example-007` / `claude-example-006` were found orphaned by
the entity-only version). An episode with at least one entity relies
solely on that entity's link -- it does not also get a direct edge -- so
every promoted episode is represented in its project's hub cluster
exactly one way.

Source of truth for which episode belongs to which project is the journal
ledger (imports/journal/journal.db): promotions.graph_name/episode_name
joined to derived_memories.project -- not anything already in FalkorDB.
Entity (and orphan-episode) project membership is then derived from the
graph's own MENTIONS edges plus the e.project property this script just
set, not the ledger.

Idempotent and self-healing: every run first deletes ALL existing
Episodic-[:IN_PROJECT]->Project edges and recomputes both the entity
links and the orphan-episode fallback from scratch, so it stays correct
even if an episode's entity count changes between runs (e.g. re-extraction).

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


async def _reset_episode_project_edges(driver) -> int:
    """Delete every Episodic-[:IN_PROJECT]->Project edge so the orphan-fallback pass below is a clean recompute."""
    result = await driver.execute_query(
        "MATCH (:Episodic)-[r:IN_PROJECT]->(:Project) DELETE r RETURN count(r) AS n"
    )
    recs = result[0] if result and isinstance(result[0], list) else (result or [])
    return recs[0]["n"] if recs else 0


async def _link_entities_to_projects(driver) -> dict[str, int]:
    """One [:IN_PROJECT] edge per (entity, distinct project among its mentioning episodes).

    Reads e.project directly off the graph (set by _tag_episodes above), not the ledger --
    this only needs to know what's actually mentioned, not what was promoted.
    """
    pairs_result = await driver.execute_query(
        "MATCH (e:Episodic)-[:MENTIONS]->(n:Entity) "
        "WHERE e.project IS NOT NULL "
        "RETURN DISTINCT n.uuid AS entity_uuid, e.project AS project"
    )
    pairs = pairs_result[0] if pairs_result and isinstance(pairs_result[0], list) else (pairs_result or [])

    by_project: dict[str, int] = {}
    for pair in pairs:
        entity_uuid = pair["entity_uuid"]
        label = _label_for(pair["project"])
        await driver.execute_query(
            "MATCH (n:Entity {uuid: $entity_uuid}) "
            "MERGE (proj:Project {name: $label}) "
            "MERGE (n)-[:IN_PROJECT]->(proj)",
            entity_uuid=entity_uuid,
            label=label,
        )
        by_project[label] = by_project.get(label, 0) + 1
    return by_project


async def _link_orphan_episodes_to_projects(driver) -> dict[str, int]:
    """Direct Episodic->Project fallback for episodes with zero MENTIONS (no extracted entities) --
    otherwise entity-less episodes are completely disconnected from their project's hub.
    """
    rows_result = await driver.execute_query(
        "MATCH (e:Episodic) WHERE e.project IS NOT NULL AND NOT (e)-[:MENTIONS]->(:Entity) "
        "RETURN e.uuid AS uuid, e.project AS project"
    )
    rows = rows_result[0] if rows_result and isinstance(rows_result[0], list) else (rows_result or [])

    by_project: dict[str, int] = {}
    for row in rows:
        label = _label_for(row["project"])
        await driver.execute_query(
            "MATCH (e:Episodic {uuid: $uuid}) "
            "MERGE (proj:Project {name: $label}) "
            "MERGE (e)-[:IN_PROJECT]->(proj)",
            uuid=row["uuid"],
            label=label,
        )
        by_project[label] = by_project.get(label, 0) + 1
    return by_project


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

    reset = await _reset_episode_project_edges(driver)
    if reset:
        print(f"\nReset {reset} existing Episodic->Project edge(s) before recomputing.")

    by_project = await _link_entities_to_projects(driver)
    print(f"\nLinked entities to {len(by_project)} project(s) via MENTIONS:")
    for label, count in sorted(by_project.items(), key=lambda kv: -kv[1]):
        print(f"  {label}: {count}")

    orphan_by_project = await _link_orphan_episodes_to_projects(driver)
    orphan_total = sum(orphan_by_project.values())
    print(f"\nLinked {orphan_total} entity-less episode(s) directly to {len(orphan_by_project)} project(s):")
    for label, count in sorted(orphan_by_project.items(), key=lambda kv: -kv[1]):
        print(f"  {label}: {count}")

    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run(_parse_args())))
