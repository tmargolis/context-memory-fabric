#!/usr/bin/env python3
"""Promote MS9 Phase 4 wiki relationship edges with canonical entity resolution.

Resolves entity endpoints to canonical production entities (case-insensitive / normalized),
preventing duplicate node creation. Genuine new entities are created once each.
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
DEFAULT_TARGET = "fixgraph-canonical-test"


def _create_args(props: dict, alias: str = "x") -> tuple[str, dict]:
    """SET clause + params copying props verbatim, vector embeddings via vecf32."""
    sets, params = [], {}
    for i, (k, v) in enumerate(props.items()):
        params[f"p{i}"] = v
        if k.endswith("_embedding") and isinstance(v, list):
            sets.append(f"{alias}.`{k}` = vecf32($p{i})")
        else:
            sets.append(f"{alias}.`{k}` = $p{i}")
    return ", ".join(sets), params


def run_promotion(target_graph_name: str, apply: bool = False):
    db = falkordb_client()
    g_target = db.select_graph(target_graph_name)
    g_src = db.select_graph(SOURCE_GRAPH)

    # 1. Inspect initial state
    target_nodes_before = g_target.query("MATCH (n) RETURN count(n)").result_set[0][0]
    target_edges_before = g_target.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    target_wiki_before = g_target.query("MATCH ()-[r:RELATES_TO]->() WHERE r.source = 'wiki' RETURN count(r)").result_set[0][0]

    src_wiki_edges = g_src.query("MATCH ()-[r:RELATES_TO]->() WHERE r.source = 'wiki' RETURN count(r)").result_set[0][0]

    print(f"Source graph '{SOURCE_GRAPH}': wiki relationship edges = {src_wiki_edges}")
    print(f"Target graph '{target_graph_name}': nodes = {target_nodes_before}, edges = {target_edges_before}, wiki_edges = {target_wiki_before}")

    # 2. Index target entity nodes
    target_entity_rows = g_target.query(
        "MATCH (n:Entity) WHERE n.name IS NOT NULL "
        "RETURN n.name, n.uuid, labels(n), properties(n)"
    ).result_set

    # Map name -> uuid (exact and case-folded)
    exact_name_to_uuid = {}
    lower_name_to_uuid = {}
    target_existing_uuids = set()

    for row in target_entity_rows:
        name = row[0].strip()
        uuid = row[1]
        target_existing_uuids.add(uuid)
        if name not in exact_name_to_uuid:
            exact_name_to_uuid[name] = uuid
        name_lower = name.lower()
        if name_lower not in lower_name_to_uuid:
            lower_name_to_uuid[name_lower] = uuid

    print(f"Target entities indexed: {len(target_entity_rows)} nodes, {len(exact_name_to_uuid)} unique exact names, {len(lower_name_to_uuid)} unique lower names")

    # 3. Retrieve all wiki edges and their endpoints from source
    print("Reading wiki relationship edges from source...")
    src_edge_data = g_src.query(
        "MATCH (a)-[r:RELATES_TO]->(b) WHERE r.source = 'wiki' "
        "RETURN a.name, b.name, labels(a), properties(a), labels(b), properties(b), type(r), properties(r), r.uuid"
    ).result_set

    print(f"Loaded {len(src_edge_data)} edges from source.")

    # 4. Resolve entity endpoints
    canonical_uuid_map = {}  # name -> target_uuid
    nodes_to_create = {}     # name -> (labels, props)
    exact_matches = set()
    case_matches = set()

    for a_name, b_name, a_labels, a_props, b_labels, b_props, _, _, _ in src_edge_data:
        for name, labels, props in [(a_name, a_labels, a_props), (b_name, b_labels, b_props)]:
            name_clean = name.strip() if name else ""
            if not name_clean or name_clean in canonical_uuid_map:
                continue

            if name_clean in exact_name_to_uuid:
                canonical_uuid_map[name_clean] = exact_name_to_uuid[name_clean]
                exact_matches.add(name_clean)
            elif name_clean.lower() in lower_name_to_uuid:
                canonical_uuid_map[name_clean] = lower_name_to_uuid[name_clean.lower()]
                case_matches.add(name_clean)
            else:
                # Genuinely new entity
                if name_clean not in nodes_to_create:
                    # Pick best representation
                    nodes_to_create[name_clean] = (labels, props)

    print(f"Endpoint resolution:")
    print(f"  Exact matched to existing: {len(exact_matches)}")
    print(f"  Case-insensitive matched to existing: {len(case_matches)}")
    print(f"  Truly new entities to create: {len(nodes_to_create)}")

    if not apply:
        print("\n[DRY RUN] No changes made. Pass --apply to execute.")
        return

    # 5. Create genuinely new entities in target
    print(f"\nCreating {len(nodes_to_create)} new canonical entity nodes in target...")
    created_count = 0
    for name, (labels, props) in nodes_to_create.items():
        sets, params = _create_args(props, alias="x")
        lbl_str = ":".join(f"`{l}`" for l in labels)
        g_target.query(f"CREATE (x:{lbl_str}) SET {sets}", params)
        canonical_uuid_map[name] = props.get("uuid")
        created_count += 1
        if created_count % 500 == 0:
            print(f"  Created {created_count}/{len(nodes_to_create)} nodes...")

    print(f"  All {created_count} nodes created successfully.")

    # 6. Retrieve existing target edges to prevent collisions
    target_edge_uuids = {
        row[0] for row in g_target.query("MATCH ()-[r]->() WHERE r.uuid IS NOT NULL RETURN r.uuid").result_set
    }

    # 7. Apply edges
    print(f"\nApplying {len(src_edge_data)} wiki relationship edges...")
    edges_created = 0
    edges_updated = 0

    for a_name, b_name, _, _, _, _, rtype, eprops, e_uuid in src_edge_data:
        s_uuid = canonical_uuid_map.get(a_name.strip() if a_name else "")
        t_uuid = canonical_uuid_map.get(b_name.strip() if b_name else "")

        if not s_uuid or not t_uuid:
            print(f"Warning: Missing endpoint for edge {a_name} -> {b_name}, skipping.")
            continue

        if e_uuid in target_edge_uuids:
            # Edge exists with identical UUID, update its properties
            q_update = (
                "MATCH ()-[r]->() WHERE r.uuid = $u "
                "SET r.source = 'wiki', r.note_path = $np, r.section_heading = $sh"
            )
            g_target.query(q_update, {
                "u": e_uuid,
                "np": eprops.get("note_path"),
                "sh": eprops.get("section_heading"),
            })
            edges_updated += 1
        else:
            # Create new edge connecting canonical endpoints
            # Ensure edge props source_node_uuid and target_node_uuid match canonical endpoints
            eprops_copy = dict(eprops)
            eprops_copy["source_node_uuid"] = s_uuid
            eprops_copy["target_node_uuid"] = t_uuid
            if "source_uuid" in eprops_copy:
                eprops_copy["source_uuid"] = s_uuid
            if "target_uuid" in eprops_copy:
                eprops_copy["target_uuid"] = t_uuid

            sets, params = _create_args(eprops_copy, alias="r")
            params["s_uuid"] = s_uuid
            params["t_uuid"] = t_uuid

            q_create = (
                f"MATCH (s {{uuid: $s_uuid}}), (t {{uuid: $t_uuid}}) "
                f"CREATE (s)-[r:`{rtype}`]->(t) "
                f"SET {sets}"
            )
            g_target.query(q_create, params)
            edges_created += 1

        if (edges_created + edges_updated) % 1000 == 0:
            print(f"  Processed {edges_created + edges_updated}/{len(src_edge_data)} edges (created {edges_created}, updated {edges_updated})...")

    # 8. Final verification report
    target_nodes_after = g_target.query("MATCH (n) RETURN count(n)").result_set[0][0]
    target_edges_after = g_target.query("MATCH ()-[r]->() RETURN count(r)").result_set[0][0]
    target_wiki_after = g_target.query("MATCH ()-[r:RELATES_TO]->() WHERE r.source = 'wiki' RETURN count(r)").result_set[0][0]

    # Duplicate names check
    dup_names_after = g_target.query(
        "MATCH (n:Entity) WHERE n.name IS NOT NULL "
        "RETURN n.name, count(n) AS c ORDER BY c DESC"
    ).result_set
    dups_after = [r for r in dup_names_after if r[1] > 1]

    print("\n" + "=" * 60)
    print("CANONICAL PROMOTION VERIFICATION REPORT")
    print("=" * 60)
    print(f"Nodes: {target_nodes_before} -> {target_nodes_after} (+{target_nodes_after - target_nodes_before})")
    print(f"Edges: {target_edges_before} -> {target_edges_after} (+{target_edges_after - target_edges_before})")
    print(f"Wiki Edges: {target_wiki_before} -> {target_wiki_after}")
    print(f"Duplicate Entity Names in target graph: {len(dups_after)}")
    if dups_after:
        print("Top remaining duplicate entity names:")
        for name, count in dups_after[:5]:
            print(f"  {name}: {count} nodes")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Canonical wiki relationship promotion")
    parser.add_argument("--graph", default=DEFAULT_TARGET, help="Target graph name")
    parser.add_argument("--apply", action="store_true", help="Execute writes")
    args = parser.parse_args()

    run_promotion(args.graph, apply=args.apply)
