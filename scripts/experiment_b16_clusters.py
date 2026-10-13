"""B16 scratch copy and local career clustering inputs. No Spark calls.

Copy is opt-in, refuses existing targets, and waits for background persistence
before and after GRAPH.COPY. Subsequent inspection uses GRAPH.RO_QUERY only.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE = "mem-fabric-local"


def counts(graph):
    from server.core.falkordb_conn import falkordb_client
    g = falkordb_client().select_graph(graph)
    return {key: g.ro_query(q).result_set[0][0] for key, q in {
        "nodes": "MATCH (n) RETURN count(n)",
        "edges": "MATCH ()-[r]->() RETURN count(r)",
        "episodes": "MATCH (e:Episodic) RETURN count(e)",
    }.items()}


def wait_persistence(client):
    while client.info("persistence")["rdb_bgsave_in_progress"]:
        print("Waiting for background persistence", flush=True)
        time.sleep(5)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default="20261013")
    ap.add_argument("--copy", action="store_true")
    args = ap.parse_args()
    from server.core.falkordb_conn import redis_client, falkordb_client
    # A long GRAPH.COPY must never be retried after a socket timeout: the
    # original command may have succeeded even when its reply was lost.
    from redis.backoff import NoBackoff
    from redis.retry import Retry
    r = redis_client(socket_timeout=180, retry=Retry(NoBackoff(), 0))
    target = f"mem-fabric-b16-merge-{args.date}"
    out = ROOT / "imports/replay"
    out.mkdir(parents=True, exist_ok=True)
    if args.copy:
        existing = {x.decode() if isinstance(x, bytes) else x for x in r.execute_command("GRAPH.LIST")}
        if target in existing:
            raise RuntimeError("Scratch target exists; refusing to overwrite")
        before = counts(SOURCE)
        (out / f"b16-copy-start-{args.date}.json").write_text(json.dumps(
            {"source": SOURCE, "target": target, "source_before": before}, indent=2) + "\n")
        wait_persistence(r)
        r.execute_command("GRAPH.COPY", SOURCE, target)
        wait_persistence(r)
        record = {"source": SOURCE, "target": target, "source_before": before,
                  "source_after": counts(SOURCE), "scratch": counts(target),
                  "recorded_at": datetime.now(timezone.utc).isoformat()}
        (out / f"b16-copy-counts-{args.date}.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record), flush=True)
    g = falkordb_client().select_graph(target)
    rows = g.ro_query(
        "MATCH (e:Episodic) WHERE e.name STARTS WITH 'claude-cowork-career-' "
        "AND e.project = 'career' AND toString(e.created_at) >= '2026-10-03' "
        "OPTIONAL MATCH (e)-[:MENTIONS]->(o:Organization) "
        "RETURN e.name, e.content, e.source_description, toString(e.created_at), "
        "toString(e.valid_at), collect(DISTINCT o.name) ORDER BY e.name"
    ).result_set
    episodes = [dict(zip(["name", "content", "source_description", "created_at", "valid_at", "orgs"], row))
                for row in rows]
    (out / f"b16-cluster-inputs-{args.date}.json").write_text(json.dumps(episodes, indent=2) + "\n")
    print(f"Scoped episodes: {len(episodes)}; saved local clustering inputs", flush=True)


if __name__ == "__main__":
    main()
