"""Fold double-promoted episodes into one node each (dry-run by default).

A memory promoted twice has two Episodic nodes with the same content, each
extracted separately, so each carries facts the other lacks. Deleting one
would drop those facts, so the duplicate is folded into the kept node:

1. Facts (RELATES_TO) listing the duplicate in `r.episodes` list the kept
   node instead (or just drop the duplicate where the kept node is already
   there).
2. Each MENTIONS edge from the duplicate is recreated, properties copied, from
   the kept node, unless the kept node already mentions that entity.
3. The duplicate node is deleted with its own remaining edges (MENTIONS,
   IN_PROJECT).
4. Each fold is appended to a JSONL ledger.

Nodes are addressed by uuid; two of the duplicates share one name.

    uv run python scripts/fold_duplicate_episodes.py --graph fixgraph-folddup          # dry run
    uv run python scripts/fold_duplicate_episodes.py --graph fixgraph-folddup --apply
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LEDGER = _ROOT / "imports" / "fixgraph" / "fold_duplicates_ledger.jsonl"

# (keep uuid, fold-away uuid), chosen 2026-10-06 by connectivity and eval gold:
# docs/plan-active.md → Double-promoted memories.
FOLDS = [
    ("f48474cc", "12b3ba24"),  # claude-career-navigator-006 (C16 gold) <- -031
    ("5d27dc20", "3f2b0c85"),  # claude-career-navigator-005 <- -030 (empty)
    ("2f6d3d97", "71e68708"),  # chatgpt-photo-006 <- -009
    ("697be284", "f3278fe5"),  # claude-career-navigator-029 <- -004 (empty)
    ("0b030fc3", "f710c9a8"),  # claude-cowork-interlock-005 <- same-name twin
]


class Db:
    def __init__(self, graph: str, apply: bool):
        from falkordb import FalkorDB
        self.g, self.apply = FalkorDB().select_graph(graph), apply

    def q(self, cypher: str, write: bool = False, **params) -> list[list]:
        return (self.g.query if write else self.g.ro_query)(cypher, params or None).result_set

    def counts(self) -> dict[str, int]:
        one = lambda c: self.q(c)[0][0]
        return {
            "nodes": one("MATCH (n) RETURN count(n)"),
            "edges": one("MATCH ()-[r]->() RETURN count(r)"),
            "episodes": one("MATCH (e:Episodic) RETURN count(e)"),
            "facts": one("MATCH ()-[r:RELATES_TO]->() RETURN count(r)"),
            "mentions": one("MATCH ()-[r:MENTIONS]->() RETURN count(r)"),
        }

    def full_uuid(self, prefix: str) -> tuple[str, str, str]:
        rows = self.q("MATCH (e:Episodic) WHERE e.uuid STARTS WITH $p RETURN e.uuid, e.name, e.content", p=prefix)
        if len(rows) != 1:
            raise SystemExit(f"uuid prefix {prefix!r} matched {len(rows)} episodes")
        return rows[0][0], rows[0][1], rows[0][2]


def fold(db: Db, keep_prefix: str, dup_prefix: str) -> dict:
    keep, keep_name, keep_content = db.full_uuid(keep_prefix)
    dup, dup_name, dup_content = db.full_uuid(dup_prefix)
    if keep_content != dup_content:
        raise SystemExit(f"{keep_name} and {dup_name} differ in content; not duplicates")

    facts = db.q("MATCH ()-[r:RELATES_TO]->() WHERE $d IN r.episodes RETURN r.uuid, $k IN r.episodes", d=dup, k=keep)
    mentions = db.q(
        "MATCH (d:Episodic {uuid: $d})-[:MENTIONS]->(x) "
        "OPTIONAL MATCH (k:Episodic {uuid: $k})-[m:MENTIONS]->(x) RETURN x.uuid, x.name, m IS NOT NULL",
        d=dup, k=keep,
    )
    rec = {
        "keep_uuid": keep, "keep_name": keep_name, "dup_uuid": dup, "dup_name": dup_name,
        "facts_moved": sum(1 for _, has_keep in facts if not has_keep),
        "facts_already_shared": sum(1 for _, has_keep in facts if has_keep),
        "mentions_moved": [name for _, name, has in mentions if not has],
    }
    if not db.apply:
        return rec

    db.q("MATCH ()-[r:RELATES_TO]->() WHERE $d IN r.episodes "
         "SET r.episodes = CASE WHEN $k IN r.episodes THEN [x IN r.episodes WHERE x <> $d] "
         "ELSE [x IN r.episodes | CASE WHEN x = $d THEN $k ELSE x END] END",
         write=True, d=dup, k=keep)
    db.q("MATCH (d:Episodic {uuid: $d})-[m:MENTIONS]->(x), (k:Episodic {uuid: $k}) "
         "WHERE NOT (k)-[:MENTIONS]->(x) CREATE (k)-[m2:MENTIONS]->(x) SET m2 = properties(m)",
         write=True, d=dup, k=keep)
    db.q("MATCH (d:Episodic {uuid: $d}) DETACH DELETE d", write=True, d=dup)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    args = ap.parse_args()

    db = Db(args.graph, args.apply)
    before = db.counts()
    records = [fold(db, keep, dup) for keep, dup in FOLDS]
    after = db.counts()
    for r in records:
        print(f"{r['dup_name']} ({r['dup_uuid'][:8]}) -> {r['keep_name']} ({r['keep_uuid'][:8]}): "
              f"{r['facts_moved']} facts moved, {r['facts_already_shared']} already shared, "
              f"{len(r['mentions_moved'])} mentions moved")
    print("before:", before)
    print("after: ", after, "" if args.apply else "(dry run)")
    dangling = db.q("MATCH ()-[r:RELATES_TO]->() WHERE any(u IN r.episodes WHERE u IN $d) RETURN count(r)",
                    d=[r["dup_uuid"] for r in records])[0][0]
    print("facts still pointing at a folded episode:", dangling)
    if args.apply:
        Path(args.ledger).parent.mkdir(parents=True, exist_ok=True)
        with open(args.ledger, "a", encoding="utf-8") as f:
            at = datetime.now(timezone.utc).isoformat()
            for r in records:
                f.write(json.dumps({"at": at, "graph": args.graph, **r}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
