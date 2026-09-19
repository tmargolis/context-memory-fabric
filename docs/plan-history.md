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

**Goal:** Capture from Claude Desktop — and every other MCP client — without a per-harness adapter. **Built, unit-tested, and live-verified end-to-end (2026-09-16).**

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

### Live cross-harness verification — ANSWERED (2026-09-16)

The roadmap's five-step live test, run for real via [MS6c](#ms6c--mcp-server-cross-agent-verification)'s Gemini Spark phase (Cowork/Code turned out not to be usable for this — see MS6c's identity-resolution finding):

1. ✅ Record a decision — `remember()` from Gemini Spark (`gemini_verification_test_2026_09_16`, content `ALPHA-GEMINI`).
2. ✅ Verify it consolidates with source provenance — confirmed via server log (`add_episode` completed, 51s) and `recall_mem` from Claude Desktop Code mode, `source_description: "Gemini Spark session"`.
3. ✅ Retrieve it from a second harness — `recall_mem` from Code mode, content + provenance intact.
4. ✅ Correct it from that second harness — `edit_memory` from Code mode, `ALPHA-GEMINI` → `BRAVO-GEMINI`.
5. ✅ Verify the corrected state is what's now retrievable — `recall_mem` from Code mode returned the corrected content. (The reverse — does Gemini Spark's own next query see the correction — wasn't checked; not required by the original 5 steps, which only needed a second harness to see current state.)

One retry was needed: the first `remember()` attempt from Gemini Spark returned a plausible "Saved..." confirmation in chat with **no actual tool call reaching the server** (nothing in the journal, nothing in FalkorDB) — the same connectivity flakiness behind the "error 1076"s Todd hit creating new Spark sessions. The second attempt, from a session that was actually connected, is confirmed real via the server log, not just the chat transcript. **Takeaway kept for future reference:** a client-side "success" message from Gemini Spark is not sufficient evidence a write landed — verify server-side (journal or FalkorDB) before trusting it.

**Correction (MS4a2, 2026-09-18): `capture_note` removed.** Its journal row was never read back by any retrieval path — `search_wiki`/`recall_mem`/`get_context` all read Graphiti or the wiki files, never raw journal events — and the offline consolidation pipeline that could theoretically surface it has no scheduler and never ran against live capture. Zero `capture_note` events existed in the journal at removal time; it had never been used in production. Superseded by `capture_session` (see [plan-active.md](plan-active.md#ms4a2--cowork-live-session-episodeentitywiki-capture-current-priority-2026-09-18)), which stages a real, reviewable episode or Wiki proposal instead of an unread journal marker.

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

## MS6a — Review surface

**Goal:** Make the staged backlog reviewable at all, and get real material into the graph.

**What the corpus actually says.** Three measurements taken before writing code changed the design:

1. **The thread is the wrong review unit.** The 315 unpromoted tier-1 episodes fall into 215 threads — a 1.47x reduction, 73% of them singletons. `thread_key` is a free-text slug the extraction model invents per window and matches by exact equality (`consolidation/threads.py`), so `openclaw-gateway-connection` and `openclaw-gateway-setup` are two threads. Corpus-wide that is 1.9 episodes per thread and no queue design improves it. **Grouping by project bucket instead gives 301 -> 20 buckets (15.8x), median bucket 11, one singleton.** Reading is unchanged; what drops by an order of magnitude is *re-orientation*.
2. **There is no dedup shortcut.** Near-duplicate detection over the tier-1 statements finds one pair. These are 314 genuinely distinct claims across 180 topics and 10 months.
3. **The heuristic pile is a quarter the size it looks.** 25,961 `queued_for_review` heuristic rows cover only **9,757 distinct events** — the same turns were re-judged under policy versions 1.0, 1.1 and 1.2 and every pass was left queued; 16,303 rows carry an explicit `supersedes` pointer. The original plan's "~9,800 heuristic candidates" and "6,582" (the v1.2 count) were both correct. Retiring stale versions is bookkeeping, not review.

### Built

- **`server/review/projects.py`** — the project taxonomy (21 ordered first-match rules) plus `backfill()`. `thread_key` and `project` are now real columns on `derived_memories`; `thread_key` had only ever been serialised into the `reason` text.
- **`server/review/store.py`** — `reviews` + append-only `review_audit`. **Every mutation routes through `ReviewStore.record()` / `record_bulk()`** — one chokepoint, not per-action discipline. A bulk action writes one audit row carrying the filter and prior-state histogram, plus per-row verdicts.
- **`server/review/queue.py`** — `review_queue()` returning project buckets ordered by tier-1 density, UI-ready dicts, evidence optionally inlined.
- **`server/review/explain.py`** — the journal half of `explain()`: statement, unpacked rationale, resolved evidence turns, thread. The Graphiti half is MS6b.
- **`server/review/actions.py`** — `approve/reject/defer_episode`, `apply_verdicts`, `bulk_reject`, `bulk_reject_stale_policy_versions`, `bulk_confirm_superseded`, `sample_audit`, `revert_batch`, `promote_approved`.
- **`server/review/cli.py`** — `backfill`, `stats`, `queue`, `export`, `apply`, `explain`, `retire-stale-versions`, `bulk-reject`, `confirm-superseded`, `sample-audit`, `revert-batch`, `promote`. Every mutating command is dry-run by default and needs `--apply`.
- **Review artifact** — keyboard-driven, project-batched, evidence inlined, verdicts persisted to the artifact's own store so state survives across devices. It reports running keep rate and wall-clock, which is what the exit gate measures.
- **`tests/test_ms6_review.py`** — 41 tests.

### Cut from the original plan, deliberately

- **`approve_thread` / `reject_thread`** — 73% of threads hold one tier-1 episode; a thread action is an episode action with extra machinery.
- **`retier`** — tier 2 is "never promoted"; retiering then approving is just approving.
- **Scopes (`personal` / `project`)** — one person, one graph, and no `recall` caller that would be scoped. Acceptance test 6 tested a feature with no user. Deferred to MS9 access control.
- **`correct_memory` + Graphiti re-issue, deletion propagation** — deferred to MS6b. Both serve the 22 promoted rows, and the correction path cannot be designed well before watching a real review pass.

### Acceptance tests — as built

1. `explain()` returns statement, unpacked rationale, resolved evidence turns and thread. ✅
2. Approval promotes exactly the approved set, idempotently; verdicts survive a promotion that stops early on quota (two ledgers: `reviews` and `promotions`). ✅
3. Every mutation writes an audit row with actor, time, reason and prior state — asserted per action type. ✅
4. A bulk action writes **one** audit row, not one per memory, and `revert_batch` restores the prior `approval_state`. ✅
5. A date-scoped bulk action never sweeps a row whose `event_date` is unknown. ✅
6. ~~scoped recall~~ — cut, see above.
7. **Rewritten.** The plan's "decisions-made vs episodes-reviewed" passes at 215-vs-315 while saving nothing. The property that matters is the grouping: few buckets, no singleton piles. Asserted directly, and the real gate is wall-clock, instrumented by the review surface.

### Exit gate — answered by the 2026-09-08 pass (see Update 2026-09-09 below)

- **Wall clock.** Target was under 3 hours of measured review time. Not recoverable from the journal (bulk verdict write) — the review artifact holds it.
- **Tier-1 keep rate.** ~98% on the tier-1-routed slice (295/301). Settles the ranker question: the `reasoning_kind` router already does the triage a ranker would, so a ranker is only worth building for a future source that lacks one.

**Effort:** ~2 sessions. **Status:** surface built; **tier-1 pass completed 2026-09-08.**

### Update 2026-09-09 — the tier-1 pass is done, and it fed the Spark rebuild

The `reviews` table records reviewer `todd`, 2026-09-08: **295 tier-1 episodes approved, 6 rejected**, of the ~301 routed to tier 1 by `reasoning_kind`. Those 295 approved episodes are exactly what was promoted — into `mem-fabric-gemini` (285 succeeded + 24 transient-failed) and then, on the [Spark local-inference migration](SPARK-MIGRATION-PLAN.md)'s Phase 6, into the fresh **`mem-fabric-local`** (295 succeeded) on GLM-4.7-Flash + `nomic-embed`. So `mem-fabric-local` is the reviewed tier-1 corpus re-run through local models, not raw material.

- **Keep-rate exit gate — answered.** ~98% kept **on the tier-1-routed slice** (295/301). The routing did the filtering; within tier 1 almost everything was a keep. The earlier 25–68% guesses were over *all* statements, not the routed subset — so an LLM triage *ranker* for the next corpus is only worth building if a future source lacks a comparable `reasoning_kind` router. Not urgent.
- **Wall-clock exit gate.** Verdicts were bulk-written from the review artifact (all 301 `reviewed_at` within ~0.05s), so measured review time isn't in the journal — read it off the artifact's own instrumentation if the number still matters.
- **Verdicts are graph-independent.** They live in `reviews` / `derived_memories`, not FalkorDB, so re-promoting the same set into whichever graph wins the Phase 7 A/B is cheap (`promote_reviewed`, local models, no quota).
- **Open decision: which graph is production.** `.env` still points at `mem-fabric-gemini`. Making `mem-fabric-local` the live graph needs the deliberate `CMF_LLM_PROVIDER` + `CMF_EMBED_PROVIDER` + `FALKORDB_DATABASE` + `EMBEDDING_DIM` flip (they move together — SPARK plan §"Note for Phase 6"), and Phase 7's A/B is what settles whether to make it.

**Update 2026-09-09 (Phase 7 answered + executed).** The A/B ([docs/spark-phase7-ab-log.md](spark-phase7-ab-log.md)) rejected GLM-4.7-Flash and adopted **`unsloth/qwen3.5-122b-a10b` + an `EXTRACTION_INSTRUCTIONS` nudge**: Gemini-class hygiene, ~5-pt recall gap, fully local. **Migration executed same day:** GLM graph → `mem-fabric-local-glm`; fresh `mem-fabric-local` re-promoted from the 295 tier-1 episodes on qwen3.5-122b + nomic (295/295, 0 failed); episodes renamed `<harness>-<project>-NNN`; `.env` flipped — `mem-fabric-local` is now the live graph. `mem-fabric-gemini` retained for rollback. So the tier-1 corpus is retrievable on fully-local inference; MS7 evaluation now runs against that graph.
- **MS6a and Spark Phase 7 share a sample.** Both want a hand-graded set of promoted episodes compared across the two graphs — run them together.

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

MS7's exit gate is met; the remaining task-list items are refinements, tracked in **[plan-active.md → Backlog](plan-active.md#backlog)**: `search_wiki` semantic retrieval (the largest remaining retrieval lever — a one-time corpus embed, not per-write cost); explicit conflict/staleness callouts (acceptance test 3) and truncation/omission disclosure (test 4); query-intent routing, time-aware modes, context templates, token-budget allocation, retrieval-explanation debug mode; and the residual eval misses (A1's friend's-Spark episode reachable by neither arm; B7 `+memory` regression; C6's lexical dead-end).

---

## MS6b — Governance

**Goal:** Correction, deletion propagation, and `explain()` into Graphiti — the differentiation demo, once the graph held real content. Deferred from MS6a on the grounds that all of it serves the promoted rows, and the correction path should be designed after a real assembly pass rather than before it.

### What was built

- **`explain()` into Graphiti** (`server/review/graph_explain.py`, `explain_graph()`) — resolves `memory_id` → `episode_name` via `PromotionStore`, walks the FalkorDB `Episodic` node, its `MENTIONS` entities and `RELATES_TO` edges. `None` for a never-promoted memory (falls back to the journal-only `explain()`); `found_in_graph: False` when the ledger says promoted but the episode is actually absent, rather than raising. CLI: `explain <memory_id> --graph`.
- **`correct_memory`** (`server/review/correction.py`) — Graphiti has no in-place update, so this is `remove_episode` + `add_episode` under the *original* episode's `valid_at`, fresh episode name, re-run extraction against the corrected text. No-ops when new content matches current graph content. CLI: `correct-memory <memory_id> --content "..." --reason "..."`.
- **`delete_memory`** (same module) — removes the Graphiti episode and the `PromotionStore` row, leaving `derived_memories` and the journal untouched (takes back a graph presence, doesn't un-happen the reviewed event). Tolerates the episode already being absent. Recovery path: re-promote via `promote_reviewed`. CLI: `delete-memory <memory_id> --reason "..."`.
- **Journal-side supersession for corrections** (2026-09-11, at Todd's direction, after the initial build) — the first version left `derived_memories.statement` stale while the graph moved on; reworked to match MS3's supersedes convention. `ConsolidationStore.record_correction()` inserts a new `derived_memories` row (`memory_id = "<old>::corrected-<timestamp>"`, `supersedes=<old>`, everything but the statement copied from the old row) and flips the old row to `approval_state='superseded_by_correction'` + `superseded_by=<new>` (excluded from the review queue and promotion eligibility, same as `superseded_by_reasoning`). `PromotionStore`'s graph mapping moves to the new memory_id; the new memory_id gets its own `reviews` row (`approved`, same reviewer) so a promoted memory_id always has a review verdict. `explain()` surfaces `supersedes`/`superseded_by` so the chain is followable.
- **Test coverage:** `tests/test_ms6b_governance.py`, 15 tests against a hand-rolled `FakeDriver`/`FakeGraphiti` (no real FalkorDB needed for the logic).

### Exit gate — ANSWERED (2026-09-11)

No acceptance tests were defined for MS6b the way MS6a had them; the fakes proved the logic, not the real Cypher against Graphiti's actual schema. Two checks, both passed against a real FalkorDB graph:

1. **Read-only, against production.** `explain --graph` (never writes) against real promoted memory_ids, repeatedly during this session's exploration.
2. **Full round-trip, isolated from production.** `scripts/ms6b_exit_gate.py` — seeds one real episode into its own scratch FalkorDB graph (refuses to run against any real graph name) and a scratch SQLite file, then runs `explain_graph` → `correct_memory` (dry run, then applied) → `delete_memory`, asserting against real graph state at each step. **PASSED, 2026-09-11**, all 6 steps, including the supersedes rework (journal row superseded, review verdict carried forward, audit trail split correctly across old/new memory_ids).

A prerequisite for both: the SSH tunnel to the Spark (needed for any local-inference call, including a fresh `add_episode`) had been run manually (`ssh -N -L 12345:127.0.0.1:1234 spark` in a foreground terminal) and gotten killed. Replaced with a `launchd` agent (`~/Library/LaunchAgents/com.cmf.spark-tunnel.plist`, `RunAtLoad`+`KeepAlive`+`ThrottleInterval`, logs to `~/Library/Logs/cmf-spark-tunnel.{,err.}log`) so the tunnel survives logout/reboot and stops depending on a terminal staying open.

### Exercising the tooling on the real corpus found three more things, all fixed same-session (2026-09-11)

- **The 360-cam/eclipse episode was misattributed.** `explain --graph` on a promoted "decision" episode read as Todd's own gear choice; the full ChatGPT thread showed it was actually a friend's camera purchase, with Todd advising. First hypothesis (triage dropped a correction turn) was wrong — falsified by the job record (`status=succeeded`, and the episode's own window bounds already included the final turn). Actual cause: the extraction model saw the disambiguating turn ("Draft a sorry msg I can text my friend who's interested in purchasing this...") and still wrote "the user decides..." while linking only one evidence turn — an extractor evidence-linking/subject-attribution miss, not a pipeline bug. Corrected via `correct-memory`; re-extraction on the corrected text went from 1 entity/0 edges to 5 entities/4 edges. `scripts/audit_single_evidence_episodes.py` (new) checked the other 29 single-evidence promoted episodes for the same shape (a subject-correction phrase in a turn just after the evidence turn) — zero flagged, so this looks isolated rather than systemic, though the heuristic doesn't prove the other 29 are correct, only that this particular pattern didn't recur.
- **Entity sense-collapse in the graph, and a worse bug behind it.** `scripts/entity_audit.py` (new, read-only) found 31 of 393 graph entities span more than one `project` bucket — most are legitimate (`Mac Pro`, `macOS`, `rsync`, `Photoshop` genuinely recur across projects, the fabric thesis working as intended). Two were genuine sense-collapses — `Anthropic` (employer vs. AI/API vendor, 6 episodes) and `Phase 1` (a CMF dataviz phase vs. career-navigator's "Phase 1F") — cleaned with disambiguating summaries. Attempting that cleanup via `edit_memory` surfaced something more serious: it applies `new_summary`/`new_content`/`new_reference_time`/`new_name` to every node in the *connected* context (every entity co-occurring in a matched episode, every episode mentioning a matched entity), not just what `target_query` actually matched — a dry-run targeting `Anthropic` by exact uuid matched 6 entities for a summary write. No existing test caught it. **Fixed:** direct-match uuids are now captured before the connected-context expansion runs, and all four mutation loops (episode content/name/valid_at, entity summary, edge valid_at — same bug, same fix) are scoped to them; `matched_entities`/`matched_episodes`/`matched_edges` in the result are unaffected since they already only reported the modified set. New regression test seeds two co-occurring entities via raw Cypher (no LLM call) and asserts a bystander stays untouched. The two entity cleanups above were applied via direct scoped Cypher specifically to avoid this bug while it was still open.
- **`explain()` was surfacing the wrong approval field.** Its top-level `approval_state` read `derived_memories.approval_state` — set once at extraction time, never updated by review — so a fully reviewed-and-promoted memory still reported `queued_for_review`. The real verdict lives in `reviews.review_state`. Investigated coverage first (at Todd's request): all 295 promoted episodes have a `reviews` row, no orphans either direction, so the fix was a straight join. `explain()` now returns `extraction_state` (renamed, same pipeline-state value) plus a `review` sub-object (`review_state`/`tier`/`reviewer`/`reviewed_at`/`reason`, `None` if never reviewed), degrading gracefully if a connection's `reviews` table doesn't exist. `graph_name` on `reviews` and inlining the supersedes chain into the join were both considered and deliberately skipped — no multi-graph deployment exists yet, and the chain is already in the journal-only output.
- **A fourth bug, found a day later exercising `correct_memory` for real** (2026-09-11, during the [review-backlog](plan-active.md#review-backlog--corpus-beyond-the-reviewed-295-found-2026-09-11) pass): `actions.promote_approved`'s "everything approved and not yet promoted" query read only `reviews.review_state`, with no idea `derived_memories.approval_state` existed. `reviews` is last-writer-wins per memory_id and `correct_memory` never touches it, so the 360-cam episode's original `approved` verdict (from the 2026-09-08 tier-1 pass) stayed on record after its correction superseded it — and `correct_memory` separately clears the *old* memory_id's `PromotionStore` row, since the graph identity moved to the new one. Those two facts combined made the superseded old memory_id look freshly eligible: the very next `promote --apply` run silently re-created it in the graph with its original, wrong, pre-correction content, alongside the correct one. Fixed by joining `derived_memories` into the eligibility query and excluding `rejected`/`superseded_by_reasoning`/`superseded_by_correction` states (`server/review/actions.py`); regression test `test_superseded_by_correction_is_not_reeligible` reproduces the exact sequence. Cleanup: the wrongly-revived episode removed from `mem-fabric-local`, its stray promotion row deleted.

### What's left — moved to [plan-active.md → Review backlog](plan-active.md#review-backlog--corpus-beyond-the-reviewed-295-found-2026-09-11)

MS6b's tooling is done; what it surfaced about the rest of the corpus (27 never-reviewed v0.1 tier-1 episodes, 981 deliberately-unpromoted tier-2 episodes, the 25,961-row heuristic pile, and a stale `cmf_test` vector index unrelated to any of this) is tracked there, not here — it's ongoing corpus/review work, not a milestone with a fixed exit gate.

---

## MS6c — MCP server cross-agent verification

**Goal:** Verify the CMF MCP server actually works, end-to-end, as an installed connector inside the real client apps Todd uses — Claude Desktop (Cowork mode, Code mode), Gemini Spark, ChatGPT — and correct `docs/CLIENTS.md` against reality. Subsumed [MS4a](#ms4a--mcp-boundary-capture-claude-desktop)'s outstanding live cross-harness test.

### The OAuth finding

Phases 3/4 went in assuming a static bearer token would be enough and said explicitly *don't build OAuth unless it's actually needed*; the milestone's own **Risk** line said the opposite (*"Gemini Spark's DCR/OAuth requirement is an unknown until tested"*). The risk register was right. Neither ChatGPT's nor Gemini's connector UI has a field for a static header — both drive Dynamic Client Registration + authorization-code/PKCE, and Claude Desktop's own connector flow turned out to be OAuth-only too. CMF grew a real single-user authorization server it was explicitly scoped not to build (`server/core/oauth_provider.py`, `oauth_store.py`, `http_auth.py`), merged in [PR #6](https://github.com/tmargolis/context-memory-fabric/pull/6) (2026-09-16). The static-token path survives for scripted/direct access; the two are mutually exclusive at the transport level. This was the milestone's most load-bearing finding — worth recording as "the risk register beat the task list," not a flat "we were wrong."

### What was verified, per client

- **Claude Desktop, both tabs.** Each tab does its own OAuth DCR handshake and gets its own client credential — no shared registration (`oauth_clients` grew from 6 to 8 `Claude`-named registrations across the session). **Code mode:** 17/17 tools discovered (10 base + 7 gated behind `LLM_WIKI_PATH`/`_config.knowledge_enabled`); `get_context`/`search_wiki`/`recall_mem`/`capture_health` all functional; a full `remember`→`recall_mem`→`edit_memory`→`recall_mem`/`get_context` write/correct round trip proven (`ms6c_verification_test_2026_09_16`, ALPHA→BRAVO). **Cowork mode:** the same 4 read tools called live, correct output, Markdown rendered cleanly in the chat UI (the one thing Code mode's raw JSON results couldn't stand in for); one transient Cloudflare 502 on `search_wiki`, succeeded on retry.
- **Gemini Spark.** Connected via 3 `Google` DCR registrations (2026-09-14). Functional pass initially thin (2 read calls, no writes) — closed out via the MS4a cross-harness test below, which added a real `remember` call.
- **ChatGPT.** Connected via 1 `ChatGPT` DCR registration (2026-09-14). Broadest pass of any client — 25 real tool calls across 6 tools including `remember` and `propose_wiki_update` (the call that produced `prop_20260916_125736_a33f295a`, later applied via MS6d).
- **MS4a's cross-harness write/correct — done for real**, moved to [MS4a's own entry](#ms4a--mcp-boundary-capture-claude-desktop) rather than duplicated here.

### Corrections found while building

- **The Cowork-vs-Code "distinct identity" claim was wrong as originally interpreted.** An early check found `claude_desktop` (7 journal events) and `claude_code` (4) as separate harness buckets and concluded they "resolve distinctly, confirmed" — read as proof the two tabs are individually identifiable. Direct counter-evidence from a real Code-mode session (2026-09-16): every tool call it made logged under harness `claude_desktop`, not `claude_code`. Traced to `server/capture/identity.py:36-45` (`resolve_harness`): the harness field comes purely from the connecting MCP client's self-reported `client_info.name` string at handshake (`claude.*desktop`/`claude.*ai` → `claude_desktop`, `claude.*code` → `claude_code`) — never from transport, tab, or OAuth client. The two buckets are real, distinct strings, but it's unproven that the 4 `claude_code` events ever came from Desktop's Code tab specifically, as opposed to some other client announcing a `claude...code`-shaped name. Left as an open question in [Backlog](plan-active.md#backlog), not a blocker — it doesn't affect whether the server works, only whether Phase 1 vs. Phase 2 activity can be told apart after the fact in the journal.
- **`edit_memory` leaves stale fact edges behind on correction.** Confirmed by direct Cypher query against `mem-fabric-local-wiki`: both MS6c test episodes still carry their pre-correction `RELATES_TO` fact edges after being corrected via `edit_memory`. The episode's own content node updates correctly and immediately (`recall_mem`/`get_context` both surface the corrected body) — but Graphiti's originally-derived facts are never re-extracted or superseded, so a query surfacing facts rather than raw episode content can show stale wording. What first looked like Todd's Cowork pass turning up "duplicate" results was this: multiple distinct, differently-worded facts from one original extraction pass, one of them now describing a state the episode no longer states. Filed to [Backlog](plan-active.md#backlog).
- **A chat-side "success" message is not proof a write landed.** Gemini Spark's first `remember()` attempt returned a plausible "Saved... Key: ... Timestamp: ..." confirmation with zero trace in the journal or FalkorDB — see [MS4a](#ms4a--mcp-boundary-capture-claude-desktop) for the retry that worked. Same class of issue as the transient Cowork 502 and the "error 1076"s Todd hit creating Spark sessions — the shared OAuth/tunnel path this milestone put every client onto has occasional real flakiness, filed to [Backlog](plan-active.md#backlog) rather than chased down here.

### Exit gate — ANSWERED (2026-09-16)

Does the MCP server actually work, end-to-end, inside Claude Desktop (both modes), Gemini Spark, and ChatGPT, and does `docs/CLIENTS.md` reflect that reality? **Yes.** Every client family has a live, evidenced functional pass; `docs/CLIENTS.md` reflects the OAuth/DCR reality for all 4 as of 2026-09-16 (Cowork-vs-Code note in §1, dedicated ChatGPT/Gemini Spark subsections in §4). MS4a's cross-harness test — folded in as this milestone's Phase 3 — passed for real. Two real findings filed to Backlog rather than blocking (above). Deliberately left undone as low-value polish, not gaps: `remember()` specifically exercised from Cowork, and a CLIENTS.md §1 wording pass on restart behavior.

---

## MS6d — Durable-knowledge proposal review

**Goal:** Make `propose_wiki_update` a loop that closes. A proposal could be created and then nothing — no way to list, read, approve, reject, or apply one, so every proposal ever made was inert. The unbuilt half of [Milestone 6](ROADMAP.md#milestone-6--build-memory-review-and-governance)'s *"proposing durable-knowledge changes"* deliverable — MS6a/MS6b built the episodic review path, nothing had built the durable-knowledge one.

**Why now:** Found 2026-09-16 when Todd created a real proposal and asked how to review it — 76 proposals sat in `wiki-proposals/`, every one `pending_review` since 2026-09-01.

**MCP-only, by decision (Todd, 2026-09-16).** An early draft put the mutating half behind a CLI on the MS6 precedent; rejected — a wiki proposal is one diff against one file, chat is the better review surface than a terminal for that, and CMF is meant to be consumed via MCP. The safety property that motivated a CLI elsewhere is preserved inside MCP by splitting the decision from the write.

### What was built

- `list_wiki_proposals(status=...)` / `get_wiki_proposal(proposal_id)` — read-only, registering the already-existing-but-unregistered `list_proposals()`/`get_proposal()`.
- `review_wiki_proposal(proposal_id, verdict, notes, reviewer)` — records a decision (`approved`/`rejected`), refuses re-review of an already-decided proposal. Touches nothing canonical.
- `apply_wiki_proposal(proposal_id, expected_sha256, dry_run=True)` — the only tool that writes to `LLM_WIKI_PATH`. Refuses any proposal not already `approved`. **Stale-base guard, both directions:** `expected_sha256` must match the proposal's own hash (proves the caller re-fetched it) and the live target's current hash must match the base it was diffed against. Real `git add`/`commit` in the corpus on apply, best-effort (a missing/broken git repo doesn't block the write). `destructive_hint=True`, verified as the only tool carrying that flag.
- `bulk_reject_wiki_proposals(proposal_ids, reason, reviewer)` — batch reject, one recorded reason each; an already-decided id in the batch is skipped and reported, not silently dropped.
- Status vocabulary: `pending_review` → `approved`/`rejected` → `applied` (approved-only), `applied_at`/`applied_commit_sha` recorded on apply.
- **Post-apply staleness, found and fixed same day.** `apply_wiki_proposal` writes into `LLM_WIKI_PATH` but nothing downstream that assumed the corpus was static got invalidated: `search_wiki`'s filesystem-scan cache, and MS7b's offline-built wiki-derived graph. Confirmed concretely — the real apply of `prop_20260916_125736_a33f295a` didn't surface via `search_wiki` until fixed. `search_wiki`'s side fixed via `invalidate_corpus_cache()` (`server/wiki.py`), wired into `apply_wiki_proposal`; MS7b's graph is parked/offline-built, not a live consumer, so left unaddressed. **Live server restarted to pick this up 2026-09-16** (twice — once deliberately, once to pick up a second uncommitted fix — both confirmed working via subsequent live `search_wiki` calls).
- Registered tool count: 12 → 17.
- Tests: `tests/test_ms6d_proposal_review.py`, 14 cases (approve/reject, re-review refusal, both sha guards' real failure paths, dry-run-writes-nothing, real apply with a real git commit verified in the repo log, create-vs-update, bulk-reject partial success, backward-compat load on all 76 pre-MS6d proposal files). Contract fixtures + 3 tool-count assertions updated. Full suite 397 passed / 6 skipped / 8 deselected.

### Acceptance tests — all met (2026-09-16)

1. **Live end-to-end client round trip**, not a pytest simulation: `list_wiki_proposals` → `get_wiki_proposal` → `review_wiki_proposal` (approved `prop_20260916_125736_a33f295a`, rejected its superseded sibling `prop_20260916_124130_a70e33c0`) → `apply_wiki_proposal` dry-run → real apply → git commit `6dfce163` confirmed in the tool's own response.
2. `apply_wiki_proposal` on a `pending_review` proposal refused.
3. A proposal whose target drifted since creation refused, naming the drift.
4. `prop_20260916_125736_a33f295a` reached `applied` — `WIKI/projects/Context-Memory-Fabric/Context-Layers-as-the-Next-Frontier.md` created, commit `6dfce163`. Its sibling rejected as superseded (applying both was never possible — the create-path refuses once the target exists).
5. **Full 76-proposal backlog triaged.** 1 applied, 75 rejected — 73 were `propose_wiki_update` tool-development smoke tests against a path obsoleted by the 2026-09-03 reorg, 1 was the superseded sibling above, 1 was a real scope-taxonomy draft rejected as premature against the standing MS9 defer-scopes decision. Zero deferred with no reason recorded.

### Exit gate — ANSWERED (2026-09-16)

Can durable knowledge be proposed, reviewed, and promoted into the corpus entirely through MCP, without a shell — and does the approve/apply split actually hold, with no path from a single tool call to a canonical write? **Yes**, on both counts, live-verified against the real 76-proposal backlog, not just tests.

## MS7b — Wiki-derived entity layer + enriched episode bodies (experiment, 2026-09-13)

**Status:** Experiment on branch `ms7b-wiki-entities`. Builds into a new graph (`mem-fabric-local-wiki`); the current graph is preserved untouched as `mem-fabric-local-ep`. Adopt-or-discard is decided at the exit gate, not before.

**Restored to `main`'s plan 2026-09-16.** This section was written on the `ms7b-wiki-entities` branch and went with it when MS6c's work was split onto `main`, so for a few days the milestone existed in code and in no plan anyone reading `main` would see. The branch stays parked — **the adopt-or-discard decision is still open** (Phase 5 found `mem-fabric-local-wiki` does not beat `mem-fabric-local-ep` on retrieval, and Todd has not ruled). `FALKORDB_DATABASE` points at `mem-fabric-local-wiki` as the *interim* default in the meantime, which is safe because it is a strict superset of `-ep`, not because the decision went that way.

**2026-09-13 (Todd):** hold the 415 non-wiki singletons in a candidate area (not delete); do the Phase 1 rename; proceed with the full plan. In progress — see per-phase status below.

**Goal:** Stop episodes defining the graph's vocabulary. Derive entity nodes from the LLM Wiki's heading hierarchy, keep the hierarchy itself as structure, and use wikilinks as edges — then attach episodes onto that backbone. Separately, and first, stop discarding two-thirds of each episode's extracted reasoning at the promotion boundary.

**Why now:** Todd observed that many graph entities are irrelevant. Measured on `mem-fabric-local` (465 episodes / 701 entities / 523 `RELATES_TO`): **585 of 701 entities (83%) are mentioned exactly once**, and sampling them returns `table`, `claim`, `set -e`, `window margin`, `search icon`, `defined paths` — episode-local nouns that contribute nothing to traversal.

### What the measurements changed about the approach

Four findings, in the order they were made. Each one redirected the design, so they are recorded rather than just their conclusions.

1. **Wiki titles/links are not an entity source.** Note titles + `[[wikilink]]` targets (785 distinct terms) match **11 of 701** graph entities — 2%. The wiki's link graph is *topic*-level (`Interlock`, `EV-Charging`, `Observables 2026`), not *thing*-level. Seeding from titles alone would discard `Photoshop`, `macOS`, `GitHub`, `Mac Pro`, `Obsidian`, `Cursor`, `Anthropic`.

2. **Headings are the node source; links are edges** (Todd's correction). `WIKI/` + `REPORTS/` + `TO-RESEARCH/` carry 2,355 headings (H1 195 / H2 1,300 / H3 724 / H4 105 / H5 31) → **1,736 sections after boilerplate filtering** (`Sources` ×121, `Open Questions` ×56, `Summary / TL;DR` ×34) → 1,469 distinct labels. Edges: **2,049 structural parent→child**, plus **1,728 wikilink references** of which 93% resolve to a real note and **99.8% are anchored to a specific section**. That is ~3,700 edges over ~1,700 nodes, against today's 523 over 701.

3. **Full headings make bad node names; decompose them** (Todd's correction). A heading like `Why Gemma 4 12B is especially suitable artistically` cannot name-match an episode, which would force episode attachment onto embedding KNN and abandon Graphiti's native resolution. Decomposed (`Gemma 4 12B` (model) + `Art` (domain)) it matches directly. Verified on real headings: `Apple Vision Pro Status (May 2026)` → `Apple Vision Pro`; `3A. Install Docker Desktop` → `Docker Desktop`; `Protocol Layer: MCP + A2A` → `MCP`, `A2A`; `What to raise with Charles` → `Charles`. Numbering, dates and framing words strip cleanly.

   **Decompose from heading + section lede, not the heading alone.** Coverage of the 701 existing entities:

   | Source text | all | ≥2 mentions | ≥3 mentions |
   |---|---|---|---|
   | heading only | 16% | 36% | 57% |
   | **heading + first 25 words** | **28%** | **58%** | **80%** |
   | full section body | 34% | 62% | 86% |

   Section bodies are median 74 words (81% between 21–400), so they are also a natural chunking of the corpus — which incidentally serves the Backlog's `search_wiki` semantic-retrieval item.

4. **Episode capture is lossy at the promotion boundary, not at extraction.** `ReasoningEpisodePolicyV1` extracts `statement`, `driving_question`, `rationale`, `alternatives`, `status`, `thread_key`, and all of it is persisted (the latter fields packed into `derived_memories.reason`). But both promotion paths call `content=row["statement"]` ([promotion.py:413](../server/consolidation/promotion.py#L413), [promotion.py:583](../server/consolidation/promotion.py#L583)). Across all 465 promoted episodes: `statement` averages **188 chars**, the never-sent `reason` averages **406** — 98% carry a driving question, 98% a rationale. **~68% of extracted reasoning never reaches the graph.**

   Concretely: an episode whose `statement` ends "...an artifact of the SMB/NAS filesystem" drops a `reason` naming **QNAP** — and `QNAP TS-264` is in the `Storage-NAS` wiki note. The cross-channel link this whole milestone depends on was being severed by one field selection.

### Decisions taken (Todd, 2026-09-13)

- **Enriched episode body = `statement` + driving question + rationale. Alternatives excluded** — "options considered and rejected" reads as fact once it is in a graph.
- **Extraction model = `openai/gpt-oss-20b`** for now. See the A/B below.
- **No episode-mention threshold for wiki-supported entities.** Todd's objection: a mention threshold applied to terse summaries measures extraction failure, not relevance. Confirmed — 116/465 episodes (25%) extracted **zero** entities and 43% extracted ≤1. Crossing wiki support against recurrence:

  | | singleton (1 ep) | recurring (≥2) |
  |---|---|---|
  | in wiki | **170** | 73 |
  | not in wiki | 415 | 43 |

  A symmetric "wiki AND ≥2" rule keeps 73 of 701. The asymmetric "wiki OR ≥2" keeps 286 — the 213-entity difference is almost entirely wiki-backed singletons (`Kotlin`, `Illinois Electric Vehicle Charging Act`, `pyproject.toml`, `UCSD`, `IRS`, `Google Workspace`, `NVMe drive`, `Image Stacking`), singleton only because the episode channel dropped them.

### Model A/B (30 episodes, enriched template, 2026-09-13)

| | `openai/gpt-oss-20b` | `qwen3.5-122b-a10b` |
|---|---|---|
| Parsed | 30/30, 0 failures | ~20/30 attempted, **6 truncated** at 3,000 tok |
| Entities/episode | **4.97** | no data |
| **Zero-entity episodes** | **0 (0%)** | no data |
| In wiki prose | 50/149 (34%) | no data |
| Speed | **4.2s/ep → ~33 min for 465** | 131s/ep → **~17 h for 465** |

The 25% zero-entity rate disappears on the 20b. The 122b run was **stopped before completion at Todd's instruction**, so there is **no quality comparison between the two models** — only the throughput and truncation profile, which was already decisive (it reasons in proportion to input length, so enriched bodies make it worse). Revisit with a 6,000-token cap on ~10 episodes if extraction quality is ever suspected.

**Known limitation of the 20b result:** the "0% >4-word fragments" metric overstates quality. The junk changed shape rather than disappearing — it now emits generic single nouns (`wall`, `rumors`, `planet`, `tabs`, `vendor`, `Activities`, `staff group`) alongside good entities (`NVIDIA Spark`, `OCLP installer`, `Ars Electronica`, `Slack`). That is exactly the shape the wiki registry and stoplist are meant to catch, so it is a known input to Phase 4, not an unmeasured risk.

### Architecture

Three layers in `mem-fabric-local-wiki`:

- **`:Section`** — 1,736 nodes from the heading hierarchy, joined by 2,049 `CONTAINS` edges. Carries `wiki_path`, heading level, and the lede.
- **`:Entity`** — decomposed from heading + lede. This is the surface episodes attach to, by name, via Graphiti's native resolution.
- **`:Episodic`** — the 465 promoted episodes, replayed from the journal with enriched bodies. Unchanged 1:1 with promoted reasoning episodes, so `recall_mem`'s vector arm, `_resolve_episode_index`, `tag_projects.py`, `entity_audit.py` and ledger-replay DR all keep working.

Edges: `Section-[:CONTAINS]->Section`, `Section-[:MENTIONS]->Entity`, `Section-[:REFERENCES]->Note`, and the existing `Episodic-[:MENTIONS]->Entity` / `RELATES_TO`. Retrieval path becomes `episode → entity → section → wiki note`.

**Wiki notes are NOT ingested as episodes.** Entities are seeded directly (`EntityNode.save()` / `add_triplet`), so the episodic layer stays pure.

**On provenance:** the wiki is itself AI-generated (Todd, 2026-09-13), so this is LLM output extracting from LLM prose. The quality argument is not provenance but **redundancy** — an entity earns retention by appearing in a curated section *or* recurring across episodes, two independently-generated channels. Neither is trusted alone.

### Phases

**Phase 0 — enriched episode bodies — done**
- [x] Enriched content template — `enriched_episode_content()` in [promotion.py](../server/consolidation/promotion.py), wired into both call sites (`promote_auto_accepted`, `promote_reviewed`). Statement + driving question + rationale; alternatives excluded, parsed from `derived_memories.reason`'s `Q:`/`why:` fields; falls back to the bare statement on any unfamiliar `reason` shape (e.g. `promote_auto_accepted`'s heuristic-policy rows) rather than raising.
- [x] Reasoning policy not re-run — confirmed, this only changes what `promote_reviewed`/`rebuild_graph_from_ledger.py` send to `remember()`.
- [x] Model set to `openai/gpt-oss-20b` for the Phase 3 replay — via `CMF_LOCAL_LLM_MODEL` at invocation time, not a global `.env` change (the global default stays `qwen3.5-122b` for live production capture; see Phase 1 note below on why that distinction turned out to matter).
- [x] Unit tests — [tests/test_ms7b_enriched_content.py](../tests/test_ms7b_enriched_content.py), 9 cases (all fields, alternatives-never-included, missing/empty/malformed `reason`, question-only, and an end-to-end `promote_reviewed`/`promote_auto_accepted` check). All pass; no regressions in the existing 23 promotion tests.

**Phase 1 — graph rename — done**
- [x] `GRAPH.COPY mem-fabric-local mem-fabric-local-ep`; verified 465/701/31/523 match exactly; confirmed `BGSAVE` landed on the mounted `/data` volume before deleting the source. `mem-fabric-local` deleted.
- [x] `mem-fabric-local-restore-20260912` (0 nodes) left as-is — not explicitly authorized to remove, and harmless.
- **Found during this step, not anticipated in the plan:** live `server.mcp` processes (Claude Desktop's Cowork/Code connections) were still running against `.env`'s old `FALKORDB_DATABASE=mem-fabric-local`. Deleting that graph name meant their next call would have silently recreated an empty shell there (same "index-only ghost" mechanism ADR 0003 documented for `default_db`, just triggered by this delete instead of Graphiti's internal default) — and any real `remember()` from a live session before the fix would have started writing into that empty graph instead of the real one. Fixed by updating `.env`'s `FALKORDB_DATABASE` to `mem-fabric-local-ep` immediately. **Those already-running processes won't see this until restarted** — same restart requirement ADR 0003 already documents for this class of change.

**Phase 2 — section + entity registry builder — done**
- [x] [scripts/build_wiki_sections.py](../scripts/build_wiki_sections.py) — deterministic, zero LLM calls. Real run over `WIKI/`+`REPORTS/`+`TO-RESEARCH/`: **2,355 sections** (matches the earlier measurement exactly), **1,939 non-boilerplate**, **434 notes** (201 scanned + 233 stub targets outside scope), **1,725 wikilinks, 93% resolved**. Refinement over the exploratory measurement this plan was based on: boilerplate sections (`Sources`, `Open Questions`, ...) are kept as real `:Section` structure nodes and keep their own wikilinks (a "Sources" section is often a bibliography) — only flagged `is_boilerplate` so Phase 2b skips decomposing them. The earlier 1,736/1,728 figures had come from two differently-filtered passes over the same data; this is one consistent pass.
- [x] [scripts/build_wiki_entities.py](../scripts/build_wiki_entities.py) — batched (25 headings/call) heading+lede decomposition via `gpt-oss-20b`, text-mode + schema-in-prompt (same LM Studio reasoning-model quirk documented in `lmstudio_client.py`). Batching measured live at **~0.6–0.7s/section** (vs. ~4.2s/episode unbatched in the earlier A/B). One real bug caught by a full run and fixed: the model occasionally returns a bare string instead of `{"name","type"}`; now tolerated rather than crashing.
- **The Spark SSH tunnel dropped mid-run** (`ConnectionResetError`, ~525/1,939 sections in) — the old code only wrote output once at the end, so this lost all prior work. Fixed before doing anything else: `build()` now checkpoints to `--out` after every batch and `--resume` continues from it; `_call_model` retries a transient network error with backoff first. This was the first of three tunnel drops in this session (see below) — no longer a one-off risk to design around.
- **`--provider gemini` fallback added** (Todd, 2026-09-13, while the Spark was down): this step is pure text generation, no embeddings, so it isn't provider-locked the way seeding/replay is — reusable regardless of which model embeds the results later. Builds its own `GeminiRateLimiter` (real chain + budgets) rather than `get_default_rate_limiter()`, which is provider-aware and returns an unmetered stand-in whenever `CMF_LLM_PROVIDER=local` — correct for production, useless here. Off by default; spends Todd's real quota only when passed explicitly. **Used for real once** to unblock this step: full 1,939-section corpus in ~6 minutes, **1,345 distinct entities**, 5 stoplist hits, 131 of 500 daily calls spent (split across the two-chain models) — comfortably inside budget. Output: `imports/state/wiki_entities.json`.

**Phase 3 — seed + replay — done**
- [x] [scripts/seed_wiki_graph.py](../scripts/seed_wiki_graph.py) — full seed into `mem-fabric-local-wiki`: 434 Note / 2,355 Section / 1,345 Entity nodes, verified directly in FalkorDB.
- [x] **Root-cause diagnosis, not guesswork.** The first calibration attempt (10 episodes, `gpt-oss-20b`) hit a tunnel drop mid-run — genuinely inconclusive at the time. Rather than re-running blind, used `graphiti_core.utils.maintenance.node_operations.extract_nodes()` directly (read-only, no graph writes) to inspect the model's raw output for all 7 zero-entity episodes: it was cleanly, quickly returning `{"extracted_entities": []}` — not truncating, not erroring. Found the likely cause inside graphiti's own baked-in prompt: *"When in doubt, do not extract the entity"* — directly conflicting with this codebase's `EXTRACTION_INSTRUCTIONS` nudge, which was tuned against `qwen3.5-122b`/Gemini and never validated against `gpt-oss-20b`.
- [x] **Memory diagnosis, also not guesswork.** Three real tunnel drops traced to `sshd` never logging a close (client-side keepalive giving up, not a server crash — confirmed via `journalctl`/`dmesg`/`uptime` on the Spark itself, 25 days uptime, no reboot) plus a 15-min load average spike with three models already pinned resident (~95.6GB, swap 14/15 GB full per Alex's own status). Root cause: loading `gpt-oss-20b` cold, on top of that, had nowhere to go. Fixed by unloading `unsloth/qwen3.5-122b-a10b` (freeing 73.5GB) and warming `gpt-oss-20b` deliberately before any real run.
- [x] **Model comparison, measured in-graph, not assumed.** A calibration re-run under `gpt-oss-20b` (10 episodes) vs `qwen3.5-122b` (first 24 of the full run) on the *same real pipeline*: gpt-oss-20b — 0.3 entities/episode, 70% zero-entity, **0** `RELATES_TO` fact edges. qwen3.5-122b — 2.7 entities/episode, ~21% zero-entity, 43+ real fact edges. Not subtle; switched to 122b for the full replay despite the ~15-30x speed cost, since a fast replay with zero fact edges would have defeated the point of the migration.
- [x] **Full 465-episode replay**, `qwen3.5-122b`, batched (25/batch, timestamped progress — added to `rebuild_graph_from_ledger.py`): **455 newly promoted, 10 already-promoted (skipped, no dup), 0 failed.** 16,618s (4.6h), 35.7s/episode. Final (pre-Phase-4-merge): 2,180 entities, 809 `RELATES_TO` edges, 25.8% zero-entity rate overall (close to the pre-migration ~25% baseline for this model — the enrichment's real benefit shows up in *richness per successful episode*, not in cutting the zero-entity rate for this particular model).

**Phase 4 — sweep + re-tag — done**
- [x] **Duplicate-entity root cause found and fixed.** Browser inspection surfaced 94+ duplicate-name entity groups. Traced to `seed_wiki_graph.py` seeding entities with `group_id=""` while `add_episode()` defaults to `group_id="_"` — Graphiti's dedup search is scoped *by* `group_id`, so a wiki-seeded node was never a merge candidate regardless of name match (confirmed directly: two byte-identical `"Anthropic"` nodes, `group_id` `""` vs `"_"`). Fixed in `seed_wiki_graph.py` for future reseeds; the existing graph needed a repair, not a redo.
- [x] [scripts/sweep_wiki_graph.py](../scripts/sweep_wiki_graph.py) extended with a merge pass ahead of retention tagging (same `_norm()` used for IDF vouching, so it catches the separate non-breaking-space case too — `NVIDIA Spark` vs `NVIDIA␠Spark`). Run for real: **123 duplicate groups, 126 redundant entities merged, 788 edges redirected, zero data loss** (verified node/edge counts before/after; "Anthropic" now resolves to exactly 1 node).
- [x] Retention sweep on what's left (2,054 entities post-merge): **97 confirmed by recurrence (≥2 episodes), 624 confirmed by IDF-vouching, 16 held as candidates** — never deleted. Far below the 415-non-wiki-singleton number the plan was originally written against; the richer `qwen3.5-122b` extraction plus the merge fix meant most entities now clear one bar or the other on their own.
- [x] `tag_projects.py` re-run: 120 entity-less episodes linked directly to 23 project hubs — matches the measured 120 zero-entity-episode count exactly (independent cross-check).
- [x] `entity_audit.py` re-run: 68 entities span ≥2 projects. Read through the actual list rather than just counting it — every one at the top (Mac Pro across 4 projects, Photoshop across 4, GitHub across 4) reads as legitimate shared infrastructure per the script's own docstring standard, not sense-collapse. No entity found meaning two different things across its project list.

**Phase 5 — A/B against the old graph — done**
- [x] Ran the real MS7 instrument: `capture.py` against both graphs (30 queries × `recall_mem`/`search_wiki`/`get_context`), `answer_eval.py` generating real answers in 4 conditions via `claude -p` (240 answers total), graded by hand against `gold_needs` the same way the original MS7 verdicts were — [tests/fixtures/ms7_eval/verdicts_ms7b_phase5.json](../tests/fixtures/ms7_eval/verdicts_ms7b_phase5.json) has the full per-query notes.
- [x] **Result: `mem-fabric-local-wiki` did not beat `mem-fabric-local-ep` on this instrument.**

  | arm | `-ep` | `-wiki` |
  |---|---|---|
  | memory (recall_mem alone) | **0.70** | 0.43 |
  | wiki (search_wiki alone) | 0.73 | 0.73 *(graph-independent by construction — reads LLM_Wiki files directly, never touches FalkorDB; identical score is the expected sanity check, not a coincidence)* |
  | both (get_context fusion) | **1.40** | 1.33 |

  The memory arm is the real story: `-wiki` is notably *weaker* despite objectively richer graph structure (2.7 entities/episode vs `-ep`'s pre-enrichment baseline, 809 vs 523 `RELATES_TO` edges). More graph structure did not translate into better ranked retrieval — `recall_mem`'s RRF fusion (`_rrf_merge`, the per-episode cap, the vector-arm top-6 cutoff) was tuned against `-ep`'s shape, and a denser, differently-resolved entity graph changes which facts get surfaced without those tuning constants having been revisited. The `both` arm is close but `-wiki` still trails, driven by concrete misses: **C9** (build-sequencing + differentiation) — `-ep`'s fusion produces a full match by combining two facts neither single arm surfaced alone (exactly the behavior the C-group exists to test); `-wiki`'s fusion doesn't replicate it, scoring 0. **C1** (Interlock registry) and **A10** (filing status) show the same pattern. Genuine `-wiki` wins exist too — **C2** (partially recovers the friend's-Spark fact `-ep` misses entirely) and **C3** (surfaces specific camera-gear detail `-ep`'s fusion leaves generic) — so this isn't one-sided, but the aggregate doesn't clear the bar.
- **Grading caveat, stated plainly:** single-pass, by me, not independently cross-checked the way the original MS7 draft was reviewed by Todd before being treated as final. A few borderline calls (partial-credit judgment on incomplete-but-not-wrong answers) could each move the mean by ~0.03; the gap between the two `both` means (0.07) is within range of that noise. The **memory-arm gap (0.27) is larger and reads as a real effect**, not grading noise.

### Acceptance tests

1. ✅ Enriched bodies measurably raise entity yield on the real corpus: 2.55 entities/episode across all 465 (2.7 for the `qwen3.5-122b` cohort specifically) — the A/B's 4.97 prediction was on 30 episodes via a simpler prompt than graphiti's real extraction path; the real-pipeline number is lower but the direction holds. Zero-entity rate (25.8%) did **not** improve over the pre-migration baseline for this model — recorded honestly in Phase 3, not glossed over.
2. ✅ `mem-fabric-local-ep` untouched since Phase 1 — never re-opened by any Phase 2–5 script (all of which target `mem-fabric-local-wiki` explicitly).
3. ✅ Phase 2 builders re-run clean; `build_wiki_sections.py` is zero-LLM and deterministic, `build_wiki_entities.py` checkpoints/resumes.
4. ✅ Duplicate-entity rate measured directly (8.6%, 188 entities) and fixed via the Phase 4 merge — not just measured, corrected.
5. ❌ **MS7 eval on `-wiki` did not reach `-ep`** — the one acceptance test that didn't clear, and the one the exit gate below turns on.

### Exit gate

*"Does a wiki-structured entity layer plus enriched episode bodies retrieve better than the episode-derived graph on the same graded queries — and is the entity set one Todd recognises as relevant? If the answer is only 'enriched bodies helped,' that is a real result: ship Phase 0 to the existing graph and discard the rest."*

**That is where this landed.** The entity set is real and recognizable (Phase 4's audit confirmed no sense-collapse), but retrieval quality on the graded instrument did not improve — if anything, the memory arm alone measurably regressed. Per the exit gate's own pre-committed criterion, the honest recommendation is: **adopt Phase 0 (enriched episode bodies) on `mem-fabric-local-ep` directly** — that part is model-agnostic, already validated end-to-end in Phase 3's real replay, and costs nothing to keep — **and treat the wiki-structured entity/section layer as a documented, working, but not-yet-adopted experiment.** Whether to pursue tuning `recall_mem`'s fusion constants against the new graph shape (a real, separate follow-on, not a quick fix) or to set `mem-fabric-local-wiki` aside as-is is Todd's call, not a default this doc should assume.

**Effort:** 2–3 sessions estimated; actual was closer to 4, almost entirely in Phase 3's diagnosis work (two real infrastructure failures — a flaky Spark tunnel, a memory-pressure model-eviction issue — and one real architecture bug — the `group_id` mismatch) rather than in the phases themselves.
**Risk:** Realized, not just estimated. The dedup-search-timeout risk flagged going in never manifested (0 failures across the full 465-episode replay); the risks that did bite weren't on the original list, which is itself a useful note for scoping the next experiment like this one.

### Closeout — adopted, not by re-running extraction (2026-09-17)

**Decision:** Todd chose the episode-derived lineage (`-ep`) over the wiki-structured entity/section layer (`-wiki`), per the exit gate's own recommendation above. But the mechanism differed from the plan as written — re-running a full enriched-content extraction against `-ep` (the ~4.6h path Phase 3 already paid for once) turned out to be unnecessary.

**Found first: `-ep` was not actually frozen since Phase 1.** Direct comparison of every episode by name across both graphs (2026-09-17): `-ep` (467 episodes) and `-wiki` (469) share 467 names, and **460 of those 467 have different content** — `-wiki`'s version is always the later-written, enriched one (statement + driving question + rationale); `-ep`'s is the bare original statement. Zero episodes are unique to `-ep`. In other words, `-wiki`'s Phase 3 replay already *was* the enriched re-derivation of `-ep`'s full ledger, done once; re-running it against `-ep` directly would have reproduced the same result at 4.6h of cost for zero new information.

**So the close-out became clone-then-prune, not re-extract:**
1. `GRAPH.COPY mem-fabric-local-wiki mem-fabric-local` — reusing the graph's original pre-Phase-1-rename name deliberately: the SQLite `promotions` ledger already has 465 `succeeded` rows keyed to exactly that name (from before the rename), so this made a separate ledger-backfill step unnecessary — future `promote_reviewed` runs against `mem-fabric-local` correctly see those 465 as already done, with no extra bookkeeping.
2. Verified the copy matched `-wiki` exactly (469 Episodic / 2,075 Entity / 2,355 Section / 434 Note / 31 Project / 843 `RELATES_TO`) before changing anything.
3. **Collapsed the `:Section` layer's provenance up to `:Note` before deleting it**, rather than discarding it: `MERGE (Note)-[:MENTIONS]->(Entity)` and `MERGE (Note)-[:REFERENCES]->(Note)` from the existing Section-level edges (2,023 and 1,144 distinct pairs respectively, deduped; 9 self-referencing note pairs dropped as meaningless). `CONTAINS` (the heading hierarchy) has no note-level analog and was allowed to just go — it was internal structure to a note, not cross-note information.
4. `DETACH DELETE` all 2,355 `:Section` nodes. Zero entities were orphaned by this (checked first: every entity otherwise reachable only via a deep section was also reachable via an episode or a shallower section).
5. **`:Project`/`IN_PROJECT` and the ~1,219 wiki-only entities (mentioned by a `:Note` but by no episode, no `RELATES_TO` fact, no project) were deliberately left in place**, not pruned — see the two Backlog items below. This reflects a live finding, not the original plan: visualizing the result in FalkorDB Browser surfaced a concrete extraction-quality problem (a `Cityscapes` art-project note whose 13 section headings are all camera-technique jargon — `TS-E`, `Scheimpflug`, `ACR`, etc. — with no heading containing the word "Cityscape," so the note never links to the `Cityscapes`/`cityscape` topic entities that exist from other notes; separately, `Tilt` and `Shift` were extracted from a section specifically titled "Zero/Static Configuration" — the one about using *neither* — while the real `Tilt Configuration` section produced `Scheimpflug` instead). Pruning the un-corroborated entities now would have permanently destroyed evidence needed to fix that class of bug later.

**Result:** `mem-fabric-local` (3,009 nodes / 6,319 edges) is now the canonical graph — `.env`/`.env.example`'s `FALKORDB_DATABASE` updated to match. `mem-fabric-local-ep` and `mem-fabric-local-wiki` are both kept, untouched, as historical/rollback snapshots — not deleted.

**Phase 0 (enriched episode bodies) shipped to `main` separately** — [PR #7](https://github.com/tmargolis/context-memory-fabric/pull/7) cherry-picks just `enriched_episode_content()` out of the `ms7b-wiki-entities` branch, isolated from the wiki-structured layer that stays undeployed. The `ms7b-wiki-entities` branch itself is kept for now as the historical record of the experiment, not merged wholesale and not deleted.

**Outstanding, filed to Backlog rather than reopening this milestone:** whether to prune the `Project`/`IN_PROJECT` layer (an open question since before this experiment started — see [Backlog](plan-active.md#backlog)) and the wiki-entity-extraction quality gap found while inspecting the pruned result.

---

## MS4a2 — Cowork live-session episode/wiki capture (2026-09-18)

**Goal:** Auto-generate real episodes (and durable wiki content) from live Claude Desktop/Cowork conversations, without a manual export/import round trip.

**Why now:** Todd's actual near-term priority, ahead of MS4b. Cowork has no local transcript and no hook API (`docs/ROADMAP.md`'s Milestone 4 section) — confirmed architectural limit, not a gap to design around — so the only levers are server instructions and new tools the live model can call using what it already has in context.

**Terminology note (worth stating once, since the names collide):** `capture_note` (an MCP tool that existed until 2026-09-18, see below) wrote a bare marker to the *journal* only, not an episodic memory. A `:Note` *graph node* (wiki-derived layer, `seed_wiki_graph.py`) is a different concept — it represents one actual Markdown file under `LLM_WIKI_PATH`, a real durable wiki doc. `capture_session` (below) is a third thing.

**`capture_note` removed the same day (Todd, 2026-09-18).** Checked what it actually did before deciding: its journal row was never read back by any retrieval path (`search_wiki`/`recall_mem`/`get_context` all read Graphiti or the wiki files, never raw journal events) and the only mechanism that could ever surface it — offline reasoning-episode consolidation, which treats any `actor_type="user"` event as a candidate turn regardless of `event_type` — has no scheduler and has never run against live capture. The real journal confirmed this wasn't a live behavior change: **zero `capture_note` events existed** at removal time. `capture_session`'s confidence floor (0.4-0.6, "inferred from terse turns or mostly from context") covers the vague-checkpoint case `capture_note` might have been reached for, at least landing in a review queue instead of a dead end. Removed: the MCP tool (`server/mcp.py`), `capture_manual_note()` and `EVENT_TYPE_CAPTURE_NOTE` (`server/capture/middleware.py`), its dedicated test, and every doc/test reference (`README.md`, `docs/CLIENTS.md`, four hardcoded tool-count/set assertions, the MCP contract fixture). Tool count: 18 → 17.

### Design: one `capture_session` tool, routes to either destination

One tool call at a checkpoint; the model tags each item `destination: "episode" | "wiki_proposal"` rather than needing two separate tool calls:

- `destination="episode"` — reuses `ReasoningEpisodePolicyV1`'s existing extraction rubric (`server/policies/reasoning_episode_v1.py`'s `_SYSTEM` prompt, lines 54-105: 8-way `reasoning_kind` taxonomy, WHAT COUNTS exclusions, confidence bands) adapted into the tool's own docstring, so live self-extraction applies the same bar offline windowing does. Implementation, per item: (1) journal the model's own `evidence_text` as a lightweight source event first (harness=`claude_desktop`, redacted via `server/capture/filters.py`) — Cowork's raw turns are never otherwise journaled, so this is the only trace that reaches the journal and gives the episode something real to cite; (2) build a `ReasoningEpisode` (`server/policies/protocols.py:113`) from the item's fields; (3) `ConsolidationStore.record_reasoning_episode()` (`server/consolidation/store.py:267`) with `policy_name="cowork_live_v1"`, `policy_version="0.1"` (distinct provenance from offline windowing, same staging path) and `approval_state` from the shared `reasoning_auto_accept_threshold` config (see Review posture below).
- `destination="wiki_proposal"` — no new plumbing, an internal call to the existing `propose_wiki_update(target_path, proposed_content, rationale, source_context=evidence_text)`. Same review path as every other proposal.
- Routing rule (stated in both the tool docstring and `SERVER_INSTRUCTIONS`): *episodic* = something that happened/was decided/was concluded in this conversation; *wiki-worthy* = durable, reusable, still-true-read-cold-later knowledge.

### Review posture (Todd, 2026-09-18)

Start with full manual review via the existing `tier1_review_queue()` to gauge quality and tune the rubric. Once trusted, flip `reasoning_auto_accept_threshold` to a real confidence value and have a scheduled job call the existing `promote_auto_accepted()` — both pieces already exist, this is a config change made after calibration, not new code. **This threshold is shared with MS4b** (below) — one calibration, not two.

### Honesty constraint

Stays **interaction-triggered, not automatic** in the cron/hook sense — no session-end signal exists for Cowork. Per `docs/ROADMAP.md`'s own principle, docs must not imply continuous capture this mechanism can't deliver: this is a real review-queue write, not a background daemon.

### Tasks

- [x] `capture_session` tool registered in `server/mcp.py`, docstring carries the adapted rubric + routing rule.
- [x] `server/capture/session_capture.py` — per-item journal-then-stage logic. Routes to `record_reasoning_episode()` (episode) or `create_wiki_proposal()` (wiki_proposal); one bad item reported per-item, never fatal to the rest of a batch (a broad `except Exception` around each item, on top of per-field validation).
- [x] Update `SERVER_INSTRUCTIONS` (`server/mcp.py:66-86`) to point "session wrapping up" at `capture_session`.
- [x] Tests: `tests/test_capture_session.py`, 13 cases — staging, evidence-event creation (verified the cited event actually lands in `events`), secret redaction in `evidence_text` (a synthetic API-key-shaped string confirmed stripped), threshold behavior (unset → always `queued_for_review`; set → `auto_accepted` above it, `queued_for_review` below), wiki-proposal routing, mixed episode+wiki batches, per-item error isolation.
- [x] Docs: `docs/CLIENTS.md` corrected — `capture_session` added to the tool list and the MCP-boundary-capture mitigations, and the per-client table's stale "Cowork and Code tab resolve distinctly" claim replaced with the real MS6c finding (they can collapse to the same harness bucket).
- [x] `capture_note` removed same-day per the finding above — see the terminology note. Full suite green (435 passed) after every doc/test reference updated.
- [x] (Todd, outside this repo) Custom Claude instructions redrafted to reference `capture_session` (and drop the `capture_note` line that was briefly drafted, once the removal decision was made) — his to paste in.

### Exit gate — ANSWERED (2026-09-18)

**Manual dry run, run live in a real Cowork session, both destinations exercised for real:**

- **Episode path:** "we just decided to cap the review-batch size at 50 episodes per pass... wrap up this checkpoint and capture that decision." Staged for real — `episode-proposals/tier1/cf2e6189....json`: `policy_name=cowork_live_v1`, `reasoning_kind=decision`, `confidence=0.95`, `approval_state=queued_for_review`, a real evidence event journaled and cited. Verified by content grep (the mirror's filename is a content hash, not searchable by memory_id) and by confirming no matching wiki proposal was also created.
- **Wiki path:** asked to document the episode-proposals mirror's own layout as a durable wiki proposal. Created `prop_20260918_163113_0dc89442` for `WIKI/projects/Context-Memory-Fabric/Episode-Proposals-Mirror.md`, staged only, `LLM_Wiki` untouched — confirmed via direct file read.
- Both test items rejected afterward via `reject_episode`/`review_proposal` (real memory, not meant to be kept) — both correctly moved to their `rejected/` subfolders, confirmed by path check.

**Real finding, not blocking:** for the episode-path test, the model's own stated plan said it would route the item as `destination="wiki_proposal"` ("a durable operating rule"), then the actual tool call used `destination="episode"`. The content is genuinely borderline (a decision that is also a standing rule), so this is a real gap in the routing rule's disambiguation for that shape of content, not a bug — filed to Backlog rather than block this exit gate on it, since both actual writes (episode and wiki, in the two separate live tests) landed correctly and safely regardless of which one time chose which path.

**Separate finding, addressed the same day:** the live test also surfaced that Claude Desktop mirrors captured facts into its own local per-project memory (`~/.claude/projects/*/memory/*.md`), independent of CMF — confirmed the mirrored file exists correctly, but this creates two independently-writable copies of the same fact with no reconciliation. Todd's custom Claude instructions were updated with an explicit tie-break: CMF is authoritative over local project memory when the two disagree.

**Effort:** 1-2 sessions (one new tool + docstring + tests, no new subsystem) — actual: 1 session build + live verification the same day.
**Risk:** Low, and realized-low: no new storage, reused `record_reasoning_episode`/`propose_wiki_update` as-is, both real writes landed exactly as designed on the first live try.

**Correction #1 (same day): mirror content didn't match its own location on review.** Todd spotted it directly — the rejected test episode's mirror file had moved to `rejected/` but its own `approval_state` field still read `queued_for_review`. Root cause: `move_episode_mirror()` was a pure filesystem rename, never a content rewrite. Fixed: it now rewrites `approval_state` to the terminal value plus `reviewed_at`/`reviewer`/`review_reason` on move — the same single-evolving-field convention `WikiProposal.status` already used (which is why the wiki side never had this bug). Retroactively fixed the real test file; 2 new test assertions added.

**Correction #2 (same day): no MCP-exposed episode review, at parity gap with MS6d.** Todd asked why I hadn't used an MCP tool to reject the wiki test, then asked directly whether an episode equivalent existed — it didn't. `approve_episode`/`reject_episode`/`tier1_review_queue()` were CLI-only (`server/review/cli.py`); MS6d built a full `list`/`get`/`review`/`apply`/`bulk_reject` MCP lifecycle for wiki proposals but the episode side never got the equivalent. Built the same day: `list_episode_proposals`, `get_episode_proposal`, `review_episode` (approve/reject/defer), `bulk_review_episodes` (mixed verdicts in one call, strictly more capable than `bulk_reject_wiki_proposals`'s single-reason-only shape, since it wraps the already-existing `apply_verdicts()`). Reads go through the `episode-proposals/` file mirror (already dual-policy-aware), not `ConsolidationStore.query_reasoning_episodes()` directly — see the filed finding below for why. Tool count 17 → 21.

**Found while building the above, filed to Backlog rather than fixed on the spot:** `ConsolidationStore.query_reasoning_episodes()` hardcodes `policy_name = 'reasoning-episode'`, so `tier1_review_queue()` (and the CLI built on it) never sees `cowork_live_v1` rows at all. Not a one-line fix — `reasoning-episode` and `cowork_live_v1` version themselves independently (0.3 vs 0.1), so the function's single shared `policy_version` parameter would need restructuring to a per-policy-name version map, which also touches `tier1_review_queue()`'s signature and several existing tests. Real gap, deliberately not rushed into today's change.

---

## Corpus & review backlog (found 2026-09-11, closed out same day)

MS6b's governance tooling ([MS6b](plan-history.md#ms6b--governance)) surfaced this once `explain --graph`, `correct-memory`, and the review CLI could actually be pointed at the corpus. Measured directly against the live journal, not the MS6a/MS3.5-era estimates (several bulk actions and ongoing capture had moved these numbers since 2026-09-08):

- [x] **27 tier-1-shaped v0.1 orphans, reviewed (2026-09-11).** All were `decision`×16/`plan`×8/`rejected_alternative`×3, `policy_version=0.1`, correctly excluded from `tier1_review_queue()`'s v0.2 filter but never formally retired. Checked each against v0.2 for actual evidence-event overlap rather than assuming duplication: **19 confirmed duplicates** (same evidence, reprocessed under v0.2, `rejected` with reason citing the duplication) and **8 with no v0.2 counterpart**, individually read in full (statement + rationale + evidence) — **4 approved** (specific, confirmed-accurate technical/narrative decisions: Saturn-mode print resolution, moon/sun/Saturn mask config, Java-over-Kotlin for the Android project, integrating the fine-arts narrative into the career-navigator cover letter) and **4 rejected** (two were literal task instructions, not durable facts, one still `status=open`; two were thin one-off wording edits on a resume/LinkedIn post with no lasting reference value).
- [x] **969 tier-2 episodes, reviewed and promoted (2026-09-11) — closed out.** `finding`×16, `hypothesis`×33, `experiment`×138, `investigation`×755 (the live count moved from the 981 estimate — ongoing capture). Read individually — statement, rationale, status, and evidence turns for ambiguous ones — against one standard: promote only if the statement itself states a durable, specific, resolved conclusion; reject pure process narration, open unresolved threads, or task instructions misclassified as reasoning; defer anything genuinely uncertain or sensitive rather than guessing. First pass: **163 approved, 797 rejected, 9 deferred**. Promote rate varied by kind as expected (`decision`-adjacent kinds like `finding`/`hypothesis` ran ~35-50%; `investigation`/`experiment`, which are mostly exploration without a stated resolution, ran ~12-31%) — confirms MS3.5's own observation that `reasoning_kind` is a routing hint, not a keep/drop gate; individual content had to be read either way. Verdicts applied via `apply_verdicts` (the same chokepoint MS6a's tier-1 pass used).
  - **The 9 deferred, resolved by Todd (2026-09-11):** *Sensitive/personal (5)* — a finding connecting current binocular vision instability to a past brain injury + neuro-ophthalmologic history; the matching hypothesis and investigation episodes from the same thread (astigmatism theory, single-eye-vs-both testing); an investigation seeking medical guidance on OTC pain relievers after a head injury; an investigation analyzing a condo board-meeting transcript evaluating specific named candidates (Ken, Kevin, Brian) for board openings — **Todd approved all 5**. *Genuinely uncertain (4)* — a home-AV finding describing a symptom mid-troubleshooting (Shield/projector power state); a hypothesis about whether current homeowners insurance covers required EV-charger terms; a hypothesis interpreting the condo board's resistance motive as capacity-hoarding rather than genuine cost concern; an experiment with real measured data (Jackery AC-vs-DC power draw) the user themself questioned the accuracy of — Todd rejected the home-AV symptom and the power-draw measurement, approved both EV-charging hypotheses.
  - **Final tally: 171 approved, 799 rejected, 0 deferred**, all 171 promoted into `mem-fabric-local` across three batches, **171/171 succeeded, 0 failed** (real qwen3.5-122b extraction per episode, local/unmetered). Graph grew **295 → 465 Episodic nodes** (verified via direct Cypher count), entities 393→665, `RELATES_TO` edges →501.
  - **A real bug surfaced when Todd asked why the first batch was 164, not 163** (2026-09-11): one of the 164 wasn't a tier-2 approval at all — it was the *original, pre-correction* 360-cam/eclipse episode (the misattribution `correct_memory` fixed earlier in the MS6b work), silently re-promoted with its stale wrong content. Root cause: `reviews` is last-writer-wins per memory_id and `correct_memory` never touches it, so the old memory_id's `approved` verdict from 2026-09-08 stayed on record after the correction superseded it; `correct_memory` separately clears the old memory_id's `PromotionStore` row (the graph identity moved to the new memory_id). Those two facts together made `actions.promote_approved`'s "approved and not yet promoted" query — which had no idea `derived_memories.approval_state` existed — treat the superseded old memory_id as freshly eligible. **Fixed:** the query now excludes any memory_id whose `derived_memories.approval_state` is `rejected`/`superseded_by_reasoning`/`superseded_by_correction`, joining against `derived_memories` rather than reading `reviews` alone (`server/review/actions.py`). New regression test `test_superseded_by_correction_is_not_reeligible` (`tests/test_ms6_review.py`) reproduces the exact sequence and passed on the very next real promotion batch (the 2 final EV-charging approvals). **Cleanup:** the wrongly-revived `chatgpt-photo-006` episode (uuid `2734ac71-...`) removed from `mem-fabric-local`, its stray `PromotionStore` row deleted.
- [x] **25,961 heuristic-pattern rows still `queued_for_review`, resolved for real (2026-09-18).** Resurfaced while scoping MS4a2/MS4b's episode-proposals file mirror. Root cause: the same underlying events reclassified three times as the policy version bumped 1.0 (9,658 rows) → 1.1 (9,721) → 1.2 (6,582), older versions never formally retired. Ran `python -m server.review.cli retire-stale-versions --apply` (implementation `bulk_reject_stale_policy_versions()`, `server/review/actions.py:238` — one tool, not two; there is no separate `bulk-reject-stale-policy-versions` CLI command) — dry-run first confirmed the tool's own docstring numbers exactly (19,379 stale rows / 9,757 distinct events), then applied: **19,379 v1.0/v1.1 rows rejected** (`batch_id: 67a0f5dbe07945b39799ad75ca8a11cd`, reversible via `revert-batch`), leaving heuristic-pattern's real `queued_for_review` pile at **6,582** (v1.2 only) — down from 25,961.

---

## Proposal-directory housekeeping (found 2026-09-18, scoping MS4a2/MS4b)

Both proposal-review surfaces stored everything flat with no move-on-review, and MS4a2's `capture_session` was about to add volume to both:

- [x] **`wiki-proposals/` (was 78 files, flat, all statuses mixed) — fixed (2026-09-18).** `server/proposals.py`'s `_save_proposal()` now locates a proposal wherever it currently lives (root/`approved/`/`rejected/`) and moves it to match its status on every save; `get_proposal()`/`list_proposals()` search all three locations. `applied` stays under `approved/` (sub-state, not a third folder, per MS6d's own status vocabulary). One-time migration run for real against the existing backlog: **76 moved to `rejected/`, 2 to `approved/`** (both `applied`), flat root now empty. 4 new tests in `tests/test_ms6d_proposal_review.py::TestProposalSubfolders`.
- [x] **Staged reasoning episodes had no file representation at all — fixed (2026-09-18).** `server/episode_proposals.py`: a write-through mirror hooked into `ConsolidationStore.record_reasoning_episode()` (unconditional — heuristic-pattern rows physically can't reach that method, they go through `record_consolidation()` instead, so no policy_name filter was needed) and into `approve_episode`/`reject_episode` (`server/review/actions.py`) for the approved/rejected move. SQLite stays authoritative; the file is a read-only projection. Filename is `sha256(memory_id)` rather than the raw id — a real bug surfaced backfilling production: some windowed-episode memory_ids embed long composite event ids and exceed the filesystem's filename length limit. **Real backfill run**, scoped as planned (tier1 first, tier2 second, `heuristic-pattern`/stale `reasoning-episode@0.1` excluded): **301 tier1 + 942 tier2 = 1,243 files written**, verified against `SELECT count(*)`. 10 new tests in `tests/test_episode_proposals.py`, including a regression test for the long-memory_id filename bug. Test isolation: `ConsolidationStore`/`ReviewStore` derive the mirror directory from their own `db_path` (a sibling `episode-proposals/` next to whatever db_path a test passes), so no existing test needed updating to avoid polluting the real project directory.
  - **Follow-up bug, found and fixed the same day:** Todd spotted a rejected test episode's mirror file sitting in `rejected/` while its own `approval_state` field still read `queued_for_review` — `move_episode_mirror()` was a pure filesystem rename, never a content rewrite. Fixed: it now rewrites `approval_state` to the terminal value plus `reviewed_at`/`reviewer`/`review_reason` on move, matching `WikiProposal.status`'s own single-evolving-field convention (which is why the wiki side never had this bug). 6 new test assertions.

---

## Episode-proposals review MCP tools (found 2026-09-18, parity gap with MS6d)

MS6d built a full `list`/`get`/`review`/`apply`/`bulk_reject` MCP lifecycle for wiki proposals. The episode side never got the equivalent — `approve_episode`/`reject_episode`/`tier1_review_queue()` were CLI-only. Found the same day a live `capture_session` test needed rejecting and no MCP client could do it.

- [x] **Built:** `list_episode_proposals(tier, approval_state)`, `get_episode_proposal(memory_id)` (both read the `episode-proposals/` file mirror, not `derived_memories` directly — see the filed gap below for why), `review_episode(memory_id, verdict, reason, reviewer)` (approve/reject/defer, never calls `remember()`), `bulk_review_episodes(verdicts, reviewer)` (mixed verdicts in one call, wraps the already-existing `apply_verdicts()` — strictly more capable than `bulk_reject_wiki_proposals`'s single-reason-only shape). Tool count 17 → 21. 6 new tests in `tests/test_episode_proposals.py` covering the read helpers; the MCP wrapper functions themselves are registration-checked only (same convention `promote_auto_accepted_memories` already follows for tools with no test-injectable path — the underlying logic gets the real unit coverage, the thin wrapper doesn't get exercised against production state).

**Filed to Backlog, not fixed as part of this pass:** `ConsolidationStore.query_reasoning_episodes()` hardcodes `policy_name = 'reasoning-episode'`, so `tier1_review_queue()` (and the CLI built on it) never sees `cowork_live_v1` rows at all — a `capture_session`-staged episode is invisible to `server/review/cli.py queue --tier 1` even though it shows up in the new MCP tools. Not a one-line fix: `reasoning-episode` and `cowork_live_v1` version themselves independently (0.3 vs 0.1), so the function's single shared `policy_version` parameter needs restructuring to a per-policy-name version map, which also touches `tier1_review_queue()`'s signature and several existing tests (`test_ms6_review.py`, `test_ms3_6_promotion.py`, `test_ms7b_enriched_content.py`). Still open in `docs/plan-active.md`'s Backlog.

---

## Post-apply staleness (found 2026-09-16, fixed same day, MS6d)

`apply_wiki_proposal` is the first tool that writes into `LLM_WIKI_PATH`, but nothing downstream that assumes the corpus is static was getting invalidated when it ran: `search_wiki`'s filesystem-scan cache, and (at the time) [MS7b](plan-history.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13)'s offline-built wiki-derived entity/section graph. Confirmed concretely, not just theoretically — the real apply of `prop_20260916_125736_a33f295a` created `WIKI/projects/Context-Memory-Fabric/Context-Layers-as-the-Next-Frontier.md` (commit `6dfce163`) and it did not surface via `search_wiki` until this fix.

- [x] `search_wiki`'s side fixed: `apply_wiki_proposal` now calls `invalidate_corpus_cache()` (`server/wiki.py`) on every real (non-dry-run) apply — lazy invalidation, drops the cached engine/assets rather than forcing an immediate rescan, since applies are rare and a rescan can be non-trivial cost. Tested (`tests/test_ms6d_proposal_review.py::TestPostApplyStaleness`, 3 cases: pure invalidation, real-apply wiring, dry-run does *not* invalidate). **Live server restarted 2026-09-16** to pick this up — confirmed live via subsequent `search_wiki` calls from Code mode and Cowork.
- [x] The wiki-derived graph's own staleness question is moot now that MS7b closed onto a static, no-longer-rebuilt `mem-fabric-local` — see below.

---

## Corpus & review backlog (found 2026-09-11)

MS6b's governance tooling surfaced this once `explain --graph`, `correct-memory`, and the review CLI could actually be pointed at the corpus. Measured directly against the live journal, not the MS6a/MS3.5-era estimates (several bulk actions and ongoing capture had moved these numbers since 2026-09-08):

- **27 tier-1-shaped v0.1 orphans, reviewed (2026-09-11).** All were `decision`×16/`plan`×8/`rejected_alternative`×3, `policy_version=0.1`, correctly excluded from `tier1_review_queue()`'s v0.2 filter but never formally retired. Checked each against v0.2 for actual evidence-event overlap rather than assuming duplication: **19 confirmed duplicates** (same evidence, reprocessed under v0.2, `rejected` with reason citing the duplication) and **8 with no v0.2 counterpart**, individually read in full (statement + rationale + evidence) — **4 approved** (specific, confirmed-accurate technical/narrative decisions: Saturn-mode print resolution, moon/sun/Saturn mask config, Java-over-Kotlin for the Android project, integrating the fine-arts narrative into the career-navigator cover letter) and **4 rejected** (two were literal task instructions, not durable facts, one still `status=open`; two were thin one-off wording edits on a resume/LinkedIn post with no lasting reference value).
- **969 tier-2 episodes, reviewed and promoted (2026-09-11) — closed out.** `finding`×16, `hypothesis`×33, `experiment`×138, `investigation`×755 (the live count moved from the 981 estimate — ongoing capture). Read individually — statement, rationale, status, and evidence turns for ambiguous ones — against one standard: promote only if the statement itself states a durable, specific, resolved conclusion; reject pure process narration, open unresolved threads, or task instructions misclassified as reasoning; defer anything genuinely uncertain or sensitive rather than guessing. First pass: **163 approved, 797 rejected, 9 deferred**. Promote rate varied by kind as expected (`decision`-adjacent kinds like `finding`/`hypothesis` ran ~35-50%; `investigation`/`experiment`, which are mostly exploration without a stated resolution, ran ~12-31%) — confirms MS3.5's own observation that `reasoning_kind` is a routing hint, not a keep/drop gate; individual content had to be read either way. Verdicts applied via `apply_verdicts` (the same chokepoint MS6a's tier-1 pass used).
  - **The 9 deferred, resolved by Todd (2026-09-11):** *Sensitive/personal (5)* — a finding connecting current binocular vision instability to a past brain injury + neuro-ophthalmologic history; the matching hypothesis and investigation episodes from the same thread (astigmatism theory, single-eye-vs-both testing); an investigation seeking medical guidance on OTC pain relievers after a head injury; an investigation analyzing a condo board-meeting transcript evaluating specific named candidates (Ken, Kevin, Brian) for board openings — **Todd approved all 5**. *Genuinely uncertain (4)* — a home-AV finding describing a symptom mid-troubleshooting (Shield/projector power state); a hypothesis about whether current homeowners insurance covers required EV-charger terms; a hypothesis interpreting the condo board's resistance motive as capacity-hoarding rather than genuine cost concern; an experiment with real measured data (Jackery AC-vs-DC power draw) the user themself questioned the accuracy of — Todd rejected the home-AV symptom and the power-draw measurement, approved both EV-charging hypotheses.
  - **Final tally: 171 approved, 799 rejected, 0 deferred**, all 171 promoted into `mem-fabric-local` across three batches, **171/171 succeeded, 0 failed** (real qwen3.5-122b extraction per episode, local/unmetered). Graph grew **295 → 465 Episodic nodes** (verified via direct Cypher count), entities 393→665, `RELATES_TO` edges →501.
  - **A real bug surfaced when Todd asked why the first batch was 164, not 163** (2026-09-11): one of the 164 wasn't a tier-2 approval at all — it was the *original, pre-correction* 360-cam/eclipse episode (the misattribution `correct_memory` fixed earlier in the MS6b work), silently re-promoted with its stale wrong content. Root cause: `reviews` is last-writer-wins per memory_id and `correct_memory` never touches it, so the old memory_id's `approved` verdict from 2026-09-08 stayed on record after the correction superseded it; `correct_memory` separately clears the old memory_id's `PromotionStore` row (the graph identity moved to the new memory_id). Those two facts together made `actions.promote_approved`'s "approved and not yet promoted" query — which had no idea `derived_memories.approval_state` existed — treat the superseded old memory_id as freshly eligible. **Fixed:** the query now excludes any memory_id whose `derived_memories.approval_state` is `rejected`/`superseded_by_reasoning`/`superseded_by_correction`, joining against `derived_memories` rather than reading `reviews` alone (`server/review/actions.py`). New regression test `test_superseded_by_correction_is_not_reeligible` (`tests/test_ms6_review.py`) reproduces the exact sequence and passed on the very next real promotion batch (the 2 final EV-charging approvals). **Cleanup:** the wrongly-revived `chatgpt-photo-006` episode (uuid `2734ac71-...`) removed from `mem-fabric-local`, its stray `PromotionStore` row deleted.
- **25,961 heuristic-pattern rows still `queued_for_review`, resolved for real (2026-09-18).** Resurfaced while scoping MS4a2/MS4b's episode-proposals file mirror. Root cause: the same underlying events reclassified three times as the policy version bumped 1.0 (9,658 rows) → 1.1 (9,721) → 1.2 (6,582), older versions never formally retired. Ran `python -m server.review.cli retire-stale-versions --apply` (implementation `bulk_reject_stale_policy_versions()`, `server/review/actions.py:238` — one tool, not two; there is no separate `bulk-reject-stale-policy-versions` CLI command) — dry-run first confirmed the tool's own docstring numbers exactly (19,379 stale rows / 9,757 distinct events), then applied: **19,379 v1.0/v1.1 rows rejected** (`batch_id: 67a0f5dbe07945b39799ad75ca8a11cd`, reversible via `revert-batch`), leaving heuristic-pattern's real `queued_for_review` pile at **6,582** (v1.2 only) — down from 25,961.

Remaining open items for this topic stay in [plan-active.md's Backlog](plan-active.md#backlog) (the corpus keeps growing, and the two new adapters haven't touched it yet).

---

## Proposal-directory housekeeping (found 2026-09-18, scoping MS4a2/MS4b)

Both proposal-review surfaces stored everything flat with no move-on-review, and MS4a2's `capture_session` was about to add volume to both:

- **`wiki-proposals/` (was 78 files, flat, all statuses mixed) — fixed (2026-09-18).** `server/proposals.py`'s `_save_proposal()` now locates a proposal wherever it currently lives (root/`approved/`/`rejected/`) and moves it to match its status on every save; `get_proposal()`/`list_proposals()` search all three locations. `applied` stays under `approved/` (sub-state, not a third folder, per MS6d's own status vocabulary). One-time migration run for real against the existing backlog: **76 moved to `rejected/`, 2 to `approved/`** (both `applied`), flat root now empty. 4 new tests in `tests/test_ms6d_proposal_review.py::TestProposalSubfolders`.
- **Staged reasoning episodes had no file representation at all — fixed (2026-09-18).** `server/episode_proposals.py`: a write-through mirror hooked into `ConsolidationStore.record_reasoning_episode()` (unconditional — heuristic-pattern rows physically can't reach that method, they go through `record_consolidation()` instead, so no policy_name filter was needed) and into `approve_episode`/`reject_episode` (`server/review/actions.py`) for the approved/rejected move. SQLite stays authoritative; the file is a read-only projection. Filename is `sha256(memory_id)` rather than the raw id — a real bug surfaced backfilling production: some windowed-episode memory_ids embed long composite event ids and exceed the filesystem's filename length limit. **Real backfill run**, scoped as planned (tier1 first, tier2 second, `heuristic-pattern`/stale `reasoning-episode@0.1` excluded): **301 tier1 + 942 tier2 = 1,243 files written**, verified against `SELECT count(*)`. 10 new tests in `tests/test_episode_proposals.py`, including a regression test for the long-memory_id filename bug. Test isolation: `ConsolidationStore`/`ReviewStore` derive the mirror directory from their own `db_path` (a sibling `episode-proposals/` next to whatever db_path a test passes), so no existing test needed updating to avoid polluting the real project directory.
  - **Follow-up bug, found and fixed the same day:** Todd spotted a rejected test episode's mirror file sitting in `rejected/` while its own `approval_state` field still read `queued_for_review` — `move_episode_mirror()` was a pure filesystem rename, never a content rewrite. Fixed: it now rewrites `approval_state` to the terminal value plus `reviewed_at`/`reviewer`/`review_reason` on move, matching `WikiProposal.status`'s own single-evolving-field convention (which is why the wiki side never had this bug). 6 new test assertions.

---

## Episode-proposals review MCP tools (found 2026-09-18, parity gap with MS6d)

MS6d built a full `list`/`get`/`review`/`apply`/`bulk_reject` MCP lifecycle for wiki proposals. The episode side never got the equivalent — `approve_episode`/`reject_episode`/`tier1_review_queue()` were CLI-only. Found the same day a live `capture_session` test needed rejecting and no MCP client could do it.

- **Built:** `list_episode_proposals(tier, approval_state)`, `get_episode_proposal(memory_id)` (both read the `episode-proposals/` file mirror, not `derived_memories` directly — see [plan-active.md's Backlog](plan-active.md#backlog) for the filed gap on why), `review_episode(memory_id, verdict, reason, reviewer)` (approve/reject/defer, never calls `remember()`), `bulk_review_episodes(verdicts, reviewer)` (mixed verdicts in one call, wraps the already-existing `apply_verdicts()` — strictly more capable than `bulk_reject_wiki_proposals`'s single-reason-only shape). Tool count 17 → 21. 6 new tests in `tests/test_episode_proposals.py` covering the read helpers; the MCP wrapper functions themselves are registration-checked only (same convention `promote_auto_accepted_memories` already follows for tools with no test-injectable path — the underlying logic gets the real unit coverage, the thin wrapper doesn't get exercised against production state).

The remaining item for this topic (the `query_reasoning_episodes()` hardcoded-policy-name gap) stays open in [plan-active.md's Backlog](plan-active.md#backlog).

---

## Post-apply staleness (found 2026-09-16, fixed same day, MS6d)

`apply_wiki_proposal` is the first tool that writes into `LLM_WIKI_PATH`, but nothing downstream that assumes the corpus is static was getting invalidated when it ran: `search_wiki`'s filesystem-scan cache, and (at the time) [MS7b](plan-history.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13)'s offline-built wiki-derived entity/section graph. Confirmed concretely, not just theoretically — the real apply of `prop_20260916_125736_a33f295a` created `WIKI/projects/Context-Memory-Fabric/Context-Layers-as-the-Next-Frontier.md` (commit `6dfce163`) and it did not surface via `search_wiki` until this fix.

- `search_wiki`'s side fixed: `apply_wiki_proposal` now calls `invalidate_corpus_cache()` (`server/wiki.py`) on every real (non-dry-run) apply — lazy invalidation, drops the cached engine/assets rather than forcing an immediate rescan, since applies are rare and a rescan can be non-trivial cost. Tested (`tests/test_ms6d_proposal_review.py::TestPostApplyStaleness`, 3 cases: pure invalidation, real-apply wiring, dry-run does *not* invalidate). **Live server restarted 2026-09-16** to pick this up — confirmed live via subsequent `search_wiki` calls from Code mode and Cowork.
- The wiki-derived graph's own staleness question is moot now that MS7b closed onto a static, no-longer-rebuilt `mem-fabric-local` — see [plan-active.md's Backlog](plan-active.md#backlog) (wiki entity-extraction quality item) for what's still open there.

---

## Backlog — closed items

Completed backlog items, moved out of [plan-active.md](plan-active.md)'s Backlog section once done. Grouped under the same topic headings used there.

### Corpus & review backlog (found 2026-09-11, closed out same day)

MS6b's governance tooling ([plan-history.md](plan-history.md#ms6b--governance)) surfaced this once `explain --graph`, `correct-memory`, and the review CLI could actually be pointed at the corpus. Measured directly against the live journal, not the MS6a/MS3.5-era estimates (several bulk actions and ongoing capture had moved these numbers since 2026-09-08):

- [x] **27 tier-1-shaped v0.1 orphans, reviewed (2026-09-11).** All were `decision`×16/`plan`×8/`rejected_alternative`×3, `policy_version=0.1`, correctly excluded from `tier1_review_queue()`'s v0.2 filter but never formally retired. Checked each against v0.2 for actual evidence-event overlap rather than assuming duplication: **19 confirmed duplicates** (same evidence, reprocessed under v0.2, `rejected` with reason citing the duplication) and **8 with no v0.2 counterpart**, individually read in full (statement + rationale + evidence) — **4 approved** (specific, confirmed-accurate technical/narrative decisions: Saturn-mode print resolution, moon/sun/Saturn mask config, Java-over-Kotlin for the Android project, integrating the fine-arts narrative into the career-navigator cover letter) and **4 rejected** (two were literal task instructions, not durable facts, one still `status=open`; two were thin one-off wording edits on a resume/LinkedIn post with no lasting reference value).
- [x] **969 tier-2 episodes, reviewed and promoted (2026-09-11) — closed out.** `finding`×16, `hypothesis`×33, `experiment`×138, `investigation`×755 (the live count moved from the 981 estimate — ongoing capture). Read individually — statement, rationale, status, and evidence turns for ambiguous ones — against one standard: promote only if the statement itself states a durable, specific, resolved conclusion; reject pure process narration, open unresolved threads, or task instructions misclassified as reasoning; defer anything genuinely uncertain or sensitive rather than guessing. First pass: **163 approved, 797 rejected, 9 deferred**. Promote rate varied by kind as expected (`decision`-adjacent kinds like `finding`/`hypothesis` ran ~35-50%; `investigation`/`experiment`, which are mostly exploration without a stated resolution, ran ~12-31%) — confirms MS3.5's own observation that `reasoning_kind` is a routing hint, not a keep/drop gate; individual content had to be read either way. Verdicts applied via `apply_verdicts` (the same chokepoint MS6a's tier-1 pass used).
  - **The 9 deferred, resolved by Todd (2026-09-11):** *Sensitive/personal (5)* — a finding connecting current binocular vision instability to a past brain injury + neuro-ophthalmologic history; the matching hypothesis and investigation episodes from the same thread (astigmatism theory, single-eye-vs-both testing); an investigation seeking medical guidance on OTC pain relievers after a head injury; an investigation analyzing a condo board-meeting transcript evaluating specific named candidates (Ken, Kevin, Brian) for board openings — **Todd approved all 5**. *Genuinely uncertain (4)* — a home-AV finding describing a symptom mid-troubleshooting (Shield/projector power state); a hypothesis about whether current homeowners insurance covers required EV-charger terms; a hypothesis interpreting the condo board's resistance motive as capacity-hoarding rather than genuine cost concern; an experiment with real measured data (Jackery AC-vs-DC power draw) the user themself questioned the accuracy of — Todd rejected the home-AV symptom and the power-draw measurement, approved both EV-charging hypotheses.
  - **Final tally: 171 approved, 799 rejected, 0 deferred**, all 171 promoted into `mem-fabric-local` across three batches, **171/171 succeeded, 0 failed** (real qwen3.5-122b extraction per episode, local/unmetered). Graph grew **295 → 465 Episodic nodes** (verified via direct Cypher count), entities 393→665, `RELATES_TO` edges →501.
  - **A real bug surfaced when Todd asked why the first batch was 164, not 163** (2026-09-11): one of the 164 wasn't a tier-2 approval at all — it was the *original, pre-correction* 360-cam/eclipse episode (the misattribution `correct_memory` fixed earlier in the MS6b work), silently re-promoted with its stale wrong content. Root cause: `reviews` is last-writer-wins per memory_id and `correct_memory` never touches it, so the old memory_id's `approved` verdict from 2026-09-08 stayed on record after the correction superseded it; `correct_memory` separately clears the old memory_id's `PromotionStore` row (the graph identity moved to the new memory_id). Those two facts together made `actions.promote_approved`'s "approved and not yet promoted" query — which had no idea `derived_memories.approval_state` existed — treat the superseded old memory_id as freshly eligible. **Fixed:** the query now excludes any memory_id whose `derived_memories.approval_state` is `rejected`/`superseded_by_reasoning`/`superseded_by_correction`, joining against `derived_memories` rather than reading `reviews` alone (`server/review/actions.py`). New regression test `test_superseded_by_correction_is_not_reeligible` (`tests/test_ms6_review.py`) reproduces the exact sequence and passed on the very next real promotion batch (the 2 final EV-charging approvals). **Cleanup:** the wrongly-revived `chatgpt-photo-006` episode (uuid `2734ac71-...`) removed from `mem-fabric-local`, its stray `PromotionStore` row deleted.
- [x] **25,961 heuristic-pattern rows still `queued_for_review`, resolved for real (2026-09-18).** Resurfaced while scoping MS4a2/MS4b's episode-proposals file mirror. Root cause: the same underlying events reclassified three times as the policy version bumped 1.0 (9,658 rows) → 1.1 (9,721) → 1.2 (6,582), older versions never formally retired. Ran `python -m server.review.cli retire-stale-versions --apply` (implementation `bulk_reject_stale_policy_versions()`, `server/review/actions.py:238` — one tool, not two; there is no separate `bulk-reject-stale-policy-versions` CLI command) — dry-run first confirmed the tool's own docstring numbers exactly (19,379 stale rows / 9,757 distinct events), then applied: **19,379 v1.0/v1.1 rows rejected** (`batch_id: 67a0f5dbe07945b39799ad75ca8a11cd`, reversible via `revert-batch`), leaving heuristic-pattern's real `queued_for_review` pile at **6,582** (v1.2 only) — down from 25,961.

(The remaining items under this heading — the growing-corpus framing note and the `cmf_test` vector-dimension mismatch — are still open; see [plan-active.md](plan-active.md#backlog).)

### Proposal-directory housekeeping (found 2026-09-18, scoping MS4a2/MS4b)

Both proposal-review surfaces stored everything flat with no move-on-review, and MS4a2's `capture_session` was about to add volume to both:

- [x] **`wiki-proposals/` (was 78 files, flat, all statuses mixed) — fixed (2026-09-18).** `server/proposals.py`'s `_save_proposal()` now locates a proposal wherever it currently lives (root/`approved/`/`rejected/`) and moves it to match its status on every save; `get_proposal()`/`list_proposals()` search all three locations. `applied` stays under `approved/` (sub-state, not a third folder, per MS6d's own status vocabulary). One-time migration run for real against the existing backlog: **76 moved to `rejected/`, 2 to `approved/`** (both `applied`), flat root now empty. 4 new tests in `tests/test_ms6d_proposal_review.py::TestProposalSubfolders`.
- [x] **Staged reasoning episodes had no file representation at all — fixed (2026-09-18).** `server/episode_proposals.py`: a write-through mirror hooked into `ConsolidationStore.record_reasoning_episode()` (unconditional — heuristic-pattern rows physically can't reach that method, they go through `record_consolidation()` instead, so no policy_name filter was needed) and into `approve_episode`/`reject_episode` (`server/review/actions.py`) for the approved/rejected move. SQLite stays authoritative; the file is a read-only projection. Filename is `sha256(memory_id)` rather than the raw id — a real bug surfaced backfilling production: some windowed-episode memory_ids embed long composite event ids and exceed the filesystem's filename length limit. **Real backfill run**, scoped as planned (tier1 first, tier2 second, `heuristic-pattern`/stale `reasoning-episode@0.1` excluded): **301 tier1 + 942 tier2 = 1,243 files written**, verified against `SELECT count(*)`. 10 new tests in `tests/test_episode_proposals.py`, including a regression test for the long-memory_id filename bug. Test isolation: `ConsolidationStore`/`ReviewStore` derive the mirror directory from their own `db_path` (a sibling `episode-proposals/` next to whatever db_path a test passes), so no existing test needed updating to avoid polluting the real project directory.
  - **Follow-up bug, found and fixed the same day:** Todd spotted a rejected test episode's mirror file sitting in `rejected/` while its own `approval_state` field still read `queued_for_review` — `move_episode_mirror()` was a pure filesystem rename, never a content rewrite. Fixed: it now rewrites `approval_state` to the terminal value plus `reviewed_at`/`reviewer`/`review_reason` on move, matching `WikiProposal.status`'s own single-evolving-field convention (which is why the wiki side never had this bug). 6 new test assertions.

### Episode-proposals review MCP tools (found 2026-09-18, parity gap with MS6d)

MS6d built a full `list`/`get`/`review`/`apply`/`bulk_reject` MCP lifecycle for wiki proposals. The episode side never got the equivalent — `approve_episode`/`reject_episode`/`tier1_review_queue()` were CLI-only. Found the same day a live `capture_session` test needed rejecting and no MCP client could do it.

- [x] **Built:** `list_episode_proposals(tier, approval_state)`, `get_episode_proposal(memory_id)` (both read the `episode-proposals/` file mirror, not `derived_memories` directly — see the filed gap below for why), `review_episode(memory_id, verdict, reason, reviewer)` (approve/reject/defer, never calls `remember()`), `bulk_review_episodes(verdicts, reviewer)` (mixed verdicts in one call, wraps the already-existing `apply_verdicts()` — strictly more capable than `bulk_reject_wiki_proposals`'s single-reason-only shape). Tool count 17 → 21. 6 new tests in `tests/test_episode_proposals.py` covering the read helpers; the MCP wrapper functions themselves are registration-checked only (same convention `promote_auto_accepted_memories` already follows for tools with no test-injectable path — the underlying logic gets the real unit coverage, the thin wrapper doesn't get exercised against production state).

(The filed-not-fixed `query_reasoning_episodes()` policy-name gap is still open; see [plan-active.md](plan-active.md#backlog).)

### Post-apply staleness (found 2026-09-16, fixed same day, MS6d)

`apply_wiki_proposal` is the first tool that writes into `LLM_WIKI_PATH`, but nothing downstream that assumes the corpus is static was getting invalidated when it ran: `search_wiki`'s filesystem-scan cache, and (at the time) [MS7b](plan-history.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13)'s offline-built wiki-derived entity/section graph. Confirmed concretely, not just theoretically — the real apply of `prop_20260916_125736_a33f295a` created `WIKI/projects/Context-Memory-Fabric/Context-Layers-as-the-Next-Frontier.md` (commit `6dfce163`) and it did not surface via `search_wiki` until this fix.

- [x] `search_wiki`'s side fixed: `apply_wiki_proposal` now calls `invalidate_corpus_cache()` (`server/wiki.py`) on every real (non-dry-run) apply — lazy invalidation, drops the cached engine/assets rather than forcing an immediate rescan, since applies are rare and a rescan can be non-trivial cost. Tested (`tests/test_ms6d_proposal_review.py::TestPostApplyStaleness`, 3 cases: pure invalidation, real-apply wiring, dry-run does *not* invalidate). **Live server restarted 2026-09-16** to pick this up — confirmed live via subsequent `search_wiki` calls from Code mode and Cowork.
- [x] The wiki-derived graph's own staleness question is moot now that MS7b closed onto a static, no-longer-rebuilt `mem-fabric-local` — see below.
