"""MS9 Phase 2 — merge approved duplicate entities (dry-run by default).

Reads the Phase 1 outputs (auto clusters from dedupe_clusters.csv and
the user's review decisions from dedupe_decisions.json) and folds each
cluster's duplicates into its kept node:

- Every edge touching a duplicate is recreated on the kept node, with its
  properties copied inside FalkorDB (SET r2 = properties(r)). That keeps the
  fact text, dates, the edge uuid (episodes list their fact edges by uuid)
  and the fact_embedding's vector type. RELATES_TO's own endpoint
  properties (source_node_uuid / target_node_uuid) are rewritten to the kept
  node.
- A structural edge the kept node already has (a second MENTIONS from the
  same episode or note, a second IN_PROJECT to the same project) is dropped.
  RELATES_TO facts are never dropped: a fact between two twins becomes a
  self-loop on the kept node and is counted.
- The kept node gets the union of labels, a wiki_type if it had none, the
  duplicate's summary only if its own is empty, and the fuller name where
  the cluster has one (re-embedding name_embedding with CMF's embedder).
  Removed names go into a plain `aliases` list for audit; summaries are not
  rewritten (see FIX-GRAPH-PLAN Phase 1 results).
- The duplicate is deleted once it has no edges left. Each merge is appended
  to a JSONL ledger so it can be replayed onto a rebuilt graph.

Checks: RELATES_TO count unchanged, node count down by exactly the number
of duplicates removed. Refuses any graph not named fixgraph-* (production
merges get their own gate). Idempotent: a duplicate that no longer exists is
skipped.

Usage:
    uv run python scripts/merge_entities.py --graph fixgraph-work            # dry run
    uv run python scripts/merge_entities.py --graph fixgraph-work --apply
"""

from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIX = _ROOT / "imports" / "fixgraph"
STRUCTURAL = ("MENTIONS", "IN_PROJECT", "REFERENCES")
FACTS = "RELATES_TO"
ALLOWED = set(STRUCTURAL) | {FACTS}
_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class Group:
    keep: str
    name: str
    dups: list[str]
    source: str
    names: dict[str, str] = field(default_factory=dict)


def load_groups(clusters_csv: Path, pairs_csv: Path, decisions_json: Path) -> list[Group]:
    """Auto clusters plus approved review decisions, as disjoint merge groups."""
    members: dict[str, dict[str, str]] = {}
    for p in csv.DictReader(open(pairs_csv)):
        if p["cluster"]:
            m = members.setdefault(p["cluster"], {})
            m[p["a_uuid"]] = p["a_name"]
            m[p["b_uuid"]] = p["b_name"]
    groups = []
    for c in csv.DictReader(open(clusters_csv)):
        if c["tier"] == "auto":
            names = members[c["cluster"]]
            groups.append(Group(c["canonical_uuid"], c["canonical"],
                                [u for u in names if u != c["canonical_uuid"]], f"auto:{c['cluster']}", names))
    for d in json.loads(Path(decisions_json).read_text())["decisions"]:
        if d["decision"] == "reject":
            continue
        names = {m["uuid"]: m["name"] for m in d["members"]}
        uuids = d["merge_uuids"] if d["decision"] == "partial" else list(names)
        if d["canonical_uuid"] not in uuids:
            raise ValueError(f"cluster {d['cluster']}: kept node is not in its own merge set")
        groups.append(Group(d["canonical_uuid"], d["canonical_name"],
                            [u for u in uuids if u != d["canonical_uuid"]], f"review:{d['cluster']}",
                            {u: names[u] for u in uuids}))
    seen: set[str] = set()
    for g in groups:
        ids = {g.keep, *g.dups}
        if ids & seen:
            raise ValueError(f"{g.source}: a node is in more than one merge group")
        seen |= ids
    return groups


