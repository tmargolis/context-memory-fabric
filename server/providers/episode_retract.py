"""Remove one episode and everything extracted from it, by uuid.

The async, library form of scripts/retract_episodes.py (MS9 Phase 2b), used
by edit_memory's re-extraction path. For the episode:

1. Facts (RELATES_TO) it alone supports are deleted; facts other episodes
   also support only lose this episode from `r.episodes`.
2. Its MENTIONS and IN_PROJECT edges are deleted, then the episode node.
3. Entities it mentioned that are now fully unconnected (no MENTIONS from any
   episode or note, no RELATES_TO) are deleted. Anything still referenced
   stays.

Looked up by uuid only, never by name: Phase 2b's damage came from mapping
names to memories through stale ledger rows (FIX-GRAPH-PLAN.md, Phase 2b
repair), and edit_memory briefly has two episodes with one name.
"""

from __future__ import annotations

from typing import Any


def _rows(result: Any) -> list:
    return result[0] if result and isinstance(result[0], list) else (result or [])


async def retract_episode(driver: Any, episode_uuid: str, *, apply: bool = True) -> dict[str, Any]:
    """Retract one episode. `apply=False` reports what would change without writing."""
    ep = _rows(await driver.execute_query(
        "MATCH (ep:Episodic {uuid: $u}) RETURN ep.name AS name, ep.source_description AS source_description",
        u=episode_uuid,
    ))
    if not ep:
        return {"uuid": episode_uuid, "found": False}

    mentions = _rows(await driver.execute_query(
        "MATCH (ep:Episodic {uuid: $u})-[:MENTIONS]->(e:Entity) RETURN e.uuid AS uuid, e.name AS name",
        u=episode_uuid,
    ))
    facts = _rows(await driver.execute_query(
        "MATCH ()-[r:RELATES_TO]->() WHERE $u IN r.episodes RETURN r.uuid AS uuid, r.fact AS fact, r.episodes AS episodes",
        u=episode_uuid,
    ))
    deleted = [{"uuid": f["uuid"], "fact": f["fact"]} for f in facts if len(f["episodes"] or []) <= 1]
    detached = [{"uuid": f["uuid"], "fact": f["fact"]} for f in facts if len(f["episodes"] or []) > 1]

    rec: dict[str, Any] = {
        "uuid": episode_uuid,
        "name": ep[0]["name"],
        "source_description": ep[0]["source_description"],
        "found": True,
        "mentions": [m["name"] for m in mentions],
        "facts_deleted": deleted,
        "facts_detached": detached,
        "orphaned_entities": [],
    }
    if not apply:
        return rec

    if deleted:
        await driver.execute_query(
            "MATCH ()-[r:RELATES_TO]->() WHERE r.uuid IN $uuids DELETE r",
            uuids=[f["uuid"] for f in deleted],
        )
    if detached:
        await driver.execute_query(
            "MATCH ()-[r:RELATES_TO]->() WHERE r.uuid IN $uuids SET r.episodes = [x IN r.episodes WHERE x <> $u]",
            uuids=[f["uuid"] for f in detached], u=episode_uuid,
        )
    await driver.execute_query("MATCH (ep:Episodic {uuid: $u})-[r:MENTIONS]->() DELETE r", u=episode_uuid)
    await driver.execute_query("MATCH (ep:Episodic {uuid: $u})-[r:IN_PROJECT]->() DELETE r", u=episode_uuid)
    await driver.execute_query("MATCH (ep:Episodic {uuid: $u}) DETACH DELETE ep", u=episode_uuid)

    if mentions:
        orphans = _rows(await driver.execute_query(
            "MATCH (e:Entity) WHERE e.uuid IN $uuids "
            "AND NOT ()-[:MENTIONS]->(e) AND NOT (e)-[:RELATES_TO]-() "
            "RETURN e.uuid AS uuid, e.name AS name",
            uuids=[m["uuid"] for m in mentions],
        ))
        if orphans:
            await driver.execute_query(
                "MATCH (e:Entity) WHERE e.uuid IN $uuids DETACH DELETE e",
                uuids=[o["uuid"] for o in orphans],
            )
            rec["orphaned_entities"] = [o["name"] for o in orphans]
    return rec
