"""Restore pre-Phase-2b names on episodes that Phase 2b re-added (2026-10-02).

docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md step 5a. MS9 Phase 2b retracted 136
legacy episodes and re-added them under `typed-recall`. 128 came back
under *new* `<harness>-<project>-NNN` names, and 76 of those collided with
the name of an unrelated, untouched episode, leaving 76 names on two nodes
each (mem-fabric-local and its copy fixgraph-p4; the pre-p2b backup has
none). Lookups by name -- edit_memory, eval gold_episodes -- can land on
the wrong episode.

Fix, keyed by identity rather than by the (non-unique) name: for every
Episodic node whose uuid is absent from the pre-2b backup (i.e. re-added
by 2b), take the `memory_id=` from its source_description, find that
memory's name in the backup, and set it back. Untouched episodes keep
their names; the pre-2b names were unique, so the result is unique
(verified: zero duplicate names afterwards, else exit 3). The graph's
`promotions` ledger rows are updated by memory_id in one transaction.
Only `name` changes -- uuids, content, edges untouched; counts must not move.

Same safety pattern as scripts/rename_episode_prefix.py: dry run by
default; non-rehearsal graphs need --make-backup/--backup-graph with
matching counts; Redis SAVE before and after; map written once to
imports/journal/episode_rename_map.p2b-restore-20261002.json (per graph).

    uv run python scripts/restore_p2b_episode_names.py --graph mem-fabric-local
    uv run python scripts/restore_p2b_episode_names.py --graph rename-rehearsal-p2b-20261002 --rehearsal --apply
    uv run python scripts/restore_p2b_episode_names.py --graph fixgraph-p4 --make-backup --apply
    uv run python scripts/restore_p2b_episode_names.py --graph mem-fabric-local --make-backup --apply
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
import time

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.journal.store import DEFAULT_JOURNAL_PATH  # noqa: E402

PRE_P2B = "mem-fabric-local.pre-p2b-20261001"
MAP_PATH = _ROOT / "imports" / "journal" / "episode_rename_map.p2b-restore-20261002.json"
STAMP = "pre-p2b-restore-20261002"


def _graph(name: str):
    from falkordb import FalkorDB
    return FalkorDB().select_graph(name)


def _redis():
    import redis
    return redis.Redis()


def memory_id(sd) -> str | None:
    return sd.split("memory_id=", 1)[1].strip() if sd and "memory_id=" in sd else None


def counts(g) -> dict[str, int]:
    dup = g.ro_query("MATCH (e:Episodic) WITH e.name AS n, count(e) AS c WHERE c > 1 RETURN count(n)").result_set[0][0]
    return {
        "nodes": g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
        "edges": g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
        "episodes": g.ro_query("MATCH (e:Episodic) RETURN count(e)").result_set[0][0],
        "duplicate_names": dup,
    }


def build_plan(g, pre) -> tuple[list[dict], Counter]:
    pre_uuids = {r[0] for r in pre.ro_query("MATCH (e:Episodic) RETURN e.uuid").result_set}
    pre_name_by_mid: dict[str, str] = {}
    for name, sd in pre.ro_query("MATCH (e:Episodic) RETURN e.name, e.source_description").result_set:
        mid = memory_id(sd)
        if mid:
            pre_name_by_mid[mid] = name
    renames, stats = [], Counter()
    for uuid, name, sd in g.ro_query("MATCH (e:Episodic) RETURN e.uuid, e.name, e.source_description").result_set:
        if uuid in pre_uuids:
            continue
        stats["re-added since pre-p2b"] += 1
        mid = memory_id(sd)
        orig = pre_name_by_mid.get(mid) if mid else None
        if orig is None:
            stats["no pre-p2b match (left alone)"] += 1
            continue
        if orig == name:
            stats["already has its pre-p2b name"] += 1
            continue
        renames.append({"uuid": uuid, "memory_id": mid, "from": name, "to": orig})
    stats["to rename"] = len(renames)
    return renames, stats


def projected_duplicates(g, renames: list[dict]) -> list[str]:
    by_uuid = {r["uuid"]: r["to"] for r in renames}
    names = Counter(by_uuid.get(u, n) for u, n in g.ro_query("MATCH (e:Episodic) RETURN e.uuid, e.name").result_set)
    return sorted(n for n, c in names.items() if c > 1)


def ledger_plan(graph: str, renames: list[dict]) -> list[tuple[str, str, str]]:
    out = []
    with sqlite3.connect(f"file:{DEFAULT_JOURNAL_PATH}?mode=ro", uri=True) as c:
        for r in renames:
            row = c.execute("SELECT episode_name FROM promotions WHERE graph_name = ? AND memory_id = ?",
                            (graph, r["memory_id"])).fetchone()
            if row and row[0] != r["to"]:
                out.append((r["memory_id"], row[0], r["to"]))
    return out


def wait_bgsave(rc) -> None:
    while rc.info("persistence").get("rdb_bgsave_in_progress"):
        time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", required=True)
    ap.add_argument("--rehearsal", action="store_true", help="GRAPH.COPY mem-fabric-local into --graph (rename-rehearsal-*) and fix the copy; no ledger")
    ap.add_argument("--backup-graph", default=None)
    ap.add_argument("--make-backup", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    rc = _redis()
    if args.rehearsal:
        if not args.graph.startswith("rename-rehearsal-"):
            raise SystemExit("--rehearsal graphs must be named rename-rehearsal-*")
        if rc.exists(args.graph):
            raise SystemExit(f"ABORT: {args.graph} already exists")
        if not args.apply:
            print(f"(dry run) would copy mem-fabric-local -> {args.graph}; inspecting mem-fabric-local")
            args.graph = "mem-fabric-local"
        else:
            wait_bgsave(rc)
            rc.execute_command("GRAPH.COPY", "mem-fabric-local", args.graph)
            print(f"copied mem-fabric-local -> {args.graph}")

    # An already-existing rename-rehearsal-* graph is scratch too: no backup, no ledger.
    scratch = args.rehearsal or args.graph.startswith("rename-rehearsal-")
    g, pre = _graph(args.graph), _graph(PRE_P2B)
    before = counts(g)
    renames, stats = build_plan(g, pre)
    print(f"{args.graph} before: {before}")
    print(f"plan: {dict(stats)}")
    for r in renames[:5]:
        print(f"   {r['from']} -> {r['to']}")
    dups_after = projected_duplicates(g, renames)
    print(f"projected duplicate names after restore: {len(dups_after)} {dups_after[:5]}")
    ledger = [] if scratch else ledger_plan(args.graph, renames)
    print(f"ledger rows to update (graph_name={args.graph!r}): {len(ledger)}")
    if dups_after:
        print("ABORT: restore would still leave duplicate names", file=sys.stderr)
        return 2
    if not args.apply:
        print("DRY RUN -- nothing written.")
        return 0

    maps = json.loads(MAP_PATH.read_text()) if MAP_PATH.exists() else {
        "created": "2026-10-02", "source": PRE_P2B,
        "reason": "MS9 Phase 2b re-added episodes under new, colliding names; restore pre-2b names by memory_id. docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md step 5a",
        "graphs": {}}
    maps["graphs"][args.graph] = renames
    MAP_PATH.write_text(json.dumps(maps, indent=2) + "\n")
    print(f"wrote map for {args.graph} -> {MAP_PATH.name}")

    if not scratch:
        backup = args.backup_graph
        if args.make_backup:
            backup = f"{args.graph}.{STAMP}"
            if rc.exists(backup):
                raise SystemExit(f"ABORT: backup graph {backup} already exists")
            wait_bgsave(rc)
            rc.execute_command("GRAPH.COPY", args.graph, backup)
            print(f"backed up {args.graph} -> {backup}")
        if not backup:
            raise SystemExit("refusing: non-rehearsal runs need --backup-graph or --make-backup")
        b = counts(_graph(backup))
        if (b["nodes"], b["edges"]) != (before["nodes"], before["edges"]):
            raise SystemExit(f"ABORT: backup {backup} counts {b} != target {before}")
        wait_bgsave(rc)
        rc.execute_command("SAVE")
        print("Redis SAVE done (pre-write)")

    for i, r in enumerate(renames):
        n = g.query("MATCH (e:Episodic {uuid: $u}) SET e.name = $n RETURN count(e)",
                    {"u": r["uuid"], "n": r["to"]}).result_set[0][0]
        if n != 1:
            raise SystemExit(f"ABORT after {i} renames: uuid {r['uuid']} matched {n} nodes")
    print(f"renamed {len(renames)} episodes")

    if ledger:
        with sqlite3.connect(DEFAULT_JOURNAL_PATH) as c:
            changed = sum(c.execute("UPDATE promotions SET episode_name = ? WHERE graph_name = ? AND memory_id = ?",
                                    (to, args.graph, mid)).rowcount for mid, _frm, to in ledger)
            if changed != len(ledger):
                c.rollback()
                raise SystemExit(f"ABORT: ledger updated {changed} rows, expected {len(ledger)} -- rolled back")
        print(f"ledger: {changed} promotions rows updated")

    after = counts(g)
    print(f"{args.graph} after:  {after}")
    wrong = [r for r in renames if g.ro_query("MATCH (e:Episodic {uuid: $u}) RETURN e.name", {"u": r["uuid"]}).result_set[0][0] != r["to"]]
    ok = (after["nodes"], after["edges"], after["episodes"]) == (before["nodes"], before["edges"], before["episodes"]) \
        and after["duplicate_names"] == 0 and not wrong
    if not scratch:
        with sqlite3.connect(f"file:{DEFAULT_JOURNAL_PATH}?mode=ro", uri=True) as c:
            ledger_dups = c.execute("SELECT count(*) FROM (SELECT episode_name FROM promotions WHERE graph_name = ? "
                                    "AND status = 'succeeded' GROUP BY episode_name HAVING count(*) > 1)", (args.graph,)).fetchone()[0]
        print(f"ledger duplicate names for {args.graph}: {ledger_dups}")
        wait_bgsave(rc)
        rc.execute_command("SAVE")
        print("Redis SAVE done (post-write)")
    print("VERIFY OK" if ok else f"VERIFY FAILED (wrong names: {len(wrong)})")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
