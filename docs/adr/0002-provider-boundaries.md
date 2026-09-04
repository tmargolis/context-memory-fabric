# ADR 0002: Provider protocols for memory, knowledge, and event storage

**Status:** Accepted
**Implemented:** Yes (Milestone 1, 2026-09-03) — `MemoryProvider`/`KnowledgeProvider`/`EventStore`/`Importer`/`ContextAssembler`/`ProposalProvider` protocols exist under `server/core/`; the existing Graphiti and filesystem-Wiki code now sits behind `GraphitiMemoryProvider`/`FileKnowledgeProvider`. Exit gate passed: `tests/fakes/fake_memory_provider.py` and `fake_knowledge_provider.py` satisfy the protocols (`isinstance()`-verified) and `get_context()` runs end-to-end against both fakes with no live FalkorDB/Gemini/filesystem dependency.
**Date:** 2026-09-03

## Context

`server/memory.py` imports `graphiti_core` and `FalkorDriver` directly; `server/wiki.py` and `server/corpus.py` assume a local filesystem Wiki; `server/context.py` imports both concretely. There is no seam at which a second memory backend, a second knowledge source, or a fake for testing could be substituted. Concretely today:

- `tests/test_step5b_init.py` and `tests/test_step5c_acceptance.py` require a live FalkorDB and a real `GEMINI_API_KEY` to do anything (they are not currently pytest-collected, but that is incidental — they have no `def test_*` functions, not because they were designed to run against a fake).
- `LLM_WIKI_PATH` is documented as required (`SETUP.md`); the server has no path that starts without it.
- ROADMAP.md principles 4–6 ("No required Wiki," "No required importer," "No required memory backend") are not yet true of the implementation.

## Decision

Before Milestone 2 (event journal) or Milestone 4 (adapters) add more callers of memory/knowledge access, Milestone 1 will introduce explicit protocols — `MemoryProvider`, `KnowledgeProvider`, `EventStore`, `Importer`, `ContextAssembler`, `ProposalProvider` — under `server/core/`, and move the existing Graphiti and filesystem-Wiki code behind `GraphitiMemoryProvider` and `FileKnowledgeProvider` implementations without changing observable behavior (verified against the Milestone 0.5 MCP contract fixtures in `tests/fixtures/mcp_contracts/`).

This ADR fixes the sequencing rationale: **provider boundaries come before the journal**, not after, because the journal is itself a new provider (`EventStore`) and writing it against an established protocol pattern is cheaper than retrofitting one later. This reverses a natural instinct to build the journal first (it feels more urgent) — see IMPLEMENTATION-PLAN.md's Milestone 1 exit gate, which requires proving a second, trivial `MemoryProvider` can be stubbed against the protocol before Milestone 2 begins.

## Consequences

- `LLM_WIKI_PATH` becomes optional; tool registration for `search_wiki`/`propose_wiki_update` becomes conditional on a configured `KnowledgeProvider`.
- Graphiti-specific types (e.g. `EpisodeType`) must not leak past `server/providers/memory_graphiti.py` into `server/mcp.py` or `server/context.py`.
- The cost is a refactor of currently-working code with no immediate feature payoff, which is why Milestone 0.5's contract fixtures exist first — they are the regression guard that makes this refactor safe to do quickly.

## Alternatives considered

- **Defer provider boundaries until a second provider is actually needed.** Rejected: by the time a second memory or knowledge provider is needed (Milestone 5's GitHub knowledge provider, or a future non-Graphiti memory backend), the event journal, capture middleware (Milestone 4a), and consolidation pipeline (Milestone 3) will all have been built against the concrete Graphiti/filesystem APIs, multiplying the refactor's blast radius.
