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
from typing import Optional, Protocol, runtime_checkable

from server.core.models import DatePrecision, SourceEvent


class ExtractionCategory(StrEnum):
    """Broader than server.importer.CandidateCategory (which has no
    non_memory value — the markdown importer it serves assumes everything
    handed to it is at least a candidate). The policy layer needs the
    fuller vocabulary: NON_MEMORY covers assistant-authored content
    (see ExtractionCategory's role in the actor-type guard, Milestone 3
    acceptance test 1) and other content a policy can positively rule out,
    not just fail to positively classify.
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


@runtime_checkable
class ExtractionPolicy(Protocol):
    name: str
    version: str

    def evaluate(self, event: SourceEvent, context: PolicyContext) -> ExtractionResult: ...
