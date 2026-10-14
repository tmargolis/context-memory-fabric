"""Retag promoted episodes still carrying an old project slug (B08, 2026-10-13).

`CMF_PROJECT_ALIASES` in `.env` folds `obsidian` -> `llm-wiki` and
`finances` -> `finance` at extraction time, but `retag_review_projects.py`
only touches NOT-yet-reviewed items -- "anything approved, rejected or
promoted is history and stays as it is" by design. These two slugs still
have promoted episodes in `mem-fabric-local`: 25 `obsidian`, 17 `finances`
(checked 2026-10-13; the third bucket the backlog text named,
`project-epsilon`/`career-navigator` -> `career`/`career-navigator-dev`,
was already closed on 2026-10-07 -- see docs/plan-history.md "Ledger made
durable" -- and has zero promoted episodes left under the old name).

Per episode, three writes:
  - FalkorDB            `Episodic.project` (verified 1:1 against the ledger
                         before any write -- every old-slug node in the
                         graph must correspond to exactly one `succeeded`
                         promotion row for that graph, and vice versa)
  - journal.db           `derived_memories.project`, scoped to those exact
                         memory_ids (never a blanket `WHERE project = ...`,
                         since plenty of non-promoted rows -- pending,
                         rejected -- carry the same old slug and must stay)
  - approved mirrors     `proposals-episode/tier{1,2}/approved/*.json`
                         `"project"` field, for whichever of the memory_ids
                         have one (most of these predate the mirror-based
                         review flow and have none)

`:Project` hub nodes and entities' `IN_PROJECT` edges are untouched here --
re-run `scripts/tag_projects.py` afterward to recompute them from the
corrected `derived_memories.project`.

Safety, mirroring scripts/rename_episode_prefix.py:
  - dry run by default; --apply writes;
  - a non-rehearsal graph needs --backup-graph whose node/edge counts match
    the target (or --make-backup to GRAPH.COPY it first, after waiting for
    any background save);
  - Redis SAVE before and after the write;
  - ledger/mirror writes happen only for --graph mem-fabric-local (the only
    graph_name with real promotions for these memory_ids); a rehearsal
    copy gets the FalkorDB change only, so its before/after counts can be
    compared against a plain dry run of the same source.

    uv run python scripts/retag_promoted_projects.py --graph slugmigration-rehearsal-20261013 --rehearsal --apply
    uv run python scripts/retag_promoted_projects.py --graph mem-fabric-local --make-backup --apply

B17 (2026-10-13) generalized it: `--map OLD=NEW` (repeatable) replaces the
built-in mapping, several old slugs may fold into one new slug, `--stamp`
names the backup, and each retagged episode's direct project label (see
scripts/tag_projects.py) is swapped from the old slug's to the new one's,
since tag_projects.py adds labels but never removes them. Entity `project`
lists are not touched here; re-run tag_projects.py afterward.

    uv run python scripts/retag_promoted_projects.py --map mac-infra=infra --map local-llm=infra \
        --stamp pre-b17-renames-20261013 --graph mem-fabric-local --make-backup --apply
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

MAPPING = {"obsidian": "llm-wiki", "finances": "finance"}
MIRROR_DIRS = [
    _ROOT / "proposals-episode" / "tier1" / "approved",
    _ROOT / "proposals-episode" / "tier2" / "approved",
]
STAMP = "pre-slugmigration-20261013"


def _redis():
    from server.core.falkordb_conn import redis_client
    return redis_client()


def _graph(name: str):
    from server.core.falkordb_conn import falkordb_client
    return falkordb_client().select_graph(name)


def counts(g) -> dict[str, int]:
    out = {
        "nodes": g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
        "edges": g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0],
    }
    for old in MAPPING:
        out[f"old:{old}"] = g.ro_query("MATCH (e:Episodic {project: $p}) RETURN count(e)", {"p": old}).result_set[0][0]
    for new in set(MAPPING.values()):
        out[f"new:{new}"] = g.ro_query("MATCH (e:Episodic {project: $p}) RETURN count(e)", {"p": new}).result_set[0][0]
    return out


def ledger_memory_ids(graph: str) -> dict[str, list[str]]:
    """old slug -> promoted memory_ids for that slug, graph_name=graph."""
    out: dict[str, list[str]] = {old: [] for old in MAPPING}
    with sqlite3.connect(f"file:{DEFAULT_JOURNAL_PATH}?mode=ro", uri=True) as c:
        for old in MAPPING:
            rows = c.execute(
                "SELECT p.memory_id FROM promotions p JOIN derived_memories d ON d.memory_id = p.memory_id "
                "WHERE p.status = 'succeeded' AND p.graph_name = ? AND d.project = ?",
                (graph, old),
            ).fetchall()
            out[old] = [r[0] for r in rows]
    return out


def precheck(g, graph: str, by_slug: dict[str, list[str]]) -> list[str]:
    problems = []
    for old, new in MAPPING.items():
        node_n = g.ro_query("MATCH (e:Episodic {project: $p}) RETURN count(e)", {"p": old}).result_set[0][0]
        ledger_n = len(by_slug[old])
        if node_n != ledger_n:
            problems.append(f"{old}: {node_n} graph nodes vs {ledger_n} ledger rows for graph_name={graph!r} (want equal)")
    return problems


def copy_graph(r, src: str, dst: str) -> None:
    """GRAPH.COPY, tolerating FalkorDB's spurious "destination key already
    exists" reply after it has in fact created the copy (seen twice on
    2026-10-13). The caller has already checked dst didn't exist, so accept
    the copy only if its node/edge counts match src exactly."""
    try:
        r.execute_command("GRAPH.COPY", src, dst)
    except Exception as exc:
        if "already exists" not in str(exc) or not r.exists(dst):
            raise
        a, b = counts(_graph(src)), counts(_graph(dst))
        if (a["nodes"], a["edges"]) != (b["nodes"], b["edges"]):
            raise SystemExit(f"ABORT: GRAPH.COPY errored and {dst} counts {b} != {src} {a}")
        print(f"  GRAPH.COPY reported {exc!s}; copy verified by counts")


def wait_bgsave(r) -> None:
    while r.info("persistence").get("rdb_bgsave_in_progress"):
        time.sleep(2)


def mirror_files(memory_ids: set[str]) -> list[Path]:
    """Mirrors for these memory_ids whose own `project` field still says an old
    slug. Some pre-date the mirror-based review flow and already show
    `project: null`; those are left alone rather than stamped with a value
    the mirror itself never carried."""
    out = []
    for d in MIRROR_DIRS:
        if not d.is_dir():
            continue
        for path in d.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("memory_id") in memory_ids and data.get("project") in MAPPING:
                out.append(path)
    return out


def run_moves(args, r) -> int:
    """Per-episode moves (B17 review, 2026-10-14): a JSON file with
    {"moves": [{"episode", "from", "to"}, ...]} -- the export of the B17 review
    page. Same three writes as the slug mapping, keyed by episode name. An
    episode with no succeeded promotion row (written directly by `remember`)
    gets the FalkorDB change only. A ledger row whose project is NULL is
    accepted as the `from` side and filled in."""
    from server.consolidation.graph_tagging import project_label_for
    moves = json.loads(Path(args.moves).read_text(encoding="utf-8"))["moves"]
    if len({m["episode"] for m in moves}) != len(moves):
        raise SystemExit("ABORT: an episode appears twice in the moves file")
    scratch = args.graph.startswith("slugmigration-rehearsal-")
    ledger_graph = args.source if scratch else args.graph

    if args.rehearsal:
        if not scratch:
            raise SystemExit("--rehearsal graphs must be named slugmigration-rehearsal-*")
        if r.exists(args.graph):
            raise SystemExit(f"ABORT: {args.graph} already exists")
        if not args.apply:
            raise SystemExit("--rehearsal needs --apply (the copy is the rehearsal)")
        wait_bgsave(r)
        copy_graph(r, args.source, args.graph)
        print(f"copied {args.source} -> {args.graph}")

    g = _graph(args.graph)
    problems, mids = [], {}
    with sqlite3.connect(f"file:{DEFAULT_JOURNAL_PATH}?mode=ro", uri=True) as c:
        for m in moves:
            src = None if m["from"] == "(none)" else m["from"]
            got = g.ro_query("MATCH (e:Episodic {name: $n}) RETURN e.project", {"n": m["episode"]}).result_set
            if len(got) != 1 or got[0][0] != src:
                problems.append(f"{m['episode']}: graph has {got}, expected project {src!r}")
            rows = c.execute(
                "SELECT p.memory_id, d.project FROM promotions p JOIN derived_memories d ON d.memory_id = p.memory_id "
                "WHERE p.status = 'succeeded' AND p.graph_name = ? AND p.episode_name = ?",
                (ledger_graph, m["episode"]),
            ).fetchall()
            if len(rows) > 1:
                problems.append(f"{m['episode']}: {len(rows)} ledger rows")
            elif rows:
                if rows[0][1] not in (src, None):
                    problems.append(f"{m['episode']}: ledger project {rows[0][1]!r}, expected {src!r} or NULL")
                mids[m["episode"]] = rows[0][0]
    if problems:
        print("PRE-CHECK FAILED:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    print(f"pre-check ok: {len(moves)} moves, {len(mids)} with ledger rows, "
          f"{len(moves) - len(mids)} graph-only: {[m['episode'] for m in moves if m['episode'] not in mids]}")

    to_mirror = {mids[m["episode"]]: m["to"] for m in moves if m["episode"] in mids}
    mirrors = []
    if not scratch:
        for d in MIRROR_DIRS:
            if d.is_dir():
                for path in d.glob("*.json"):
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if data.get("memory_id") in to_mirror and data.get("project"):
                        mirrors.append(path)
    print(f"approved mirror files to update: {len(mirrors)}")

    before = {"nodes": g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
              "edges": g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]}
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
            copy_graph(r, args.graph, backup)
            print(f"backed up {args.graph} -> {backup}")
        if not backup:
            raise SystemExit("refusing: non-rehearsal moves need --backup-graph or --make-backup")
        b = _graph(backup)
        if (b.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
                b.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]) != (before["nodes"], before["edges"]):
            raise SystemExit(f"ABORT: backup {backup} counts differ from {args.graph}")
        wait_bgsave(r)
        r.execute_command("SAVE")
        print("Redis SAVE done (pre-write)")

    for m in moves:
        old_label = project_label_for(m["from"]) if m["from"] != "(none)" else None
        remove = f"REMOVE e:`{old_label}` " if old_label else ""
        n = g.query(f"MATCH (e:Episodic {{name: $n}}) SET e.project = $to {remove}SET e:`{project_label_for(m['to'])}` "
                    f"RETURN count(e)", {"n": m["episode"], "to": m["to"]}).result_set[0][0]
        if n != 1:
            raise SystemExit(f"ABORT at {m['episode']}: matched {n} nodes")
    print(f"moved {len(moves)} episodes in FalkorDB")

    if not scratch:
        with sqlite3.connect(DEFAULT_JOURNAL_PATH) as c:
            changed = sum(c.execute("UPDATE derived_memories SET project = ? WHERE memory_id = ?",
                                    (to, mid)).rowcount for mid, to in to_mirror.items())
            if changed != len(to_mirror):
                c.rollback()
                raise SystemExit(f"ABORT: journal updated {changed} rows, expected {len(to_mirror)} -- rolled back")
        print(f"journal: {changed} derived_memories rows updated")
        for path in mirrors:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["project"] = to_mirror[data["memory_id"]]
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"mirrors: {len(mirrors)} files updated")

    after = {"nodes": g.ro_query("MATCH (n) RETURN count(n)").result_set[0][0],
             "edges": g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]}
    wrong = [m["episode"] for m in moves
             if g.ro_query("MATCH (e:Episodic {name: $n}) RETURN e.project", {"n": m["episode"]}).result_set[0][0] != m["to"]]
    if not scratch:
        wait_bgsave(r)
        r.execute_command("SAVE")
        print("Redis SAVE done (post-write)")
    print(f"{args.graph} after:  {after}; episodes not at their target: {wrong}")
    ok = after == before and not wrong
    print("VERIFY OK" if ok else "VERIFY FAILED")
    return 0 if ok else 3


def main() -> int:
    global MAPPING, STAMP
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", required=True)
    ap.add_argument("--source", default="mem-fabric-local", help="graph a --rehearsal copy is made from")
    ap.add_argument("--rehearsal", action="store_true", help="GRAPH.COPY --source into --graph (must not exist); FalkorDB-only, no ledger/mirror writes")
    ap.add_argument("--backup-graph", default=None)
    ap.add_argument("--make-backup", action="store_true", help=f"GRAPH.COPY --graph to <graph>.{STAMP} first")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--map", action="append", default=None, metavar="OLD=NEW",
                    help="replace the built-in mapping; repeatable; several OLD may share one NEW")
    ap.add_argument("--stamp", default=None, help="backup suffix for --make-backup")
    ap.add_argument("--moves", default=None, metavar="FILE",
                    help="per-episode moves JSON (B17 review export) instead of a slug mapping")
    args = ap.parse_args()
    if args.stamp:
        STAMP = args.stamp
    if args.moves:
        return run_moves(args, _redis())
    if args.map:
        MAPPING = dict(m.split("=", 1) for m in args.map)
        if set(MAPPING) & set(MAPPING.values()):
            raise SystemExit("ABORT: a slug can't be both an old and a new name in one run")
    if args.stamp:
        STAMP = args.stamp
    print(f"mapping: {MAPPING}")

    r = _redis()

    if args.rehearsal:
        if not args.graph.startswith("slugmigration-rehearsal-"):
            raise SystemExit("--rehearsal graphs must be named slugmigration-rehearsal-*")
        if r.exists(args.graph):
            raise SystemExit(f"ABORT: {args.graph} already exists")
        if args.apply:
            wait_bgsave(r)
            copy_graph(r, args.source, args.graph)
            print(f"copied {args.source} -> {args.graph}")
        else:
            print(f"(dry run) would copy {args.source} -> {args.graph}")
            args.graph = args.source  # inspect the source read-only

    scratch = args.rehearsal or args.graph.startswith("slugmigration-rehearsal-")
    by_slug = ledger_memory_ids(args.source if scratch else args.graph)
    for old, ids in by_slug.items():
        print(f"{old} -> {MAPPING[old]}: {len(ids)} promoted episodes (graph_name={args.source if scratch else args.graph!r})")

    g = _graph(args.graph)
    before = counts(g)
    print(f"{args.graph} before: {before}")
    problems = precheck(g, args.source if scratch else args.graph, by_slug)
    if problems:
        print("PRE-CHECK FAILED:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    print("pre-check ok: graph node counts match ledger row counts for both slugs")

    all_ids = {mid for ids in by_slug.values() for mid in ids}
    mirrors = [] if scratch else mirror_files(all_ids)
    print(f"approved mirror files to update: {len(mirrors)}")

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
            copy_graph(r, args.graph, backup)
            print(f"backed up {args.graph} -> {backup}")
        if not backup:
            raise SystemExit("refusing: non-rehearsal retags need --backup-graph or --make-backup")
        b = counts(_graph(backup))
        if (b["nodes"], b["edges"]) != (before["nodes"], before["edges"]):
            raise SystemExit(f"ABORT: backup {backup} counts {b} != target {before}")
        wait_bgsave(r)
        r.execute_command("SAVE")
        print("Redis SAVE done (pre-write)")

    from server.consolidation.graph_tagging import project_label_for
    retagged = 0
    for old, new in MAPPING.items():
        # Labels can't be parameterized; project_label_for only emits [a-z0-9_]+.
        old_label, new_label = project_label_for(old), project_label_for(new)
        res = g.query(f"MATCH (e:Episodic {{project: $old}}) SET e.project = $new "
                      f"REMOVE e:`{old_label}` SET e:`{new_label}` RETURN count(e)",
                      {"old": old, "new": new}).result_set[0][0]
        if res != len(by_slug[old]):
            raise SystemExit(f"ABORT after {retagged} slugs: {old} matched {res} nodes, expected {len(by_slug[old])}")
        retagged += res
    print(f"retagged {retagged} episodes in FalkorDB")

    if not scratch:
        with sqlite3.connect(DEFAULT_JOURNAL_PATH) as c:
            changed = 0
            for old, new in MAPPING.items():
                ids = by_slug[old]
                if not ids:
                    continue
                qmarks = ",".join("?" * len(ids))
                changed += c.execute(
                    f"UPDATE derived_memories SET project = ? WHERE project = ? AND memory_id IN ({qmarks})",
                    (new, old, *ids),
                ).rowcount
            if changed != len(all_ids):
                c.rollback()
                raise SystemExit(f"ABORT: journal updated {changed} rows, expected {len(all_ids)} -- rolled back")
        print(f"journal: {changed} derived_memories rows retagged")

        for path in mirrors:
            data = json.loads(path.read_text(encoding="utf-8"))
            old_proj = data.get("project")
            if old_proj in MAPPING:
                data["project"] = MAPPING[old_proj]
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"mirrors: {len(mirrors)} files updated")

    after = counts(g)
    print(f"{args.graph} after:  {after}")
    ok = (after["nodes"], after["edges"]) == (before["nodes"], before["edges"])
    for old in MAPPING:
        ok = ok and after[f"old:{old}"] == 0
    for new in set(MAPPING.values()):
        gained = sum(len(by_slug[o]) for o, n in MAPPING.items() if n == new)
        ok = ok and after[f"new:{new}"] == before[f"new:{new}"] + gained
    if not scratch:
        wait_bgsave(r)
        r.execute_command("SAVE")
        print("Redis SAVE done (post-write)")
    print("VERIFY OK" if ok else "VERIFY FAILED")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
