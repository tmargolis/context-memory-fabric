"""Best-effort FalkorDB project/harness tagging, applied inline at promotion time.

Historically this metadata was applied only as a separate post-hoc pass
(`scripts/tag_projects.py`, full-graph, idempotent) that had to be re-run by
hand after every promotion batch -- easy to forget, and it left newly
promoted episodes (the entire claude_code harness, MS4b onward) completely
unlinked from their Project until someone remembered to run it (found
2026-09-23, DelayedVideoTablet review). The same review found claude_code
episodes have never carried a harness label at all (`Gemini`/`ChatGPT`/
`Claude` exist on 100% of their harnesses' episodes; claude_code has 0/79) --
apparently a one-off manual pass predating the claude_code adapter that was
never repeated as a script.

`scripts/tag_projects.py` / `scripts/tag_harness.py` remain the right tools
for a full-graph backfill or repair pass (a fresh graph rebuild, a `project`
value corrected retroactively, etc.). This module is their per-episode
counterpart, called automatically right after a single promotion's
`remember()` succeeds (see server/consolidation/promotion.py), so day-to-day
review-and-promote no longer depends on remembering to run a script.

Deliberately best-effort: a tagging failure here must never fail or roll
back a promotion that already succeeded in FalkorDB and the ledger -- caller
wraps this in its own try/except; log and move on.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

TagFn = Callable[[str, Optional[str], Optional[str]], Awaitable[None]]

# tag_projects.py imports this dict rather than keeping its own copy. Maps a
# project id to a different FalkorDB label (e.g. {"old-slug": "new-slug"}, to
# fold a retired slug into its replacement). Empty by default.
PROJECT_DISPLAY_OVERRIDES: dict[str, str] = {}

# Harness id (server/core/models.py SourceEvent.source.harness) -> FalkorDB
# label. Not a bare .title() of the harness string -- "chatgpt" -> "ChatGPT"
# isn't reachable that way -- so new harnesses get their label added here
# explicitly rather than relying on the best-effort fallback below.
HARNESS_LABELS: dict[str, str] = {
    "gemini": "Gemini",
    "chatgpt": "ChatGPT",
    "claude": "Claude",
    "claude_code": "Claude_Code",
    "claude_desktop": "Claude_Desktop",
    "claude_desktop_code": "Claude_Desktop_Code",
    "claude_cowork": "Claude_Cowork",
    "antigravity": "Antigravity",
    "local_agent_mode_context_memory_fabric": "Local_Agent_Mode",
}


def project_label_for(project: str) -> str:
    """Cypher-safe project label -- underscores, no dashes, no `ep_` prefix.

    Identical rule to scripts/tag_projects.py's `_label_for` (that script
    now imports this function instead of keeping its own copy).
    """
    friendly = PROJECT_DISPLAY_OVERRIDES.get(project, project)
    label = re.sub(r"[^a-z0-9]+", "_", friendly.lower()).strip("_")
    return label or "misc"


def harness_label_for(harness: Optional[str]) -> Optional[str]:
    """Cypher-safe harness label, or None if harness is falsy/unresolved."""
    if not harness or harness == "unknown":
        return None
    if harness in HARNESS_LABELS:
        return HARNESS_LABELS[harness]
    label = re.sub(r"[^A-Za-z0-9]+", "_", harness).strip("_")
    return label or None


def _rows(result: Any) -> list:
    return result[0] if result and isinstance(result[0], list) else (result or [])


async def tag_promoted_episode(
    driver: Any,
    episode_name: str,
    project: Optional[str],
    harness: Optional[str],
) -> None:
    """Apply project + harness labels/edges to one just-promoted Episodic node.

    Safe to call with project/harness = None (skips whichever is missing)
    and safe to call more than once (every write is MERGE/SET, matching
    tag_projects.py's own idempotency). Does NOT reset/recompute existing
    edges graph-wide the way tag_projects.py does -- it only adds this one
    episode's own edges, so it's cheap enough to run on every promotion.
    """
    h_label = harness_label_for(harness)
    if h_label:
        await driver.execute_query(
            f"MATCH (e:Episodic {{name: $name}}) SET e:`{h_label}`",
            name=episode_name,
        )

    if not project:
        return

    p_label = project_label_for(project)
    await driver.execute_query(
        f"MATCH (e:Episodic {{name: $name}}) SET e.project = $project, e:`{p_label}`",
        name=episode_name,
        project=project,
    )

    # Primary link: this episode's mentioned entities get the IN_PROJECT
    # edge (entities can legitimately belong to more than one project;
    # episodes can't -- same design tag_projects.py uses).
    linked = await driver.execute_query(
        "MATCH (e:Episodic {name: $name})-[:MENTIONS]->(n:Entity) "
        "MERGE (proj:Project {name: $label}) "
        "MERGE (n)-[:IN_PROJECT]->(proj) "
        "RETURN count(n) AS linked",
        name=episode_name,
        label=p_label,
    )
    mention_count = _rows(linked)[0]["linked"] if _rows(linked) else 0

    # No extracted entities at all -- direct Episodic->Project fallback so
    # the episode isn't orphaned from its project's hub.
    if not mention_count:
        await driver.execute_query(
            "MATCH (e:Episodic {name: $name}) "
            "MERGE (proj:Project {name: $label}) "
            "MERGE (e)-[:IN_PROJECT]->(proj)",
            name=episode_name,
            label=p_label,
        )


def get_default_tag_fn() -> TagFn:
    """Build a TagFn bound to the live FalkorDB driver used for promotion.

    Import-deferred so this module has no hard dependency on graphiti_core
    or a live FalkorDB connection at import time (mirrors how
    server/mcp.py's remember_memory is passed into promotion as
    `remember_fn` rather than imported eagerly at module scope there).
    """

    async def _tag_fn(episode_name: str, project: Optional[str], harness: Optional[str]) -> None:
        from server.providers.memory_graphiti import get_graphiti_for_operation

        graphiti, _ = get_graphiti_for_operation()
        await tag_promoted_episode(graphiti.driver, episode_name, project, harness)

    return _tag_fn
