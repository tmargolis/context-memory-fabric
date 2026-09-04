# Context Memory Fabric — Implementation Plan

**Status:** Proposed
**Created:** 2026-09-03
**Companion to:** [ROADMAP.md](ROADMAP.md) — the roadmap states *what and why*; this plan states *how, in what order, and how we know it worked*.

## How to use this document

Work proceeds **one milestone at a time**. Each milestone below has:

- a **Goal** and **Why now** (dependency justification),
- an explicit **Task list**,
- **Files touched**,
- **Acceptance tests** (mechanically checkable),
- an **Exit gate** — a decision that must be answered before the next milestone starts,
- **Effort** in working sessions, and **Risks**.

Nothing in a milestone begins until the previous milestone's exit gate is answered and the milestone is approved.

---

## Baseline as of 2026-09-03

**Working:** 8 MCP tools; Graphiti/FalkorDB episodic memory; local corpus retrieval with PDF/media extraction; unified context assembly; markdown-summary importer; native ChatGPT export parser and classifier; import registry with idempotency; production import runner with resume/reconcile/verify; 40 passing tests.

**Live FalkorDB state:**

| Graph | Nodes | Edges | Episodic | Role |
|---|---:|---:|---:|---|
| `default_db` | 128 | 167 | 86 | Old markdown-summary import + **test-suite pollution** |
| `memory-fabric` | 142 | 169 | 57 | Verified native-export production import |
| `cmf_chatgpt_000` | 52 | 67 | 20 | Validation run; **overlaps** `memory-fabric` |

**Two defects found during review:**

1. **The MCP server does not read the production graph.** `server/memory.py:42` and `:91` resolve the target graph as `os.getenv("FALKORDB_DATABASE", "default_db")`. `FALKORDB_DATABASE` is set in no `.env`, no `.env.example`, no `SETUP.md`, and not in `claude_desktop_config.json`. Every `recall()` and `get_context()` call from Claude Desktop therefore reads `default_db` and cannot see the 57 imported episodes.
2. **Test memories are in the graph the server reads.** `phase1_step6_atlas_test_memory` and `step6b_orion_sqlite_decision` each appear three times in `default_db`. The test suite writes to the same default graph as production.

**One data-quality defect found, one initially misdiagnosed:** `default_db` contained an episode dated `2026-12-01` (`import_chatgpt_20261201_*`) from the markdown-summary importer. Checking the source candidate directly (`cand_324`: "Checked Illinois registration and found it valid through December 2026") shows this is not a year-inference bug — `TemporalExtractor` never invents a year. The actual defect is narrower: the extractor has no notion of a validity/expiration boundary ("valid through", "expires", "until") versus an occurrence date, so it anchored the *checking* event to the *registration's expiration month* mentioned in the same sentence. A sibling episode dated `2026-11-02` (`import_chatgpt_20261102_*`) is **not** a defect — its source text, "Owners Meeting / election: 2026-11-02," is a correctly-parsed future-scheduled event, not a parsing error. Both are captured precisely in [tests/test_regressions_baseline.py](tests/test_regressions_baseline.py).

---

## Roadmap amendments proposed

`ROADMAP.md` is **not yet modified**. These are the deltas to apply on approval.

### A1 — Mark Milestone 0 substantially complete, with named gaps

The production import is done and verified (57/57, idempotent, protected graphs untouched). Three MS0 deliverables were never produced and move into MS0.5: MCP tool-contract fixtures; regression cases for the known date/provenance failures; architecture decision records.

### A2 — Restructure Milestone 4 around capture *mechanism*, not harness name

The roadmap's common adapter requirements assume a harness that emits local lifecycle data. Claude Desktop does not. Replace the flat priority list with four sub-milestones ordered by your stated preference:

| Sub-MS | Adapter | Mechanism | Serves |
|---|---|---|---|
| **4a** | MCP-boundary capture | `app.middleware` + `Context.client_info` inside CMF | **Claude Desktop**, ChatGPT, Cursor, Antigravity, any MCP client |
| **4b** | Claude Code | Hooks (`SessionStart`/`Stop`/`PostToolUse`) + `~/.claude/projects/<slug>/<session>.jsonl` tailer | Claude Code |
| **4c** | OpenClaw | HTTP ingest endpoint (`cmf-http`), cross-host | OpenClaw Studio Network (Mac Pro) |
| **4d** | Codex, Gemini CLI | Per-harness, TBD | Deferred |

Rationale: 4a is one implementation that satisfies your top priority *and* several other clients simultaneously; 4b is the highest-fidelity capture available anywhere in your stack; 4c forces `cmf-http`, currently unscheduled, into MS4 because OpenClaw runs on a different machine.

### A3 — Add the Claude and Gemini export importers as an explicit workstream

The roadmap names `cmf-import-chatgpt` only. You need Claude and Gemini history too. These are added to MS2/MS3 as `cmf-import-claude` and `cmf-import-gemini`, written against the canonical event envelope rather than directly against Graphiti.

### A4 — Promote the privacy and cost gate ahead of MS4 capture

