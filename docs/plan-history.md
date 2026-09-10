# Implementation Plan — Completed milestones

The record of what was built and what was learned building it. Index and decisions log: [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). Milestones still to do: [plan-active.md](plan-active.md).

Each entry keeps its exit-gate answer and the **corrections found while building** — the parts of the record that are load-bearing later.

---

## Context: baseline and roadmap amendments (2026-09-03)

**Phase-1 baseline as tagged `phase-1-baseline`:** 8 MCP tools; Graphiti/FalkorDB episodic memory; local corpus retrieval with PDF/media extraction; unified context assembly; markdown-summary importer; native ChatGPT export parser + classifier; import registry with idempotency; production import runner.

**Two defects found in the 2026-09-03 review, both fixed in MS0.5:** (1) the MCP server resolved `os.getenv("FALKORDB_DATABASE", "default_db")` with `FALKORDB_DATABASE` set nowhere — so every `recall`/`get_context` read the wrong graph; (2) the test suite wrote to the same default graph as production.

**Roadmap amendments applied:** MS0 marked substantially complete with three deliverables moved to MS0.5 (contract fixtures, regression cases, ADRs). MS4 restructured around capture *mechanism* (4a MCP-boundary / 4b Claude Code / 4c OpenClaw / 4d Codex+Gemini CLI) rather than harness name. Claude + Gemini export importers added as an explicit MS2/MS3 workstream. Privacy/cost gate promoted to a blocking decision before MS4a. Reasoning-episode capture ([ADR 0005](adr/0005-reasoning-episode-capture.md)) added as **MS3.5**, run before the MS4 adapters.

---

## MS0.5 — Baseline correctness and wiring

**Goal:** Make the system reflect the work actually done; close the MS0 gaps.

### What was done

- **Graph split resolved.** `FALKORDB_DATABASE` added to `.env` / `.env.example` / `SETUP.md` / `CLIENTS.md`; the `"default_db"` fallback removed from `server/memory.py` in favour of required-with-clear-error.
- **Test writes isolated.** Tests target `cmf_test` via fixture-set env (`tests/conftest.py`); a session-scoped guard refuses to run if the resolved test graph equals production.
- **MCP tool-contract fixtures** recorded to `tests/fixtures/mcp_contracts/` with a drift test — the safety net for the MS1 refactor. *(Found + fixed a doc-drift bug: there are 9 registered tools, not 8 — `import_chatgpt_exports` was missing from README / CLIENTS since `a0f14bf`.)*
- **Regression cases** added for the known failures: validity/expiration-date-as-event-date (`2026-12-01` from "valid through December 2026"), assistant-inference-as-personal-fact, re-import idempotency, retrospective `reference_time` preservation.
- **First three ADRs** written: `0001-four-layer-model`, `0002-provider-boundaries`, `0003-graph-and-state-topology`.
- **Secret hygiene:** `GEMINI_API_KEY` in plaintext in `.env` (gitignored). Todd's call: don't rotate; use the exposure as a live fixture for the MS4a secret-filtering test.
- Baseline tagged `phase-1-baseline` (`2e1ee1b`).

### Exit gate — ANSWERED

**`memory-fabric` is the production graph**, pinned in `.env`. `default_db` cleared (its 86 episodes re-imported through the journal in MS2, where the validity-boundary defect is fixed at the parser); `cmf_chatgpt_000` deleted (a validation-run subset). Recoverability verified via `imports/results/*_committed.json` before deletion; both graphs snapshotted to `imports/state/`.

---

## MS1 — Extract provider interfaces without changing behavior

**Goal:** Establish seams before adding capability. Zero behavior change.

### What was done

- `server/core/` protocols (`typing.Protocol`, structural): `MemoryProvider`, `KnowledgeProvider`, `EventStore`, `Importer`, `ContextAssembler`, `ProposalProvider`.
- Canonical dataclasses in `server/core/models.py`: `SourceEvent`, `DerivedMemory`, `KnowledgeResult`, `AssembledContext`, `DatePrecision`, provenance types — forward-declared, first real writer is MS2's journal.
- Graphiti/FalkorDB access moved behind `GraphitiMemoryProvider` (`server/providers/memory_graphiti.py`); `server/memory.py` is a thin re-export shim. `wiki.py`/`corpus.py` wrapped in place by `FileKnowledgeProvider` (not relocated — 9+ test files import their classes by name).
- Config centralized in `server/core/config.py`; `LLM_WIKI_PATH` made optional (server never actually crashed without it — `WikiCorpusManager` inits lazily; the real fix was tool registration + `get_context` degradation).
- `server/context.py` consumes providers via `get_default_*_provider()` factories, not concrete class names.
- Tools registered conditionally on capability: 9 tools with `LLM_WIKI_PATH` set, 7 without (`search_wiki` / `propose_wiki_update` absent).
- Provider fakes in `tests/fakes/`.

### Corrections to the plan as written

- **"Majority of the suite runs against fakes"** — not met. The ~95 pre-existing tests were not converted; converting well-functioning integration tests carried refactor risk with no benefit for a zero-behavior-change milestone. What was built: a dedicated fake-based module (`tests/test_ms1_provider_interfaces.py`, 8 tests) exercising `get_context()` end-to-end with zero live deps.
- **`grep graphiti\|falkor server/` outside providers is not empty** — categorized: the `memory.py` shim (deliberate), MCP tool `description=` prose (not code coupling), `chatgpt_export_parser.py`'s real import (MS2's job), `CMFConfig`'s `falkordb_*` fields (only backend today). The one substantive instance — `context.py` importing providers by class name — was fixed with the factory functions.

