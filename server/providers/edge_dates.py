"""Same-day fact dates anchored to the episode's own time (2026-11-10).

Graphiti decides which of two contradicting facts is older from their
`valid_at`s (edge_operations.resolve_edge_contradictions): the older one gets
`invalid_at` set to the newer one's `valid_at`. Episodes are dated to the
second, but the fact-extraction model writes a day-only date whenever the
text says "On 2026-11-06 ..." -- midnight. Two episodes from the same day
then compare backwards: the 17:11 episode's "Todd sent his responses" fact
(valid 00:00) looked older than the 16:21 episode's "drafted, not yet sent"
fact, so Graphiti invalidated the *new* fact at the old one's time and left
the draft current.

`anchor_same_day_valid_at` moves such a midnight `valid_at` up to the
episode's time. It only touches a fact dated midnight on the same UTC day as
its episode and earlier than it: a fact the text dates to another day (a
past event, a future meeting) keeps its date, and nothing else about the
edge changes.

The fix has to run between extraction and contradiction resolution, both of
which happen inside Graphiti.add_episode(), so `install()` wraps the
`extract_edges` name graphiti_core.graphiti calls. It is idempotent.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
import functools
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_INSTALLED_ATTR = "__cmf_same_day_anchor__"


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


def _wrap(extract_edges: Any) -> Any:
    @functools.wraps(extract_edges)
    async def extract_edges_anchored(clients: Any, episode: Any, *args: Any, **kwargs: Any) -> Any:
        edges = await extract_edges(clients, episode, *args, **kwargs)
        episodes = episode if isinstance(episode, list) else [episode]
        times = [_utc(getattr(e, "valid_at", None)) for e in episodes]
        times = [t for t in times if t is not None]
        if len(episodes) == 1 and times:
            n = anchor_same_day_valid_at(edges, times[0])
            if n:
                logger.info("anchored %d same-day fact date%s to the episode time", n, "" if n == 1 else "s")
        return edges

    setattr(extract_edges_anchored, _INSTALLED_ATTR, True)
    return extract_edges_anchored


def install() -> bool:
    """Wrap graphiti_core.graphiti.extract_edges once. Returns True if it
    wrapped it now, False if it already was."""
    import graphiti_core.graphiti as graphiti_module

    current = graphiti_module.extract_edges
    if getattr(current, _INSTALLED_ATTR, False):
        return False
    graphiti_module.extract_edges = _wrap(current)
    return True
