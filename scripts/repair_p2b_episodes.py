"""Repair MS9 Phase 2b episode damage (2026-10-02, provenance plan step 5a).

Phase 2b mapped episode names to memory_ids through stale `promotions`
rows (written 2026-09-09, before the 2026-09-12 FalkorDB rebuild), so for
14 episodes it retracted the right node but re-added a *different* memory
-- duplicating an episode that still exists -- and never re-added the
retracted memory. Identification is by identity against the pre-2b backup
(`mem-fabric-local.pre-p2b-20261001`), never by name:

  wrong re-add  a node absent from the backup whose memory_id is also
                carried by a node that IS in the backup (same memory twice)
  lost memory   a memory_id carried by a backup episode with no node at all now

Subcommands (each dry-run by default, --apply to write; one graph at a time):

  remove-wrong      graphiti.remove_episode() each wrong re-add (no LLM).
                    Its originals stay. Episodes drop by exactly that count.
  prepare-readd     write the lost memory_ids file for
                    scripts/rebuild_graph_from_ledger.py --memory-ids-file;
                    for a graph whose ledger still marks them `succeeded`
                    (stale rows, no node), delete those rows so
                    promote_reviewed doesn't skip them as already_promoted.
  reconcile-ledger  set promotions.episode_name to the graph's node name for
                    every memory_id in the graph, and drop `succeeded` rows
                    whose memory has no node. The graph is authoritative;
                    ledger names are never replayed (rebuild mints new ones).

  restore-edges     after remove-wrong: graphiti.remove_episode() also deletes
                    entities only *episodes* mention, taking any non-episode
                    edges on them (Phase 4 wiki edges, found on fixgraph-p4)
                    with them. Recreate, exactly from --backup, every edge
                    not created by an episode (`episodes` empty) that is
                    missing, plus its missing endpoint nodes (labels and
                    properties; embeddings re-wrapped with vecf32).

remove-wrong's dry run lists entities it would delete that carry such
non-episode edges, so a graph can be checked before applying.

Between prepare-readd and reconcile-ledger: re-add with
rebuild_graph_from_ledger.py (Spark, sequential), restore names with
scripts/restore_p2b_episode_names.py, then `tag-readded` (project + harness
tags by the now-unique names; the rebuild doesn't tag).
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
import os
from pathlib import Path
import sqlite3
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.journal.store import DEFAULT_JOURNAL_PATH  # noqa: E402

PRE_P2B = "mem-fabric-local.pre-p2b-20261001"
PROTECTED = {"mem-fabric-local", "fixgraph-p4"}


def _graph(name: str):
    from server.core.falkordb_conn import falkordb_client
    return falkordb_client().select_graph(name)


def memory_id(sd):
    return sd.split("memory_id=", 1)[1].strip() if sd and "memory_id=" in sd else None


def episodes(g) -> list[tuple[str, str, str | None]]:
    return [(u, n, memory_id(sd)) for u, n, sd in
            g.ro_query("MATCH (e:Episodic) RETURN e.uuid, e.name, e.source_description").result_set]


def diagnose(graph: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str]]]:
    cur, pre = episodes(_graph(graph)), episodes(_graph(PRE_P2B))
    pre_uuids = {u for u, _, _ in pre}
    by_mid = defaultdict(list)
    for u, n, m in cur:
        if m:
            by_mid[m].append(u)
    wrong = [(u, n, m) for u, n, m in cur
             if u not in pre_uuids and m and any(o in pre_uuids for o in by_mid[m] if o != u)]
    cur_mids = set(by_mid)
    lost = sorted({(m, n) for _, n, m in pre if m and m not in cur_mids})
    return wrong, lost


def counts(g) -> dict[str, int]:
    return {
        "nodes": g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
        "edges": g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
        "episodes": g.ro_query("MATCH (e:Episodic) RETURN count(e)").result_set[0][0],
    }


def _require_backup(graph: str) -> None:
    if graph not in PROTECTED:
        return
    stamp = {"mem-fabric-local": "mem-fabric-local.pre-p2b-repair-20261002",
             "fixgraph-p4": "fixgraph-p4.pre-p2b-repair-20261002"}[graph]
    b, t = counts(_graph(stamp)), counts(_graph(graph))
    if (b["nodes"], b["edges"]) != (t["nodes"], t["edges"]):
        raise SystemExit(f"ABORT: backup {stamp} {b} does not match {graph} {t}; make the backup first")


async def remove_wrong(graph: str, apply: bool) -> int:
    wrong, lost = diagnose(graph)
    g = _graph(graph)
    before = counts(g)
    print(f"{graph} before: {before}")
    print(f"wrong re-adds: {len(wrong)}; lost memories: {len(lost)}")
    for u, n, m in wrong:
        print(f"   remove {u[:8]} {n}  (memory ...{m[-40:]})")
    at_risk = g.ro_query(
        "MATCH (ep:Episodic)-[:MENTIONS]->(n:Entity) WHERE ep.uuid IN $u "
        "WITH DISTINCT n MATCH (:Episodic)-[m:MENTIONS]->(n) WITH n, count(m) AS c WHERE c = 1 "
        "MATCH (n)-[r]-() WHERE NOT type(r) = 'MENTIONS' AND (r.episodes IS NULL OR size(r.episodes) = 0) "
        "RETURN n.name, type(r), count(r)", {"u": [u for u, _, _ in wrong]}).result_set
    print(f"entities removal would delete that carry non-episode edges: {len(at_risk)}"
          + ("  -> run restore-edges after removal" if at_risk else ""))
    for name, rtype, c in at_risk:
        print(f"   {name!r}: {c} x {rtype}")
    if not apply:
        print("DRY RUN -- nothing written.")
        return 0
    _require_backup(graph)
    from server.providers.memory_graphiti import get_graphiti
    graphiti = get_graphiti(graph_name=graph)
    for u, _n, _m in wrong:
        await graphiti.remove_episode(u)
    after = counts(g)
    print(f"{graph} after:  {after}")
    still, _ = diagnose(graph)
    ok = after["episodes"] == before["episodes"] - len(wrong) and not still
    print("VERIFY OK" if ok else f"VERIFY FAILED (wrong re-adds left: {len(still)})")
    return 0 if ok else 3


def _create_args(props: dict) -> tuple[str, dict]:
    """SET clause + params copying `props` verbatim, embeddings via vecf32."""
    sets, params = [], {}
    for i, (k, v) in enumerate(props.items()):
        params[f"p{i}"] = v
        sets.append(f"x.`{k}` = " + (f"vecf32($p{i})" if k.endswith("_embedding") else f"$p{i}"))
    return ", ".join(sets), params


def copy_project_tags(g, b, node_uuids: list[str]) -> int:
    """Copy recreated entities' (:Entity)-[:IN_PROJECT]->(:Project) tags from the
    backup. They carry no uuid (scripts/tag_projects.py MERGEs them), so
    restore-edges' uuid diff can't see them. Returns edges created."""
    made = 0
    for nu in node_uuids:
        for (proj,) in b.ro_query("MATCH (n {uuid: $u})-[:IN_PROJECT]->(p:Project) RETURN p.name", {"u": nu}).result_set:
            made += g.query("MATCH (n {uuid: $u}) MERGE (p:Project {name: $p}) MERGE (n)-[r:IN_PROJECT]->(p) "
                            "RETURN count(r)", {"u": nu, "p": proj}).relationships_created or 0
    return int(made)


def restore_edges(graph: str, backup: str, apply: bool) -> int:
    g, b = _graph(graph), _graph(backup)
    q = ("MATCH (a)-[r]->(c) WHERE r.uuid IS NOT NULL AND NOT type(r) = 'MENTIONS' "
         "AND (r.episodes IS NULL OR size(r.episodes) = 0) RETURN r.uuid")
    have = {row[0] for row in g.ro_query(q).result_set}
    missing = [row[0] for row in b.ro_query(q).result_set if row[0] not in have]
    print(f"non-episode edges in {backup} missing from {graph}: {len(missing)}")
    nodes_needed: dict[str, None] = {}
    edges = []
    for eu in missing:
        a, c, rtype, eprops = b.ro_query(
            "MATCH (a)-[r {uuid: $u}]->(c) RETURN a.uuid, c.uuid, type(r), properties(r)", {"u": eu}).result_set[0]
        edges.append((a, c, rtype, eprops))
        for nu in (a, c):
            if not g.ro_query("MATCH (n {uuid: $u}) RETURN count(n)", {"u": nu}).result_set[0][0]:
                nodes_needed[nu] = None
        print(f"   {rtype} {eprops.get('fact', '')[:70]!r}")
    print(f"endpoint nodes to recreate: {len(nodes_needed)}")
    if not apply:
        print("DRY RUN -- nothing written.")
        return 0
    before = counts(g)
    for nu in nodes_needed:
        labels, props = b.ro_query("MATCH (n {uuid: $u}) RETURN labels(n), properties(n)", {"u": nu}).result_set[0]
        sets, params = _create_args(props)
        g.query(f"CREATE (x:{':'.join(f'`{l}`' for l in labels)}) SET {sets}", params)
    for a, c, rtype, eprops in edges:
        sets, params = _create_args(eprops)
        params.update({"a": a, "c": c})
        g.query(f"MATCH (s {{uuid: $a}}), (t {{uuid: $c}}) CREATE (s)-[x:`{rtype}`]->(t) SET {sets}", params)
    tags = copy_project_tags(g, b, list(nodes_needed))
    after = counts(g)
    left = [row[0] for row in b.ro_query(q).result_set
            if row[0] not in {r[0] for r in g.ro_query(q).result_set}]
    ok = not left and after["nodes"] == before["nodes"] + len(nodes_needed) and after["edges"] == before["edges"] + len(edges) + tags
    print(f"{graph}: {before} -> {after}")
    print("VERIFY OK" if ok else f"VERIFY FAILED (still missing {len(left)})")
    return 0 if ok else 3


async def tag_readded(graph: str, ids_file: Path, apply: bool) -> int:
    """Project + harness tags for the re-added episodes, by their restored
    (unique) names -- rebuild_graph_from_ledger.py doesn't tag, and the
    tagging helpers match by name, so this runs after restore_p2b_episode_names."""
    from server.consolidation.graph_tagging import tag_promoted_episode
    wanted = {l.strip() for l in ids_file.read_text().splitlines() if l.strip()}
    pre_uuids = {u for u, _, _ in episodes(_graph(PRE_P2B))}
    targets = [(u, n, m) for u, n, m in episodes(_graph(graph)) if m in wanted and u not in pre_uuids]
    g = _graph(graph)
    with sqlite3.connect(f"file:{DEFAULT_JOURNAL_PATH}?mode=ro", uri=True) as c:
        plan = []
        for u, n, m in targets:
            project, ev = c.execute("SELECT project, source_event_id FROM derived_memories WHERE memory_id = ?", (m,)).fetchone()
            harness = (c.execute("SELECT harness FROM events WHERE event_id = ?", (ev,)).fetchone() or [None])[0]
            same = g.ro_query("MATCH (e:Episodic {name: $n}) RETURN count(e)", {"n": n}).result_set[0][0]
            plan.append((n, project, harness, same))
    print(f"re-added episodes to tag in {graph}: {len(plan)} (of {len(wanted)} requested)")
    for n, project, harness, same in plan:
        print(f"   {n:40s} project={project} harness={harness} nodes_with_name={same}")
    if any(same != 1 for *_, same in plan):
        raise SystemExit("ABORT: a target name is not unique; restore names first")
    if not apply:
        print("DRY RUN -- nothing written.")
        return 0
    from server.providers.memory_graphiti import get_graphiti
    driver = get_graphiti(graph_name=graph).driver
    for n, project, harness, _ in plan:
        await tag_promoted_episode(driver, n, project, harness)
    untagged = g.ro_query("MATCH (e:Episodic) WHERE e.name IN $n AND e.project IS NULL RETURN count(e)",
                          {"n": [p[0] for p in plan if p[1]]}).result_set[0][0]
    print(f"tagged {len(plan)}; episodes with a project still untagged: {untagged}")
    print("VERIFY OK" if not untagged else "VERIFY FAILED")
    return 0 if not untagged else 3


def prepare_readd(graph: str, out: Path, apply: bool) -> int:
    _, lost = diagnose(graph)
    print(f"lost memories in {graph}: {len(lost)}")
    with sqlite3.connect(f"file:{DEFAULT_JOURNAL_PATH}?mode=ro", uri=True) as c:
        stale = [(m, *c.execute("SELECT episode_name, status FROM promotions WHERE graph_name = ? AND memory_id = ?",
                                (graph, m)).fetchone()) for m, _ in lost
                 if c.execute("SELECT 1 FROM promotions WHERE graph_name = ? AND memory_id = ?", (graph, m)).fetchone()]
    for m, n in lost:
        print(f"   {n:40s} ...{m[-40:]}")
    print(f"stale ledger rows for graph_name={graph!r} blocking re-add: {len(stale)}")
    if not apply:
        print("DRY RUN -- nothing written.")
        return 0
    out.write_text("".join(f"{m}\n" for m, _ in lost))
    print(f"wrote {len(lost)} memory_ids -> {out}")
    if stale:
        with sqlite3.connect(DEFAULT_JOURNAL_PATH) as c:
            n = sum(c.execute("DELETE FROM promotions WHERE graph_name = ? AND memory_id = ?", (graph, m)).rowcount
                    for m, _n, _s in stale)
        print(f"deleted {n} stale ledger rows")
    return 0


def reconcile_ledger(graph: str, apply: bool) -> int:
    cur = episodes(_graph(graph))
    mids_in_graph = defaultdict(set)
    for _u, n, m in cur:
        if m:
            mids_in_graph[m].add(n)
    # A memory on several nodes (pre-2b double promotions, 2026-09-13 and
    # 09-23 -- separate backlog item) can't be expressed by a one-row-per-
    # (memory, graph) ledger: keep its row if it names one of the nodes, and
    # report it. Abort only if the 2b wrong re-adds are still present.
    multi = {m for m, ns in mids_in_graph.items() if len(ns) > 1}
    wrong, _ = diagnose(graph)
    if wrong:
        raise SystemExit(f"ABORT: {len(wrong)} Phase 2b wrong re-adds still in {graph}; run remove-wrong first")
    with sqlite3.connect(f"file:{DEFAULT_JOURNAL_PATH}?mode=ro", uri=True) as c:
        rows = c.execute("SELECT memory_id, episode_name, status FROM promotions WHERE graph_name = ?", (graph,)).fetchall()
    ambiguous = [(m, n, sorted(mids_in_graph[m])) for m, n, s in rows
                 if s == "succeeded" and m in multi and n not in mids_in_graph[m]]
    if ambiguous:
        raise SystemExit(f"ABORT: {len(ambiguous)} multi-node memories whose ledger name matches no node: {ambiguous[:3]}")
    print(f"multi-node memories (left as-is, ledger names one of their nodes): {len(multi)}")
    rename = [(next(iter(mids_in_graph[m])), m) for m, n, s in rows
              if s == "succeeded" and m in mids_in_graph and m not in multi and next(iter(mids_in_graph[m])) != n]
    orphan = [m for m, _n, s in rows if s == "succeeded" and m not in mids_in_graph]
    missing = [m for m in mids_in_graph if m not in {r[0] for r in rows}]
    print(f"{graph}: ledger rows {len(rows)}; names to align {len(rename)}; "
          f"succeeded rows with no node {len(orphan)}; graph memories with no ledger row {len(missing)}")
    if not apply:
        print("DRY RUN -- nothing written.")
        return 0
    with sqlite3.connect(DEFAULT_JOURNAL_PATH) as c:
        c.executemany("UPDATE promotions SET episode_name = ? WHERE graph_name = ? AND memory_id = ?",
                      [(n, graph, m) for n, m in rename])
        c.executemany("DELETE FROM promotions WHERE graph_name = ? AND memory_id = ? AND status = 'succeeded'",
                      [(graph, m) for m in orphan])
    with sqlite3.connect(f"file:{DEFAULT_JOURNAL_PATH}?mode=ro", uri=True) as c:
        rows = c.execute("SELECT memory_id, episode_name FROM promotions WHERE graph_name = ? AND status = 'succeeded'",
                         (graph,)).fetchall()
    bad = [m for m, n in rows if m not in mids_in_graph or n not in mids_in_graph[m]]
    dups = len(rows) - len({n for _m, n in rows})  # one name per row; multi-node memories hold one each
    print(f"after: mismatched rows {len(bad)}, duplicate names {dups}")
    print("VERIFY OK" if not bad and not dups else "VERIFY FAILED")
    return 0 if not bad and not dups else 3


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["diagnose", "remove-wrong", "restore-edges", "prepare-readd", "tag-readded", "reconcile-ledger"])
    ap.add_argument("--backup", default=None, help="restore-edges: graph to copy missing non-episode edges from")
    ap.add_argument("--graph", required=True)
    ap.add_argument("--out", type=Path, default=_ROOT / "imports" / "journal" / "p2b_lost_memory_ids.txt")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    os.environ["FALKORDB_DATABASE"] = args.graph
    if args.command == "diagnose":
        wrong, lost = diagnose(args.graph)
        print(f"{args.graph}: wrong re-adds {len(wrong)}, lost memories {len(lost)}, counts {counts(_graph(args.graph))}")
        return 0
    if args.command == "remove-wrong":
        return asyncio.run(remove_wrong(args.graph, args.apply))
    if args.command == "restore-edges":
        if not args.backup:
            raise SystemExit("restore-edges needs --backup")
        return restore_edges(args.graph, args.backup, args.apply)
    if args.command == "tag-readded":
        return asyncio.run(tag_readded(args.graph, args.out, args.apply))
    if args.command == "prepare-readd":
        return prepare_readd(args.graph, args.out, args.apply)
    return reconcile_ledger(args.graph, args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
