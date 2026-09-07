"""Extraction policy protocol and shared types (Milestone 3).

An ExtractionPolicy turns one journaled SourceEvent (plus light context
about its conversation) into an ExtractionResult — a classification and,
where warranted, a candidate memory statement. Policies are versioned
(`name` + `version`) so a derived memory can always answer "which policy,
which version, produced this" (IMPLEMENTATION-PLAN.md's Milestone 3 task:
"stamp every derived memory with the versions that produced it").

Structural (typing.Protocol), matching server.core.protocols's precedent
from Milestone 1: a policy need not inherit from anything, only implement
`evaluate`.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional, Protocol, runtime_checkable

from server.core.models import DatePrecision, SourceEvent


class ExtractionCategory(StrEnum):
    """Broader than server.importer.CandidateCategory (which has no
    non_memory value — the markdown importer it serves assumes everything
    handed to it is at least a candidate). The policy layer needs the
    fuller vocabulary: NON_MEMORY covers assistant-authored content
    (see ExtractionCategory's role in the actor-type guard, Milestone 3
    acceptance test 1) and other content a policy can positively rule out,
    not just fail to positively classify.

    These four values are the whole vocabulary and are meant to stay that
    way. Do NOT add a category for a "kind of thinking" (exploration,
    analysis, experiment, investigation, decision, ...) — ADR 0005 decided
    those are a `reasoning_kind` *property* on the derived memory, tagged
    by the model-based ReasoningEpisodePolicyV1 (Milestone 3.5), not a fifth
    ExtractionCategory and not a new lane. A reasoning episode is still
    classified EPISODIC here; `reasoning_kind` rides alongside it.
    """

    EPISODIC = "episodic"
    DURABLE_CANDIDATE = "durable_candidate"
    AMBIGUOUS = "ambiguous"
    NON_MEMORY = "non_memory"


@dataclass(frozen=True)
class PolicyContext:
    """Light conversational context a policy may use, gathered by the
    pipeline from surrounding journal events — never from re-parsing a
    source export file.
    """

    preceding_assistant_text: Optional[str] = None
    conversation_title: Optional[str] = None
    section_heading: Optional[str] = None
    # ADR 0005 / MS3.5 — additive, optional, ignored by per-event policies
    # like HeuristicPatternPolicyV1. A model-based policy
    # (ReasoningEpisodePolicyV1) reasons over `topical_window`, the bounded
    # span of consecutive same-subject SourceEvents it is given, rather than
    # one turn. `open_threads` is the small cross-conversation open-thread
    # index (server/consolidation/threads.py) checked before extraction so
    # an intent stated in one conversation and its outcome in another land
    # in the same thread (ADR 0004 decision 3, carried forward).
    topical_window: Optional[list[SourceEvent]] = None
    open_threads: Optional[list[Any]] = None


@dataclass(frozen=True)
class ExtractionResult:
    """One policy's verdict on one event. `confidence` is a policy-defined
    [0, 1] heuristic score, not a calibrated probability — it exists to
    drive the auto-accept/queue-for-review split (Milestone 3 exit gate),
    not to be compared numerically across different policies.
    """

    category: ExtractionCategory
    statement: str
    reason: str
    confidence: float
    event_date: Optional[datetime] = None
    date_precision: DatePrecision = DatePrecision.NONE
    # ADR 0005 / MS3.5 — populated only by ReasoningEpisodePolicyV1. `category`
    # above stays one of the four ExtractionCategory values; these ride
    # alongside it. `statement` is a concise synthesis of the window, not raw
    # turns. All optional so HeuristicPatternPolicyV1 keeps satisfying the
    # protocol unchanged.
    reasoning_kind: Optional[str] = None
    driving_question: Optional[str] = None
    rationale: Optional[str] = None
    alternatives: Optional[str] = None
    status: Optional[str] = None
    thread_key: Optional[str] = None


@runtime_checkable
class ExtractionPolicy(Protocol):
    name: str
    version: str

    def evaluate(self, event: SourceEvent, context: PolicyContext) -> ExtractionResult: ...


# --------------------------------------------------------------------------
# MS3.5 — the windowed reasoning path. Additive: everything above is the
# per-event path (Milestone 3) and is unchanged. A WindowedExtractionPolicy
# consumes a bounded span of turns and emits 0..N ReasoningEpisodes, because
# one train of thought rarely fits in one turn and one window can hold more
# than one (ADR 0005 decision 3). The per-event ExtractionPolicy protocol is
# deliberately left alone rather than overloaded to return a list.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ReasoningEpisode:
    """One piece of thinking synthesised from a topical window by a
    model-based policy. `category` stays one of the four ExtractionCategory
    values (usually EPISODIC); `reasoning_kind` is the property that names
    the *kind* of thinking (see server.core.models.REASONING_KINDS). ADR
    0005 decision 1: reasoning_kind is a property, never a fifth category.

    `statement` is a concise synthesis ("investigated why promotion produced
    junk; root cause was Case C trusting a bare date"), not raw turns.
    `evidence_event_ids` is the subset of the window's events this episode
    actually rests on, so the journal link stays precise even when one
    window yields several episodes.
    """

    category: ExtractionCategory
    reasoning_kind: str
    statement: str
    confidence: float
    evidence_event_ids: list[str]
    driving_question: Optional[str] = None
    rationale: Optional[str] = None
    alternatives: Optional[str] = None
    status: Optional[str] = None
    thread_key: Optional[str] = None
    event_date: Optional[datetime] = None
    date_precision: DatePrecision = DatePrecision.NONE


@runtime_checkable
class WindowedExtractionPolicy(Protocol):
    name: str
    version: str

    def evaluate_window(
        self, window: "list[SourceEvent]", context: PolicyContext
    ) -> list[ReasoningEpisode]: ...
