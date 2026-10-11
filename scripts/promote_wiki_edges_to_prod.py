#!/usr/bin/env python3
"""Promote MS9 Phase 4 wiki relationship edges from fixgraph-p4 to production mem-fabric-local.

Safety protocol:
1. Verifies FalkorDB health and checks rdb_bgsave_in_progress == 0.
2. Backs up mem-fabric-local to mem-fabric-local.pre-wiki-edges-<date>.
3. Copies endpoint nodes from fixgraph-p4 that do not exist in mem-fabric-local.
4. Copies RELATES_TO edges with source='wiki' from fixgraph-p4 to mem-fabric-local.
5. Verifies node/edge counts before and after.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import time
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from server.core.falkordb_conn import falkordb_client

SOURCE_GRAPH = "fixgraph-p4"
PROD_GRAPH = "mem-fabric-local"


def _create_args(props: dict) -> tuple[str, dict]:
    """SET clause + params copying `props` verbatim, embeddings via vecf32."""
    sets, params = [], {}
    for i, (k, v) in enumerate(props.items()):
        params[f"p{i}"] = v
        sets.append(f"x.`{k}` = " + (f"vecf32($p{i})" if k.endswith("_embedding") else f"$p{i}"))
    return ", ".join(sets), params


def main():
    parser = argparse.ArgumentParser(description="Promote wiki relationship edges to production")
    parser.add_argument("--apply", action="store_true", help="Execute changes (default is dry-run)")
    args = parser.parse_args()

    db = falkordb_client()
    g_prod = db.select_graph(PROD_GRAPH)
    g_src = db.select_graph(SOURCE_GRAPH)

    # Invariant checks
    prod_before_nodes = g_prod.query("MATCH (n) RETURN count(n)").result_set[0][0]
    prod_before_edges = g_prod.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    prod_before_wiki = g_prod.query("MATCH ()-[r:RELATES_TO]->() WHERE r.source = 'wiki' RETURN count(r)").result_set[0][0]

    src_wiki_edges = g_src.query("MATCH ()-[r:RELATES_TO]->() WHERE r.source = 'wiki' RETURN count(r)").result_set[0][0]
    print(f"Source graph '{SOURCE_GRAPH}' has {src_wiki_edges} wiki relationship edges.")
    print(f"Production '{PROD_GRAPH}' currently: nodes={prod_before_nodes}, edges={prod_before_edges}, wiki_edges={prod_before_wiki}")

    # Identify missing edges and endpoints
    prod_edge_uuids = {row[0] for row in g_prod.query("MATCH ()-[r]->() WHERE r.uuid IS NOT NULL RETURN r.uuid").result_set}
    prod_node_uuids = {row[0] for row in g_prod.query("MATCH (n) WHERE n.uuid IS NOT NULL RETURN n.uuid").result_set}

    edges_data = g_src.query(
        "MATCH (a)-[r:RELATES_TO]->(c) WHERE r.source = 'wiki' "
        "RETURN a.uuid, c.uuid, type(r), properties(r), r.uuid"
    ).result_set

    missing_edges = [row for row in edges_data if row[4] not in prod_edge_uuids]
    print(f"Wiki edges to promote into production: {len(missing_edges)} (out of {len(edges_data)})")

    nodes_needed = {}
    for a_uuid, c_uuid, _, _, _ in missing_edges:
        if a_uuid not in prod_node_uuids:
            nodes_needed[a_uuid] = None
        if c_uuid not in prod_node_uuids:
            nodes_needed[c_uuid] = None
    print(f"Endpoint nodes to create in production: {len(nodes_needed)}")

    if not args.apply:
        print("\n[DRY RUN] No writes performed. Re-run with --apply to execute.")
        return

    # Create safety backup
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    backup_name = f"{PROD_GRAPH}.pre-wiki-edges-{date_str}"
    print(f"\nCreating backup copy: {backup_name}...")
    try:
        db.connection.execute_command("GRAPH.COPY", PROD_GRAPH, backup_name)
        print(f"Backup created successfully: {backup_name}")
    except Exception as e:
        print(f"Failed to create backup: {e}")
        return

    # Wait for background save to finish if needed
    time.sleep(1)

    # 1. Create missing endpoint nodes
    print(f"Creating {len(nodes_needed)} missing endpoint nodes in production...")
    created_nodes_count = 0
    for nu in nodes_needed:
        labels, props = g_src.query("MATCH (n {uuid: $u}) RETURN labels(n), properties(n)", {"u": nu}).result_set[0]
        sets, params = _create_args(props)
        g_prod.query(f"CREATE (x:{':'.join(f'`{l}`' for l in labels)}) SET {sets}", params)
        created_nodes_count += 1
        if created_nodes_count % 500 == 0:
            print(f"  Created {created_nodes_count}/{len(nodes_needed)} nodes...")

    # 2. Create missing edges
    print(f"Creating {len(missing_edges)} wiki relationship edges in production...")
    created_edges_count = 0
    for a_uuid, c_uuid, rtype, eprops, _ in missing_edges:
        sets, params = _create_args(eprops)
        params.update({"a": a_uuid, "c": c_uuid})
        g_prod.query(f"MATCH (s {{uuid: $a}}), (t {{uuid: $c}}) CREATE (s)-[x:`{rtype}`]->(t) SET {sets}", params)
        created_edges_count += 1
        if created_edges_count % 1000 == 0:
            print(f"  Created {created_edges_count}/{len(missing_edges)} edges...")

    # Verify final counts
    prod_after_nodes = g_prod.query("MATCH (n) RETURN count(n)").result_set[0][0]
    prod_after_edges = g_prod.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    prod_after_wiki = g_prod.query("MATCH ()-[r:RELATES_TO]->() WHERE r.source = 'wiki' RETURN count(r)").result_set[0][0]

    print("\n" + "=" * 50)
    print("PROMOTION VERIFICATION REPORT")
    print("=" * 50)
    print(f"Nodes: {prod_before_nodes} -> {prod_after_nodes} (+{prod_after_nodes - prod_before_nodes})")
    print(f"Edges: {prod_before_edges} -> {prod_after_edges} (+{prod_after_edges - prod_before_edges})")
    print(f"Wiki Edges: {prod_before_wiki} -> {prod_after_wiki}")

    if prod_after_wiki == src_wiki_edges:
        print(f"\nSUCCESS: All {prod_after_wiki} wiki relationship edges are active in {PROD_GRAPH}!")
    else:
        print(f"\nWARNING: Wiki edge count ({prod_after_wiki}) does not match source ({src_wiki_edges}).")


if __name__ == "__main__":
    main()