def final_name(current: str, proposed: str) -> str:
    """Auto-tier only (review names were approved as proposed): keep the current name when the two differ only by separators and it has more of them
    ("Hugging Face" over "HuggingFace", "Wi-Fi" over "WiFi")."""
    squash = lambda n: re.sub(r"[\s\-_]", "", n).lower()
    sep = lambda n: len(re.findall(r"[\s\-_]", n))
    return current if squash(current) == squash(proposed) and sep(current) > sep(proposed) else proposed


def merged_aliases(existing: list[str] | None, removed: list[str], old_name: str, final_name: str) -> list[str]:
    """Distinct removed names (and the kept node's old name if it was renamed), minus the final name."""
    out: list[str] = []
    for n in [*(existing or []), *removed, old_name]:
        if n and n != final_name and n not in out:
            out.append(n)
    return out


# --- graph access ----------------------------------------------------------------

class Db:
    """Typed FalkorDB access: real lists back, parameters passed natively."""

    def __init__(self, graph: str, apply: bool):
        from server.core.falkordb_conn import falkordb_client
        self.g, self.graph, self.apply = falkordb_client().select_graph(graph), graph, apply

    def q(self, cypher: str, write: bool = False, **params) -> list[list]:
        run = self.g.query if write else self.g.ro_query
        return run(cypher, params or None).result_set

    def counts(self) -> dict[str, int]:
        n = self.q("MATCH (n) RETURN count(n)")[0][0]
        e = self.q("MATCH ()-[r]->() RETURN count(r)")[0][0]
        f = self.q(f"MATCH ()-[r:{FACTS}]->() RETURN count(r)")[0][0]
        return {"nodes": n, "edges": e, "facts": f}


def node(db: Db, uuid: str) -> dict | None:
    rows = db.q("MATCH (e:Entity {uuid:$u}) RETURN e.name, labels(e), e.wiki_type, e.summary, e.aliases", u=uuid)
    if not rows:
        return None
    name, labels, wt, summ, aliases = rows[0]
    return {"name": name, "labels": list(labels), "wiki_type": wt or None,
            "summary": summ or "", "aliases": list(aliases) if aliases else []}


def edge_plan(db: Db, dup: str) -> dict[str, int]:
    rows = db.q("MATCH (d:Entity {uuid:$d})-[r]-() RETURN type(r), count(r)", d=dup)
    return {t: c for t, c in rows}


def move_edges(db: Db, dup: str, keep: str) -> dict[str, int]:
    """Recreate every edge of `dup` on `keep` (server-side property copy), then delete the originals."""
    stats: dict[str, int] = {}
    for t in STRUCTURAL:
        made = db.q(f"MATCH (d:Entity {{uuid:$d}})-[r:{t}]->(x), (k:Entity {{uuid:$k}}) "
                    f"WHERE x <> k AND NOT (k)-[:{t}]->(x) CREATE (k)-[r2:{t}]->(x) SET r2 = properties(r) "
                    f"RETURN count(r2)", write=True, d=dup, k=keep)
        made_in = db.q(f"MATCH (x)-[r:{t}]->(d:Entity {{uuid:$d}}), (k:Entity {{uuid:$k}}) "
                       f"WHERE x <> k AND NOT (x)-[:{t}]->(k) CREATE (x)-[r2:{t}]->(k) SET r2 = properties(r) "
                       f"RETURN count(r2)", write=True, d=dup, k=keep)
        gone = db.q(f"MATCH (d:Entity {{uuid:$d}})-[r:{t}]-() DELETE r RETURN count(r)", write=True, d=dup)
        moved = made[0][0] + made_in[0][0]
        if moved or gone[0][0]:
            stats[t] = moved
            stats[f"{t}_dropped_duplicate"] = gone[0][0] - moved
    loops = db.q(f"MATCH (d:Entity {{uuid:$d}})-[r:{FACTS}]->(d), (k:Entity {{uuid:$k}}) "
                 f"CREATE (k)-[r2:{FACTS}]->(k) SET r2 = properties(r), r2.source_node_uuid = $k, "
                 f"r2.target_node_uuid = $k DELETE r RETURN count(r2)", write=True, d=dup, k=keep)[0][0]
    out_self = db.q(f"MATCH (d:Entity {{uuid:$d}})-[r:{FACTS}]->(k:Entity {{uuid:$k}}) RETURN count(r)", d=dup, k=keep)[0][0]
    in_self = db.q(f"MATCH (k:Entity {{uuid:$k}})-[r:{FACTS}]->(d:Entity {{uuid:$d}}) RETURN count(r)", d=dup, k=keep)[0][0]
    fo = db.q(f"MATCH (d:Entity {{uuid:$d}})-[r:{FACTS}]->(x), (k:Entity {{uuid:$k}}) WHERE x <> d "
              f"CREATE (k)-[r2:{FACTS}]->(x) SET r2 = properties(r), r2.source_node_uuid = $k DELETE r "
              f"RETURN count(r2)", write=True, d=dup, k=keep)[0][0]
    fi = db.q(f"MATCH (x)-[r:{FACTS}]->(d:Entity {{uuid:$d}}), (k:Entity {{uuid:$k}}) WHERE x <> d "
              f"CREATE (x)-[r2:{FACTS}]->(k) SET r2 = properties(r), r2.target_node_uuid = $k DELETE r "
              f"RETURN count(r2)", write=True, d=dup, k=keep)[0][0]
    if fo or fi or loops:
        stats[FACTS] = fo + fi + loops
        stats["self_loops_created"] = loops + out_self + in_self
    return stats


