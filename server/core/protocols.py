"""Provider protocols for Context Memory Fabric (Milestone 1).

Structural (typing.Protocol) rather than ABC-based on purpose: the existing
Graphiti and file-corpus implementations are free functions, not classes,
and Protocol lets a thin class wrapper satisfy the interface without
touching that working code's internals. See docs/adr/0002-provider-boundaries.md
for why these are introduced before the event journal (EventStore) has a
real implementation.

Signatures match what server/mcp.py's tools already call today
(server.memory.remember/recall/edit_memory/reconcile_memories and
server.wiki.search_wiki/search_corpus) rather than the fuller canonical
shapes in core/models.py — the goal of Milestone 1 is a seam with zero
behavior change, not a rewrite of what data flows through it. Tightening
these to return core.models types (DerivedMemory, KnowledgeResult) is
Milestone 2/5 work, done once a real second provider needs it.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from server.corpus import SearchResult


@runtime_checkable
class MemoryProvider(Protocol):
    """Episodic memory: write, search, correct, and reconcile temporal facts."""

    async def remember(
        self,
        content: str,
        name: Optional[str] = None,
        source_description: str = "Context Memory Fabric MCP",
        reference_time: Optional[datetime] = None,
    ) -> dict[str, Any]: ...

    async def recall(
        self,
        query: str,
        max_results: int = 10,
    ) -> list[dict[str, Any]]: ...

    async def edit(
        self,
        target_query: str,
        new_reference_time: Optional[str | datetime] = None,
        new_content: Optional[str] = None,
        new_summary: Optional[str] = None,
        new_name: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]: ...

    async def reconcile(
        self,
        records: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


@runtime_checkable
class KnowledgeProvider(Protocol):
    """Durable knowledge: read-only lexical/semantic retrieval over a corpus."""

    def is_configured(self) -> bool:
        """Cheap, side-effect-free check — no filesystem/network access.

        Distinguishes "not configured" (skip this provider, per
        ProviderNotConfiguredError) from "configured but broken", which a
        provider should raise ProviderConfigurationError for when search()
        is actually called.
        """
        ...

    def search(
        self,
        query: str,
        max_results: int = 10,
        force_rescan: bool = False,
    ) -> list[SearchResult]: ...


@runtime_checkable
class EventStore(Protocol):
    """Append-only source-event persistence. Stubbed now; implemented in
    Milestone 2. Shaped so Milestone 2 does not need to revisit this file.
    """

    def append(self, event: Any) -> None: ...

    def get(self, event_id: str) -> Optional[Any]: ...

    def query(
        self,
        *,
        harness: Optional[str] = None,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[Any]: ...


@runtime_checkable
class Importer(Protocol):
    """A batch or interactive historical-source importer."""

    async def import_content(
        self,
        content: str,
        source: str,
        source_description: Optional[str] = None,
        dry_run: bool = True,
    ) -> Any: ...


@runtime_checkable
class ContextAssembler(Protocol):
    """Cross-provider retrieval and rendering for get_context()."""

    async def assemble(
        self,
        topic: str,
        max_wiki_results: int = 5,
        max_memory_results: int = 5,
    ) -> str: ...


@runtime_checkable
class ProposalProvider(Protocol):
    """Reviewable durable-knowledge change proposals (never a direct write)."""

    def create_proposal(
        self,
        target_path: str,
        proposed_content: str,
        rationale: str,
        source_context: Optional[str] = None,
    ) -> Any: ...
