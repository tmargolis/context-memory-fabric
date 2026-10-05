# ADR 0001: Evidence, memory, knowledge, and context as four distinct layers

**Status:** Accepted
**Implemented:** Partially, as of 2026-09-04 — Evidence layer real and populated (Milestone 2, `imports/journal/journal.db`); Memory-derivation staging real (Milestone 3, `derived_memories`); the four canonical dataclasses (`SourceEvent`/`DerivedMemory`/`KnowledgeResult`/`AssembledContext`) exist in `server/core/models.py` (Milestone 1). Knowledge layer is still only the pre-existing `FileKnowledgeProvider`. Context layer is not yet wired to `AssembledContext` — `get_context()` still returns a formatted Markdown string directly; that migration is Milestone 7's job, not this ADR's.
**Date:** 2026-09-03

## Context

The Phase 1 implementation has two stores — Graphiti/FalkorDB (episodic memory) and the local Wiki corpus (durable knowledge) — unified by `get_context()`. As Context Memory Fabric grows adapters, importers, and a journal (Milestone 2 onward), it needs an explicit answer to a recurring question: when something is captured, ingested, or asked about, which of several distinct things is it?

Three concrete problems motivate formalizing this now rather than later:

1. **No separation between what was said and what CMF believes.** The 57 production episodes in `memory-fabric` are Graphiti's *derived* interpretation of ChatGPT export text — entity/fact extraction run through the Gemini API. There is currently no separately-stored record of the original source text those derivations came from, independent of Graphiti's internal representation. If the extraction is later found to be wrong, there is nothing to re-derive from except re-running the importer against the original export files.
2. **Durable knowledge and episodic memory are already kept separate, on purpose** (`propose_wiki_update()` never mutates `LLM_Wiki`), but there is no equivalent boundary between "raw capture" and "accepted memory." Every `remember()` call goes directly into the graph; there is no staging state between "this happened" and "this is durable episodic truth."
3. **Assembled context is currently synthesized fresh on every `get_context()` call** with no record of what was included, excluded, or judged conflicting. This is fine at today's scale but forecloses replay, evaluation, and "why did the system say that" debugging later (Milestones 6 and 8).

## Decision

Adopt four explicitly distinct layers, matching ROADMAP.md's "Architectural principles" 1–3 and its target logical architecture:

| Layer | What it holds | Mutability |
|---|---|---|
| **Evidence** (source events) | What a user, agent, tool, or import actually produced, verbatim or minimally normalized | Append-only |
| **Derived memory** | What an extraction/consolidation process inferred was worth remembering, with a link back to its evidence | Correctable, supersedable, never silently overwritten |
| **Durable knowledge** | Deliberately maintained, authored documentation (the Wiki, or any future knowledge provider) | Explicit, reviewed promotion only |
| **Assembled context** | A task-specific, temporary projection across the above, with provenance | Not persisted as truth; regenerated per query |

The implementation consequence: Milestone 2 introduces an append-only event journal beneath Graphiti, so `remember()` and every importer write evidence first and derive memory second — reversing today's flow where `remember()` writes directly to Graphiti with no separate evidence record. This is a real behavior change scheduled for Milestone 2, not implemented yet.

## Consequences

- Every derived memory must be able to answer "what evidence produced this?" — a capability the current implementation does not have, because no evidence layer exists yet independent of Graphiti's own episode nodes.
- Deletion or correction (Milestone 6) must be able to remove/amend a derived memory without deleting the evidence it came from, and must never retroactively rewrite evidence to make history "consistent."
- Promotion into durable knowledge remains explicit and reviewable (`propose_wiki_update()`, generalized to `propose_knowledge_change()` in Milestone 5) — this decision does not change that; it extends the same discipline one layer earlier, between evidence and memory.
- Cost: an extra write (event journal) on every capture path. Accepted because the alternative — deriving memory with no addressable source — is the thing blocking Milestone 6 (governance) and Milestone 8 (replay).

## Alternatives considered

- **Two layers only (memory + knowledge), as today.** Rejected: this is the status quo, and it is precisely what makes "why does the system believe this" unanswerable for the 57 already-imported episodes (see the Milestone 2 backfill task, which exists only because this ADR was not in place when they were imported).
- **Store evidence inside Graphiti as episode metadata rather than a separate journal.** Rejected: couples evidence retention to the memory provider, violating ROADMAP.md principle 6 (no required memory backend) and blocking Milestone 1's provider-interface extraction.