### Exit gate — ANSWERED

**Yes** — `FakeMemoryProvider` / `FakeKnowledgeProvider` satisfy the protocols (`isinstance`-verified against `runtime_checkable`) with zero changes to `protocols.py`; `get_context()` runs end-to-end against both with no live FalkorDB/Gemini/filesystem. No Graphiti semantics leaked into the signatures.

---

## MS2 — Canonical event journal, importers, and backfill

**Goal:** Preserve evidence before deriving memory. The keystone of the differentiation argument.

### What was done

- **Source-event schema** `1.0` (`docs/schemas/source-event-1.0.json` + worked examples). `observed_at` / `event_date` / `date_precision` distinction documented.
- **Event store:** SQLite `imports/journal/journal.db`, append-only, WAL + `synchronous=FULL` (durability over throughput — evidence of record). Indices on harness / conversation / session / actor / type / both time fields.
- **Dedup keyed on `event_id`**, not raw `content_hash` — two events can legitimately share text ("Understood.") in different conversations. `content_hash` feeds `event_id` only when no native stable id exists.
- **Journal CLI:** `export`, `replay`, `inspect`, `stats`. `replay` re-emits evidence deterministically; it does not re-run consolidation.
- **Retention policy** (`server/journal/retention.py`): raw / redacted / excluded per `content_class`, applied at capture time.
- **Importers** (`server/importers/{chatgpt,claude,gemini,backfill}.py`), all emitting source events:
  - **ChatGPT** — 577 conversations, 6,343 `turn.completed` events. `chatgpt_export_parser.py` unmodified; scoped as evidence-emission, not a rewrite of its 2k-line classifier.
  - **Claude** — 122 conversations / 2,267 messages, 7 project snapshots, 21 memory snapshots (2,274 events). Memory snapshots are `actor_type='assistant'` — Claude's synthesis of the user, not the user's words.
  - **Gemini** — Google Takeout "Gemini Apps" activity: 5,691 prompt + 4,692 response events. Google's format is a flat prompt+response list, not per-conversation files. Fixed a `"Prompted "` literal prefix on 90% of prompt records (`_strip_prompted_prefix`); original kept in `metadata.raw_title`.
- **Backfill** of the 57 pre-journal `memory-fabric` episodes and 38 `default_db`-recovered ones, marked `provenance_reconstructed: true`. **Both deleted from the live journal 2026-09-04** at Todd's direction once native ChatGPT coverage existed — the backfill code + tests are unchanged and still correct; only their journal output was removed. `SqliteEventStore.stats()` gained a `by_provenance` breakdown so the captured/reconstructed distinction stays visible.

### Corrections to the MS0.5 record, found while building

- `default_db`'s 86 episodes were **not** "86 markdown-summary + pollution from two known test files". Only **38** were real content (20 markdown-summary, 18 from `reconcile_memories` — a source MS0.5 didn't account for); the other **48** were pollution from a *third* offender (`tests/test_step7_import_memories.py`). No new fix needed — MS0.5's `conftest.py` isolation already covers any test file.
- The 2026-09-04 deletion of the 95 reconstructed events **removed the event_ids that 39 of `labeled_events.json`'s 48 labels pointed to** — including all 35 positive labels — degrading MS3's precision fixture to 9 all-negative rows. Foreseeable in hindsight; recorded, not hidden. Rebuilt in MS3.5's Phase D as `reasoning_labels.json`.

### Production journal

`imports/journal/journal.db` (gitignored) holds **19,012 events** — 6,343 chatgpt, 2,274 claude, 10,395 gemini — spanning December 2022 → September 2026, zero `provenance_reconstructed` remaining. (The figure moved 12,764 → 19,107 → 19,012 across two 2026-09-04 passes as native ChatGPT landed and the reconstructed events were deleted; none of it moves the SQLite-vs-PostgreSQL judgment.) Zero Graphiti writes were made — the journal captures evidence; deriving memory from it was out of scope until the MS4a privacy/cost gate.

### Cross-cutting fix landed here

`server/importer.py`'s `TemporalExtractor` now skips a date immediately preceded by a validity/expiration/renewal marker (`VALIDITY_BOUNDARY_MARKER_PATTERN` — "valid through", "expires", "renews") rather than anchoring the event to it. This is the code fix behind MS0.5's `expectedFailure` regression, which now passes unmarked.

### Exit gate — ANSWERED

**SQLite is sufficient.** 19,012 events across 3 real sources ingest in seconds; `stats`/`query`/`replay` run well under a second. Not the full measured picture the gate asked for (no p95 latency; OpenClaw cross-host unaddressed until MS4c) — revisit if MS4 pushes volume up an order of magnitude or MS4c's cross-host writes make single-writer SQLite awkward.

---

## MS3 — Separate capture from consolidation

**Goal:** Prevent every raw turn from becoming a memory.

### What was done

