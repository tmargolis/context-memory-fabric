# Context Memory Fabric Roadmap

**Status:** Draft implementation roadmap  
**Updated:** 2026-09-03  
**Current baseline:** Phase 1 implementation on `main`

## Purpose

Evolve Context Memory Fabric (CMF) from a personal integration of Graphiti/FalkorDB and an LLM Wiki into an open, user-controlled context layer that can:

- capture activity from multiple AI harnesses and historical sources;
- preserve original source evidence and temporal provenance;
- derive useful episodic, semantic, and procedural memory;
- connect optional, replaceable durable-knowledge providers;
- assemble task-relevant context without erasing source boundaries;
- expose the same context through MCP, HTTP, and future transports;
- remain usable without an LLM Wiki, ChatGPT export, or Graphiti.

The product thesis is:

> **Your context, across every AI—not every AI's separate memory of you.**

Strategic research and competitive context are documented in [Context Memory Fabric — Competitive Landscape and Differentiation](https://github.com/tmargolis/LLM_Wiki/blob/main/WIKI/projects/Context-Memory-Fabric/Context-Memory-Fabric-Competitive-Landscape-and-Differentiation.md).

## Current baseline

The repository currently provides:

- temporal episodic memory through Graphiti and FalkorDB;
- durable corpus retrieval from a configured local Wiki/directory;
- unified context assembly;
- MCP tools for remembering, recalling, editing, reconciling, importing, searching knowledge, and proposing Wiki updates;
- conservative ChatGPT export parsing and historical-import classification;
- import registry/provenance state;
- dry-run and acceptance-test coverage.

This working behavior should be preserved while the internal boundaries are extracted.

## Architectural principles

1. **Evidence is not memory.** Original events must remain distinguishable from derived memories.
2. **Memory is not durable knowledge.** Promotion into maintained knowledge is explicit and reviewable.
3. **Context is assembled, not stored as truth.** A context response is a task-specific projection with provenance.
4. **No required Wiki.** Knowledge providers are optional.
5. **No required importer.** Historical-source parsers are plugins or optional packages.
6. **No required memory backend.** Graphiti is the first provider, not the public CMF contract.
7. **Preserve time correctly.** Keep `event_date`, `observed_at`, temporal validity, and date precision distinct.
8. **Preserve source identity.** Retain original source, conversation, turn, tool, and attachment identifiers where available.
9. **User statements and source evidence outrank model inference.** Assistant-generated claims alone must not establish personal facts.
10. **Conflicts remain visible.** Do not silently merge contradictory sources.
11. **Deletion and correction are first-class.** They must propagate through derived state without falsifying history.
12. **Modular monolith first.** Establish provider interfaces and package seams before introducing distributed services.

## Target logical architecture

```text
harness adapters ─┐
batch importers ───┼──> canonical event journal
activity sources ──┘              │
                                  ▼
                       normalization / redaction
                                  │
                                  ▼
                      extraction / consolidation
                                  │
                                  ▼
                         derived memory providers
                                  │
knowledge providers ───> retrieval / context assembly
                                  │
                                  ▼
                             MCP / HTTP

review, correction, retention, and provenance span every layer
```

## Proposed package boundaries

The names below are logical boundaries. They may initially remain within the existing Python distribution.

| Package/module | Responsibility | Required |
|---|---|---:|
| `cmf-core` | Canonical schemas, identifiers, errors, provider protocols | Yes |
| `cmf-journal` | Append-only event persistence, lookup, replay, retention | Yes for continuous capture |
| `cmf-context` | Query planning, cross-provider retrieval, conflicts, rendering | Yes |
| `cmf-memory-graphiti` | Graphiti/FalkorDB adapter | Default but replaceable |
| `cmf-knowledge-files` | Local Markdown/document corpus | Optional |
| `cmf-knowledge-github` | GitHub-backed knowledge corpus | Optional |
| `cmf-mcp` | MCP server and tool schemas | Optional transport |
| `cmf-http` | HTTP/webhook ingestion and retrieval | Optional transport |
| `cmf-import-chatgpt` | ChatGPT export parsing and classification | Optional |
| `cmf-adapter-claude-code` | Claude Code lifecycle capture | Optional |
| `cmf-adapter-codex` | Codex lifecycle capture | Optional |
| `cmf-adapter-gemini-cli` | Gemini CLI lifecycle capture | Optional |
| `cmf-adapter-openclaw` | OpenClaw message/session capture | Optional |
| `cmf-review` | Evidence inspection, correction, rejection, promotion | Strongly recommended |

## Canonical data model

### Source event

A source event records what was observed without claiming that the content is true.

Minimum candidate envelope:

```json
{
  "schema_version": "1.0",
  "event_id": "source-stable-id",
  "event_type": "turn.completed",
  "source": {
    "harness": "claude-code",
    "account_scope": null,
    "conversation_id": "source-conversation-id",
    "session_id": "source-session-id",
    "turn_id": "source-turn-id",
    "model": "source-model"
  },
  "actor": {
    "type": "user",
    "id": null
  },
  "observed_at": "2026-09-03T00:00:00Z",
  "event_date": null,
  "date_precision": "timestamp",
  "content": {},
  "parent_event_ids": [],
  "attachment_refs": [],
  "privacy": {},
  "content_hash": "sha256:...",
  "metadata": {}
}
```

### Derived memory

A derived memory should reference, rather than replace, its evidence:

- stable `memory_id`;
- memory type;
- statement/content;
- evidence event IDs;
- extraction policy and model version;
- `observed_at`;
- `event_date` and precision when applicable;
- `valid_from` and `valid_to`;
- confidence/ambiguity status;
- approval state;
- supersedes/superseded-by relationships;
- provider-specific references;
- correction and deletion state.

### Knowledge result

Every knowledge provider should return a normalized result containing:

- provider and source identity;
- stable document/item identifier;
- title/path/URI;
- retrieved excerpt;
- source timestamp/version where available;
- permissions/scope metadata where available;
- retrieval score and method;
- provider-specific metadata;
- no implicit global authority rank.

### Assembled context

An assembled response should retain sections or annotations identifying:

- source evidence;
- derived memory;
- durable knowledge;
- conflicts and likely supersession;
- unresolved questions;
- retrieval and truncation limits.

## Milestone 0 — Stabilize the Phase 1 baseline

**Goal:** Create a known-good point before restructuring.

Deliverables:

- complete the reviewed production import into the intended FalkorDB graph;
- retain the import registry and validation reports;
- run the complete existing test suite;
- record current MCP tool contracts and representative outputs as fixtures;
- add regression cases for known date, provenance, idempotency, and assistant-inference failures;
- document which behavior is current Graphiti behavior versus CMF registry behavior.

Acceptance criteria:

- repeated imports remain idempotent;
- source IDs and timestamps are preserved;
- production graph, Graphiti inference, and CMF registry can be audited separately;
- no Wiki mutation occurs during imports;
- all current MCP acceptance tests pass.

## Milestone 1 — Extract core interfaces without changing behavior

**Goal:** Make the present implementation modular before adding capabilities.

Deliverables:

- introduce provider protocols/interfaces for:
  - `MemoryProvider`;
  - `KnowledgeProvider`;
  - `EventStore`;
  - `Importer`;
  - `ContextAssembler`;
  - `ProposalProvider`;
- move Graphiti/FalkorDB access behind `MemoryProvider`;
- move local corpus retrieval behind `KnowledgeProvider`;
- keep current MCP tool names backward compatible;
- allow the server to start with the knowledge provider disabled;
- centralize configuration validation and capability discovery.

Acceptance criteria:

- memory-only deployment works without `LLM_WIKI_PATH`;
- knowledge-only read operations can run without Graphiti where appropriate;
- existing full deployment behaves equivalently;
- tests use provider fakes rather than requiring every backend.

## Milestone 2 — Add the canonical event journal

**Goal:** Preserve evidence before deriving memory.

Deliverables:

- finalize the versioned source-event schema;
- implement an append-only local event store, initially SQLite or JSONL with explicit durability semantics;
- support stable source IDs and content-hash deduplication;
- support event lookup by source, session, conversation, actor, type, and time;
- store content or redacted content according to policy;
- add journal export and replay commands;
- link imported ChatGPT candidates back to source events;
- distinguish receipt time from historical event time.

Acceptance criteria:

- the same source event cannot be duplicated;
- journal replay is deterministic for a pinned normalization policy;
- deleting or correcting derived memory does not rewrite source evidence;
- a source event can be located from every derived memory;
- retention policy behavior is tested.

## Milestone 3 — Separate capture from consolidation

**Goal:** Prevent every raw turn from being treated as memory.

Pipeline:

```text
capture
→ normalize
→ redact/filter
→ classify
→ extract candidate
→ reconcile
→ approve or auto-accept by policy
→ write derived memory
```

Deliverables:

- define consolidation jobs and statuses;
- extract the current importer classification logic into reusable policies;
- support explicit, synchronous memory capture and asynchronous background consolidation;
- version extraction prompts, models, and policies;
- preserve rejected/no-op candidates with reasons;
- support reprocessing selected events after policy changes;
- add memory-quality evaluation fixtures.

Acceptance criteria:

- routine or unsupported assistant statements do not become personal facts;
- event replay with a new extractor creates a new derivation version rather than overwriting lineage;
- dry-run results identify proposed additions, updates, rejections, and ambiguities;
- consolidation failure never loses the captured event.

## Milestone 4 — Implement supported harness adapters

**Goal:** Demonstrate genuine cross-harness continuity.

Priority order:

1. Claude Code;
2. Codex;
3. Gemini CLI;
4. OpenClaw;
5. API/framework adapters;
6. consumer-chat export or explicit-save paths.

Common adapter requirements:

- translate native lifecycle data into canonical events;
- preserve native session, conversation, turn, tool, and model IDs;
- never block the harness on nonessential background ingestion;
- queue and retry delivery;
- report capture health and dropped events;
- provide allow/deny filters for repositories, projects, tools, and content classes;
- avoid capturing secrets and sensitive tool payloads by default.

Acceptance test:

1. Record a decision in one supported harness.
2. Consolidate it into memory with source provenance.
3. Retrieve it from a second harness.
4. Correct it from the second harness.
5. Verify the first harness receives the current state while history remains inspectable.

## Milestone 5 — Generalize knowledge providers and promotion

**Goal:** Make LLM Wiki one supported knowledge provider rather than a requirement.

Deliverables:

- formalize the normalized knowledge-result contract;
- retain the existing local file/Markdown provider;
- add GitHub repository retrieval as a separate provider;
- allow multiple providers in one query;
- preserve provider provenance, source version, and access scope;
- replace Wiki-specific assumptions inside context assembly;
- introduce provider-neutral `propose_knowledge_change(...)`;
- retain `search_wiki` and `propose_wiki_update` as backward-compatible aliases when the Wiki provider is configured.

Acceptance criteria:

- CMF runs with no knowledge provider;
- CMF can run against an arbitrary Markdown directory that is not an LLM Wiki;
- provider results are not silently collapsed or assigned a fixed authority hierarchy;
- conflicting documents remain separately attributable;
- proposals never modify a knowledge source without its configured approval workflow.

## Milestone 6 — Build memory review and governance

**Goal:** Make inferred context inspectable and correctable.

Deliverables:

- API and initial UI/CLI for:
  - inspecting evidence;
  - viewing derivation metadata;
  - approving or rejecting candidates;
  - correcting content and dates;
  - viewing conflicts and supersession;
  - editing retention and privacy classifications;
  - exporting or deleting scoped context;
  - proposing durable-knowledge changes;
- explicit personal, project, team, and organization scopes;
- access-policy enforcement at ingestion and retrieval;
- audit records for memory and knowledge mutations.

Acceptance criteria:

- a user can answer why each memory exists;
- every mutation records actor, time, reason, and prior state;
- source deletion triggers defined downstream behavior;
- retrieval never crosses a configured scope boundary;
- export includes portable identities and provenance.

## Milestone 7 — Improve context assembly

**Goal:** Deliver useful context rather than a bag of retrieved items.

Deliverables:

- query intent classification;
- provider-aware retrieval planning;
- time-aware current-state and historical modes;
- explicit conflict and staleness signals;
- token-budget allocation across evidence, memory, and knowledge;
- context templates for project continuation, decision history, troubleshooting, and research;
- retrieval explanations suitable for debugging;
- quality, latency, and token-cost metrics.

Acceptance criteria:

- current-state queries prefer valid current facts without erasing history;
- historical queries reconstruct prior decisions accurately;
- conflicts are surfaced rather than silently resolved;
- context responses identify omitted/truncated categories;
- evaluation shows improvement over memory-only and knowledge-only baselines.

## Milestone 8 — Replay and evaluation

**Goal:** Use accumulated evidence to improve agents and CMF itself.

Deliverables:

- convert selected event sequences into replayable cases;
- snapshot the context available at a historical time;
- support counterfactual comparisons of retrieval and memory policies;
- create regression suites from real corrected failures;
- grade temporal accuracy, provenance, relevance, harmful retention, and task outcomes;
- export trajectories for compatible evaluation or post-training systems;
- keep private source data out of exported cases unless explicitly authorized.

Acceptance criteria:

- a historical case can be rerun with its original available context;
- two context policies can be compared on the same cases;
- evaluation cases retain source lineage and consent state;
- replay cannot mutate production memory by default.

## Milestone 9 — Distribution and ecosystem

**Goal:** Make CMF useful beyond its original personal deployment.

Deliverables:

- one-command local Docker Compose deployment;
- documented minimal memory-only configuration;
- documented full journal + memory + knowledge configuration;
- Python SDK and OpenAPI description;
- MCP server package;
- adapter development kit and conformance tests;
- provider development kit and conformance tests;
- migration and backup tools;
- threat model and privacy guide;
- sample deployments for individual, developer-team, and self-hosted server use.

Acceptance criteria:

- a new user can start without an LLM Wiki;
- optional importers and adapters install independently;
- third parties can implement a provider without modifying CMF core;
- backups restore journal, memory identity, provenance, and configuration;
- provider failures degrade visibly and do not silently discard context.

## Cross-cutting work

### Security and privacy

- secret and credential filtering;
- content-class allow/deny policies;
- encryption in transit and at rest;
- scoped identities and least privilege;
- prompt-injection boundaries for retrieved content;
- retention, deletion, and legal-hold semantics;
- explicit treatment of sensitive personal data;
- auditable provider calls;
- local/private extraction options.

CMF context governance should remain distinct from, but interoperable with, agent control-plane work such as Interlock.

### Observability

Measure:

- capture success and lag;
- consolidation latency and failure;
- extraction precision and rejection rate;
- duplicate and conflict rates;
- retrieval relevance;
- temporal correctness;
- provenance coverage;
- token cost;
- provider latency and errors;
- deletion/correction propagation.

### Compatibility

- version all canonical schemas;
- offer migrations rather than silent changes;
- maintain backward-compatible MCP aliases through the modularization period;
- distinguish CMF IDs from provider-native IDs;
- avoid leaking Graphiti-specific types into the public contract.

## Near-term execution sequence

The next implementation work should follow this order:

1. finish and verify the current production import;
2. tag or record the Phase 1 baseline;
3. add architecture decision records for the four-layer model and provider boundaries;
4. define provider protocols;
5. make Wiki configuration optional;
6. specify the canonical event envelope;
7. implement the journal and link the ChatGPT importer to it;
8. separate capture from consolidation;
9. build the first Claude Code adapter;
10. prove cross-harness recall with Codex or Gemini CLI;
11. build review/governance surfaces;
12. begin retrieval and memory evaluations.

## Explicit non-goals for the next phase

- matching Glean's enterprise connector catalog;
- building a general enterprise-search product;
- replacing Graphiti before provider boundaries are proven;
- splitting CMF into independently deployed microservices;
- automatically promoting inferred memory into durable knowledge;
- ingesting every turn directly into Graphiti;
- browser scraping as a required capture mechanism;
- reinforcement-learning or model post-training infrastructure;
- combining CMF and an agent control plane into one service.

## Decision gates

Before each expansion, answer:

| Gate | Decision |
|---|---|
| Journal backend | Is the local implementation sufficiently durable and queryable, or is PostgreSQL required? |
| Capture policy | Which event types are stored raw, redacted, summarized, or excluded? |
| Consolidation policy | Which memories may be accepted automatically and which require review? |
| Scope model | How do personal, project, team, and organization contexts inherit or isolate access? |
| Provider API | Can a second memory provider and a second knowledge provider pass conformance tests? |
| Remote deployment | What authentication, encryption, and tenancy model is required? |
| Evaluation | Does cross-provider context measurably improve correctness and continuity? |
| Product audience | Is the strongest adoption among individuals, developer teams, or organizations? |

## Success definition

CMF reaches the next meaningful product state when:

- a user can run it without an LLM Wiki;
- a captured event from one AI harness becomes an evidence-backed memory;
- another harness can retrieve that memory;
- a correction preserves both history and current validity;
- knowledge from an optional provider can be combined without losing provenance;
- the user can inspect why the memory exists;
- all context can be exported or migrated without dependence on one model vendor or storage provider.