async def embed(texts: list[str]) -> list[list[float]]:
    from server.core.config import load_config
    from server.providers.memory_graphiti import _build_embedder
    emb = _build_embedder(load_config())
    return [await emb.create(input_data=[t.replace("\n", " ")]) for t in texts]


def merge_group(db: Db, g: Group, vectors: dict[str, list[float]]) -> dict:
    keep = node(db, g.keep)
    if keep is None:
        raise RuntimeError(f"{g.source}: kept node {g.keep} is missing")
    if g.source.startswith("auto:"):  # review-tier names were approved by the user as proposed
        g.name = final_name(keep["name"], g.name)
    rec = {"source": g.source, "keep_uuid": g.keep, "keep_name_before": keep["name"], "name": g.name,
           "merged": [], "skipped": [], "edges": {}}
    removed_names: list[str] = []
    labels = list(keep["labels"])
    wiki_type, summary = keep["wiki_type"], keep["summary"]
    for dup in g.dups:
        d = node(db, dup)
        if d is None:
            rec["skipped"].append(dup)
            continue
        plan = edge_plan(db, dup)
        unknown = set(plan) - ALLOWED
        if unknown:
            raise RuntimeError(f"{g.source}: {dup} has edge types {unknown}; refusing to merge")
        for lab in d["labels"]:
            if lab not in labels and _LABEL.match(lab):
                labels.append(lab)
        wiki_type = wiki_type or d["wiki_type"]
        summary = summary or d["summary"]
        removed_names.append(d["name"])
        rec["merged"].append({"uuid": dup, "name": d["name"], "edges_before": plan})
        if db.apply:
            for k, v in move_edges(db, dup, g.keep).items():
                rec["edges"][k] = rec["edges"].get(k, 0) + v
            left = db.q("MATCH (d:Entity {uuid:$d})-[r]-() RETURN count(r)", d=dup)[0][0]
            if left:
                raise RuntimeError(f"{g.source}: {dup} still has {left} edges after the move")
            db.q("MATCH (d:Entity {uuid:$d}) DELETE d", write=True, d=dup)
    rec["aliases"] = merged_aliases(keep["aliases"], removed_names, keep["name"], g.name)
    rec["renamed"] = g.name != keep["name"]
    if db.apply and rec["merged"]:
        for lab in labels:
            if lab not in keep["labels"]:
                db.q(f"MATCH (k:Entity {{uuid:$k}}) SET k:{lab}", write=True, k=g.keep)
        db.q("MATCH (k:Entity {uuid:$k}) SET k.aliases = $a", write=True, k=g.keep, a=rec["aliases"])
        if wiki_type and not keep["wiki_type"]:
            db.q("MATCH (k:Entity {uuid:$k}) SET k.wiki_type = $w", write=True, k=g.keep, w=wiki_type)
        if summary and not keep["summary"]:
            db.q("MATCH (k:Entity {uuid:$k}) SET k.summary = $s", write=True, k=g.keep, s=summary)
        if rec["renamed"]:
            db.q("MATCH (k:Entity {uuid:$k}) SET k.name = $n, k.name_embedding = vecf32($v)", write=True,
                 k=g.keep, n=g.name, v=vectors[g.name])
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--clusters", default=str(FIX / "dedupe_clusters.csv"))
    ap.add_argument("--pairs", default=str(FIX / "dedupe_pairs.csv"))
    ap.add_argument("--decisions", default=str(FIX / "dedupe_decisions.json"))
    ap.add_argument("--backup-graph", default=None,
                    help="Name of an existing backup graph with matching node/edge counts (required for non-fixgraph-* graphs)")
    ap.add_argument("--ledger", default=str(FIX / "merge_ledger.jsonl"))
    args = ap.parse_args()
    if not args.graph.startswith("fixgraph-"):
        if not args.backup_graph:
            print(f"refusing {args.graph!r}: non-scratch merges require --backup-graph <name>", file=sys.stderr)
            return 2
        backup_db = Db(args.backup_graph, False)
        backup_counts = backup_db.counts()
        target_db = Db(args.graph, False)
        target_counts = target_db.counts()
        if backup_counts["nodes"] != target_counts["nodes"] or backup_counts["edges"] != target_counts["edges"]:
            print(f"refusing {args.graph!r}: backup counts {backup_counts} do not match target counts {target_counts}", file=sys.stderr)
            return 2
        if args.apply:
            from server.core.falkordb_conn import redis_client
            r = redis_client()
            print(f"Running Redis SAVE before applying merge to {args.graph}...")
            r.save()

    groups = load_groups(Path(args.clusters), Path(args.pairs), Path(args.decisions))
    db = Db(args.graph, args.apply)
    before = db.counts()
    for g in groups:
        k = node(db, g.keep)
        if k and g.source.startswith("auto:"):
            g.name = final_name(k["name"], g.name)
    renames = sorted({g.name for g in groups if (node(db, g.keep) or {}).get("name") not in (None, g.name)})
    vectors = dict(zip(renames, asyncio.run(embed(renames)))) if (args.apply and renames) else {}

    records = [merge_group(db, g, vectors) for g in groups]
    removed = sum(len(r["merged"]) for r in records)
    after = db.counts()
    summary = {"graph": args.graph, "applied": args.apply, "groups": len(groups), "duplicates_removed": removed,
               "skipped_already_merged": sum(len(r["skipped"]) for r in records), "renamed": len(renames),
               "before": before, "after": after}
    if args.apply:
        edge_totals: dict[str, int] = {}
        for r in records:
            for k, v in r["edges"].items():
                edge_totals[k] = edge_totals.get(k, 0) + v
        summary["edges"] = edge_totals
        summary["checks"] = {"facts_unchanged": before["facts"] == after["facts"],
                             "nodes_down_by_removed": before["nodes"] - after["nodes"] == removed}
        stamp = datetime.now(timezone.utc).isoformat()
        with open(args.ledger, "a") as f:
            for r in records:
                if r["merged"]:
                    f.write(json.dumps({"at": stamp, "graph": args.graph} | r) + "\n")
    print(json.dumps(summary, indent=1))
    return 0 if not args.apply or all(summary["checks"].values()) else 3


if __name__ == "__main__":
    sys.exit(main())
