"""Canonical data model, per ROADMAP.md's "Canonical data model" section.

These dataclasses give Milestone 2 (the event journal) and Milestone 3
(consolidation) a stable shape to target. They are intentionally introduced
now, ahead of their first real caller, so the provider protocols in
protocols.py can reference concrete types instead of `dict[str, Any]`
placeholders that would need reconciling later.

Not yet wired into runtime behavior: `remember()`/`recall()` still operate
on the shapes Graphiti/FalkorDB already produce (plain dicts), and no code
constructs a SourceEvent yet — that starts in Milestone 2, when the event
journal exists to persist them and the ChatGPT importer is rewritten to
emit them. Introducing the schema before its first writer avoids designing
it under pressure once importers, adapters, and the journal all depend on
it (see docs/adr/0001-four-layer-model.md).

Deliberately NOT unified with server.importer.DatePrecision or the
NativeCandidateCategory-adjacent enums in server.chatgpt_export_parser:
those are working, tested code for the current markdown/native importers.
Reconciling them with this canonical enum is Milestone 2 work, done when
those importers are rewritten to emit SourceEvent/DerivedMemory rather than
writing Graphiti episodes directly.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Optional


class DatePrecision(StrEnum):
    """Precision of a canonical event/memory date, per ROADMAP.md's
    "Preserve time correctly" principle. Distinct from, and not yet
    reconciled with, server.importer.DatePrecision (see module docstring).
    """

    EXACT = "exact"
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    NONE = "none"


@dataclass(frozen=True)
class SourceProvenance:
    """Where a source event came from. Fields are optional because not
    every harness/importer can populate every identifier (see ROADMAP.md's
    source-event envelope).
    """

    harness: str
    account_scope: Optional[str] = None
    conversation_id: Optional[str] = None
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    model: Optional[str] = None


@dataclass(frozen=True)
class SourceEvent:
    """What a user, agent, tool, or import actually produced — evidence,
    not an interpretation of it. See docs/adr/0001-four-layer-model.md.
    """

    schema_version: str
    event_id: str
    event_type: str
    source: SourceProvenance
    observed_at: datetime
    content: dict[str, Any]
    content_hash: str
    actor_type: str = "user"
    actor_id: Optional[str] = None
    event_date: Optional[datetime] = None
    date_precision: DatePrecision = DatePrecision.NONE
    parent_event_ids: list[str] = field(default_factory=list)
    attachment_refs: list[str] = field(default_factory=list)
    privacy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


# Starter vocabulary for DerivedMemory.reasoning_kind (ADR 0005, MS3.5).
# Deliberately NOT an enum: ADR 0005 decision 1 — extend by adding a string,
# not by migrating a schema. This set exists for documentation and soft
# validation (warn on an unknown kind), never as a hard constraint. A
# reasoning_kind is meaningful on `episodic` memories, permitted-but-optional
# on `durable_candidate` / `ambiguous` (e.g. an as-yet-unresolved
# `investigation`), and never set on `non_memory`.
REASONING_KINDS: frozenset[str] = frozenset(
    {
        "decision",
        "investigation",
        "hypothesis",
        "experiment",
        "finding",
        "rejected_alternative",
        "retrospective",
        "plan",
    }
)


@dataclass(frozen=True)
class DerivedMemory:
    """What an extraction/consolidation process inferred was worth
    remembering. References its evidence rather than replacing it.
    """

    memory_id: str
    memory_type: str
    statement: str
    evidence_event_ids: list[str]
    extraction_policy: str
    extraction_model_version: str
    observed_at: datetime
    event_date: Optional[datetime] = None
    date_precision: DatePrecision = DatePrecision.NONE
    # ADR 0005 / MS3.5: names the *kind of thinking* this memory represents
    # (see REASONING_KINDS). A property, not a classification category and
    # not a memory_type value — the four ExtractionCategory values are
    # unchanged. Optional; existing rows / non-reasoning memories read None.
    reasoning_kind: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    confidence: Optional[float] = None
    approval_state: str = "pending"
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    provider_refs: dict[str, Any] = field(default_factory=dict)
    correction_state: Optional[str] = None
    deleted: bool = False


@dataclass(frozen=True)
class KnowledgeResult:
    """Normalized knowledge-provider result shape from ROADMAP.md.

    Load-bearing since MS5: every server.core.protocols.KnowledgeSource
    returns this, and server.knowledge fans a query out across sources
    without special-casing any of them. The provenance a caller needs to
    keep conflicting sources apart travels on each result, never on the
    fan-out: `provider` (which source), `document_id` (stable within that
    provider), `source_version` (what version of the document was read:
    a file mtime, a message date, a commit), `scope` (who may see it, e.g.
    `private:gmail:<account>`), plus `uri` and `source_timestamp`.
    `retrieval_score` is only comparable within one provider.

    (FileKnowledgeProvider.search() still returns the richer
    server.providers.wiki.corpus.SearchResult for search_wiki's formatting;
    its query() maps that onto this shape.)
    """

    provider: str
    document_id: str
    title: str
    excerpt: str
    uri: Optional[str] = None
    source_timestamp: Optional[datetime] = None
    retrieval_score: Optional[float] = None
    retrieval_method: Optional[str] = None
    scope: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    source_version: Optional[str] = None


@dataclass(frozen=True)
class AssembledContext:
    """A task-specific projection across evidence, memory, and knowledge,
    per ROADMAP.md's "Context is assembled, not stored as truth" principle.

    Not yet constructed anywhere: server.context.get_context() still
    returns a formatted Markdown string directly. This type becomes the
    intermediate representation get_context() builds and formats from once
    Milestone 7 needs conflict/staleness signals and truncation metadata
    that a bare string cannot carry.
    """

    topic: str
    rendered_markdown: str
    knowledge_results: list[Any] = field(default_factory=list)
    memory_facts: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    truncated_categories: list[str] = field(default_factory=list)
