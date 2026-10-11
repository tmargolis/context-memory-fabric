"""Rename `claude-code-*` episodes to `claude-desktop-code-*` (2026-10-02).

docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md step 5. All 161 `claude-code-*`
episodes in production came from Desktop Code-tab sessions (journal
`entrypoint: claude-desktop`). Per episode, one graph write:

  - name           claude-code-<project>-NNN -> claude-desktop-code-<project>-NNN (NNN kept)
  - harness label  :Claude_Code              -> :Claude_Desktop_Code
  - source_description "Promoted from claude_code " -> "Promoted from claude_desktop_code "

and, for a graph with ledger rows, the matching `promotions.episode_name`
rows in one SQLite transaction. memory_ids, uuids, edges and every other
property are untouched, so node/edge counts must not move (exit 3 if they
do). No LLM calls.

The map is built from the source graph (default `mem-fabric-local`) and
written once to imports/journal/episode_rename_map.claude-desktop-code-20261002.json;
later runs must reproduce it exactly. Pre-checks: 1:1 map, every old
name present exactly once in the target, no new name already present.

Safety, mirroring scripts/merge_entities.py:
  - dry run by default; --apply writes;
  - a non-fixgraph-*/rehearsal graph needs --backup-graph whose node/edge
    counts match the target (use --make-backup to GRAPH.COPY it first,
    after waiting for any background save -- two copies back to back fail
    with "could not fork");
  - Redis SAVE before and after the write.

    uv run python scripts/rename_episode_prefix.py --graph rename-rehearsal-20261002 --rehearsal --apply
    uv run python scripts/rename_episode_prefix.py --graph fixgraph-p4 --make-backup --apply
    uv run python scripts/rename_episode_prefix.py --graph mem-fabric-local --make-backup --apply
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.journal.store import DEFAULT_JOURNAL_PATH  # noqa: E402

OLD_PREFIX, NEW_PREFIX = "claude-code-", "claude-desktop-code-"
OLD_LABEL, NEW_LABEL = "Claude_Code", "Claude_Desktop_Code"
OLD_SD, NEW_SD = "Promoted from claude_code ", "Promoted from claude_desktop_code "
MAP_PATH = _ROOT / "imports" / "journal" / "episode_rename_map.claude-desktop-code-20261002.json"
STAMP = "pre-rename-20261002"


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
        "old_names": g.ro_query(f"MATCH (e:Episodic) WHERE e.name STARTS WITH '{OLD_PREFIX}' RETURN count(e)").result_set[0][0],
        "new_names": g.ro_query(f"MATCH (e:Episodic) WHERE e.name STARTS WITH '{NEW_PREFIX}' RETURN count(e)").result_set[0][0],
        "old_label": g.ro_query(f"MATCH (e:{OLD_LABEL}) RETURN count(e)").result_set[0][0],
        "new_label": g.ro_query(f"MATCH (e:{NEW_LABEL}) RETURN count(e)").result_set[0][0],
        "old_sd": g.ro_query("MATCH (e:Episodic) WHERE e.source_description STARTS WITH $p RETURN count(e)", {"p": OLD_SD}).result_set[0][0],
    }


def build_map(source: str) -> dict[str, str]:
    names = [r[0] for r in _graph(source).ro_query(
        f"MATCH (e:Episodic) WHERE e.name STARTS WITH '{OLD_PREFIX}' RETURN e.name ORDER BY e.name").result_set]
    mapping = {n: NEW_PREFIX + n[len(OLD_PREFIX):] for n in names}
    if len(set(mapping.values())) != len(mapping):
        raise SystemExit("ABORT: rename map is not 1:1")
    if MAP_PATH.exists():
        saved = json.loads(MAP_PATH.read_text())["map"]
        if saved != mapping:
            raise SystemExit(f"ABORT: {MAP_PATH.name} exists and differs from the map built from {source}")
    return mapping


def write_map(mapping: dict[str, str], source: str) -> None:
    if MAP_PATH.exists():
        return
    MAP_PATH.write_text(json.dumps({
        "created": "2026-10-02", "source_graph": source,
        "reason": "claude-code-* episodes all came from Desktop Code-tab sessions; docs/CLAUDE-HARNESS-PROVENANCE-PLAN.md step 5",
        "label": [OLD_LABEL, NEW_LABEL], "source_description": [OLD_SD, NEW_SD],
        "map": mapping,
    }, indent=2) + "\n")
    print(f"wrote {MAP_PATH}")


def precheck(g, mapping: dict[str, str]) -> list[str]:
    problems = []
    rows = g.ro_query(f"MATCH (e:Episodic) WHERE e.name STARTS WITH '{OLD_PREFIX}' OR e.name STARTS WITH '{NEW_PREFIX}' "
                      "RETURN e.name, count(e)").result_set
    present = {n: c for n, c in rows}
    for old, new in mapping.items():
        if present.get(old, 0) != 1:
            problems.append(f"{old}: present {present.get(old, 0)}x (want 1)")
        if present.get(new, 0):
            problems.append(f"{new}: already exists")
    extra = [n for n in present if n.startswith(OLD_PREFIX) and n not in mapping]
    if extra:
        problems.append(f"{len(extra)} claude-code-* names not in the map, e.g. {extra[:3]}")
    return problems


def wait_bgsave(r) -> None:
    while r.info("persistence").get("rdb_bgsave_in_progress"):
        time.sleep(2)


def ledger_rows(graph: str, mapping: dict[str, str]) -> int:
    with sqlite3.connect(f"file:{DEFAULT_JOURNAL_PATH}?mode=ro", uri=True) as c:
        return sum(c.execute("SELECT count(*) FROM promotions WHERE graph_name = ? AND episode_name = ?",
                             (graph, old)).fetchone()[0] for old in mapping)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", required=True)
    ap.add_argument("--source", default="mem-fabric-local", help="graph the rename map is built from")
    ap.add_argument("--rehearsal", action="store_true", help="GRAPH.COPY --source into --graph (must not exist) and rename the copy; no ledger")
    ap.add_argument("--backup-graph", default=None)
    ap.add_argument("--make-backup", action="store_true", help=f"GRAPH.COPY --graph to <graph>.{STAMP} first")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    r = _redis()
    mapping = build_map(args.source)
    print(f"map: {len(mapping)} episodes from {args.source}, e.g. {next(iter(mapping.items()))}")

    if args.rehearsal:
        if not args.graph.startswith("rename-rehearsal-"):
            raise SystemExit("--rehearsal graphs must be named rename-rehearsal-*")
        if r.exists(args.graph):
            raise SystemExit(f"ABORT: {args.graph} already exists")
        if args.apply:
            wait_bgsave(r)
            r.execute_command("GRAPH.COPY", args.source, args.graph)
            print(f"copied {args.source} -> {args.graph}")
        else:
            print(f"(dry run) would copy {args.source} -> {args.graph}")
            args.graph = args.source  # inspect the source read-only

    # An already-existing rename-rehearsal-* graph is scratch too: no backup, no ledger.
    scratch = args.rehearsal or args.graph.startswith("rename-rehearsal-")
    g = _graph(args.graph)
    before = counts(g)
    print(f"{args.graph} before: {before}")
    problems = precheck(g, mapping)
    if problems:
        print("PRE-CHECK FAILED:\n  " + "\n  ".join(problems[:20]), file=sys.stderr)
        return 2
    print("pre-check ok: every old name present once, no new name present")
    n_ledger = 0 if scratch else ledger_rows(args.graph, mapping)
    print(f"ledger rows to rename for graph_name={args.graph!r}: {n_ledger}")

    if not args.apply:
        print("DRY RUN -- nothing written.")
        return 0

    write_map(mapping, args.source)
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
            raise SystemExit("refusing: non-rehearsal renames need --backup-graph or --make-backup")
        b = counts(_graph(backup))
        if (b["nodes"], b["edges"]) != (before["nodes"], before["edges"]):
            raise SystemExit(f"ABORT: backup {backup} counts {b} != target {before}")
        wait_bgsave(r)
        r.execute_command("SAVE")
        print("Redis SAVE done (pre-write)")

    renamed = 0
    for old, new in mapping.items():
        res = g.query(
            f"MATCH (e:Episodic {{name: $old}}) "
            f"SET e.name = $new, e.source_description = CASE WHEN e.source_description STARTS WITH $osd "
            f"THEN $nsd + substring(e.source_description, size($osd)) ELSE e.source_description END "
            f"REMOVE e:{OLD_LABEL} SET e:{NEW_LABEL} RETURN count(e)",
            {"old": old, "new": new, "osd": OLD_SD, "nsd": NEW_SD},
        ).result_set[0][0]
        if res != 1:
            raise SystemExit(f"ABORT after {renamed} renames: {old} matched {res} nodes")
        renamed += 1
    print(f"renamed {renamed} episodes")

    if n_ledger:
        with sqlite3.connect(DEFAULT_JOURNAL_PATH) as c:
            changed = 0
            for old, new in mapping.items():
                changed += c.execute("UPDATE promotions SET episode_name = ? WHERE graph_name = ? AND episode_name = ?",
                                     (new, args.graph, old)).rowcount
            if changed != n_ledger:
                c.rollback()
                raise SystemExit(f"ABORT: ledger updated {changed} rows, expected {n_ledger} -- rolled back")
        print(f"ledger: {changed} promotions rows renamed")

    after = counts(g)
    print(f"{args.graph} after:  {after}")
    ok = (after["nodes"], after["edges"]) == (before["nodes"], before["edges"]) \
        and after["old_names"] == 0 and after["new_names"] == before["new_names"] + len(mapping) \
        and after["old_label"] == 0 and after["new_label"] == before["new_label"] + len(mapping) \
        and after["old_sd"] == 0
    if n_ledger:
        ok = ok and ledger_rows(args.graph, mapping) == 0
    if not scratch:
        wait_bgsave(r)
        r.execute_command("SAVE")
        print("Redis SAVE done (post-write)")
    print("VERIFY OK" if ok else "VERIFY FAILED")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