- **Pipeline** (`server/consolidation/pipeline.py`) as explicit resumable stages: `capture → normalize → redact → classify → extract → reconcile → approve → write`. `capture`/`redact` are no-ops (the event already exists, MS2's retention already ran); `classify`+`extract` are one `policy.evaluate()` call.
- **Consolidation job model** (`consolidation_jobs` table): pending/running/succeeded/failed, attempts, auto-retry of `running` jobs at start. Deliberately no async worker — no continuous producer exists until MS4.
- **`HeuristicPatternPolicyV1`** (`server/policies/heuristic_v1.py`) wraps `server/importer.py`'s `CandidateClassifier` + `TemporalExtractor` — **not** `chatgpt_export_parser.py`'s `StageBasedMemoryExtractor`, which turned out to contain literal hardcoded string matches against specific sentences in Todd's corpus. `CandidateClassifier` is source-agnostic by construction.
- Every `derived_memories` row carries `policy_name` + `policy_version`. Rejected / no-op candidates are persisted with a `reason`, never dropped.
- Reprocessing under a new policy version creates a **new derivation** linked via `supersedes`, not an overwrite.
- **Memory-quality fixture** (`tests/fixtures/memory_quality/labeled_events.json`) — referential only (`event_id` + label, no text), gitignored.

### A real bug the fixture caught

The first `HeuristicPatternPolicyV1` preferred a journal event's own `event_date` over text-mining unconditionally (to handle backfilled events with curated dates). Applied uniformly, **50% of known-negative real turns were wrongly auto-accepted** — every conversational turn has a send-timestamp, and "has a timestamp" ≠ "the text references a notable date". Fixed by gating the override to `metadata.provenance_reconstructed=True` only. Precision after: 100% (29/29) auto-accept, 100% (16/16) non-memory exclusion.

Full-journal run after the fix (19,012 events, zero model calls, seconds): 215 episodic (182 auto-accepted, ~1%), 1,348 durable_candidate, 8,277 ambiguous, 9,172 non_memory (every assistant-authored event). No Graphiti writes.

### Exit gate — ANSWERED

**Threshold = 0.75**, measured not chosen — the confidence formula's own bonus structure makes 0.75 the exact point requiring *both* an episodic-shaped statement *and* an exact/day-precision date. **Amended 2026-09-05 (ADR 0005):** stands for `HeuristicPatternPolicyV1`'s lexical derivations; reasoning episodes (MS3.5) auto-accept on model confidence against a fixture-derived threshold instead — which MS3.5 then found does not exist.

---

## MS3.5 — Reasoning-episode consolidation (ADR 0005)

**Goal:** Capture the *thinking* — exploration, analysis, experiments, hypotheses, dead ends — as `episodic` memories tagged with a `reasoning_kind` property. Full design: [ADR 0005](adr/0005-reasoning-episode-capture.md).

### What was built

- **`reasoning_kind`** — free-text with a documented starter set (`REASONING_KINDS` frozenset), plain `TEXT` column, no enum. The four `ExtractionCategory` values are unchanged. `ExtractionResult` gained `reasoning_kind` / `driving_question` / `rationale` / `alternatives` / `status` / `thread_key`; `PolicyContext` gained `topical_window` / `open_threads`. Additive migration on the on-disk journal.
- **`ReasoningEpisodePolicyV1`** (`server/policies/reasoning_episode_v1.py`, v0.2) via a new **additive** `WindowedExtractionPolicy` protocol + `ReasoningEpisode` dataclass (a windowed policy emits 0–N episodes per call, which `-> ExtractionResult` can't express; the per-event protocol is untouched). Model call injected, defaults to `google-genai`, rate-limited — one call per window. `429` → `GeminiQuotaExhaustedError` (clean stop); bounded retry for transient `503`.
- **Windowing** (`server/consolidation/windowing.py`). Prototyped 3 cheap strategies over a real 296-conversation slice — **none reliably found topic boundaries** (real topic shifts have no pause and no cue phrase). Decision (with Todd): keep it **loose** — `default_windower()` = `TimeGapWindower(2h gap, 20-turn cap)` bounds model-input size only; the model does topical sub-segmentation in-call. `EmbeddingBoundaryWindower` implemented but shelved for a later bake-off.
- **Triage** (`server/consolidation/triage.py`) — standalone `assess_window()` (window-scoped, not folded into the event-scoped v1 classifier). **Loose** (withholds only windows with zero question / deliberation / technical signal), **logged** (`triaged_out` job rows), **optional** (`triage=False`). Later gained a **min-window-size floor** (`min_window_events=3`) for Gemini's single-exchange-heavy corpus.
- **Cross-harness thread index** (`server/consolidation/threads.py`, `reasoning_threads` table) — `thread_key` is a `normalize_key()`-collapsed slug that ignores harness *and* conversation, so a line of reasoning that moves Claude→Gemini (or to a fresh thread with the same assistant) lands in one thread.
- **`_reasoning_approval_state()`** — separate from v1's 0.75 rule; default `threshold=None` = nothing auto-accepts (the live slice confirmed model confidence runs hot/uncalibrated).

### The reprocess (hourly batched loop, 2026-09-05 → 09-06)

Option B slice — **Claude + ChatGPT/Gemini since 2026-03-05**. Ran to completion under `v0.2`: **1,291 windows** (Claude 230, ChatGPT 261, Gemini 800), zero failed/running → **1,243 episodes / 659 threads** staged, all `queued_for_review`. Kinds: investigation 755, decision 198, experiment 138, plan 94, hypothesis 33, finding 16, rejected_alternative 8, retrospective 1. Evidence/ep median 4, single-event 13%.

Fixes landed mid-loop: `429`→`GeminiQuotaExhaustedError`; bounded `503` retry; the min-window floor. `v0.1`→`v0.2` prompt tighten (evidence linking, "is this reasoning" bar) took single-event episodes from 53% → 7%; confidence guidance did not take (model won't score below 0.90). Gemini triaged 40% of windows.

### Phase D — fixture rebuild + exit gate (2026-09-07)

**Fixture** `tests/fixtures/memory_quality/reasoning_labels.{jsonl,json}` — 51 human-labeled staged episodes, **22 keep / 28 drop / 1 maybe**, all 8 kinds and 3 harnesses. Labeled via a phone artifact (swipe keep/drop; single question = "belongs in long-term memory?"). Tooling kept under `imports/tools/` (gitignored).

### Exit gate — ANSWERED

**No auto-accept threshold exists.** Against the fixture, no feature separates keep from drop:

| signal | keep vs. drop | best single-threshold accuracy (baseline "drop all" = 56%) |
|---|---|---|
| model confidence | 0.95 vs. 0.94 — fully overlapping | 58% |
| statement length | 197 vs. 175 chars | 64% |
| evidence-event count | median 2 vs. 1 | 60% |

The only real signal is `reasoning_kind` (`decision` 7/9 kept, `plan` 5/8; `experiment` 1/8, `hypothesis` 1/5, `finding` 2/7) — a routing hint, not a gate. `_reasoning_approval_state(threshold=None)` stays; promotion is fully review-gated. A regression test (`TestPhaseDFixtureConfidenceDoesNotSeparate`) fails loudly if a future version makes confidence meaningful.

**Backlog — three tiers, not two** (Todd, 2026-09-07): "auto-accept off" does not mean 1,243 one-by-one reviews.

| tier | content | destination | ~count |
|---|---|---|---|
| **1 — promote** | durable facts, decisions, plans, findings | the graph | `decision`/`plan`/`retrospective`/`rejected_alternative` ≈ **301**, reviewed **by thread** in MS6 |
| **2 — work journal** | experiments, dead ends, resolved troubleshooting | stays in its `reasoning_thread` (659 exist), not promoted, not lost | ≈ **942** |
| **3 — discard** | thin fragments from windows that straddle topics (segmentation cost), trivia | dropped in MS6 review | subset of tier 2 |

Tier 2 is already built — the thread index. MS6's affordance is **bulk review by thread / kind**, not per-episode.

**Assistant-only-substance rate:** ~5% Claude, ~13–26% ChatGPT/Gemini per batch. The follow-on ("can assistant turns *seed* an episode, marked assistant-originated, never a personal fact, always queued") is a scoped-later mini-milestone, not folded into MS3.5. The `actor_type != 'user'` guard is unchanged.

---

## MS4a — MCP-boundary capture (Claude Desktop)

**Goal:** Capture from Claude Desktop — and every other MCP client — without a per-harness adapter. **Built and unit-tested; live cross-harness verification pending** (see [plan-active.md](plan-active.md#ms4a--mcp-boundary-capture--live-verification)).

### The mechanism and its limits

Claude Desktop stores no local transcript and exposes no hook API, but it speaks MCP, and `mcp` 2.1.1 exposes `MCPServer.middleware` + `Context.client_info`. So CMF journals, per tool call: harness identity + version, session id, tool name, arguments, result summary, timing. **It does not see turns where no CMF tool is called** — Claude Desktop capture is *interaction-triggered*, not continuous. Mitigations: a `capture_note` tool, server instructions encouraging `get_context` at session start, periodic Claude export ingestion. The docs must not imply continuous capture.

### What was built

- **Gemini free-tier rate limiter** (`server/core/rate_limiter.py`, 12 tests) — persisted per-model RPM/RPD ledger (midnight-Pacific reset), automatic fallback through a configurable model chain from Todd's real AI Studio numbers. Raises `GeminiQuotaExhaustedError` *before* any call when the chain lacks headroom. Wired into `get_graphiti_for_operation()`, used by `remember()`/`recall()`.
- **Model-reliability conflict resolved:** a concurrent session found `gemini-3.5-flash-lite` returning 503s and switched to `gemini-3.8-flash`; this session found `gemini-3.8-flash` returning consistent 503s. Root cause: neither session's retry logic treated `503`/`UNAVAILABLE`/"high demand" as retryable — only 429 was — so a transient Google-side capacity error failed outright on whichever model was configured and got misread as a model property. Fixed by classifying 503/`UNAVAILABLE`/"overloaded" as a second retryable category (`_is_transient_gemini_error`). The chain is built on `gemini-3.5-flash-lite → gemini-3.1-flash-lite` (500 RPD vs 20 the deciding factor).
- **Capture middleware** (`server/capture/`) — a source event per tool call, `call_next(ctx)` first, capture scheduled fire-and-forget so it adds zero latency / zero failure risk. Bounded `asyncio.Queue` (500) + single consumer; a full queue drops the newest and counts it.
- **Harness identity** from `client_info` (regex table + safe fallback). **Session identity** — the transport's real `session_id` when it exists, else a cached UUID4 per connection (stdio has none).
- **`capture_note(content, kind)`** and **`capture_health`** MCP tools.
- **Secret filtering** on captured arguments *before* the journal write (`server/capture/filters.py`, reuses `retention`'s scrub + a key-name heuristic).
- Tool/client allow-deny (`CMF_CAPTURE_DENY_TOOLS`/`_CLIENTS`); `import_*` tools denied by default. Content-class filtering **not** built (Todd deferred it).

### Exit gate — ANSWERED (2026-09-04)

**Privacy and cost:** Gemini-only (no local routing yet), no content-class filtering, journal-everything / auto-consolidate-selectively, spend bounded by the hard free-tier RPM/RPD gate. Capture registered unconditionally (no feature flag).

### Remaining

The roadmap's five-step live cross-harness test — needs a live Claude Desktop on a middleware build. Not a blocker for MS6/MS7.

---

## MS3.6 — Promotion: staged memories into the retrievable graph

**Goal:** Move staged `derived_memories` into FalkorDB via `remember()` so `get_context`/`recall` return them. The stage that closes the loop to retrieval.

### The auto-accepted path (built 2026-09-04, for the heuristic classifier)

- `server/consolidation/promotion.py`: `PromotionStore` (idempotency ledger, `promotions` table in the journal DB) + `promote_auto_accepted()` — reads `approval_state='auto_accepted'` rows not yet promoted, calls injected `remember_fn`, isolates per-row failures, `GeminiQuotaExhaustedError` stops the run cleanly. `promote_auto_accepted_memories(dry_run, limit)` MCP tool. 7 tests.

### A real defect the first live run caught (2026-09-04)

Ran `limit=5, dry_run=False`. Of the 4 that landed, **3 were junk** — raw Android logcat lines from a Claude Code debugging session, promoted verbatim.

- **Root cause 1 — `CandidateClassifier` Case C:** classified *any* text with a parseable date and 4+ words as `EPISODIC`, no positive-language requirement. A logcat line always carries an embedded timestamp → cleared the bar → past 0.75 with the exact-date bonus. Fixed: Case C now requires a durable/episodic-language signal; a bare date is not sufficient. Bumped to `v1.1`, reprocessed: `auto_accepted` 182 → 119.
- **Full manual inspection** (all 119, not a sample) found `auto_accepted` still ~90% junk — troubleshooting narration incidentally containing episodic verbs ("fixed", "resolved") plus an embedded log timestamp (Case B). Fixed: a **500-character length guard** downgrades an otherwise-`EPISODIC` candidate to `AMBIGUOUS` (genuine personal statements are concise). Bumped to `v1.2`: `auto_accepted` 119 → 7.
- The final 7: 4 genuinely good, 3 the same short-log-narration pattern under 500 chars — structurally indistinguishable from a real short episodic statement with regex alone. **This residual gap is owned by MS3.5** — a model-based policy over topical windows, not another regex patch. (Todd's follow-on question — can a debugging session become a genuinely useful memory, distinct from a decision and from the raw paste — is answered by ADR 0005: the existing `episodic` category + a `reasoning_kind` property, a real synthesis over a window, not a one-line label.)
- Cleanup: the 4 promoted episodes removed from `memory-fabric` (`graphiti.remove_episode`), `PromotionStore` rows reverted to `failed`.

### The reasoning-episode path (2026-09-07)

MS3.5's exit gate settled that **there is no auto-accept for reasoning episodes**. So MS3.6 for them is entirely review-gated, structured by the three tiers.

- [x] **`promote_reviewed(memory_ids)`** — explicit ordered human-approved id list (any policy). Same `PromotionStore` ledger, per-row failure isolation, `GeminiQuotaExhaustedError` clean-stop, `not_found` reporting. `remember()` gets the `statement` synthesis with `reasoning_kind` + `evidence_event_ids` as `source_description` provenance. `promote_auto_accepted()` kept for the heuristic path. 7 tests (`tests/test_ms3_6_promotion.py`).
- [x] **`default_tier()` / `tier1_review_queue()`** — `decision`/`plan`/`retrospective`/`rejected_alternative` → tier 1; rest → tier 2. Routing hint, not a gate.
- [x] **`ConsolidationStore.mark_superseded_by_reasoning()`** + `revert_…()` — a heuristic `queued_for_review` row whose `source_event_id` is cited by a reasoning episode's evidence → `approval_state='superseded_by_reasoning'`, `superseded_by=<episode memory_id>`. Idempotent, reversible. **Applied to the real journal: 3,251 of 9,833 v1.2 heuristic rows superseded (~33%) → 6,582 remain.**
- [x] **First real promotion batch** — the **22 human-`keep` Phase D episodes** → `memory-fabric` via live rate-limited `remember()`. 21/22 first pass; 1 transient Gemini failure recorded `failed` and cleanly picked up on retry (the 21 `succeeded` skipped — idempotency verified on real infra). **`memory-fabric`: 57 → 79 `Episodic` nodes.**
- [x] **Retrieval verified** — `recall()` returns the promoted episodes with Graphiti's entity extraction done (a promoted decision comes back as entity + relationship facts, not the raw statement). The journal → episode → keep → promote → retrievable loop is closed.
- [x] **Deletion/correction propagation** — architecturally satisfied (journal + `derived_memories` have no code path to FalkorDB; `remove_episode` on a promoted episode cannot touch either). Becomes a *tested* cross-store invariant in MS6.

### Exit gate — ANSWERED (2026-09-07)

A promoted reasoning episode survives `remember()` → `recall()` with the synthesis retrievable and Graphiti's entities/edges attached (`reasoning_kind` / thread key ride in `source_description`, not yet as first-class graph properties — that's MS5/MS6 when the knowledge contract formalizes). Coverage auto-resolve cut the heuristic pile 9,833 → 6,582; every superseded row keeps a `superseded_by` pointer so a reviewer follows it back rather than re-judging. Whether by-thread bulk review beats per-episode is an **MS6** question — `tier1_review_queue()` is in place for it to build on.

---

## Spark local-inference migration — Phases 0-6 (2026-09-08 → 09)

Not an IMPLEMENTATION-PLAN milestone — a parallel track with its own file, [SPARK-MIGRATION-PLAN.md](SPARK-MIGRATION-PLAN.md). Replaces the Google Gemini Developer API as CMF's LLM + embedding + reranking backend with models served from the DGX Spark (`nanospark`) over LM Studio's OpenAI-compatible endpoint. **Phases 0-6 done; Phase 7 (quality A/B) is still open and lives in [plan-active.md](plan-active.md) alongside MS6a.**

### What was built

- **Phase 0 — network path.** SSH tunnel (`12345:127.0.0.1:1234`) over Tailscale, hardened with keepalives + `ExitOnForwardFailure`. `/v1/models` reachable (10 models); embeddings return **768 dims**. Nothing on CMF's side touches the Spark's `0.0.0.0:1234` public binding.
- **Phase 1 — config plumbing.** Split provider switches `CMF_LLM_PROVIDER` / `CMF_EMBED_PROVIDER` (`gemini` | `local`), `local_*` settings, `embedding_dim`. Landed with **zero behaviour change** — both switches stay `gemini`. Fixed an import-order hazard: `EMBEDDING_DIM` freezes into a graphiti_core module constant at import, so `server/__init__.py` now does `load_dotenv(override=False)` before any `server.*` submodule loads. `tests/test_spark_config.py` — 17 tests (`CMFConfig` had none).
- **Phases 2-3 — client swap + LM Studio proxy.** `LMStudioCompatClient` (`server/providers/lmstudio_client.py`): request side rewrites `json_object` → `text` (LM Studio rejects `json_object`); response side promotes `reasoning_content` into an empty `content` **only when it parses as JSON**. `create_graphiti` split into `_build_llm_client` / `_build_embedder` / `_build_cross_encoder`, each branching on its own switch. `assert_embedding_width()` because every layer fails silently on a width mismatch. `PassthroughReranker` default (D3 — CMF never invokes a cross-encoder; `search()` resolves to `EDGE_HYBRID_SEARCH_RRF`). `tests/test_lmstudio_client.py` — 17 offline tests.
- **Phase 4 — rate limiter.** Local path built with `unmetered=True`: `reserve()` returns immediately, **no ledger file touched**. Gemini path gained the missing embedder metering — `reserve_model()` + `MeteredEmbedder`, debiting per input not per batch. `"model unloaded"` added to `TRANSIENT_ERROR_MARKERS`. `inter_call_delay` provider-aware (3.5s Gemini / 0.2s local). `tests/test_spark_rate_limiter.py` — 18 tests.
- **Phase 5 — reasoning-episode policy.** `ReasoningEpisodePolicyV1` bypassed Graphiti (direct `google.genai`), so Phases 2-3 missed it. Added `_local_generate()` routed through `LMStudioCompatClient`; `_select_generate_fn()` picks it from `CMF_LLM_PROVIDER`. **Version bumped 0.2 → 0.3** — a different extraction model is a different policy. `tests/test_spark_reasoning_policy.py` — 22 tests.
- **Phase 6 — fresh graph.** `memory-fabric` renamed to `mem-fabric-gemini` (Redis `RENAME`, verified on a throwaway first); new `mem-fabric-local` built by re-promoting the **MS6a tier-1-approved set** — 295 episodes (reviewer todd, 2026-09-08, 295 approved / 6 rejected of ~301 tier-1-routed; `reviews` table) — on GLM-4.7-Flash + `nomic-embed-text`, ~3.2 h at 38.6s/episode, 0 failures, 0 model evictions. The same 295 were promoted into `mem-fabric-gemini` too, so the two graphs hold the same reviewed corpus at different extractor/embedder. Result: `mem-fabric-local` 295 Episodic / 559 Entity / 567 RELATES_TO / 768-dim; `mem-fabric-gemini` 337/337/187/1024-dim, untouched.

### Decisions

| | Decision |
|---|---|
| **D1** | Extraction model **GLM-4.7-Flash**, via the client proxy. `Mode A` (`json_schema` + response-side shim) after end-to-end testing — Mode B made GLM echo the schema. Qwen3.5-35B-A3B and `qwen3-coder-30b` join the Phase 7 A/B rather than sitting in reserve. |
| **D2** | Fresh graph `mem-fabric-local`, full re-promotion. Gemini-era graph retained untouched for the A/B and rollback. Graph stays on the Mac; only inference runs on the Spark. |
| **D3** | Reranking `PassthroughReranker` — CMF has no reachable cross-encoder path today; `CMF_RERANKER=bge` + `sentence-transformers` is the switch when MS7 turns reranking on. Off the critical path. |
| **D4** | Gemini escape hatch kept. Provider split into `CMF_LLM_PROVIDER` + `CMF_EMBED_PROVIDER` from the start so the hybrid (local embeddings, Gemini extraction) is a config change. |
| **D5** | MS4a cost gate superseded — local inference has no per-call cost; the ledger is a throughput control on Gemini, unmetered on local. |

### Corrections found while building

- **Mode A, not Mode B.** Rev 3 recommended Mode B on hand-written probes; graphiti's actual `extract_nodes` prompt made GLM return the JSON schema itself. Constrained decoding can't fail that way, so `DEFAULT_LOCAL_STRUCTURED_MODE = json_schema` and the proxy is **required**, not a convenience. Cost: Gemma-4-26B-A4B's `<|channel>` control-token leak fires under constrained decoding specifically, so it's out as an extraction model.
- **Embeddings were the real quota wall.** One `add_episode` issues **~3 LLM calls and ~20 embeddings** — nobody was counting embeddings. At a 1,000/day free-tier embedding ceiling that's ~50 episodes/day, not the ~160 the 500 RPD generation ceiling implied. `CMF_EMBED_PROVIDER=local` is the single highest-value part of the migration and worth landing on its own.
- **The rate limiter never metered the embedder.** `gemini-embedding-001` had a budget in `KNOWN_MODEL_BUDGETS` but was absent from `DEFAULT_MODEL_CHAIN`, so `reserve()` never debited it — and `status()` had the same blind spot. Both fixed in Phase 4. Independent of the migration; the Gemini path needed it regardless.
- **`DEFAULT_CALLS_PER_OPERATION` stays 3.** The proposed raise to 6 was withdrawn — measurement said 3 was right for LLM calls all along.
- **`thread_key` regression.** GLM returned `thread_key=None` where Gemini populated it on 1,242/1,243 rows; the field is load-bearing (58 refs across 10 modules). Fixed by making `thread_key` required and non-nullable in `_EPISODES_SCHEMA`.
- **The 0.2 → 0.3 version bump was a trap.** `"0.2"` was a hardcoded default argument in three production call sites and the review CLI had no `--policy-version` flag — bumping alone would have silently emptied the review queue away from the 1,243 rows at 0.2. `REASONING_POLICY_VERSION` is now the single source of truth; `--policy-version` added to `stats` / `queue` / `export`; an empty queue explains itself.
- **The graph was renamed, not replaced**, and the `promotions` ledger was **never cleared** — widening its primary key to `(memory_id, graph_name)` made the planned `DELETE FROM promotions` unnecessary and kept the full Gemini promotion history. `.env` was left on `mem-fabric-gemini` so concurrent sessions kept working throughout.

### Exit gate — ANSWERED (2026-09-09)

**Phase 7 quality A/B** — full write-up in [docs/spark-phase7-ab-log.md](spark-phase7-ab-log.md).

- **GLM-4.7-Flash rejected.** Comparing Gemini vs GLM extraction of the same 275 tier-1 statements (both already in `mem-fabric-gemini` / the Phase 6 `mem-fabric-local` — no new inference): GLM produced 47 pronoun entities, 8 self-referential edges, paraphrase-spam (one node pair carried 12 near-duplicate facts plus leaked prompt fragments — `SOURCE_ENTITY_0`, a verbatim copy of graphiti's `extract_edges` system prompt), and on a 38-episode hand-score an outright hallucination (`Product Manager`). Gemini was at **zero** on every defect class. GLM's 2×/4× entity/edge volume was noise, not richness. Root cause: qwen/GLM-family reasoning-model degeneration on graphiti's instruction-dense prompt under Mode-A `json_schema` decoding, which constrains JSON shape but not string-field content.
- **`unsloth/qwen3.5-122b-a10b` adopted.** Re-ran a 38-episode subset through it. Clean like Gemini (0 pronoun / 0 self-loop / 0 dup / 0 garbage) but initially under-extracted — **0 entities on 45%** of statements vs Gemini's 24%. Adding `EXTRACTION_INSTRUCTIONS` (a `custom_extraction_instructions` nudge to `add_episode`, in [`memory_graphiti.py`](../server/providers/memory_graphiti.py)) cut that to **29%** with no new defects and recall volume ≥ Gemini; 7 of the 11 residual misses are statements Gemini also can't extract. Fully local, ~25 s/episode.
- **Config landed** (`feat(spark): adopt qwen3.5-122b …`): `CMF_LOCAL_LLM_MODEL=unsloth/qwen3.5-122b-a10b`, `EXTRACTION_INSTRUCTIONS` constant. Suite green (363 passed).
- **Graph housekeeping:** the Phase 6 GLM `mem-fabric-local` was renamed `mem-fabric-local-glm` (kept for the A/B record, like `mem-fabric-gemini`); a fresh `mem-fabric-local` is re-promoted from the 295 tier-1 episodes on qwen3.5-122b + nomic. `mem-fabric-gemini` and the journal backup (`journal.db.pre-qwen-20260909`) are the rollback path.
- **Residual risk:** qwen3.5-122b's 29% zero-entity rate (vs Gemini's 24%) and one ~600 s mid-run stall on the probe — watched on the full re-promotion; the hybrid (Gemini extraction + local embeddings, D4's split provider vars) is the fallback if it regresses at scale.

**Executed 2026-09-09 — migration complete.** GLM graph renamed `mem-fabric-local-glm` (+ its 295 ledger rows); fresh `mem-fabric-local` re-promoted from the 295 tier-1 episodes on qwen3.5-122b + nomic — **295/295, 0 failed**, ~2.5 h, no stalls. Final graph: 295 Episodic / 393 Entity / 263 RELATES_TO / 768-dim, **0 pronoun entities / 0 self-loops / 0 echo-facts** (= Gemini), 28% zero-entity (vs Gemini 22%). Episodes renamed `<harness>-<project>-NNN` (`chatgpt-astrophotography-001`) — migration for the 295, plus `_semantic_episode_name` wired into `promote_reviewed` for future runs. `.env` flipped to local/768/`mem-fabric-local`; `recall()` verified live. Probe graphs deleted. Backups: `journal.db.pre-qwen-20260909`, `.env.pre-qwen-flip-20260909`, `episode_rename_map.pre-qwen-20260909.json`. Rollback ledger in [spark-phase7-ab-log.md](spark-phase7-ab-log.md).

---

## MS7 — Context assembly quality

**Goal:** Deliver *useful* context, not a bag of retrieved items — measurably better than either single provider on real queries.

### The instrument (built before code)

- **30 graded queries** (`tests/fixtures/ms7_eval/queries.json`, gitignored) — 3 groups of 10: **A** memory-domain, **B** wiki-domain, **C** spanning, each with a hand-written `gold_needs`.
- **`capture.py`** → `runs.json` — `recall_mem` / `search_wiki` / `get_context` output + latency for all 30, one command, re-run after every change.
- **Round 1 — sufficiency grader** (Artifact `ms7-assembly-grader`): 0/1/2 "does this context contain the answer". **Retired** — that made the grader simulate the answer generator, which is the hard, noisy part.
- **Round 2 — answer-quality eval** (`answer_eval.py`, Artifact `ms7-answer-grader`): for each query, Claude (Sonnet, isolated via `claude -p --restricted --strict-mcp-config` — no MCP, no CLAUDE.md, no memory) generates an actual answer in 4 conditions (`model_only` / `+memory` / `+wiki` / `+both`); the *answer* is graded 0/1/2 against `gold_needs`. This is the live instrument; re-runs are `--resume`-able.

### What was built

- **Step 1 — `search_wiki` tokenizer + stopwords** (`799572d`). The first-occurrence substring scorer kept trailing punctuation on query terms (`interlock?`, `glean,`) and let function words match every doc; big stopword-dense PDFs dominated. `_tokenize` strips leading/trailing non-word chars and drops a ~90-word function-word set. Gold doc in top-5 **12/20 → 18/20**, MRR 0.49 → 0.73, zero regressions.
- **Step 2 — `recall` → `recall_mem`; render fidelity; `get_context` fan-in** (`4581696`, `60d4ef0`, `d4fb058`). Renamed the module fn + MCP wire tool (the `MemoryProvider.recall` protocol method is kept — receiver-scoped). `recall_mem` now resolves each fact's episode and attaches the source-episode synthesized statement + a `reasoning_kind · project · evidence` provenance line — a consumer saw only the terse RELATES_TO edge before. `get_context` over-fetches 12/side, drops wiki hits below 0.4× the top score, dedups facts, then caps — was a blind top-5 concatenation.
- **Snippet + extractor fixes** (`f50f351`, `0b0e52c`). `_extract_snippet` returns up to 3 windows around the densest query-term clusters (the gold fact routinely sat outside a single ±80 window — OpenClaw's agent count, the Colorado-repeal line). `ExtractionResult.__post_init__` strips C0 control chars — a NUL from the 1400 State Pkwy inspection PDF was reaching MCP clients and crashed a subprocess in the eval.
- **Step 3 — episode-content vector retrieval for `recall_mem`** (`64fffdd` + follow-up `7ff999f`; spike record [spark-ms7-episode-vector-spike.md](spark-ms7-episode-vector-spike.md)). `recall_mem` gains a KNN arm over `Episodic.content_embedding` — the synthesized statement itself, reachable past qwen's ~28% zero-entity rate — RRF-fused with the edge search, deduped, capped at 2 facts per source episode, **auto-gated on the vector index existing** (a no-op on a graph without it). Proven on throwaway `mem-fabric-spike-eps`, then landed on `mem-fabric-local` (nomic-embed of all 295 Episodic nodes + a vector index; backup `mem-fabric-local.pre-epvec-20260910`; rollback = `DROP VECTOR INDEX`). Follow-ups: vector arm feeds only its top 6 into the fusion; `get_context` renders `recall_mem`'s full ranked output instead of re-truncating.

### Exit gate — ANSWERED (2026-09-10)

**Does cross-provider context measurably beat the single-provider baselines?** **Yes, decisively.** On the 30 graded queries, answer completeness (0/1/2 vs gold, meaned):

| answering with | score | % of a complete answer |
|---|---|---|
| bare model (no memory, no context) | 0.07 | 3% |
| `recall_mem` only | 1.00 | 50% |
| `search_wiki` only | 1.06 | 53% |
| **`get_context` (both)** | **1.60** | **80%** |

`+both` ≥ every single arm on all 30 and wins outright on several; lift over the bare model **+1.53**. For comparison, the same 30 questions answered from Gemini 3.8 Flash / GPT-5.6 / Claude's *own* built-in memory of the user scored 10% / 18% / 18% — and several of those answers are confidently wrong (a contradicted electrical spec, a stale positioning claim, invented agent names), where `get_context` grounds every returned claim in a stored decision or document.

Progression: bare `get_context` **0.07 → 1.07** after Steps 1+2+fixes → **1.60** after Step 3 + follow-ups.

### Corrections found while building

- **`recall` was the weaker retriever, not `search_wiki`** — the opposite of the going-in assumption. Post-Step-1, `search_wiki` put the gold doc in the top-5 on 18/20; `recall_mem` missed the gold *episode* on 13/20, and on A5/A9 retrieved a **contradictory** episode (the graph edge search reaches RELATES_TO facts, not the episode's synthesized statement, and ~28% of tier-1 statements have zero extracted entities).
- **`get_context`'s memory section was a byte-identical prefix of `recall`** on 30/30 — the "combined re-retrieves worse than recall" diagnosis from the first pass was wrong; it was pure pre-merge truncation.
- **"Does the context contain the answer" is the wrong thing to grade.** It forces the grader to predict downstream answerability. Grading the generated *answer* is both easier and the real target — the round-1 grader was retired for this reason.
- **`recall_mem` / `get_context` / `search_wiki` never call the extraction model.** They use nomic (query embedding) + graph/BM25 + RRF; `CMF_RERANKER` is `PASSTHROUGH`. `recall_mem` median latency ~0.4 s. Only *writes* (`remember`, promotion) touch qwen-122b.

### Deferred to backlog

MS7's exit gate is met; the remaining task-list items are refinements, tracked in **[plan-active.md → Backlog](plan-active.md#backlog--deferred-assembly-refinements)**: `search_wiki` semantic retrieval (the largest remaining retrieval lever — a one-time corpus embed, not per-write cost); explicit conflict/staleness callouts (acceptance test 3) and truncation/omission disclosure (test 4); query-intent routing, time-aware modes, context templates, token-budget allocation, retrieval-explanation debug mode; and the residual eval misses (A1's friend's-Spark episode reachable by neither arm; B7 `+memory` regression; C6's lexical dead-end).
