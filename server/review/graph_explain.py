"""`explain()` into Graphiti — MS6b, the differentiation demo.

Extends server.review.explain's journal walk (episode -> derived_memory ->
source events -> journal turns -> thread) one hop further, into the graph
itself: which entities and edges did promotion actually extract from this
episode. See that module's docstring for why the two halves are split —
the journal walk runs over every unpromoted episode a reviewer looks at,
so it stays free of graphiti_core; this module is only meaningful for a
memory_id that has already been promoted, so it's the one that pays for
the import and the network round-trip.

`PromotionStore` is the join key: memory_id -> (episode_name, graph_name)
for whichever graph actually holds it. The Cypher here mirrors the
patterns already exercised by edit_memory in
server/providers/memory_graphiti.py rather than inventing a new style.
"""

from __future__ import annotations

from typing import Any, Optional

from graphiti_core import Graphiti

from server.consolidation.promotion import PromotionStore


def _records(rows: Any) -> list[dict[str, Any]]:
    """Normalize a driver.execute_query(...) result to a list of row dicts.

    FalkorDriver's execute_query returns (records, header, summary); other
    drivers in graphiti_core's test doubles sometimes return bare records.
    Handling both here keeps every call site below a one-liner.
    """
    recs = rows[0] if rows and isinstance(rows[0], list) else (rows or [])
    return [dict(r) for r in recs]


async def explain_graph(
    promotion_store: PromotionStore,
    memory_id: str,
    graphiti: Graphiti,
    graph_name: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Why does the system believe this — all the way into the graph.

    Returns None when `memory_id` was never successfully promoted into the
    graph `graphiti` is connected to; callers should fall back to
    server.review.explain.explain()'s journal-only answer in that case,
    which is meaningful for every episode, promoted or not.

    Returns `found_in_graph: False` (rather than raising) when the ledger
    says promoted but the episode is actually absent — e.g. a graph was
    rebuilt from a different ledger snapshot. That is a real, reportable
    state, not an error in this call.
    """
    row = promotion_store.get(memory_id, graph_name)
    if row is None or row["status"] != "succeeded":
        return None

    episode_name = row["episode_name"]
    graph = row["graph_name"]
    driver = graphiti.driver

    ep_rows = await driver.execute_query(
        "MATCH (e:Episodic {name: $name}) "
        "RETURN e.uuid AS uuid, e.name AS name, e.content AS content, "
        "e.valid_at AS valid_at, e.source_description AS source_description",
        name=episode_name,
    )
    episodes = _records(ep_rows)
    if not episodes:
        return {
            "memory_id": memory_id,
            "episode_name": episode_name,
            "graph_name": graph,
            "found_in_graph": False,
            "entities": [],
            "edges": [],
            "entity_count": 0,
            "edge_count": 0,
        }
    episode = episodes[0]
    uuid = episode["uuid"]

    ent_rows = await driver.execute_query(
        "MATCH (e:Episodic {uuid: $uuid})-[:MENTIONS]->(n:Entity) "
        "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary",
        uuid=uuid,
    )
    entities = _records(ent_rows)

    edge_rows = await driver.execute_query(
        "MATCH (s:Entity)-[r:RELATES_TO]->(t:Entity) WHERE $uuid IN r.episodes "
        "RETURN r.uuid AS uuid, s.name AS source, r.fact AS fact, t.name AS target, "
        "r.valid_at AS valid_at, r.invalid_at AS invalid_at",
        uuid=uuid,
    )
    edges = _records(edge_rows)

    return {
        "memory_id": memory_id,
        "episode_name": episode_name,
        "graph_name": graph,
        "found_in_graph": True,
        "episode_uuid": uuid,
        "episode_content": episode.get("content"),
        "valid_at": episode.get("valid_at"),
        "source_description": episode.get("source_description"),
        "entities": entities,
        "entity_count": len(entities),
        "edges": edges,
        "edge_count": len(edges),
    }
