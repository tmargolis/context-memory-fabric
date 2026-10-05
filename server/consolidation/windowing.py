"""Topical-window segmentation for MS3.5 reasoning-episode consolidation.

`ReasoningEpisodePolicyV1` reasons over a *window* of consecutive turns on one
subject, not a single turn (ADR 0005 decision 3). This module groups a
conversation's journaled `SourceEvent`s into bounded `TopicalWindow`s.

The segmentation strategy is genuinely unsolved. ADR 0005: "prototype 2-3
segmentation strategies ... against the real ChatGPT/Claude/Gemini journal and
pick from measured output -- do not build blind." This module supplies the
candidate strategies; `scripts`/scratch probes run them over the real journal
and the choice is recorded in the MS3.5 exit-gate notes before the model-based
policy is wired in.

Invariants shared by every strategy:

- **Conversation-scoped.** A window never spans more than one
  `source.conversation_id`. Strategies only subdivide within a conversation,
  never merge across.
- **Chronological.** Events in a window are ordered by `observed_at`.
- **Total.** Every input event lands in exactly one window; nothing is dropped.
- **Short conversations pass through.** A conversation with fewer than
  `min_window` events is emitted as one window with `boundary_reason="short
  conversation"`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Callable, Optional, Protocol, Sequence, runtime_checkable

from server.core.models import SourceEvent


@dataclass(frozen=True)
class TopicalWindow:
    """A bounded span of consecutive same-conversation turns handed to a
    windowed extraction policy as its unit of analysis.
    """

    conversation_id: str
    events: list[SourceEvent]
    strategy: str
    boundary_reason: str

    @property
    def event_ids(self) -> list[str]:
        return [e.event_id for e in self.events]

    @property
    def user_turn_count(self) -> int:
        return sum(1 for e in self.events if e.actor_type == "user")

    @property
    def span(self) -> tuple[Optional[datetime], Optional[datetime]]:
        if not self.events:
            return (None, None)
        return (self.events[0].observed_at, self.events[-1].observed_at)

    def __len__(self) -> int:
        return len(self.events)


@runtime_checkable
class Windower(Protocol):
    name: str

    def windows(self, events: Sequence[SourceEvent]) -> list[TopicalWindow]:
        """Segment ONE conversation's events (any order) into topical windows."""
        ...


def group_by_conversation(events: Sequence[SourceEvent]) -> dict[str, list[SourceEvent]]:
    """Bucket a mixed event list by `source.conversation_id` (falling back to
    `event_id` for events with no conversation), each bucket sorted by time.
    """
    buckets: dict[str, list[SourceEvent]] = defaultdict(list)
    for event in events:
        key = event.source.conversation_id or event.event_id
        buckets[key].append(event)
    for bucket in buckets.values():
        bucket.sort(key=lambda e: e.observed_at)
    return dict(buckets)


def _prepared(events: Sequence[SourceEvent]) -> tuple[str, list[SourceEvent]]:
    ordered = sorted(events, key=lambda e: e.observed_at)
    conv_id = ordered[0].source.conversation_id or ordered[0].event_id if ordered else ""
    return conv_id, ordered


def _text(event: SourceEvent) -> str:
    return (event.content.get("text") or "").strip()


# --------------------------------------------------------------------------
# Strategy 1 -- fixed turn count. The dumb baseline: chop the conversation
# into consecutive N-turn slices with no attention to content. Cheap,
# deterministic, and the thing every smarter strategy has to beat.
# --------------------------------------------------------------------------
class FixedTurnCountWindower:
    name = "fixed_turn_count"

    def __init__(self, window_size: int = 8, min_window: int = 3) -> None:
        self.window_size = window_size
        self.min_window = min_window

    def windows(self, events: Sequence[SourceEvent]) -> list[TopicalWindow]:
        conv_id, ordered = _prepared(events)
        if not ordered:
            return []
        if len(ordered) < self.min_window:
            return [TopicalWindow(conv_id, ordered, self.name, "short conversation")]

        out: list[TopicalWindow] = []
        for start in range(0, len(ordered), self.window_size):
            chunk = ordered[start : start + self.window_size]
            # Fold a runt tail into the previous window rather than emitting a
            # sub-min_window fragment.
            if len(chunk) < self.min_window and out:
                prev = out.pop()
                merged = prev.events + chunk
                out.append(TopicalWindow(conv_id, merged, self.name, f"fixed {self.window_size} (+runt tail)"))
                continue
            out.append(TopicalWindow(conv_id, chunk, self.name, f"fixed {self.window_size}"))
        return out


