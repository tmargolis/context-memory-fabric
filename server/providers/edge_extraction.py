"""CMF's adjustments to Graphiti's fact (edge) extraction (2026-11-10).

Both run between extraction and contradiction resolution, which happen
inside Graphiti.add_episode(), so `install()` wraps the `extract_edges` name
graphiti_core.graphiti calls. It is idempotent.

1. Fact-prompt instructions. add_episode() takes one
   custom_extraction_instructions string and pastes it into the entity
   prompt and the fact prompt alike. CMF's typed-recall text is entity rules,
   so the fact prompt got "never extract numbers ... labels" and nothing about
   facts; adding fact rules to the same text made qwen return no entities on
   some long episodes. The wrapper hands extract_edges
   extraction_profile.fact_instructions_for(<the entity text>) instead.

2. Same-day fact dates. Graphiti decides which of two contradicting facts
   is older from their `valid_at`s (edge_operations.resolve_edge_contradictions):
   the older one gets `invalid_at` set to the newer one's `valid_at`. Episodes
   are dated to the second, but the model writes a day-only date whenever the
   text says "On 2026-11-06 ..." -- midnight. Two same-day episodes then
   compare backwards: the 17:11 "Todd sent his responses" fact (valid 00:00)
   looked older than the 16:21 "drafted, not yet sent" fact, so Graphiti
   invalidated the *new* fact. `anchor_same_day_valid_at` moves such a
   midnight `valid_at` up to the episode's time; a fact the text dates to
   another day (a past event, a future meeting) keeps its date.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
import functools
import logging
from typing import Any, Iterable, Optional

from server.providers.extraction_profile import fact_instructions_for

logger = logging.getLogger(__name__)

_INSTALLED_ATTR = "__cmf_edge_extraction__"
# extract_edges(clients, episode, nodes, previous_episodes, edge_type_map,
#               group_id, edge_types, custom_extraction_instructions)
_INSTRUCTIONS_ARG = "custom_extraction_instructions"
_INSTRUCTIONS_POS = 5  # index in *args, after clients and episode


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def anchor_same_day_valid_at(edges: Iterable[Any], reference_time: Optional[datetime]) -> int:
    """Raise each edge's midnight `valid_at` on `reference_time`'s UTC day to
    `reference_time`. Mutates the edges; returns how many changed."""
    ref = _utc(reference_time)
    if ref is None:
        return 0
    changed = 0
    for edge in edges:
        valid = _utc(getattr(edge, "valid_at", None))
        if valid is None or valid.time() != time(0) or valid.date() != ref.date() or valid >= ref:
            continue
        edge.valid_at = ref
        changed += 1
    return changed


def _with_fact_instructions(args: tuple, kwargs: dict) -> tuple[tuple, dict]:
    if _INSTRUCTIONS_ARG in kwargs:
        kwargs = {**kwargs, _INSTRUCTIONS_ARG: fact_instructions_for(kwargs[_INSTRUCTIONS_ARG])}
    elif len(args) > _INSTRUCTIONS_POS:
        args = (*args[:_INSTRUCTIONS_POS], fact_instructions_for(args[_INSTRUCTIONS_POS]), *args[_INSTRUCTIONS_POS + 1:])
    return args, kwargs


def _wrap(extract_edges: Any) -> Any:
    @functools.wraps(extract_edges)
    async def extract_edges_for_cmf(clients: Any, episode: Any, *args: Any, **kwargs: Any) -> Any:
        args, kwargs = _with_fact_instructions(args, kwargs)
        edges = await extract_edges(clients, episode, *args, **kwargs)
        episodes = episode if isinstance(episode, list) else [episode]
        times = [_utc(getattr(e, "valid_at", None)) for e in episodes]
        times = [t for t in times if t is not None]
        if len(episodes) == 1 and times:
            n = anchor_same_day_valid_at(edges, times[0])
            if n:
                logger.info("anchored %d same-day fact date%s to the episode time", n, "" if n == 1 else "s")
        return edges

    setattr(extract_edges_for_cmf, _INSTALLED_ATTR, True)
    return extract_edges_for_cmf


def install() -> bool:
    """Wrap graphiti_core.graphiti.extract_edges once. Returns True if it
    wrapped it now, False if it already was."""
    import graphiti_core.graphiti as graphiti_module

    current = graphiti_module.extract_edges
    if getattr(current, _INSTALLED_ATTR, False):
        return False
    graphiti_module.extract_edges = _wrap(current)
    return True
