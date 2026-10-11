"""MS9 Phase 2b — retract legacy episodes before re-extraction (dry-run by default).

For each episode listed in --episodes-file:
1. Retracts its MENTIONS edges to entities.
2. For facts (RELATES_TO) where this episode is in r.episodes:
   - If this episode is the ONLY provenance (len(r.episodes) == 1), delete the fact.
   - If other episodes also support the fact, remove this episode from r.episodes.
3. Deletes any IN_PROJECT edges on the episode, then deletes the Episodic node.
4. Cleans up entities that are now completely orphaned (0 MENTIONS from any episode
   or note, and 0 RELATES_TO edges). Entities supported by other episodes or
   notes remain untouched.
5. Appends each retraction to a JSONL ledger so it is auditable and replayable.

Refuses any graph not named fixgraph-* (scratch graphs only).

Usage:
    uv run python scripts/retract_episodes.py --graph fixgraph-p2b            # dry run
    uv run python scripts/retract_episodes.py --graph fixgraph-p2b --apply
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

FIX = _ROOT / "imports" / "fixgraph"
DEFAULT_EPISODES = FIX / "reextract_episodes.txt"
DEFAULT_LEDGER = FIX / "retract_ledger.jsonl"


class Db:
    def __init__(self, graph: str, apply: bool):
        from server.core.falkordb_conn import falkordb_client
        self.g, self.graph, self.apply = falkordb_client().select_graph(graph), graph, apply

    def q(self, cypher: str, write: bool = False, **params) -> list[list]:
        run = self.g.query if write else self.g.ro_query
        return run(cypher, params or None).result_set

    def counts(self) -> dict[str, int]:
        n = self.q("MATCH (n) RETURN count(n)")[0][0]
        e = self.q("MATCH ()-[r]->() RETURN count(r)")[0][0]
        eps = self.q("MATCH (ep:Episodic) RETURN count(ep)")[0][0]
        ents = self.q("MATCH (e:Entity) RETURN count(e)")[0][0]
        facts = self.q("MATCH ()-[r:RELATES_TO]->() RETURN count(r)")[0][0]
        mentions = self.q("MATCH ()-[r:MENTIONS]->() RETURN count(r)")[0][0]
        return {"nodes": n, "edges": e, "episodes": eps, "entities": ents, "facts": facts, "mentions": mentions}


def retract_episode(db: Db, ep_name: str) -> dict:
    ep_rows = db.q("MATCH (ep:Episodic {name: $n}) RETURN ep.uuid, ep.source_description", n=ep_name)
    if not ep_rows:
        return {"name": ep_name, "found": False}
    ep_uuid = ep_rows[0][0]
    source_desc = ep_rows[0][1] or ""

    mentions = db.q("MATCH (ep:Episodic {uuid: $u})-[r:MENTIONS]->(e:Entity) RETURN e.uuid, e.name", u=ep_uuid)
    mentioned_uuids = [m[0] for m in mentions]

    # Find facts touching this episode
    facts_rows = db.q("MATCH ()-[r:RELATES_TO]->() WHERE $u IN r.episodes RETURN r.uuid, r.fact, r.episodes", u=ep_uuid)
    facts_deleted = []
    facts_disassociated = []
    for f_uuid, f_text, f_eps in facts_rows:
        f_eps_list = list(f_eps) if f_eps else []
        if len(f_eps_list) <= 1:
            facts_deleted.append({"uuid": f_uuid, "fact": f_text})
        else:
            facts_disassociated.append({"uuid": f_uuid, "fact": f_text, "remaining_episodes": len(f_eps_list) - 1})

    rec = {
        "name": ep_name,
        "uuid": ep_uuid,
        "source_description": source_desc,
        "found": True,
        "mentions_count": len(mentions),
        "facts_deleted": len(facts_deleted),
        "facts_disassociated": len(facts_disassociated),
        "orphaned_entities": 0,
    }

    if db.apply:
        # Delete exclusive facts
        if facts_deleted:
            del_uuids = [f["uuid"] for f in facts_deleted]
            db.q("MATCH ()-[r:RELATES_TO]->() WHERE r.uuid IN $uuids DELETE r", write=True, uuids=del_uuids)

        # Disassociate shared facts
        if facts_disassociated:
            dis_uuids = [f["uuid"] for f in facts_disassociated]
            db.q("MATCH ()-[r:RELATES_TO]->() WHERE r.uuid IN $uuids "
                 "SET r.episodes = [x IN r.episodes WHERE x <> $u]", write=True, uuids=dis_uuids, u=ep_uuid)

        # Delete structural edges and the episode node
        db.q("MATCH (ep:Episodic {uuid: $u})-[r:MENTIONS]->() DELETE r", write=True, u=ep_uuid)
        db.q("MATCH (ep:Episodic {uuid: $u})-[r:IN_PROJECT]->() DELETE r", write=True, u=ep_uuid)
        db.q("MATCH (ep:Episodic {uuid: $u}) DELETE ep", write=True, u=ep_uuid)

        # Check for completely orphaned entities among those mentioned
        if mentioned_uuids:
            orphans = db.q(
                "MATCH (e:Entity) WHERE e.uuid IN $uuids "
                "AND NOT ()-[:MENTIONS]->(e) "
                "AND NOT (e)-[:RELATES_TO]-() "
                "RETURN e.uuid", uuids=mentioned_uuids
            )
            orphan_uuids = [o[0] for o in orphans]
            if orphan_uuids:
                db.q("MATCH (e:Entity) WHERE e.uuid IN $uuids DELETE e", write=True, uuids=orphan_uuids)
                rec["orphaned_entities"] = len(orphan_uuids)

    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--graph", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--episodes-file", default=str(DEFAULT_EPISODES))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    args = ap.parse_args()

    if not args.graph.startswith("fixgraph-"):
        print(f"refusing {args.graph!r}: retractions run on fixgraph-* scratch graphs only", file=sys.stderr)
        return 2

    ep_names = [line.strip() for line in Path(args.episodes_file).read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    db = Db(args.graph, args.apply)
    before = db.counts()

    records = [retract_episode(db, name) for name in ep_names]
    found_count = sum(1 for r in records if r["found"])
    facts_deleted = sum(r.get("facts_deleted", 0) for r in records)
    facts_disassociated = sum(r.get("facts_disassociated", 0) for r in records)
    orphans_cleaned = sum(r.get("orphaned_entities", 0) for r in records)
    after = db.counts()

    summary = {
        "graph": args.graph,
        "applied": args.apply,
        "episodes_requested": len(ep_names),
        "episodes_found": found_count,
        "facts_deleted": facts_deleted,
        "facts_disassociated": facts_disassociated,
        "orphaned_entities_removed": orphans_cleaned,
        "before": before,
        "after": after,
    }

    if args.apply:
        stamp = datetime.now(timezone.utc).isoformat()
        with open(args.ledger, "a") as f:
            for r in records:
                if r.get("found"):
                    f.write(json.dumps({"at": stamp, "graph": args.graph} | r) + "\n")

    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