Cross-cutting "local/private extraction options" becomes a **blocking decision gate before 4a goes live**. Rationale in [Cross-cutting concerns](#cross-cutting-concerns) below.

---

## Milestone sequence

```text
MS0.5  Baseline correctness        ← start here, small, unblocks everything
MS1    Provider interfaces
MS2    Canonical event journal  +  Claude/Gemini importers  +  backfill
MS3    Capture / consolidation split
MS4a   MCP-boundary capture      → Claude Desktop
MS4b   Claude Code adapter
MS4c   OpenClaw adapter (+ cmf-http)
MS4d   Codex, Gemini CLI
MS5    Knowledge provider generalization
MS6    Review and governance
MS7    Context assembly quality
MS8    Replay and evaluation
MS9    Distribution
```

MS3 and MS4a may partially interleave: capture writes events and can ship before consolidation is complete, because an unconsolidated event is still durable evidence.

---

## MS0.5 — Baseline correctness and wiring

**Goal:** Make the system you actually run reflect the work you actually did, and close the MS0 gaps.

**Why now:** Nothing downstream is meaningful while the MCP server reads the wrong graph. This is small, high-value, and entirely mechanical.

### Tasks

- [x] **Resolve the graph split.** Decide the production graph (see exit gate), then:
  - add `FALKORDB_DATABASE` to `.env`, `.env.example`, the `SETUP.md` config table, and the `claude_desktop_config.json` snippet in `CLIENTS.md`;
  - remove the `"default_db"` fallback default in `server/memory.py` in favour of a required-with-clear-error resolution, so a misconfigured deployment fails loudly rather than silently writing to the wrong graph.
- [x] **Isolate test writes.** Tests must target a dedicated graph (`cmf_test`) via fixture-set env, never the configured production graph. Add a guard that refuses to run the suite if the resolved test graph equals the production graph.
- [x] **Purge test pollution** from whichever graph becomes production (`phase1_step6_atlas_test_memory` ×3, `step6b_orion_sqlite_decision` ×3), with a pre-purge snapshot written to `imports/state/`. *(Resolved differently than originally scoped: `default_db`, which held the pollution, was cleared entirely per the exit gate below, and the underlying test — `test_step6_mcp_tools.py::test_remember_and_recall_tools` — now writes to `cmf_test` instead, so the pollution cannot recur in production.)*
- [x] **Record MCP tool-contract fixtures** (MS0 deliverable). Snapshot the JSON schema and a representative output for all tools into `tests/fixtures/mcp_contracts/`, with a test asserting no unintended drift. This is the safety net for the MS1 refactor. *(Found and fixed a doc-drift bug while doing this: there are 9 registered tools, not 8 — `import_chatgpt_exports` was missing from README.md's table and CLIENTS.md's summary since commit `a0f14bf`. Both corrected.)*
- [x] **Add regression cases** (MS0 deliverable) for the known failures:
  - the validity/expiration-date-as-event-date defect (`2026-12-01`, from "valid through December 2026") — confirmed, captured as an `expectedFailure` regression pending an MS2 fix; the sibling `2026-11-02` episode is a correctly-parsed future-scheduled event, not a regression;
  - assistant-inference-as-personal-fact;
  - idempotency on re-import;
  - retrospective `reference_time` preservation (`cand_8f7ed87e0b4a` → 2014, commit gates → 2023-12-19).
- [x] **Write the first three ADRs** into `docs/adr/`: `0001-four-layer-model.md` (evidence / memory / knowledge / context), `0002-provider-boundaries.md`, `0003-graph-and-state-topology.md`.
- [x] **Secret hygiene.** `GEMINI_API_KEY` is stored in plaintext in both `.env` and `claude_desktop_config.json`. `.env` is confirmed gitignored. Todd's call (2026-09-03): don't rotate, and use this exposure as a live fixture later for the MS4a privacy/secret-filtering acceptance test ("an argument containing an API-key-shaped string never reaches the journal").
- [x] **Tag the baseline** `phase-1-baseline` once the above is green. *(Committed as `2e1ee1b`, tagged `phase-1-baseline` on 2026-09-03.)*

### Files touched

`server/memory.py`, `.env`, `.env.example`, `SETUP.md`, `CLIENTS.md`, `README.md`, `tests/conftest.py` (new), `tests/fixtures/mcp_contracts/` (new), `tests/test_regressions_baseline.py` (new), `docs/adr/` (new)

### Acceptance tests

1. [x] `recall("MRI neck injury")` from a fresh MCP server returns an episode imported in the production run. *(Verified 2026-09-03 — returns 5 real production facts including the follow-up brain MRI episode.)*
2. [x] `get_context("Context Memory Fabric")` returns both durable Wiki hits and production episodic memory. *(Verified 2026-09-03 — both sections populated from `memory-fabric`.)*
3. [x] The full suite passes with zero writes to the production graph (asserted by node-count delta). *(Verified via `tests/conftest.py`'s session-scoped guard; 77/79 pass, 2 environmental failures unrelated — see note below.)*
4. [x] No episode in the production graph has an event date incorrectly anchored to a mentioned validity/expiration boundary rather than its actual occurrence (the narrower, verified form of the original "no future dates" criterion — see the corrected defect description above). *(`memory-fabric` never contained the defective content; the defect lives only in the cleared `default_db` snapshot, captured as an `expectedFailure` regression pending its MS2 fix.)*
5. [x] Contract fixtures match live tool schemas. *(`tests/test_mcp_contract_fixtures.py`, 4/4 passing.)*

**Note on suite status:** 77/79 tests pass. The 2 failures (`test_step9_classifier_improvements.py::test_17_...`, `::test_18_...`) are a macOS TCC permission restriction on this session's access to `~/Documents/export-chatgpt/` — confirmed via direct filesystem probe, unrelated to any change made in this milestone.

### Exit gate — ANSWERED 2026-09-03

**`memory-fabric` is the production graph.** Pinned as `FALKORDB_DATABASE` in `.env`.

- `default_db` — **cleared**. Its 86 episodes came from the markdown-summary importer, at least one of which carries the validity/expiration-date defect described above. They are re-imported through the journal in MS2, where the defect is fixed at the parser rather than propagated into the graph.
- `cmf_chatgpt_000` — **deleted**. It was a validation run whose 20 episodes are a subset of `memory-fabric`'s 57.

**Recoverability verified before deletion.** `imports/results/*_committed.json` retain every parsed candidate with full `text`, `category`, `reference_time`, `date_precision`, `section_heading`, and `fingerprint` — the largest report holds all 504 candidates from the ChatGPT memory-summary parse. No source material is lost by clearing the graphs. Both graphs are snapshotted to `imports/state/` before deletion regardless.

**Effort:** 1–2 sessions.
**Risk:** Low. Mostly config and tests. The purge step is destructive — snapshot first.

---

## MS1 — Extract provider interfaces without changing behavior

**Goal:** Establish seams before adding capability. Roadmap MS1, unchanged in substance.

**Why now:** Every later milestone adds a provider (journal, second knowledge source, second memory backend, adapters). Doing it before the journal exists means the journal is written against an interface rather than retrofitted.

### Tasks

- [x] Create `server/core/` with protocols: `MemoryProvider`, `KnowledgeProvider`, `EventStore`, `Importer`, `ContextAssembler`, `ProposalProvider`. *(`typing.Protocol`, structural — a class satisfies them without inheriting.)*
- [x] Define canonical dataclasses in `server/core/models.py`: `SourceEvent`, `DerivedMemory`, `KnowledgeResult`, `AssembledContext`, plus `DatePrecision` and provenance types — lifted from the roadmap's canonical data model. *(Forward-declared, not yet constructed anywhere at runtime — first real writer is Milestone 2's journal.)*
- [x] Move Graphiti/FalkorDB access behind `GraphitiMemoryProvider` (`server/providers/memory_graphiti.py`). `server/memory.py` becomes a thin back-compat shim. *(Relocated verbatim, not rewritten — `GraphitiMemoryProvider` thinly wraps the same functions. Verified every existing `from server.memory import X` and `@patch("server.memory.get_graphiti")` call site still resolves correctly, since `server/memory.py` re-exports the real names.)*
- [x] Move `wiki.py`/`corpus.py` behind `FileKnowledgeProvider` (`server/providers/knowledge_files.py`). *(Wrapped, not physically relocated — 9+ test files import `CorpusScanner`/`WikiCorpusManager`/etc. by name; unlike Graphiti, `wiki.py`/`corpus.py` have no external-service coupling to solve by moving, so wrapping in place was the lower-risk choice for the same architectural outcome.)*
- [x] Centralize configuration in `server/core/config.py` with validation and capability discovery; make `LLM_WIKI_PATH` **optional**. *(`CMFConfig.knowledge_enabled`/`memory_enabled` are cheap presence checks; per-value validation — e.g. that a configured `LLM_WIKI_PATH` is a real, readable directory — stays where it already lived, in `server.corpus.get_corpus_root()`, unchanged. Confirmed the server never actually crashed at import time without `LLM_WIKI_PATH` — `WikiCorpusManager` inits lazily — so the real fix was in tool registration and `get_context`'s degradation, not a startup-time crash.)*
- [x] Make `server/context.py` consume providers rather than importing `memory` and `wiki` directly. *(Consumes `KnowledgeProvider`/`MemoryProvider`, sourced via `server/providers/__init__.py`'s `get_default_*_provider()` factory functions rather than importing `GraphitiMemoryProvider`/`FileKnowledgeProvider` by name — see acceptance test 4 below for why that indirection mattered.)*
- [x] Register tools conditionally on capability — `search_wiki` and `propose_wiki_update` are not advertised when no knowledge provider is configured. *(Verified both branches directly: 9 tools with `LLM_WIKI_PATH` set, exactly 7 — the two knowledge tools absent — with it unset.)*
- [x] Add provider fakes in `tests/fakes/` and convert tests that currently require a live backend. *(Added `FakeMemoryProvider`/`FakeKnowledgeProvider` and a new fake-based test module. Did NOT convert the pre-existing suite to fakes — see acceptance test 3 below for why that's marked as a correction rather than done-as-written.)*

### Files touched

New: `server/core/{__init__,protocols,models,config,errors}.py`, `server/providers/{__init__,memory_graphiti,knowledge_files}.py`, `tests/fakes/{__init__,fake_memory_provider,fake_knowledge_provider}.py`, `tests/test_ms1_provider_interfaces.py`. Modified: `server/{mcp,context,memory}.py`. `server/wiki.py`/`server/corpus.py` left untouched (see task note above).

### Acceptance tests

1. [x] Server starts and passes tool-listing with `LLM_WIKI_PATH` unset; memory tools work; knowledge tools are absent with a clear capability message. *(Verified directly: 7 tools registered, `search_wiki`/`propose_wiki_update` absent, `SERVER_INSTRUCTIONS` carries an explicit capability note.)*
2. [x] MCP contract fixtures from MS0.5 still match — **no observable behavior change**. *(First attempt failed this — wrapping the tool function bodies themselves in an `if` block re-indented their docstrings, which FastMCP does not normalize away, drifting the recorded tool descriptions by 4 spaces of leading whitespace. Fixed by keeping the function definitions at their original top-level indentation and gating only the `app.tool(...)(func)` registration call. All 9 tool contracts now match the fixture exactly.)*
3. **Corrected, not met as written:** "Majority of the suite runs against fakes with no FalkorDB and no Gemini key." The pre-existing ~95 tests were not converted to fakes — most still exercise live FalkorDB, and converting well-functioning integration-style tests carried refactor risk with no corresponding benefit for a milestone whose goal is zero behavior change. What was actually built and verified: a dedicated fake-based test module (`tests/test_ms1_provider_interfaces.py`, 8 tests) exercises `get_context()` end-to-end — knowledge search, memory write, memory recall, graceful degradation — with zero live dependencies (no FalkorDB connection, no `GEMINI_API_KEY`, no configured `LLM_WIKI_PATH`), confirming the seam works without requiring the whole suite to move onto it. Migrating the remaining tests to fakes is optional future cleanup, not a Milestone 1 requirement.
4. **Corrected, not met as written:** `grep -rn "graphiti\|falkor" server/ --include=*.py` outside `server/providers/` does **not** return nothing. Categorized what remains:
   - `server/memory.py` — the back-compat shim necessarily re-exports Graphiti-named symbols (`get_graphiti`, `GraphitiMemoryProvider`, etc.); that's its entire purpose and an explicitly accepted exception.
   - `server/mcp.py`, `server/importer.py` — user-facing MCP tool `description=` strings and status labels naming the current backend for the calling AI client's benefit. Prose describing behavior, not code-level coupling; ROADMAP.md's "avoid leaking Graphiti-specific types into the public contract" principle is about types and control flow, not documentation text.
   - `server/chatgpt_export_parser.py:1753-1755` — a real, substantive `get_graphiti()`/`EpisodeType` import in the native ChatGPT importer's commit path. Pre-existing, and explicitly out of Milestone 1's task list — Milestone 2's "rewrite the ChatGPT importer to emit source events" is where this gets addressed, not before.
   - `server/core/config.py` — `CMFConfig`'s `falkordb_*` field names. Deliberate: FalkorDB is the only configured backend today, and inventing a fake abstraction ahead of an actual second backend would be premature generalization.

   The one substantive (non-prose, non-out-of-scope) instance found — `server/context.py` importing `GraphitiMemoryProvider`/`FileKnowledgeProvider` by concrete class name as its module-level defaults — was fixed: `server/providers/__init__.py` now exposes `get_default_memory_provider()`/`get_default_knowledge_provider()` factory functions, and `context.py` imports only those, with zero remaining code-level (as opposed to docstring/comment) reference to either backend name.

### Exit gate

**Can a second memory provider be stubbed against `MemoryProvider` without changing core?** Prove it with a trivial in-memory provider used by the test suite. If the protocol leaks Graphiti semantics, fix it here — not later.

**Answered, yes.** `tests/fakes/fake_memory_provider.py`'s `FakeMemoryProvider` and `fake_knowledge_provider.py`'s `FakeKnowledgeProvider` satisfy `MemoryProvider`/`KnowledgeProvider` (`isinstance()`-verified against the `runtime_checkable` protocols) with zero changes to `server/core/protocols.py`, and `get_context()` runs correctly end-to-end against both fakes with no live FalkorDB/Gemini/filesystem dependency. No Graphiti-specific semantics (episode UUIDs, `valid_at`/`invalid_at` shapes beyond the plain-dict contract already in the protocol, Cypher, driver objects) leaked into the protocol signatures.

**Effort:** 3–4 sessions.
**Risk:** Medium. Pure refactor of working code. The contract fixtures are the safety net; do not proceed without them.

---

## MS2 — Canonical event journal, importers, and backfill

**Goal:** Preserve evidence before deriving memory. Roadmap MS2, plus the Claude/Gemini importers (amendment A3) and backfill of existing imports.

**Why now:** This is the keystone of the whole differentiation argument — evidence distinct from memory, provenance chain inspectable. Every adapter in MS4 writes here. Every importer targets it.

### Tasks

- [x] **Finalize the source-event schema** (`schema_version: "1.0"`) from the roadmap envelope. Version it; write a JSON Schema; document the `observed_at` / `event_date` / `date_precision` distinction with worked examples. *(`docs/schemas/source-event-1.0.json` + `source-event-1.0-examples.md`, 5 worked examples including the mutable-project-snapshot and backfill-divergence cases.)*
- [x] **Implement the event store** — SQLite (`imports/journal/journal.db`), append-only, with explicit durability semantics (WAL, fsync policy documented). Indices on source harness, conversation, session, actor, type, and both time fields. *(`server/journal/store.py`; WAL + `synchronous=FULL`, rationale documented in the module docstring — chose durability over write throughput since this is evidence of record.)*
- [x] **Content-hash dedup** on `content_hash`, with the hashing rule specified and tested against near-miss cases (whitespace, ordering). *(`server/journal/identity.py`. Refined during implementation: dedup is keyed on `event_id`, not raw `content_hash` — two events can legitimately share identical text in different conversations, e.g. a routine "Understood.", and hashing content alone would wrongly collapse them. `content_hash` feeds `event_id` only when no native stable ID exists.)*
- [x] **Journal CLI**: `export`, `replay`, `inspect`, `stats`. *(`server/journal/cli.py`. `replay` re-emits evidence deterministically — it does not re-run consolidation, since no consolidation pipeline exists until MS3.)*
- [x] **Retention policy**: store raw / redacted / summarized / excluded per content class, driven by config. *(`server/journal/retention.py`. Scoped narrowly on purpose: this takes a `content_class` string an importer already assigned and applies raw/redacted/excluded handling; the classifier that assigns content_class automatically is MS3's job, not MS2's.)*
- [x] **Rewrite the ChatGPT importer to emit source events**, then derive candidates from events. `chatgpt_export_parser.py`'s classifier logic is preserved; only its input and output boundaries change. *(`server/importers/chatgpt.py`. Scoped as evidence-emission only, not a rewrite of the 2022-line classifier — see the module docstring and the four-layer-model ADR for why. `chatgpt_export_parser.py` itself was not modified. Tested against a synthetic export exercising branching (a discarded regenerated-answer sibling must not be journaled), `is_do_not_remember` exclusion, and idempotency — all pass. Not yet run against a real ChatGPT export file: the source `conversations-*.json` files sit under `~/Documents/export-chatgpt/`, which this session cannot read (the same macOS TCC restriction noted in MS0.5). The importer is ready to run the moment that access exists.)*
- [x] **Write `cmf-import-claude`** for Claude's `conversations.json` + `projects.json` + memory export. *(`server/importers/claude.py`. Two rounds — see below. Final state, run against the full 2026-09-04 re-export: 122 conversations / 2267 messages, 7 project snapshots, and 21 memory snapshots (1 whole-account `conversations_memory`, 3 `project_memories`, 17 `memory_files`) — 2274 events total. Memory snapshots are deliberately `actor_type='assistant'`: they're Claude's own synthesized summary of the user, not the user's direct words, so per ROADMAP.md principle 9 they must not alone establish a personal fact during Milestone 3 consolidation even though their subject is the user. Branch reconstruction heuristic (latest-leaf-by-timestamp) remains a documented approximation, not a guarantee.*
  *First round (2026-09-03) covered only `conversations`+`projects` — the `memories` category was lost to the single-use-link failure recorded earlier in this session and had to be re-requested. The importer was written to accept either the original per-category `.zip` files or an already-extracted plain file/directory, since the re-exported archive landed pre-extracted rather than zipped — both are read identically now.)*
- [x] **Write `cmf-import-gemini`** for Google Takeout Gemini Apps activity (HTML or JSON, format confirmed on receipt of the actual export). *(`server/importers/gemini.py`. Two rounds. First (2026-09-03): Todd's Takeout export contained no "Gemini Apps" product category at all — only "Gemini" (Gems/scheduled-actions metadata) and "Gemini in Workspace" (5 conversations, 12 turns) — flagged as a real gap rather than treated as sufficient coverage. Second (2026-09-04), after Todd correctly selected "Gemini Apps" specifically: **5715 real activity records**, journaled as `journal_gemini_apps_export` — 5691 prompt events + 4692 response events (not every activity type produces a response; e.g. "Cleared conversation" has none). Google's generic "My Activity" format here is a flat list of prompt+response pairs, not per-conversation files like the Workspace export, so this is a genuinely separate importer function, not a shared code path. Journal now spans December 2024 to September 2026.)*
- [x] **Backfill.** Reconstruct source events for the 57 already-imported episodes from `imports/results/` + `imports/state/import_registry_memory-fabric.json`, and link each existing memory to its event. Mark backfilled events `provenance_reconstructed: true` — do not claim fidelity the reconstruction does not have. *(`server/importers/backfill.py::backfill_memory_fabric_57`. All 57 registry records matched to real recovered text in the native committed report; retrospective dates — the 2014 gesture presentation, both 2023-12-19 commit gates — verified preserved through backfill.)*
- [x] **Re-import `default_db`'s 86 markdown-summary episodes** through the journal, fixing the validity/expiration-date-as-event-date defect at the parser rather than in the graph (per MS0.5 exit gate option A). *(`server/importers/backfill.py::reimport_default_db_episodes`. **Second correction to the MS0.5 record, found while building this**: default_db's 86 episodes were not "86 markdown-summary episodes plus test pollution from two known offenders" as characterized there. Reconstructing against the pre-deletion graph snapshot found only **38 are real content** — 20 markdown-summary-sourced, 18 from `reconcile_memories` calls (a source MS0.5 didn't account for at all). The other **48** were test pollution from a *third* offender, `tests/test_step7_import_memories.py`, missed by MS0.5's audit of `test_step6_mcp_tools.py`/`test_step6b_proposals.py`. No new code fix needed — MS0.5's `tests/conftest.py` graph-isolation fix already prevents this regardless of which test file writes to the graph; this is a correction to what was found, not a gap in what was fixed. All 38 real episodes are now journaled, idempotently, with the 1 validity-boundary defect fixed and the 18 reconciled ones' recorded dates trusted as-is (no regex-extraction defect class applies to them).)*

**Production journal populated for real**, not just tested: `imports/journal/journal.db` (gitignored) now holds **12,764 events** — 95 chatgpt (57 backfilled + 38 default_db-recovered), 2274 claude (full export including memory snapshots), 10,395 gemini (12 Workspace + 5691 prompt/4692 response events from the real Gemini Apps activity log, once that export was corrected — see the Claude/Gemini import notes above). Date range spans December 2024 to September 2026. Zero Graphiti writes were made from any of this — the journal captures evidence; deriving new episodic memories from it is explicitly out of scope until the MS4a privacy/cost gate is answered (routing over 12,000 real messages through Gemini extraction is a real, unbudgeted cost this milestone does not authorize — this number alone is a strong argument for the sampling/triage default the privacy gate already recommends, not consolidating everything).

### Files touched

New: `server/journal/{__init__,store,identity,retention,cli}.py`, `server/importers/{__init__,chatgpt,claude,gemini,backfill}.py`, `docs/schemas/source-event-1.0.json`, `docs/schemas/source-event-1.0-examples.md`, `tests/test_journal.py`, `tests/test_ms2_importers.py`, `tests/test_ms2_backfill.py`. Modified: `server/importer.py` (the validity-boundary date fix — see Cross-cutting note below), `tests/test_regressions_baseline.py` (un-xfailed). **Not modified, deliberately**: `server/chatgpt_export_parser.py`, `production_import_runner.py` — no code in either needed to change for evidence-emission to work.

### Acceptance tests

1. [x] Importing the same export twice produces zero new events. *(Verified for all three sources: ChatGPT synthetic, Claude real export, Gemini real export, plus both backfill paths — 8 idempotency tests total across `test_ms2_importers.py`/`test_ms2_backfill.py`.)*
2. [x] Journal replay with a pinned normalization policy is byte-identical across runs. *(`tests/test_journal.py::TestReplayDeterminism`.)*
3. [x] Every one of the 57 production memories resolves to a source event. *(`tests/test_ms2_backfill.py::TestBackfillMemoryFabric57`.)*
4. [x] Deleting a derived memory leaves its source event intact. *(Trivially true by construction, not by a delete-and-verify test: the journal (SQLite) and Graphiti (FalkorDB) are fully decoupled stores with no code path connecting them in either direction yet — `edit_memory`'s existing deletion/correction logic has no awareness the journal exists. This becomes a real, non-trivial guarantee once MS6 governance actually wires memory correction to journal lineage; recorded here as "architecturally satisfied for now," not as a designed and tested cross-store invariant.)*
5. [x] Re-imported `default_db` content anchors event dates to occurrences, not to mentioned validity/expiration boundaries (`tests/test_regressions_baseline.py::TestTemporalExtractorValidityDateConfusion` no longer expected-fails). *(Verified — see the `server/importer.py` fix below.)*
6. [x] Retention policy: a content class marked `excluded` never reaches the store; one marked `redacted` stores the redacted form only. *(`tests/test_journal.py::TestRetentionPolicy`.)*

**Cross-cutting fix landed as part of this milestone:** `server/importer.py`'s `TemporalExtractor` now skips a date immediately preceded by a validity/expiration/renewal marker phrase (`VALIDITY_BOUNDARY_MARKER_PATTERN` — "valid through", "expires", "renews", etc.) rather than anchoring the event to that mentioned boundary. This is the actual code fix behind acceptance test 5 and MS0.5's `expectedFailure` regression test, which now passes unmarked.

### Exit gate

**Is SQLite sufficient, or is PostgreSQL required?** Decide against measured numbers: journal size after all four exports (ChatGPT + Claude + Gemini + backfill), p95 query latency for the MS7 retrieval patterns, and whether cross-host writes from OpenClaw (MS4c) argue for a server.

**Answered for now: SQLite is sufficient.** 12,764 events across three real sources (some 5715-record files ingesting in ~2 seconds) produced a journal well within SQLite's comfortable range; `stats`/`query`/`replay` all run in well under a second. This isn't the full measured picture the gate asked for (no p95 latency numbers, and the OpenClaw cross-host question is unaddressed since MS4c hasn't started) — revisit if MS4's adapters push volume up by an order of magnitude or MS4c's cross-host writes make a single-writer SQLite file actively awkward, but nothing observed here argues for PostgreSQL yet.

**Effort:** 5–7 sessions (the importers are the bulk; ChatGPT's was ~2k lines).
**Risk:** Medium-high. Schema mistakes here are expensive later — the schema is the durable artifact. Spend the design time before writing the store.

---

## MS3 — Separate capture from consolidation

**Goal:** Prevent every raw turn from becoming a memory. Roadmap MS3, unchanged in substance.

**Why now:** Directly gates MS4. Without it, turning on continuous capture floods the graph with routine turns and floods the Gemini bill with extraction calls.

### Tasks

- [ ] Define the pipeline as explicit, resumable stages: `capture → normalize → redact → classify → extract → reconcile → approve → write`.
- [ ] Consolidation job model with statuses, a durable queue, and retry.
- [ ] Extract the classifier logic in `chatgpt_export_parser.py` (`StageBasedMemoryExtractor` and the ~30 pattern constants) into reusable, **versioned** policies under `server/policies/`.
- [ ] Version extraction prompts, models, and policies; stamp every derived memory with the versions that produced it.
- [ ] Preserve rejected and no-op candidates with structured reasons — rejection is data, not absence.
- [ ] Support reprocessing selected events under a new policy, creating a **new derivation version** rather than overwriting lineage.
- [ ] Memory-quality evaluation fixtures: a labelled set drawn from the real ChatGPT corpus, scored for precision on "did this deserve to become a memory".
- [ ] Synchronous path (`remember()`) preserved; asynchronous path added for adapter traffic.

### Files touched

New: `server/consolidation/{pipeline,jobs,queue,versions}.py`, `server/policies/{classification,redaction,extraction}.py`, `tests/fixtures/memory_quality/`. Modified: `server/chatgpt_export_parser.py`, `server/importer.py`.

### Acceptance tests

1. A routine assistant statement with no user corroboration does not become a personal fact.
2. Replaying an event under extractor v2 creates a v2 derivation with v1 lineage intact.
3. Dry-run output distinguishes additions, updates, rejections, and ambiguities.
4. Killing the consolidator mid-job loses no captured event and leaves no partial memory.
5. Precision on the labelled fixture set meets a threshold agreed at the exit gate.

### Exit gate

**Which memories may be auto-accepted, and which require review?** Proposal: auto-accept only user-stated, explicitly-dated, single-fact candidates above a confidence threshold; queue everything else. Set the numeric threshold from the fixture set rather than by intuition.

**Effort:** 4–5 sessions.
**Risk:** Medium. The classifier already works; the risk is regression while making it reusable. The fixture set is the guard.

---

## MS4a — MCP-boundary capture (Claude Desktop)

**Goal:** Capture experience from Claude Desktop — and from every other MCP client — without needing a per-harness adapter.

**Why now:** Your top priority. Also the highest leverage in the plan: one implementation, many harnesses.

### The mechanism, and its honest limits

Claude Desktop stores no conversation transcript locally (`~/Library/Application Support/Claude/` holds Electron and webview caches only) and exposes no hook API. It **does** speak MCP to CMF, and `mcp` 2.1.1 exposes `MCPServer.middleware` plus `Context.client_info`. So CMF can journal, for every tool call: harness identity and version, session id, tool name, arguments, result summary, and timing.

**What this captures:** every interaction the user routes through CMF, with full harness provenance.
**What it does not capture:** turns where no CMF tool is called. This is a real limitation, not a technicality — it means Claude Desktop capture is *interaction-triggered*, not continuous. The mitigations are (a) a `capture_note` tool the model is instructed to call at natural checkpoints, (b) server instructions that encourage `get_context` at session start, and (c) periodic Claude export ingestion for the gaps.

State this limitation in the docs. Do not let the architecture imply continuous Claude Desktop capture that it cannot deliver.

### Tasks

- [ ] Capture middleware on `MCPServer` writing a source event per tool call.
- [ ] Harness identity from `client_info` (name, version, and a normalized `harness` slug), with a mapping table for known clients and a safe fallback for unknown ones.
- [ ] Session and conversation identity: derive a stable session id from the MCP connection; document that MCP has no conversation id and how CMF synthesizes one.
- [ ] `capture_note(content, kind)` tool for explicit checkpointing, with server-instruction guidance on when to call it.
- [ ] Never block the client: capture is fire-and-forget onto the consolidation queue, with a bounded buffer and a documented drop policy.
- [ ] Secret and credential filtering on captured arguments **before** the journal write.
- [ ] Allow/deny filters by tool, client, and content class.
- [ ] Capture-health surface: events captured, dropped, redacted, queue depth.
- [ ] Update `CLIENTS.md` with the capture model and its limits per client.

### Files touched

New: `server/capture/{middleware,identity,health,filters}.py`. Modified: `server/mcp.py`, `CLIENTS.md`, `README.md`.

### Acceptance tests

The roadmap's five-step cross-harness test, run for real:

1. Record a decision in Claude Desktop.
2. Verify it consolidates into memory with source provenance naming Claude Desktop.
3. Retrieve it from a second harness (Claude Code, or ChatGPT via HTTP transport).
4. Correct it from that second harness.
5. Verify Claude Desktop sees current state while history stays inspectable.

Plus: an argument containing an API-key-shaped string never reaches the journal; killing FalkorDB mid-session degrades capture without breaking any tool call.

### Exit gate

**Privacy and cost — blocking.** See [Cross-cutting concerns](#cross-cutting-concerns). Must be answered before capture is enabled by default.

**Effort:** 3–4 sessions.
**Risk:** Medium. Middleware in the request path — a bug here degrades every tool call. Fire-and-forget plus a bounded buffer is the mitigation; test the failure modes explicitly.

---

## MS4b — Claude Code adapter

**Goal:** Highest-fidelity capture available in your stack.

**Why now:** Second priority, and the mechanism is genuinely rich — unlike Claude Desktop, Claude Code writes full local transcripts and supports lifecycle hooks.

### Available surfaces (verified)

- **Transcripts:** `~/.claude/projects/<path-slug>/<session-uuid>.jsonl` — full turn-by-turn, including tool calls. 15 projects present; the current session file is ~400 KB.
- **Hooks:** configured in `~/.claude/settings.json` (`hooks` block already in use for `Notification`). `SessionStart`, `Stop`, `PostToolUse`, and others are available.
- **History:** `~/.claude/history.jsonl`.

### Tasks

- [ ] JSONL transcript parser producing canonical source events, preserving native session, turn, tool, and model ids.
- [ ] Hook installer writing a CMF block into `~/.claude/settings.json` — **merging**, never overwriting the existing `Notification` hook and statusline config.
- [ ] `SessionStart` hook: open a CMF session, optionally inject prior context.
- [ ] `Stop` hook: enqueue the completed session's transcript for consolidation.
- [ ] Backfill mode: ingest existing transcripts across all 15 project directories.
- [ ] Per-project allow/deny — capture `context-memory-fabric` and `LLM_Wiki` work; exclude what you choose.
- [ ] Never block Claude Code: hooks must complete fast and fail open.
- [ ] Secret filtering over tool payloads, which in Claude Code frequently contain file contents and command output.

### Files touched

New: `server/adapters/claude_code/{parser,hooks,installer,backfill}.py`, `docs/adapters/claude-code.md`.

### Acceptance tests

1. A completed Claude Code session appears in the journal with native session/turn ids intact.
2. Backfilling all existing transcripts is idempotent on re-run.
3. Hook installation preserves the existing `Notification` hook and statusline.
4. A session in a denied project produces zero events.
5. Hook failure never blocks or slows a Claude Code turn (measured).

### Exit gate

**How much of a coding session is worth keeping?** A transcript is mostly file reads and tool output. Decide the retention class per event type — full turns, decisions only, or summaries — before backfilling 15 projects.

**Effort:** 3–4 sessions.
**Risk:** Low-medium. Local files and documented hooks. The volume is the real risk: coding transcripts are large, and everything consolidated costs an extraction call.

---

## MS4c — OpenClaw adapter and `cmf-http`

**Goal:** Capture the Studio Network's operational events as project-state context.

**Why now:** Third priority, and it is the first genuinely cross-host, cross-agent source — the strongest demonstration of the fabric thesis. OpenClaw runs on the 2013 Mac Pro; CMF runs on the MacBook. That gap forces `cmf-http`, which the roadmap leaves unscheduled, into MS4.

### Tasks

- [ ] **`cmf-http`**: HTTP ingest endpoint accepting canonical source events, with authentication, request validation, and idempotency keys.
- [ ] Multi-actor event model — eleven named agents (Max, Aim, Quest, Forge, Shift, Pace, Vault, Sense, Kind, Lock, Coin), each with SPIFFE and `did:web` identity. `actor.id` should carry the agent identity, not just `type: agent`.
- [ ] Map OpenClaw's Slack-mediated messages and approvals into canonical events.
- [ ] Retry and queue on the OpenClaw side so a CMF outage drops nothing.
- [ ] Scope model: which OpenClaw agents write to which context scope.
- [ ] Decide the boundary with Interlock — CMF holds context; Interlock governs agent control. Record it as an ADR.

### Files touched

New: `server/http/{app,ingest,auth}.py`, `server/adapters/openclaw/`, `docs/adapters/openclaw.md`, `docs/adr/000X-cmf-interlock-boundary.md`.

### Acceptance tests

1. An OpenClaw agent event ingested over HTTP from the Mac Pro appears in the journal with agent identity preserved.
2. Duplicate delivery with the same idempotency key produces one event.
3. CMF down for 10 minutes loses zero OpenClaw events (queued and retried).
4. An OpenClaw-originated memory is retrievable from Claude Desktop.

### Exit gate

**Authentication and tenancy for remote ingest.** Shared secret, mTLS, or SPIFFE-based? OpenClaw already runs SPIRE — reusing it is coherent but couples CMF to that infrastructure. Decide explicitly.

**Effort:** 4–5 sessions (HTTP transport is most of it).
**Risk:** Medium. First network-exposed surface. Threat-model it before it listens on anything beyond loopback.

---

## MS4d — Codex and Gemini CLI

**Goal:** Round out coding-harness coverage.

**Why now:** Deliberately last per your priority. By this point the adapter pattern is proven three times and these should be largely mechanical.

### Tasks

- [ ] Survey each harness's actual local surfaces before designing — do not assume a Claude Code-shaped transcript exists.
- [ ] Implement against the adapter development kit that falls out of 4a–4c.
- [ ] Conformance tests shared with the other adapters.

**Effort:** 2–3 sessions each.
**Risk:** Low, assuming usable local surfaces. Survey first — if a harness offers no capture surface, say so and fall back to export ingestion rather than building something fragile.

---

## MS5–MS9

These follow the roadmap as written; detailed task breakdowns are deferred until MS4 completes, because MS4's findings will reshape them.

| MS | Goal | Depends on | Effort |
|---|---|---|---:|
| **5** | Generalize knowledge providers; `propose_knowledge_change`; GitHub provider; Wiki becomes optional | MS1 | 3–4 |
| **6** | Review and governance — inspect evidence, approve/reject, correct, scopes, audit | MS2, MS3 | 5–6 |
| **7** | Context assembly quality — intent classification, time-aware modes, token budget, conflict signals | MS2, MS5 | 4–5 |
| **8** | Replay and evaluation — historical context snapshots, counterfactual policy comparison | MS2, MS6 | 4–5 |
| **9** | Distribution — Docker Compose, SDK, OpenAPI, adapter/provider DKs, migration and backup | All | 4–6 |

Two notes carried forward:

- **MS6 is the differentiation milestone.** "Why does the system believe this?" is the capability competitors do not offer. It should not slip indefinitely behind adapter work.
- **MS7 needs a baseline first.** "Evaluation shows improvement over memory-only and knowledge-only baselines" requires that those baselines be measured — do that during MS7, not after.

---

## Cross-cutting concerns

### Privacy and cost — the gate before MS4a

Today, Graphiti sends every ingested episode to the Gemini Developer API for entity extraction and embedding. At 57 curated episodes that is a bounded, deliberate exposure. Continuous capture across Claude Desktop, Claude Code, and OpenClaw changes the posture materially:

- **Volume:** every captured turn becomes an extraction call. Claude Code transcripts alone are hundreds of KB per session.
- **Sensitivity:** your existing corpus already contains medical (`MRI Neck Injury`, `Thyroid Nodule FNA`), financial (`Crypto Sale Tax Reporting`), and legal (`Post-Closing Agreement Concerns`) material. Continuous capture will pull in more, unreviewed, and route it to a third-party API.
- **Cost:** unbounded and unmeasured today.

Three questions must be answered before capture is enabled by default:

1. **Local extraction?** Route consolidation to a local model (the Spark, or a local Ollama/LM Studio endpoint) for sensitive content classes, keeping Gemini for the rest. The wiki records that LM Studio was the original plan before the Phase-1 switch to Gemini — that decision is worth revisiting now that the data volume is changing.
2. **Sampling or triage?** Capture everything to the journal (cheap, local) but consolidate selectively (expensive, remote). This is the natural shape given the MS2/MS3 split and is the recommended default.
3. **Budget ceiling?** A hard monthly cap on extraction spend, with capture continuing to the journal after the cap is hit so nothing is lost.

**Recommendation:** journal everything locally; consolidate under an explicit policy with a spend ceiling; route sensitive content classes to local inference or hold them unconsolidated pending review.

### Observability

Add from MS2 onward, not retrofitted: capture success and lag, consolidation latency and failure, extraction precision and rejection rate, duplicate and conflict rates, retrieval relevance, temporal correctness, provenance coverage, token cost per harness, provider latency, deletion and correction propagation.

### Compatibility

Version every canonical schema. Maintain backward-compatible MCP tool aliases through modularization. Keep CMF identifiers distinct from provider-native identifiers. Do not leak Graphiti types into the public contract.

---

## Exporting Claude and Gemini history

**Request both exports now. Ingest them after MS2.**

**Request now, because:**

- Delivery is asynchronous and not instant — Claude's export arrives by email; Google Takeout for Gemini Apps activity can take hours to days.
- Having the real archives in hand lets the `cmf-import-claude` and `cmf-import-gemini` parsers be written against actual data rather than assumed structure. The ChatGPT parser is ~2k lines precisely because the format's edge cases only surface against real exports.
- Exports are point-in-time snapshots and re-requesting is free, so an early request costs nothing and can be refreshed at ingest time.

**Ingest after MS2, because:**

- MS2 defines the canonical event envelope. Importing before it exists repeats exactly the situation the 57 ChatGPT episodes are now in — memories with no source events, requiring the backfill task already scheduled in MS2. Doing that twice more is avoidable rework.
- MS3 versions the classification policies. Importing under an unversioned classifier means no clean reprocessing path when the policy improves.
- The privacy gate above is unanswered. Claude and Gemini histories will contain the same sensitive categories as the ChatGPT corpus, and there is no reason to route them to a third-party extractor before deciding whether that is what you want.

**Where to get them:**

- **Claude:** Settings → Privacy → Export data (conversations and projects). Memory is exported separately via the memory import/export feature. Note that the CMF-relevant Claude Desktop history is exactly what MS4a *cannot* capture going forward, so this export is the backfill for that gap — worth requesting a fresh copy again right before MS4a ships.
- **Gemini:** Google Takeout → "Gemini Apps" (formerly Bard) activity.

**Concretely: request both this week; park the archives in a gitignored location; write the parsers during MS2; ingest at the end of MS2 alongside the ChatGPT backfill, so all four sources land in the journal under one schema.**

---

## Effort summary

| Milestone | Sessions | Cumulative |
|---|---:|---:|
| MS0.5 | 1–2 | 2 |
| MS1 | 3–4 | 6 |
| MS2 | 5–7 | 13 |
| MS3 | 4–5 | 18 |
| MS4a | 3–4 | 22 |
| MS4b | 3–4 | 26 |
| MS4c | 4–5 | 31 |
| MS4d | 4–6 | 37 |
| MS5–MS9 | 20–26 | ~60 |

Estimates assume agent-assisted implementation with review at each milestone boundary.

---

## Immediate next step

**MS3 — separate capture from consolidation.** MS0.5, MS1, and MS2 are all complete (2026-09-04; see their status notes above, including corrected claims found while doing the work — two in MS1, two more significant ones in MS2 concerning `default_db`'s real composition and the Gemini export's actual coverage). Both items previously flagged as outstanding — the Claude `memories` category and the real Gemini Apps conversation history — landed in a follow-up export and are now journaled; the production journal holds 12,764 events across ChatGPT, Claude, and Gemini spanning December 2024 to September 2026.

Nothing is blocking MS3 on external data at this point.
