"""What CMF does to an episode right after Graphiti's add_episode() (MS4e).

Three steps, each one Graphiti either can't do or can't be relied on for:

1. **Debris filter** (server.providers.entity_filter): deletes debris-shaped
   entities only this episode mentions. On when CMF_ENTITY_DEBRIS_FILTER=1.
2. **Project entity** (typed-recall profile only): link the episode to an
   entity named for its project (`project=<bucket>` in source_description,
   display name from the taxonomy). The prompt asks for it, but qwen skipped
   it on 2 of 30 episodes in the MS4e replay, so code guarantees it. An
   existing entity with that name (any case) is reused, never duplicated.
3. **Episode embedding**: `Episodic.content_embedding`, which recall_mem's
   episode-text search arm (64fffdd) needs. Graphiti never writes it; before
   this, only a one-off backfill did, so every episode promoted afterwards was
   invisible to that arm. On unless CMF_EMBED_EPISODES=0.

Each step is best-effort: the episode is already saved when this runs, so a
failure is logged and reported, never raised.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import re
from typing import Any, Optional

from server.core.config import TYPED_RECALL_EXTRACTION, extraction_profile_from_env
from server.providers.entity_filter import filter_debris_after_add
from server.providers.extraction_profile import project_entity_name

logger = logging.getLogger(__name__)


def _records(rows: Any) -> list[Any]:
    return rows[0] if rows and isinstance(rows[0], list) else (rows or [])


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def episode_embedding_enabled() -> bool:
    raw = (os.getenv("CMF_EMBED_EPISODES") or "").strip().lower()
    if raw in ("", "1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"CMF_EMBED_EPISODES={raw!r} is not a boolean (use 1 or 0).")


async def ensure_project_entity(graphiti: Any, result: Any, source_description: Optional[str]) -> Optional[str]:
    """Link the episode to its project's entity; return the entity name, or None if none applies."""
    from graphiti_core.edges import EpisodicEdge
    from graphiti_core.nodes import EntityNode

    name = project_entity_name(source_description)
    if not name:
        return None
    if any(_norm(n.name) == _norm(name) for n in result.nodes):
        return name  # extraction already produced (or resolved to) it
    episode = result.episode
    rows = _records(await graphiti.driver.execute_query(
        "MATCH (n:Entity) WHERE toLower(n.name) = toLower($name) AND n.group_id = $group_id "
        "RETURN n.uuid AS uuid ORDER BY n.created_at LIMIT 1",
        name=name, group_id=episode.group_id,
    ))
    now = datetime.now(timezone.utc)
    if rows:
        entity_uuid = rows[0]["uuid"]
    else:
        node = EntityNode(
            name=name, group_id=episode.group_id, labels=["Entity", "Workstream"],
            summary=f"The user's {name} project.", created_at=now,
        )
        await node.generate_name_embedding(graphiti.embedder)
        await node.save(graphiti.driver)
        entity_uuid = node.uuid
    await EpisodicEdge(
        source_node_uuid=episode.uuid, target_node_uuid=entity_uuid, group_id=episode.group_id, created_at=now,
    ).save(graphiti.driver)
    return name


async def embed_episode(graphiti: Any, episode_uuid: str, content: str) -> bool:
    vec = await graphiti.embedder.create(input_data=[content or " "])
    await graphiti.driver.execute_query(
        "MATCH (e:Episodic {uuid: $uuid}) SET e.content_embedding = vecf32($vec)",
        uuid=episode_uuid, vec=list(vec),
    )
    return True


async def postprocess_episode(
    graphiti: Any, result: Any, *, episode_name: str, content: str, source_description: Optional[str],
) -> dict[str, Any]:
    """Run the three steps on an add_episode() result. Returns what each did."""
    report: dict[str, Any] = {"debris_removed": [], "project_entity": None, "embedded": False, "errors": []}
    if result is None:
        return report
    try:
        report["debris_removed"] = await filter_debris_after_add(graphiti, result, episode_name)
    except Exception as e:  # noqa: BLE001 - the episode is already saved
        report["errors"].append(f"debris filter: {e!r}")
    if extraction_profile_from_env() == TYPED_RECALL_EXTRACTION:
        try:
            report["project_entity"] = await ensure_project_entity(graphiti, result, source_description)
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"project entity: {e!r}")
    if episode_embedding_enabled():
        try:
            report["embedded"] = await embed_episode(graphiti, result.episode.uuid, content)
        except Exception as e:  # noqa: BLE001
            report["errors"].append(f"episode embedding: {e!r}")
    for err in report["errors"]:
        logger.warning("post-processing %r: %s", episode_name, err)
    return report
