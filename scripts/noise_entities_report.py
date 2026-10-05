"""MS9 Phase 1b — noise-entity candidate report (read-only).

MS4e's debris filter only acts on new episodes, so the 634 legacy episodes
and the wiki layer still hold implementation debris as entities: file names,
code identifiers, numbers, local labels. This report finds them graph-wide
with MS4e's own noise_category() and proposes what a cleanup could do with
each. Nothing is deleted here.

Dispositions:
- drop-candidate: flagged, and no RELATES_TO fact touches it. Deleting it
  loses only MENTIONS edges, which carry no text.
- has-facts: flagged, but facts are attached. DETACH DELETE would remove
  those facts too, and fact text is real information even when the entity
  name is debris, so these need a decision.
- in-merge: flagged, but also part of an approved Phase 1 merge cluster
  (--decisions). The merge decides its fate.

Wiki-only flagged entities are reported the same way; noise_category() was
calibrated on episode debris, so they deserve a spot-check before any rule
is trusted there.

Reads with GRAPH.RO_QUERY only.

Usage:
    uv run python scripts/noise_entities_report.py --graph fixgraph-work \\
        --decisions imports/fixgraph/dedupe_decisions.json --out-dir imports/fixgraph
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from server.providers.entity_filter import noise_category  # noqa: E402  (after the sys.path insert)


def disposition(relates: int, in_merge: bool) -> str:
    if in_merge:
        return "in-merge"
    return "has-facts" if relates else "drop-candidate"


def _ro(r, graph: str, cypher: str) -> list[list]:
    return r.execute_command("GRAPH.RO_QUERY", graph, cypher)[1]


def _s(v):
    return v.decode() if isinstance(v, bytes) else v


def merge_uuids(decisions_path: str | None) -> set[str]:
    """Every node uuid in an approved (merge or partial) Phase 1 cluster."""
    if not decisions_path or not Path(decisions_path).exists():
        return set()
    out: set[str] = set()
    for d in json.loads(Path(decisions_path).read_text())["decisions"]:
        if d["decision"] == "merge":
            out |= {m["uuid"] for m in d["members"]}
        elif d["decision"] == "partial":
            out |= set(d.get("merge_uuids", []))
    return out


def load(r, graph: str, page: int = 500) -> list[dict]:
    rows = []
    total = _ro(r, graph, "MATCH (e:Entity) RETURN count(e)")[0][0]
    for skip in range(0, total, page):
        for uuid, name, wt, eps, notes, rel in _ro(r, graph, f"""
                MATCH (e:Entity) WITH e ORDER BY e.uuid SKIP {skip} LIMIT {page}
                OPTIONAL MATCH (ep:Episodic)-[:MENTIONS]->(e) WITH e, count(DISTINCT ep) AS eps
                OPTIONAL MATCH (n:Note)-[:MENTIONS]->(e) WITH e, eps, count(DISTINCT n) AS notes
                OPTIONAL MATCH (e)-[x:RELATES_TO]-(:Entity)
                RETURN e.uuid, e.name, e.wiki_type, eps, notes, count(x)"""):
            rows.append({"uuid": _s(uuid), "name": _s(name) or "", "wiki_type": _s(wt) or "",
                         "episodes": eps, "notes": notes, "relates": rel})
    return rows


def details(r, graph: str, uuid: str) -> tuple[list[str], list[str]]:
    """Up to two fact texts, and up to three mentioning episode names / note paths."""
    esc = uuid.replace("'", "\\'")
    facts = [_s(f[0]) for f in _ro(r, graph, f"MATCH (e:Entity {{uuid:'{esc}'}})-[x:RELATES_TO]-(:Entity) "
                                             "RETURN x.fact LIMIT 2") if f[0]]
    src = [_s(s[0]) for s in _ro(r, graph, f"MATCH (s)-[:MENTIONS]->(e:Entity {{uuid:'{esc}'}}) "
                                           "RETURN coalesce(s.note_path, s.name) LIMIT 3") if s[0]]
    return facts, src


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", default="fixgraph-work")
    ap.add_argument("--decisions", default=str(_ROOT / "imports" / "fixgraph" / "dedupe_decisions.json"))
    ap.add_argument("--out-dir", default=str(_ROOT / "imports" / "fixgraph"))
    args = ap.parse_args()

    import redis
    r = redis.Redis()
    merging = merge_uuids(args.decisions)
    out = []
    for e in load(r, args.graph):
        cat = noise_category(e["name"])
        if not cat:
            continue
        facts, src = details(r, args.graph, e["uuid"])
        side = "episode" if e["episodes"] else ("wiki-only" if e["notes"] else "unmentioned")
        out.append(e | {"category": cat, "side": side,
                        "disposition": disposition(e["relates"], e["uuid"] in merging),
                        "facts": " || ".join(facts), "sources": " | ".join(src)})
    out.sort(key=lambda x: (x["disposition"], x["category"], x["side"], x["name"].lower()))
    Path(args.out_dir).mkdir(parents=True, exist_ok=True)
    with open(Path(args.out_dir) / "noise_candidates.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]) if out else ["uuid"])
        w.writeheader(); w.writerows(out)
    summary = {
        "graph": args.graph, "flagged": len(out),
        "by_disposition": dict(Counter(x["disposition"] for x in out)),
        "by_category": dict(Counter(x["category"] for x in out)),
        "by_side": dict(Counter(x["side"] for x in out)),
        "facts_at_risk": sum(x["relates"] for x in out if x["disposition"] == "has-facts"),
    }
    (Path(args.out_dir) / "noise_summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