# --------------------------------------------------------------------------
# Strategy 2 -- time gap + size cap. A long pause between turns is a strong,
# content-free signal that the user walked away and came back to something
# else (or the same thing much later, which is still worth splitting so the
# window's time span stays meaningful for date resolution). Size cap keeps a
# marathon single-sitting session from becoming one giant window.
# --------------------------------------------------------------------------
class TimeGapWindower:
    name = "time_gap"

    def __init__(self, max_gap_minutes: float = 120.0, max_window: int = 20, min_window: int = 3) -> None:
        self.max_gap = timedelta(minutes=max_gap_minutes)
        self.max_window = max_window
        self.min_window = min_window

    def windows(self, events: Sequence[SourceEvent]) -> list[TopicalWindow]:
        conv_id, ordered = _prepared(events)
        if not ordered:
            return []
        if len(ordered) < self.min_window:
            return [TopicalWindow(conv_id, ordered, self.name, "short conversation")]

        out: list[TopicalWindow] = []
        current: list[SourceEvent] = [ordered[0]]
        reason = "size cap"
        for prev, event in zip(ordered, ordered[1:]):
            gap = event.observed_at - prev.observed_at
            if gap >= self.max_gap:
                out.append(TopicalWindow(conv_id, current, self.name, f"time gap {_fmt_gap(gap)}"))
                current = [event]
            elif len(current) >= self.max_window:
                out.append(TopicalWindow(conv_id, current, self.name, "size cap"))
                current = [event]
            else:
                current.append(event)
        out.append(TopicalWindow(conv_id, current, self.name, reason if len(current) >= self.max_window else "conversation end"))
        return _merge_runts(out, self.min_window, self.name)


# --------------------------------------------------------------------------
# Strategy 3 -- topic-shift cue. Split when a *user* turn opens by explicitly
# announcing a change of subject. High precision, low recall: it only fires
# when the user says so, but when it does it is almost always a real
# boundary. Size cap catches the runaway case where the user never signals.
# --------------------------------------------------------------------------
_CUE_PATTERNS = [
    r"^(ok(ay)?|alright|right)[,\s].{0,20}\b(now|next|so|different|another|new)\b",
    r"^(now|next|moving on|next up|separately|unrelated|new (topic|question|thing)|different (topic|question|thing))\b",
    r"^(let'?s|lets|can we|could we|i want to|i'd like to|switching to|back to|going back to)\b.{0,40}\b(talk about|discuss|move on|switch|look at|focus on|instead)\b",
    r"^(changing|change of) (subject|topic)\b",
    r"\bon (a|an)(other| different| separate) (note|topic|matter|question)\b",
]
_CUE_RE = re.compile("|".join(f"(?:{p})" for p in _CUE_PATTERNS), re.IGNORECASE)


class MarkerCueWindower:
    name = "marker_cue"

    def __init__(self, max_window: int = 20, min_window: int = 3) -> None:
        self.max_window = max_window
        self.min_window = min_window

    def windows(self, events: Sequence[SourceEvent]) -> list[TopicalWindow]:
        conv_id, ordered = _prepared(events)
        if not ordered:
            return []
        if len(ordered) < self.min_window:
            return [TopicalWindow(conv_id, ordered, self.name, "short conversation")]

        out: list[TopicalWindow] = []
        current: list[SourceEvent] = []
        for event in ordered:
            cue = event.actor_type == "user" and current and _CUE_RE.search(_text(event)[:120])
            if cue:
                out.append(TopicalWindow(conv_id, current, self.name, f"cue: {_cue_snippet(_text(event))!r}"))
                current = [event]
            elif len(current) >= self.max_window:
                out.append(TopicalWindow(conv_id, current, self.name, "size cap"))
                current = [event]
            else:
                current.append(event)
        if current:
            out.append(TopicalWindow(conv_id, current, self.name, "conversation end"))
        return _merge_runts(out, self.min_window, self.name)


# --------------------------------------------------------------------------
# Strategy 4 -- embedding-similarity boundary. Split where consecutive turns'
# embeddings diverge past a threshold. Needs an embedding model, so it costs
# quota (gemini-embedding-001 via the rate limiter) and is NOT run in the
# offline probe -- `embed_fn` is injected so it can be exercised on a small
# slice or with a local model, and evaluated against the cheap strategies
# before committing to per-turn embedding cost over the whole journal.
# --------------------------------------------------------------------------
class EmbeddingBoundaryWindower:
    name = "embedding_boundary"

    def __init__(
        self,
        embed_fn: Callable[[list[str]], list[list[float]]],
        threshold: float = 0.72,
        max_window: int = 20,
        min_window: int = 3,
    ) -> None:
        self.embed_fn = embed_fn
        self.threshold = threshold
        self.max_window = max_window
        self.min_window = min_window

    def windows(self, events: Sequence[SourceEvent]) -> list[TopicalWindow]:
        conv_id, ordered = _prepared(events)
        if not ordered:
            return []
        if len(ordered) < self.min_window:
            return [TopicalWindow(conv_id, ordered, self.name, "short conversation")]

        vectors = self.embed_fn([_text(e) for e in ordered])
        out: list[TopicalWindow] = []
        current: list[SourceEvent] = [ordered[0]]
        for idx in range(1, len(ordered)):
            sim = _cosine(vectors[idx - 1], vectors[idx])
            if sim < self.threshold:
                out.append(TopicalWindow(conv_id, current, self.name, f"embedding shift (cos {sim:.2f})"))
                current = [ordered[idx]]
            elif len(current) >= self.max_window:
                out.append(TopicalWindow(conv_id, current, self.name, "size cap"))
                current = [ordered[idx]]
            else:
                current.append(ordered[idx])
        out.append(TopicalWindow(conv_id, current, self.name, "conversation end"))
        return _merge_runts(out, self.min_window, self.name)


# -- shared helpers --------------------------------------------------------
def _merge_runts(windows: list[TopicalWindow], min_window: int, strategy: str) -> list[TopicalWindow]:
    """Fold any window below `min_window` into its predecessor so no
    sub-threshold fragment survives. A leading runt is folded into its
    successor instead.
    """
    if not windows:
        return windows
    merged: list[TopicalWindow] = []
    for win in windows:
        if len(win) < min_window and merged:
            prev = merged.pop()
            reason = prev.boundary_reason if prev.boundary_reason.endswith("(+tail)") else f"{prev.boundary_reason} (+tail)"
            merged.append(TopicalWindow(prev.conversation_id, prev.events + win.events, strategy, reason))
        else:
            merged.append(win)
    # A single leading runt with a follower after it.
    if len(merged) >= 2 and len(merged[0]) < min_window:
        head, nxt = merged[0], merged[1]
        merged[:2] = [TopicalWindow(head.conversation_id, head.events + nxt.events, strategy, nxt.boundary_reason)]
    return merged


def _fmt_gap(gap: timedelta) -> str:
    minutes = gap.total_seconds() / 60
    if minutes < 90:
        return f"{minutes:.0f}m"
    hours = minutes / 60
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _cue_snippet(text: str) -> str:
    return re.split(r"[.\n?!]", text.strip(), maxsplit=1)[0][:60]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# Registry for the offline probe. Embedding strategy is intentionally absent
# -- it needs an injected embed_fn and is evaluated separately.
CHEAP_STRATEGIES: dict[str, Callable[[], Windower]] = {
    "fixed_turn_count": lambda: FixedTurnCountWindower(),
    "time_gap": lambda: TimeGapWindower(),
    "marker_cue": lambda: MarkerCueWindower(),
}


def default_windower() -> Windower:
    """The one strategy wired into the live MS3.5 consolidation stage.

    Deliberately loose (2h gap, 20-turn cap): after the Phase B probe over
    the real journal, no cheap rule reliably found *topic* boundaries, and
    the user's call (2026-09-05) was to keep segmentation loose for now and let
    the model do topical sub-segmentation inside its own call, revisiting
    once the Spark local-inference stack can carry that work. This windower
    therefore only bounds the model's input size and splits obvious session
    breaks; it does not try to be a topic detector. FixedTurnCount /
    MarkerCue / EmbeddingBoundary stay in this module for a later bake-off.
    """
    return TimeGapWindower(max_gap_minutes=120.0, max_window=20, min_window=3)
