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
- **Secret hygiene:** `GEMINI_API_KEY` in plaintext in `.env` (gitignored). User's call: don't rotate; use the exposure as a live fixture for the MS4a secret-filtering test.
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
- **Backfill** of the 57 pre-journal `memory-fabric` episodes and 38 `default_db`-recovered ones, marked `provenance_reconstructed: true`. **Both deleted from the live journal 2026-09-04** at User's direction once native ChatGPT coverage existed — the backfill code + tests are unchanged and still correct; only their journal output was removed. `SqliteEventStore.stats()` gained a `by_provenance` breakdown so the captured/reconstructed distinction stays visible.

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
- **`HeuristicPatternPolicyV1`** (`server/policies/heuristic_v1.py`) wraps `server/importer.py`'s `CandidateClassifier` + `TemporalExtractor` — **not** `chatgpt_export_parser.py`'s `StageBasedMemoryExtractor`, which turned out to contain literal hardcoded string matches against specific sentences in User's corpus. `CandidateClassifier` is source-agnostic by construction.
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
- **Windowing** (`server/consolidation/windowing.py`). Prototyped 3 cheap strategies over a real 296-conversation slice — **none reliably found topic boundaries** (real topic shifts have no pause and no cue phrase). Decision (with User): keep it **loose** — `default_windower()` = `TimeGapWindower(2h gap, 20-turn cap)` bounds model-input size only; the model does topical sub-segmentation in-call. `EmbeddingBoundaryWindower` implemented but shelved for a later bake-off.
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

**Backlog — three tiers, not two** (User, 2026-09-07): "auto-accept off" does not mean 1,243 one-by-one reviews.

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

- **Gemini free-tier rate limiter** (`server/core/rate_limiter.py`, 12 tests) — persisted per-model RPM/RPD ledger (midnight-Pacific reset), automatic fallback through a configurable model chain from User's real AI Studio numbers. Raises `GeminiQuotaExhaustedError` *before* any call when the chain lacks headroom. Wired into `get_graphiti_for_operation()`, used by `remember()`/`recall()`.
- **Model-reliability conflict resolved:** a concurrent session found `gemini-3.5-flash-lite` returning 503s and switched to `gemini-3.8-flash`; this session found `gemini-3.8-flash` returning consistent 503s. Root cause: neither session's retry logic treated `503`/`UNAVAILABLE`/"high demand" as retryable — only 429 was — so a transient Google-side capacity error failed outright on whichever model was configured and got misread as a model property. Fixed by classifying 503/`UNAVAILABLE`/"overloaded" as a second retryable category (`_is_transient_gemini_error`). The chain is built on `gemini-3.5-flash-lite → gemini-3.1-flash-lite` (500 RPD vs 20 the deciding factor).
- **Capture middleware** (`server/capture/`) — a source event per tool call, `call_next(ctx)` first, capture scheduled fire-and-forget so it adds zero latency / zero failure risk. Bounded `asyncio.Queue` (500) + single consumer; a full queue drops the newest and counts it.
- **Harness identity** from `client_info` (regex table + safe fallback). **Session identity** — the transport's real `session_id` when it exists, else a cached UUID4 per connection (stdio has none).
- **`capture_note(content, kind)`** and **`capture_health`** MCP tools.
- **Secret filtering** on captured arguments *before* the journal write (`server/capture/filters.py`, reuses `retention`'s scrub + a key-name heuristic).
- Tool/client allow-deny (`CMF_CAPTURE_DENY_TOOLS`/`_CLIENTS`); `import_*` tools denied by default. Content-class filtering **not** built (User deferred it).

### Exit gate — ANSWERED (2026-09-04)

**Privacy and cost:** Gemini-only (no local routing yet), no content-class filtering, journal-everything / auto-consolidate-selectively, spend bounded by the hard free-tier RPM/RPD gate. Capture registered unconditionally (no feature flag).

### Live cross-harness verification — ANSWERED (2026-09-16)

The roadmap's five-step live test, run for real via [MS6c](#ms6c--mcp-server-cross-agent-verification)'s Gemini Spark phase (Cowork/Code turned out not to be usable for this — see MS6c's identity-resolution finding):

1. ✅ Record a decision — `remember()` from Gemini Spark (`gemini_verification_test_2026_09_16`, content `ALPHA-GEMINI`).
2. ✅ Verify it consolidates with source provenance — confirmed via server log (`add_episode` completed, 51s) and `recall_mem` from Claude Desktop Code mode, `source_description: "Gemini Spark session"`.
3. ✅ Retrieve it from a second harness — `recall_mem` from Code mode, content + provenance intact.
4. ✅ Correct it from that second harness — `edit_memory` from Code mode, `ALPHA-GEMINI` → `BRAVO-GEMINI`.
5. ✅ Verify the corrected state is what's now retrievable — `recall_mem` from Code mode returned the corrected content. (The reverse — does Gemini Spark's own next query see the correction — wasn't checked; not required by the original 5 steps, which only needed a second harness to see current state.)

One retry was needed: the first `remember()` attempt from Gemini Spark returned a plausible "Saved..." confirmation in chat with **no actual tool call reaching the server** (nothing in the journal, nothing in FalkorDB) — the same connectivity flakiness behind the "error 1076"s User hit creating new Spark sessions. The second attempt, from a session that was actually connected, is confirmed real via the server log, not just the chat transcript. **Takeaway kept for future reference:** a client-side "success" message from Gemini Spark is not sufficient evidence a write landed — verify server-side (journal or FalkorDB) before trusting it.

**Correction (MS4a2, 2026-09-18): `capture_note` removed.** Its journal row was never read back by any retrieval path — `search_wiki`/`recall_mem`/`get_context` all read Graphiti or the wiki files, never raw journal events — and the offline consolidation pipeline that could theoretically surface it has no scheduler and never ran against live capture. Zero `capture_note` events existed in the journal at removal time; it had never been used in production. Superseded by `capture_session` (see [plan-active.md](plan-history.md#ms4a2--cowork-live-session-episodewiki-capture-2026-09-18)), which stages a real, reviewable episode or Wiki proposal instead of an unread journal marker.

---

## MS3.6 — Promotion: staged memories into the retrievable graph

**Goal:** Move staged `derived_memories` into FalkorDB via `remember()` so `get_context`/`recall` return them. The stage that closes the loop to retrieval.

### The auto-accepted path (built 2026-09-04, for the heuristic classifier)

- `server/consolidation/promotion.py`: `PromotionStore` (idempotency ledger, `promotions` table in the journal DB) + `promote_auto_accepted()` — reads `approval_state='auto_accepted'` rows not yet promoted, calls injected `remember_fn`, isolates per-row failures, `GeminiQuotaExhaustedError` stops the run cleanly. `promote_auto_accepted_memories(dry_run, limit)` MCP tool. 7 tests.

### A real defect the first live run caught (2026-09-04)

Ran `limit=5, dry_run=False`. Of the 4 that landed, **3 were junk** — raw Android logcat lines from a Claude Code debugging session, promoted verbatim.

- **Root cause 1 — `CandidateClassifier` Case C:** classified *any* text with a parseable date and 4+ words as `EPISODIC`, no positive-language requirement. A logcat line always carries an embedded timestamp → cleared the bar → past 0.75 with the exact-date bonus. Fixed: Case C now requires a durable/episodic-language signal; a bare date is not sufficient. Bumped to `v1.1`, reprocessed: `auto_accepted` 182 → 119.
- **Full manual inspection** (all 119, not a sample) found `auto_accepted` still ~90% junk — troubleshooting narration incidentally containing episodic verbs ("fixed", "resolved") plus an embedded log timestamp (Case B). Fixed: a **500-character length guard** downgrades an otherwise-`EPISODIC` candidate to `AMBIGUOUS` (genuine personal statements are concise). Bumped to `v1.2`: `auto_accepted` 119 → 7.
- The final 7: 4 genuinely good, 3 the same short-log-narration pattern under 500 chars — structurally indistinguishable from a real short episodic statement with regex alone. **This residual gap is owned by MS3.5** — a model-based policy over topical windows, not another regex patch. (User's follow-on question — can a debugging session become a genuinely useful memory, distinct from a decision and from the raw paste — is answered by ADR 0005: the existing `episodic` category + a `reasoning_kind` property, a real synthesis over a window, not a one-line label.)
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
- **Scopes (`personal` / `project`)** — one person, one graph, and no `recall` caller that would be scoped. Acceptance test 6 tested a feature with no user. Deferred to MS10 access control (numbered MS9 until 2026-09-30).
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
- **Journal-side supersession for corrections** (2026-09-11, at User's direction, after the initial build) — the first version left `derived_memories.statement` stale while the graph moved on; reworked to match MS3's supersedes convention. `ConsolidationStore.record_correction()` inserts a new `derived_memories` row (`memory_id = "<old>::corrected-<timestamp>"`, `supersedes=<old>`, everything but the statement copied from the old row) and flips the old row to `approval_state='superseded_by_correction'` + `superseded_by=<new>` (excluded from the review queue and promotion eligibility, same as `superseded_by_reasoning`). `PromotionStore`'s graph mapping moves to the new memory_id; the new memory_id gets its own `reviews` row (`approved`, same reviewer) so a promoted memory_id always has a review verdict. `explain()` surfaces `supersedes`/`superseded_by` so the chain is followable.
- **Test coverage:** `tests/test_ms6b_governance.py`, 15 tests against a hand-rolled `FakeDriver`/`FakeGraphiti` (no real FalkorDB needed for the logic).

### Exit gate — ANSWERED (2026-09-11)

No acceptance tests were defined for MS6b the way MS6a had them; the fakes proved the logic, not the real Cypher against Graphiti's actual schema. Two checks, both passed against a real FalkorDB graph:

1. **Read-only, against production.** `explain --graph` (never writes) against real promoted memory_ids, repeatedly during this session's exploration.
2. **Full round-trip, isolated from production.** `scripts/ms6b_exit_gate.py` — seeds one real episode into its own scratch FalkorDB graph (refuses to run against any real graph name) and a scratch SQLite file, then runs `explain_graph` → `correct_memory` (dry run, then applied) → `delete_memory`, asserting against real graph state at each step. **PASSED, 2026-09-11**, all 6 steps, including the supersedes rework (journal row superseded, review verdict carried forward, audit trail split correctly across old/new memory_ids).

A prerequisite for both: the SSH tunnel to the Spark (needed for any local-inference call, including a fresh `add_episode`) had been run manually (`ssh -N -L 12345:127.0.0.1:1234 spark` in a foreground terminal) and gotten killed. Replaced with a `launchd` agent (`~/Library/LaunchAgents/com.cmf.spark-tunnel.plist`, `RunAtLoad`+`KeepAlive`+`ThrottleInterval`, logs to `~/Library/Logs/cmf-spark-tunnel.{,err.}log`) so the tunnel survives logout/reboot and stops depending on a terminal staying open.

### Exercising the tooling on the real corpus found three more things, all fixed same-session (2026-09-11)

- **The 360-cam/eclipse episode was misattributed.** `explain --graph` on a promoted "decision" episode read as User's own gear choice; the full ChatGPT thread showed it was actually a friend's camera purchase, with User advising. First hypothesis (triage dropped a correction turn) was wrong — falsified by the job record (`status=succeeded`, and the episode's own window bounds already included the final turn). Actual cause: the extraction model saw the disambiguating turn ("Draft a sorry msg I can text my friend who's interested in purchasing this...") and still wrote "the user decides..." while linking only one evidence turn — an extractor evidence-linking/subject-attribution miss, not a pipeline bug. Corrected via `correct-memory`; re-extraction on the corrected text went from 1 entity/0 edges to 5 entities/4 edges. `scripts/audit_single_evidence_episodes.py` (new) checked the other 29 single-evidence promoted episodes for the same shape (a subject-correction phrase in a turn just after the evidence turn) — zero flagged, so this looks isolated rather than systemic, though the heuristic doesn't prove the other 29 are correct, only that this particular pattern didn't recur.
- **Entity sense-collapse in the graph, and a worse bug behind it.** `scripts/entity_audit.py` (new, read-only) found 31 of 393 graph entities span more than one `project` bucket — most are legitimate (`Mac Pro`, `macOS`, `rsync`, `Photoshop` genuinely recur across projects, the fabric thesis working as intended). Two were genuine sense-collapses — `Anthropic` (employer vs. AI/API vendor, 6 episodes) and `Phase 1` (a CMF dataviz phase vs. project-epsilon's "Phase 1F") — cleaned with disambiguating summaries. Attempting that cleanup via `edit_memory` surfaced something more serious: it applies `new_summary`/`new_content`/`new_reference_time`/`new_name` to every node in the *connected* context (every entity co-occurring in a matched episode, every episode mentioning a matched entity), not just what `target_query` actually matched — a dry-run targeting `Anthropic` by exact uuid matched 6 entities for a summary write. No existing test caught it. **Fixed:** direct-match uuids are now captured before the connected-context expansion runs, and all four mutation loops (episode content/name/valid_at, entity summary, edge valid_at — same bug, same fix) are scoped to them; `matched_entities`/`matched_episodes`/`matched_edges` in the result are unaffected since they already only reported the modified set. New regression test seeds two co-occurring entities via raw Cypher (no LLM call) and asserts a bystander stays untouched. The two entity cleanups above were applied via direct scoped Cypher specifically to avoid this bug while it was still open.
- **`explain()` was surfacing the wrong approval field.** Its top-level `approval_state` read `derived_memories.approval_state` — set once at extraction time, never updated by review — so a fully reviewed-and-promoted memory still reported `queued_for_review`. The real verdict lives in `reviews.review_state`. Investigated coverage first (at User's request): all 295 promoted episodes have a `reviews` row, no orphans either direction, so the fix was a straight join. `explain()` now returns `extraction_state` (renamed, same pipeline-state value) plus a `review` sub-object (`review_state`/`tier`/`reviewer`/`reviewed_at`/`reason`, `None` if never reviewed), degrading gracefully if a connection's `reviews` table doesn't exist. `graph_name` on `reviews` and inlining the supersedes chain into the join were both considered and deliberately skipped — no multi-graph deployment exists yet, and the chain is already in the journal-only output.
- **A fourth bug, found a day later exercising `correct_memory` for real** (2026-09-11, during the [review-backlog](plan-history.md#corpus--review-backlog-found-2026-09-11-closed-out-same-day) pass): `actions.promote_approved`'s "everything approved and not yet promoted" query read only `reviews.review_state`, with no idea `derived_memories.approval_state` existed. `reviews` is last-writer-wins per memory_id and `correct_memory` never touches it, so the 360-cam episode's original `approved` verdict (from the 2026-09-08 tier-1 pass) stayed on record after its correction superseded it — and `correct_memory` separately clears the *old* memory_id's `PromotionStore` row, since the graph identity moved to the new one. Those two facts combined made the superseded old memory_id look freshly eligible: the very next `promote --apply` run silently re-created it in the graph with its original, wrong, pre-correction content, alongside the correct one. Fixed by joining `derived_memories` into the eligibility query and excluding `rejected`/`superseded_by_reasoning`/`superseded_by_correction` states (`server/review/actions.py`); regression test `test_superseded_by_correction_is_not_reeligible` reproduces the exact sequence. Cleanup: the wrongly-revived episode removed from `mem-fabric-local`, its stray promotion row deleted.

### What was left — see [Backlog — closed items → Corpus & review backlog](plan-history.md#corpus--review-backlog-found-2026-09-11-closed-out-same-day)

MS6b's tooling is done; what it surfaced about the rest of the corpus (27 never-reviewed v0.1 tier-1 episodes, 981 deliberately-unpromoted tier-2 episodes, the 25,961-row heuristic pile, and a stale `cmf_test` vector index unrelated to any of this) is tracked there, not here — it's ongoing corpus/review work, not a milestone with a fixed exit gate.

---

## MS6c — MCP server cross-agent verification

**Goal:** Verify the CMF MCP server actually works, end-to-end, as an installed connector inside the real client apps User uses — Claude Desktop (Cowork mode, Code mode), Gemini Spark, ChatGPT — and correct `docs/CLIENTS.md` against reality. Subsumed [MS4a](#ms4a--mcp-boundary-capture-claude-desktop)'s outstanding live cross-harness test.

### The OAuth finding

Phases 3/4 went in assuming a static bearer token would be enough and said explicitly *don't build OAuth unless it's actually needed*; the milestone's own **Risk** line said the opposite (*"Gemini Spark's DCR/OAuth requirement is an unknown until tested"*). The risk register was right. Neither ChatGPT's nor Gemini's connector UI has a field for a static header — both drive Dynamic Client Registration + authorization-code/PKCE, and Claude Desktop's own connector flow turned out to be OAuth-only too. CMF grew a real single-user authorization server it was explicitly scoped not to build (`server/core/oauth_provider.py`, `oauth_store.py`, `http_auth.py`), merged in [PR #6](https://github.com/username/context-memory-fabric/pull/6) (2026-09-16). The static-token path survives for scripted/direct access; the two are mutually exclusive at the transport level. This was the milestone's most load-bearing finding — worth recording as "the risk register beat the task list," not a flat "we were wrong."

### What was verified, per client

- **Claude Desktop, both tabs.** Each tab does its own OAuth DCR handshake and gets its own client credential — no shared registration (`oauth_clients` grew from 6 to 8 `Claude`-named registrations across the session). **Code mode:** 17/17 tools discovered (10 base + 7 gated behind `LLM_WIKI_PATH`/`_config.knowledge_enabled`); `get_context`/`search_wiki`/`recall_mem`/`capture_health` all functional; a full `remember`→`recall_mem`→`edit_memory`→`recall_mem`/`get_context` write/correct round trip proven (`ms6c_verification_test_2026_09_16`, ALPHA→BRAVO). **Cowork mode:** the same 4 read tools called live, correct output, Markdown rendered cleanly in the chat UI (the one thing Code mode's raw JSON results couldn't stand in for); one transient Cloudflare 502 on `search_wiki`, succeeded on retry.
- **Gemini Spark.** Connected via 3 `Google` DCR registrations (2026-09-14). Functional pass initially thin (2 read calls, no writes) — closed out via the MS4a cross-harness test below, which added a real `remember` call.
- **ChatGPT.** Connected via 1 `ChatGPT` DCR registration (2026-09-14). Broadest pass of any client — 25 real tool calls across 6 tools including `remember` and `propose_wiki_update` (the call that produced `prop_20260916_125736_a33f295a`, later applied via MS6d).
- **MS4a's cross-harness write/correct — done for real**, moved to [MS4a's own entry](#ms4a--mcp-boundary-capture-claude-desktop) rather than duplicated here.

### Corrections found while building

- **The Cowork-vs-Code "distinct identity" claim was wrong as originally interpreted.** An early check found `claude_desktop` (7 journal events) and `claude_code` (4) as separate harness buckets and concluded they "resolve distinctly, confirmed" — read as proof the two tabs are individually identifiable. Direct counter-evidence from a real Code-mode session (2026-09-16): every tool call it made logged under harness `claude_desktop`, not `claude_code`. Traced to `server/capture/identity.py:36-45` (`resolve_harness`): the harness field comes purely from the connecting MCP client's self-reported `client_info.name` string at handshake (`claude.*desktop`/`claude.*ai` → `claude_desktop`, `claude.*code` → `claude_code`) — never from transport, tab, or OAuth client. The two buckets are real, distinct strings, but it's unproven that the 4 `claude_code` events ever came from Desktop's Code tab specifically, as opposed to some other client announcing a `claude...code`-shaped name. Left as an open question in [Backlog](plan-active.md#backlog), not a blocker — it doesn't affect whether the server works, only whether Phase 1 vs. Phase 2 activity can be told apart after the fact in the journal.
- **`edit_memory` leaves stale fact edges behind on correction.** Confirmed by direct Cypher query against `mem-fabric-local-wiki`: both MS6c test episodes still carry their pre-correction `RELATES_TO` fact edges after being corrected via `edit_memory`. The episode's own content node updates correctly and immediately (`recall_mem`/`get_context` both surface the corrected body) — but Graphiti's originally-derived facts are never re-extracted or superseded, so a query surfacing facts rather than raw episode content can show stale wording. What first looked like User's Cowork pass turning up "duplicate" results was this: multiple distinct, differently-worded facts from one original extraction pass, one of them now describing a state the episode no longer states. Filed to [Backlog](plan-active.md#backlog).
- **A chat-side "success" message is not proof a write landed.** Gemini Spark's first `remember()` attempt returned a plausible "Saved... Key: ... Timestamp: ..." confirmation with zero trace in the journal or FalkorDB — see [MS4a](#ms4a--mcp-boundary-capture-claude-desktop) for the retry that worked. Same class of issue as the transient Cowork 502 and the "error 1076"s User hit creating Spark sessions — the shared OAuth/tunnel path this milestone put every client onto has occasional real flakiness, filed to [Backlog](plan-active.md#backlog) rather than chased down here.

### Exit gate — ANSWERED (2026-09-16)

Does the MCP server actually work, end-to-end, inside Claude Desktop (both modes), Gemini Spark, and ChatGPT, and does `docs/CLIENTS.md` reflect that reality? **Yes.** Every client family has a live, evidenced functional pass; `docs/CLIENTS.md` reflects the OAuth/DCR reality for all 4 as of 2026-09-16 (Cowork-vs-Code note in §1, dedicated ChatGPT/Gemini Spark subsections in §4). MS4a's cross-harness test — folded in as this milestone's Phase 3 — passed for real. Two real findings filed to Backlog rather than blocking (above). Deliberately left undone as low-value polish, not gaps: `remember()` specifically exercised from Cowork, and a CLIENTS.md §1 wording pass on restart behavior.

---

## MS6d — Durable-knowledge proposal review

**Goal:** Make `propose_wiki_update` a loop that closes. A proposal could be created and then nothing — no way to list, read, approve, reject, or apply one, so every proposal ever made was inert. The unbuilt half of [Milestone 6](ROADMAP.md#milestone-6--build-memory-review-and-governance)'s *"proposing durable-knowledge changes"* deliverable — MS6a/MS6b built the episodic review path, nothing had built the durable-knowledge one.

**Why now:** Found 2026-09-16 when User created a real proposal and asked how to review it — 76 proposals sat in `wiki-proposals/`, every one `pending_review` since 2026-09-01.

**MCP-only, by decision (User, 2026-09-16).** An early draft put the mutating half behind a CLI on the MS6 precedent; rejected — a wiki proposal is one diff against one file, chat is the better review surface than a terminal for that, and CMF is meant to be consumed via MCP. The safety property that motivated a CLI elsewhere is preserved inside MCP by splitting the decision from the write.

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
5. **Full 76-proposal backlog triaged.** 1 applied, 75 rejected — 73 were `propose_wiki_update` tool-development smoke tests against a path obsoleted by the 2026-09-03 reorg, 1 was the superseded sibling above, 1 was a real scope-taxonomy draft rejected as premature against the standing defer-scopes-to-MS10 decision (numbered MS9 until 2026-09-30). Zero deferred with no reason recorded.

### Exit gate — ANSWERED (2026-09-16)

Can durable knowledge be proposed, reviewed, and promoted into the corpus entirely through MCP, without a shell — and does the approve/apply split actually hold, with no path from a single tool call to a canonical write? **Yes**, on both counts, live-verified against the real 76-proposal backlog, not just tests.

## MS7b — Wiki-derived entity layer + enriched episode bodies (experiment, 2026-09-13)

**Status:** Experiment on branch `ms7b-wiki-entities`. Builds into a new graph (`mem-fabric-local-wiki`); the current graph is preserved untouched as `mem-fabric-local-ep`. Adopt-or-discard is decided at the exit gate, not before.

**Restored to `main`'s plan 2026-09-16.** This section was written on the `ms7b-wiki-entities` branch and went with it when MS6c's work was split onto `main`, so for a few days the milestone existed in code and in no plan anyone reading `main` would see. The branch stays parked — **the adopt-or-discard decision is still open** (Phase 5 found `mem-fabric-local-wiki` does not beat `mem-fabric-local-ep` on retrieval, and User has not ruled). `FALKORDB_DATABASE` points at `mem-fabric-local-wiki` as the *interim* default in the meantime, which is safe because it is a strict superset of `-ep`, not because the decision went that way.

**2026-09-13 (User):** hold the 415 non-wiki singletons in a candidate area (not delete); do the Phase 1 rename; proceed with the full plan. In progress — see per-phase status below.

**Goal:** Stop episodes defining the graph's vocabulary. Derive entity nodes from the LLM Wiki's heading hierarchy, keep the hierarchy itself as structure, and use wikilinks as edges — then attach episodes onto that backbone. Separately, and first, stop discarding two-thirds of each episode's extracted reasoning at the promotion boundary.

**Why now:** User observed that many graph entities are irrelevant. Measured on `mem-fabric-local` (465 episodes / 701 entities / 523 `RELATES_TO`): **585 of 701 entities (83%) are mentioned exactly once**, and sampling them returns `table`, `claim`, `set -e`, `window margin`, `search icon`, `defined paths` — episode-local nouns that contribute nothing to traversal.

### What the measurements changed about the approach

Four findings, in the order they were made. Each one redirected the design, so they are recorded rather than just their conclusions.

1. **Wiki titles/links are not an entity source.** Note titles + `[[wikilink]]` targets (785 distinct terms) match **11 of 701** graph entities — 2%. The wiki's link graph is *topic*-level (`Interlock`, `Home-Project`, `Observables 2026`), not *thing*-level. Seeding from titles alone would discard `Photoshop`, `macOS`, `GitHub`, `Mac Pro`, `Obsidian`, `Cursor`, `Anthropic`.

2. **Headings are the node source; links are edges** (User's correction). `WIKI/` + `REPORTS/` + `TO-RESEARCH/` carry 2,355 headings (H1 195 / H2 1,300 / H3 724 / H4 105 / H5 31) → **1,736 sections after boilerplate filtering** (`Sources` ×121, `Open Questions` ×56, `Summary / TL;DR` ×34) → 1,469 distinct labels. Edges: **2,049 structural parent→child**, plus **1,728 wikilink references** of which 93% resolve to a real note and **99.8% are anchored to a specific section**. That is ~3,700 edges over ~1,700 nodes, against today's 523 over 701.

3. **Full headings make bad node names; decompose them** (User's correction). A heading like `Why Gemma 4 12B is especially suitable artistically` cannot name-match an episode, which would force episode attachment onto embedding KNN and abandon Graphiti's native resolution. Decomposed (`Gemma 4 12B` (model) + `Art` (domain)) it matches directly. Verified on real headings: `CorpC Vision Pro Status (May 2026)` → `CorpC Vision Pro`; `3A. Install Docker Desktop` → `Docker Desktop`; `Protocol Layer: MCP + A2A` → `MCP`, `A2A`; `What to raise with ColleagueB` → `ColleagueB`. Numbering, dates and framing words strip cleanly.

   **Decompose from heading + section lede, not the heading alone.** Coverage of the 701 existing entities:

   | Source text | all | ≥2 mentions | ≥3 mentions |
   |---|---|---|---|
   | heading only | 16% | 36% | 57% |
   | **heading + first 25 words** | **28%** | **58%** | **80%** |
   | full section body | 34% | 62% | 86% |

   Section bodies are median 74 words (81% between 21–400), so they are also a natural chunking of the corpus — which incidentally serves the Backlog's `search_wiki` semantic-retrieval item.

4. **Episode capture is lossy at the promotion boundary, not at extraction.** `ReasoningEpisodePolicyV1` extracts `statement`, `driving_question`, `rationale`, `alternatives`, `status`, `thread_key`, and all of it is persisted (the latter fields packed into `derived_memories.reason`). But both promotion paths call `content=row["statement"]` ([promotion.py:413](../server/consolidation/promotion.py#L413), [promotion.py:583](../server/consolidation/promotion.py#L583)). Across all 465 promoted episodes: `statement` averages **188 chars**, the never-sent `reason` averages **406** — 98% carry a driving question, 98% a rationale. **~68% of extracted reasoning never reaches the graph.**

   Concretely: an episode whose `statement` ends "...an artifact of the SMB/NAS filesystem" drops a `reason` naming **QNAP** — and `QNAP TS-264` is in the `Storage-NAS` wiki note. The cross-channel link this whole milestone depends on was being severed by one field selection.

### Decisions taken (User, 2026-09-13)

- **Enriched episode body = `statement` + driving question + rationale. Alternatives excluded** — "options considered and rejected" reads as fact once it is in a graph.
- **Extraction model = `openai/gpt-oss-20b`** for now. See the A/B below.
- **No episode-mention threshold for wiki-supported entities.** User's objection: a mention threshold applied to terse summaries measures extraction failure, not relevance. Confirmed — 116/465 episodes (25%) extracted **zero** entities and 43% extracted ≤1. Crossing wiki support against recurrence:

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

The 25% zero-entity rate disappears on the 20b. The 122b run was **stopped before completion at User's instruction**, so there is **no quality comparison between the two models** — only the throughput and truncation profile, which was already decisive (it reasons in proportion to input length, so enriched bodies make it worse). Revisit with a 6,000-token cap on ~10 episodes if extraction quality is ever suspected.

**Known limitation of the 20b result:** the "0% >4-word fragments" metric overstates quality. The junk changed shape rather than disappearing — it now emits generic single nouns (`wall`, `rumors`, `planet`, `tabs`, `vendor`, `Activities`, `staff group`) alongside good entities (`NVIDIA Spark`, `OCLP installer`, `Ars Electronica`, `Slack`). That is exactly the shape the wiki registry and stoplist are meant to catch, so it is a known input to Phase 4, not an unmeasured risk.

### Architecture

Three layers in `mem-fabric-local-wiki`:

- **`:Section`** — 1,736 nodes from the heading hierarchy, joined by 2,049 `CONTAINS` edges. Carries `wiki_path`, heading level, and the lede.
- **`:Entity`** — decomposed from heading + lede. This is the surface episodes attach to, by name, via Graphiti's native resolution.
- **`:Episodic`** — the 465 promoted episodes, replayed from the journal with enriched bodies. Unchanged 1:1 with promoted reasoning episodes, so `recall_mem`'s vector arm, `_resolve_episode_index`, `tag_projects.py`, `entity_audit.py` and ledger-replay DR all keep working.

Edges: `Section-[:CONTAINS]->Section`, `Section-[:MENTIONS]->Entity`, `Section-[:REFERENCES]->Note`, and the existing `Episodic-[:MENTIONS]->Entity` / `RELATES_TO`. Retrieval path becomes `episode → entity → section → wiki note`.

**Wiki notes are NOT ingested as episodes.** Entities are seeded directly (`EntityNode.save()` / `add_triplet`), so the episodic layer stays pure.

**On provenance:** the wiki is itself AI-generated (User, 2026-09-13), so this is LLM output extracting from LLM prose. The quality argument is not provenance but **redundancy** — an entity earns retention by appearing in a curated section *or* recurring across episodes, two independently-generated channels. Neither is trusted alone.

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
- **`--provider gemini` fallback added** (User, 2026-09-13, while the Spark was down): this step is pure text generation, no embeddings, so it isn't provider-locked the way seeding/replay is — reusable regardless of which model embeds the results later. Builds its own `GeminiRateLimiter` (real chain + budgets) rather than `get_default_rate_limiter()`, which is provider-aware and returns an unmetered stand-in whenever `CMF_LLM_PROVIDER=local` — correct for production, useless here. Off by default; spends User's real quota only when passed explicitly. **Used for real once** to unblock this step: full 1,939-section corpus in ~6 minutes, **1,345 distinct entities**, 5 stoplist hits, 131 of 500 daily calls spent (split across the two-chain models) — comfortably inside budget. Output: `imports/state/wiki_entities.json`.

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
- **Grading caveat, stated plainly:** single-pass, by me, not independently cross-checked the way the original MS7 draft was reviewed by User before being treated as final. A few borderline calls (partial-credit judgment on incomplete-but-not-wrong answers) could each move the mean by ~0.03; the gap between the two `both` means (0.07) is within range of that noise. The **memory-arm gap (0.27) is larger and reads as a real effect**, not grading noise.

### Acceptance tests

1. ✅ Enriched bodies measurably raise entity yield on the real corpus: 2.55 entities/episode across all 465 (2.7 for the `qwen3.5-122b` cohort specifically) — the A/B's 4.97 prediction was on 30 episodes via a simpler prompt than graphiti's real extraction path; the real-pipeline number is lower but the direction holds. Zero-entity rate (25.8%) did **not** improve over the pre-migration baseline for this model — recorded honestly in Phase 3, not glossed over.
2. ✅ `mem-fabric-local-ep` untouched since Phase 1 — never re-opened by any Phase 2–5 script (all of which target `mem-fabric-local-wiki` explicitly).
3. ✅ Phase 2 builders re-run clean; `build_wiki_sections.py` is zero-LLM and deterministic, `build_wiki_entities.py` checkpoints/resumes.
4. ✅ Duplicate-entity rate measured directly (8.6%, 188 entities) and fixed via the Phase 4 merge — not just measured, corrected.
5. ❌ **MS7 eval on `-wiki` did not reach `-ep`** — the one acceptance test that didn't clear, and the one the exit gate below turns on.

### Exit gate

*"Does a wiki-structured entity layer plus enriched episode bodies retrieve better than the episode-derived graph on the same graded queries — and is the entity set one User recognises as relevant? If the answer is only 'enriched bodies helped,' that is a real result: ship Phase 0 to the existing graph and discard the rest."*

**That is where this landed.** The entity set is real and recognizable (Phase 4's audit confirmed no sense-collapse), but retrieval quality on the graded instrument did not improve — if anything, the memory arm alone measurably regressed. Per the exit gate's own pre-committed criterion, the honest recommendation is: **adopt Phase 0 (enriched episode bodies) on `mem-fabric-local-ep` directly** — that part is model-agnostic, already validated end-to-end in Phase 3's real replay, and costs nothing to keep — **and treat the wiki-structured entity/section layer as a documented, working, but not-yet-adopted experiment.** Whether to pursue tuning `recall_mem`'s fusion constants against the new graph shape (a real, separate follow-on, not a quick fix) or to set `mem-fabric-local-wiki` aside as-is is User's call, not a default this doc should assume.

**Effort:** 2–3 sessions estimated; actual was closer to 4, almost entirely in Phase 3's diagnosis work (two real infrastructure failures — a flaky Spark tunnel, a memory-pressure model-eviction issue — and one real architecture bug — the `group_id` mismatch) rather than in the phases themselves.
**Risk:** Realized, not just estimated. The dedup-search-timeout risk flagged going in never manifested (0 failures across the full 465-episode replay); the risks that did bite weren't on the original list, which is itself a useful note for scoping the next experiment like this one.

### Closeout — adopted, not by re-running extraction (2026-09-17)

**Decision:** User chose the episode-derived lineage (`-ep`) over the wiki-structured entity/section layer (`-wiki`), per the exit gate's own recommendation above. But the mechanism differed from the plan as written — re-running a full enriched-content extraction against `-ep` (the ~4.6h path Phase 3 already paid for once) turned out to be unnecessary.

**Found first: `-ep` was not actually frozen since Phase 1.** Direct comparison of every episode by name across both graphs (2026-09-17): `-ep` (467 episodes) and `-wiki` (469) share 467 names, and **460 of those 467 have different content** — `-wiki`'s version is always the later-written, enriched one (statement + driving question + rationale); `-ep`'s is the bare original statement. Zero episodes are unique to `-ep`. In other words, `-wiki`'s Phase 3 replay already *was* the enriched re-derivation of `-ep`'s full ledger, done once; re-running it against `-ep` directly would have reproduced the same result at 4.6h of cost for zero new information.

**So the close-out became clone-then-prune, not re-extract:**
1. `GRAPH.COPY mem-fabric-local-wiki mem-fabric-local` — reusing the graph's original pre-Phase-1-rename name deliberately: the SQLite `promotions` ledger already has 465 `succeeded` rows keyed to exactly that name (from before the rename), so this made a separate ledger-backfill step unnecessary — future `promote_reviewed` runs against `mem-fabric-local` correctly see those 465 as already done, with no extra bookkeeping.
2. Verified the copy matched `-wiki` exactly (469 Episodic / 2,075 Entity / 2,355 Section / 434 Note / 31 Project / 843 `RELATES_TO`) before changing anything.
3. **Collapsed the `:Section` layer's provenance up to `:Note` before deleting it**, rather than discarding it: `MERGE (Note)-[:MENTIONS]->(Entity)` and `MERGE (Note)-[:REFERENCES]->(Note)` from the existing Section-level edges (2,023 and 1,144 distinct pairs respectively, deduped; 9 self-referencing note pairs dropped as meaningless). `CONTAINS` (the heading hierarchy) has no note-level analog and was allowed to just go — it was internal structure to a note, not cross-note information.
4. `DETACH DELETE` all 2,355 `:Section` nodes. Zero entities were orphaned by this (checked first: every entity otherwise reachable only via a deep section was also reachable via an episode or a shallower section).
5. **`:Project`/`IN_PROJECT` and the ~1,219 wiki-only entities (mentioned by a `:Note` but by no episode, no `RELATES_TO` fact, no project) were deliberately left in place**, not pruned — see the two Backlog items below. This reflects a live finding, not the original plan: visualizing the result in FalkorDB Browser surfaced a concrete extraction-quality problem (a `Cityscapes` art-project note whose 13 section headings are all camera-technique jargon — `TS-E`, `Scheimpflug`, `ACR`, etc. — with no heading containing the word "Cityscape," so the note never links to the `Cityscapes`/`cityscape` topic entities that exist from other notes; separately, `Tilt` and `Shift` were extracted from a section specifically titled "Zero/Static Configuration" — the one about using *neither* — while the real `Tilt Configuration` section produced `Scheimpflug` instead). Pruning the un-corroborated entities now would have permanently destroyed evidence needed to fix that class of bug later.

**Result:** `mem-fabric-local` (3,009 nodes / 6,319 edges) is now the canonical graph — `.env`/`.env.example`'s `FALKORDB_DATABASE` updated to match. `mem-fabric-local-ep` and `mem-fabric-local-wiki` are both kept, untouched, as historical/rollback snapshots — not deleted.

**Phase 0 (enriched episode bodies) shipped to `main` separately** — [PR #7](https://github.com/username/context-memory-fabric/pull/7) cherry-picks just `enriched_episode_content()` out of the `ms7b-wiki-entities` branch, isolated from the wiki-structured layer that stays undeployed. The `ms7b-wiki-entities` branch itself is kept for now as the historical record of the experiment, not merged wholesale and not deleted.

**Outstanding, filed to Backlog rather than reopening this milestone:** whether to prune the `Project`/`IN_PROJECT` layer (an open question since before this experiment started — see [Backlog](plan-active.md#backlog)) and the wiki-entity-extraction quality gap found while inspecting the pruned result.

---

## MS4a2 — Cowork live-session episode/wiki capture (2026-09-18)

**Goal:** Auto-generate real episodes (and durable wiki content) from live Claude Desktop/Cowork conversations, without a manual export/import round trip.

**Why now:** User's actual near-term priority, ahead of MS4b. Cowork has no local transcript and no hook API (`docs/ROADMAP.md`'s Milestone 4 section) — confirmed architectural limit, not a gap to design around — so the only levers are server instructions and new tools the live model can call using what it already has in context.

**Terminology note (worth stating once, since the names collide):** `capture_note` (an MCP tool that existed until 2026-09-18, see below) wrote a bare marker to the *journal* only, not an episodic memory. A `:Note` *graph node* (wiki-derived layer, `seed_wiki_graph.py`) is a different concept — it represents one actual Markdown file under `LLM_WIKI_PATH`, a real durable wiki doc. `capture_session` (below) is a third thing.

**`capture_note` removed the same day (User, 2026-09-18).** Checked what it actually did before deciding: its journal row was never read back by any retrieval path (`search_wiki`/`recall_mem`/`get_context` all read Graphiti or the wiki files, never raw journal events) and the only mechanism that could ever surface it — offline reasoning-episode consolidation, which treats any `actor_type="user"` event as a candidate turn regardless of `event_type` — has no scheduler and has never run against live capture. The real journal confirmed this wasn't a live behavior change: **zero `capture_note` events existed** at removal time. `capture_session`'s confidence floor (0.4-0.6, "inferred from terse turns or mostly from context") covers the vague-checkpoint case `capture_note` might have been reached for, at least landing in a review queue instead of a dead end. Removed: the MCP tool (`server/mcp.py`), `capture_manual_note()` and `EVENT_TYPE_CAPTURE_NOTE` (`server/capture/middleware.py`), its dedicated test, and every doc/test reference (`README.md`, `docs/CLIENTS.md`, four hardcoded tool-count/set assertions, the MCP contract fixture). Tool count: 18 → 17.

### Design: one `capture_session` tool, routes to either destination

One tool call at a checkpoint; the model tags each item `destination: "episode" | "wiki_proposal"` rather than needing two separate tool calls:

- `destination="episode"` — reuses `ReasoningEpisodePolicyV1`'s existing extraction rubric (`server/policies/reasoning_episode_v1.py`'s `_SYSTEM` prompt, lines 54-105: 8-way `reasoning_kind` taxonomy, WHAT COUNTS exclusions, confidence bands) adapted into the tool's own docstring, so live self-extraction applies the same bar offline windowing does. Implementation, per item: (1) journal the model's own `evidence_text` as a lightweight source event first (harness=`claude_desktop`, redacted via `server/capture/filters.py`) — Cowork's raw turns are never otherwise journaled, so this is the only trace that reaches the journal and gives the episode something real to cite; (2) build a `ReasoningEpisode` (`server/policies/protocols.py:113`) from the item's fields; (3) `ConsolidationStore.record_reasoning_episode()` (`server/consolidation/store.py:267`) with `policy_name="cowork_live_v1"`, `policy_version="0.1"` (distinct provenance from offline windowing, same staging path) and `approval_state` from the shared `reasoning_auto_accept_threshold` config (see Review posture below).
- `destination="wiki_proposal"` — no new plumbing, an internal call to the existing `propose_wiki_update(target_path, proposed_content, rationale, source_context=evidence_text)`. Same review path as every other proposal.
- Routing rule (stated in both the tool docstring and `SERVER_INSTRUCTIONS`): *episodic* = something that happened/was decided/was concluded in this conversation; *wiki-worthy* = durable, reusable, still-true-read-cold-later knowledge.

### Review posture (User, 2026-09-18)

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
- [x] (User, outside this repo) Custom Claude instructions redrafted to reference `capture_session` (and drop the `capture_note` line that was briefly drafted, once the removal decision was made) — his to paste in.

### Exit gate — ANSWERED (2026-09-18)

**Manual dry run, run live in a real Cowork session, both destinations exercised for real:**

- **Episode path:** "we just decided to cap the review-batch size at 50 episodes per pass... wrap up this checkpoint and capture that decision." Staged for real — `episode-proposals/tier1/cf2e6189....json`: `policy_name=cowork_live_v1`, `reasoning_kind=decision`, `confidence=0.95`, `approval_state=queued_for_review`, a real evidence event journaled and cited. Verified by content grep (the mirror's filename is a content hash, not searchable by memory_id) and by confirming no matching wiki proposal was also created.
- **Wiki path:** asked to document the episode-proposals mirror's own layout as a durable wiki proposal. Created `prop_20260918_163113_0dc89442` for `WIKI/projects/Context-Memory-Fabric/Episode-Proposals-Mirror.md`, staged only, `LLM_Wiki` untouched — confirmed via direct file read.
- Both test items rejected afterward via `reject_episode`/`review_proposal` (real memory, not meant to be kept) — both correctly moved to their `rejected/` subfolders, confirmed by path check.

**Real finding, not blocking:** for the episode-path test, the model's own stated plan said it would route the item as `destination="wiki_proposal"` ("a durable operating rule"), then the actual tool call used `destination="episode"`. The content is genuinely borderline (a decision that is also a standing rule), so this is a real gap in the routing rule's disambiguation for that shape of content, not a bug — filed to Backlog rather than block this exit gate on it, since both actual writes (episode and wiki, in the two separate live tests) landed correctly and safely regardless of which one time chose which path.

**Separate finding, addressed the same day:** the live test also surfaced that Claude Desktop mirrors captured facts into its own local per-project memory (`~/.claude/projects/*/memory/*.md`), independent of CMF — confirmed the mirrored file exists correctly, but this creates two independently-writable copies of the same fact with no reconciliation. User's custom Claude instructions were updated with an explicit tie-break: CMF is authoritative over local project memory when the two disagree.

**Effort:** 1-2 sessions (one new tool + docstring + tests, no new subsystem) — actual: 1 session build + live verification the same day.
**Risk:** Low, and realized-low: no new storage, reused `record_reasoning_episode`/`propose_wiki_update` as-is, both real writes landed exactly as designed on the first live try.

**Correction #1 (same day): mirror content didn't match its own location on review.** User spotted it directly — the rejected test episode's mirror file had moved to `rejected/` but its own `approval_state` field still read `queued_for_review`. Root cause: `move_episode_mirror()` was a pure filesystem rename, never a content rewrite. Fixed: it now rewrites `approval_state` to the terminal value plus `reviewed_at`/`reviewer`/`review_reason` on move — the same single-evolving-field convention `WikiProposal.status` already used (which is why the wiki side never had this bug). Retroactively fixed the real test file; 2 new test assertions added.

**Correction #2 (same day): no MCP-exposed episode review, at parity gap with MS6d.** User asked why I hadn't used an MCP tool to reject the wiki test, then asked directly whether an episode equivalent existed — it didn't. `approve_episode`/`reject_episode`/`tier1_review_queue()` were CLI-only (`server/review/cli.py`); MS6d built a full `list`/`get`/`review`/`apply`/`bulk_reject` MCP lifecycle for wiki proposals but the episode side never got the equivalent. Built the same day: `list_episode_proposals`, `get_episode_proposal`, `review_episode` (approve/reject/defer), `bulk_review_episodes` (mixed verdicts in one call, strictly more capable than `bulk_reject_wiki_proposals`'s single-reason-only shape, since it wraps the already-existing `apply_verdicts()`). Reads go through the `episode-proposals/` file mirror (already dual-policy-aware), not `ConsolidationStore.query_reasoning_episodes()` directly — see the filed finding below for why. Tool count 17 → 21.

**Found while building the above, filed to Backlog rather than fixed on the spot:** `ConsolidationStore.query_reasoning_episodes()` hardcodes `policy_name = 'reasoning-episode'`, so `tier1_review_queue()` (and the CLI built on it) never sees `cowork_live_v1` rows at all. Not a one-line fix — `reasoning-episode` and `cowork_live_v1` version themselves independently (0.3 vs 0.1), so the function's single shared `policy_version` parameter would need restructuring to a per-policy-name version map, which also touches `tier1_review_queue()`'s signature and several existing tests. Real gap, deliberately not rushed into today's change. *(Fixed 2026-09-19 in [PR #16](https://github.com/username/context-memory-fabric/pull/16) — `policy_name` became a parameter everywhere; see [MS4b → Phase 4](#ms4b--claude-code-transcript-adapter-build-backfill-re-derivation-review-2026-09-18--09-22).)*

---

## MS4b — Claude Code transcript adapter: build, backfill, re-derivation, review (2026-09-18 → 09-22)

**Closed 2026-09-28.** This section covers the whole milestone: the adapter build, the scoped backfill, the `ExtractPolicy` re-derivation, the review-by-conversation pass, and then (below) the live hook test, the launchd poller, its first ongoing-capture run and that batch's review.

**Scope correction (2026-09-18).** MS4b was originally scoped to "the standalone Claude Code CLI". A direct filesystem check found that Desktop's **Code tab**, User's actual daily driver, writes the same JSONL transcript format to the same `~/.claude/projects/<project-slug>/<session-uuid>.jsonl` path. The parser reads by path and format, not by which binary wrote the file, so one adapter covers both. Cowork has no transcript at all; that gap is MS4a2's.

### Available surfaces (measured)

Every transcript line is JSON with a `type`: `user` (a real turn, or a `tool_result` list), `assistant` (`text`/`thinking`/`tool_use` blocks; `thinking` often carries the real substance), plus Desktop-only bridging types (`bridge-session`, `ai-title`, `atis-latch`, `frame-link`, `pr-link`, …) that are skipped and logged, never raised. I ran the parser over all 24 files (94.0MB) under this repo's project slug: 33,353 lines, 13,143 events emitted (**~140 kept events/MB**), 0 unparseable. The skip breakdown matched the `KNOWN_SKIPPED_TYPES` docstring exactly.

### Dedup against already-imported exports

`claude`-harness export events run through `2026-09-04T00:36:13Z`. A full-history backfill would re-walk the same conversations under harness `claude_code`. `compute_event_id` does not catch this, because the harness is part of the hash. Mitigation: `backfill --since` date cutoff. **Known gap:** there is no persisted per-harness cutoff registry, so the caller must pass `--since` explicitly every time. *(Closed 2026-09-28: `backfill` falls back to the `CMF_CLAUDE_CODE_BACKFILL_SINCE` env var, and the unattended poller runs `tail`, which resumes from `claude_code_tail_state` byte offsets and never takes a cutoff. See [Backlog — closed items](#persisted-backfill-cutoff-found-2026-09-18-closed-2026-09-28).)*

### What was built ([PR #12](https://github.com/username/context-memory-fabric/pull/12))

- `server/adapters/claude_code/parser.py`: line → canonical `SourceEvent`, secret redaction via `server/capture/filters.py` before hashing, harness hardcoded to `claude_code`.
- `transcript_reader.py`: byte-offset incremental tailing (`claude_code_tail_state` table). Handles partial trailing lines and truncated/replaced files. Supports `CMF_CLAUDE_CODE_PROJECT_ALLOW`/`_DENY`.
- `worker.py::process_pending()`: tails files, journals events and runs consolidation per touched conversation, under a forced `CMF_LLM_PROVIDER=local`. Local-only is deliberate: one 8MB session is ~30 extraction calls, which would exhaust the Gemini free tier unattended.
- `hooks.py`: a settings.json merge-installer (backs up first, only touches `hooks.<EventName>`) whose hook body launches a detached worker. `cli.py`: `status`, `tail --once`, `backfill --all-projects [--since]`.
- 27 tests. Two real bugs were fixed while testing. First, the parser dropped pure-`tool_result` user turns. Second, `worker.py` imported a `transcript_reader` API that didn't exist (an API mismatch between two build sessions).

### Real backfill + first consolidation run (2026-09-18)

- **Backfill** (evidence only, scoped `CMF_CLAUDE_CODE_PROJECT_ALLOW=context-memory-fabric`, `--since 2026-09-04T00:36:13Z`): 26 files, **12,612 events journaled**, 700 skipped before the cutoff, 0 errors. The other ~859 transcript files on the machine (other projects) were out of scope; that's filed in the Backlog.
- **`ReasoningEpisodePolicyV1` consolidation** over 34 `claude_code` conversations: 660 windows, 219 sent to the model, **379 episodes**, 0 errors, 3194s. That gives **~14.6s per model call**, the first real number for sizing the poller cadence. The largest conversation took ~423s.

### Wiki→doc rename and `ExtractPolicy` (decided 2026-09-19)

**Gap:** the offline windowed pipeline only ever asked for *episodic* candidates. The episode-vs-durable-doc routing judgment existed only in `capture_session`'s live path. **Decision:** don't review the 379 in their episode-only form. Instead, add a new policy and re-derive from the source journal.

- **Phase 1: rename and reorg, no behavior change ([PR #14](https://github.com/username/context-memory-fabric/pull/14)).** `WikiProposal`→`DocProposal`; the six `*_wiki_*` proposal MCP tools hard-renamed to `*_doc_*` (no aliases); `capture_session`'s `destination='wiki_proposal'`→`'doc_proposal'`; on-disk `wiki-proposals/`→`doc-proposals/`. `server/wiki.py`/`corpus.py`/`extractors.py`/`knowledge_files.py` moved under `server/providers/wiki/`. `search_wiki`/`LLM_WIKI_PATH` were deliberately left alone, since generalizing them is MS5's job. 490 tests passed.
- **Phase 2: `ExtractPolicyV1` ([PR #15](https://github.com/username/context-memory-fabric/pull/15)).** Subclasses `ReasoningEpisodePolicyV1` under a distinct identity, `name="extract"`. One model call can emit both episodes and `DURABLE_CANDIDATE` doc proposals; `run_reasoning_consolidation()` routes the latter to `create_doc_proposal()`. Live-smoked against Spark `json_schema` mode. 499 tests passed. (Later renamed to `server/policies/extract.py` in [PR #17](https://github.com/username/context-memory-fabric/pull/17), with the `_vN` filenames dropped.)
- **Phase 3: re-derivation.** The 379 old rows were rejected by exact `memory_id` via `apply_verdicts()`. `bulk_reject()` was avoided because it would also have caught unrelated `reasoning-episode` rows. One real bug was fixed along the way: passing an explicit `db_path` string wrote mirrors to `imports/journal/episode-proposals/`, where no tooling reads them. **Lesson: construct stores with `db_path=None`.** Result of the re-run: 34/34 conversations, 5280s (~24.1s/call), **436 episodes + 69 doc proposals**.
- **Phase 4: review by conversation ([PR #16](https://github.com/username/context-memory-fabric/pull/16)).** Removed three hardcoded `policy_name='reasoning-episode'` filters (`query_reasoning_episodes()`/`tier1_review_queue()`, `review_queue()`, `_hint_other_versions()`) and added a `--policy-name` CLI flag. `conversation_id`/`harness` are now carried on episode mirrors (2,059/2,059 backfilled exactly) and on doc proposals (the 69 existing ones were backfilled by timestamp correlation and flagged `source_conversation_id_inferred`). Added the new `list_review_conversations` MCP tool plus `conversation_id` filters on the list tools. 515 tests passed.
- **Follow-ups ([PR #17](https://github.com/username/context-memory-fabric/pull/17), [PR #18](https://github.com/username/context-memory-fabric/pull/18)).** Fixed extraction-quality gaps found while reviewing, which produced the `extract` 1.1–1.3 re-runs. Added the `promote_approved_episodes` MCP tool and fixed a threadmerge harness-naming bug. Tool count is now **23**.

### Review-by-conversation pass (2026-09-20 → 09-22, closed out)

User reviewed every conversation in the `list_review_conversations(harness="claude_code", policy_name="extract")` queue, mostly from Antigravity. Final state, checked against `journal.db` and the mirror directories on 2026-09-22:

- **Episodes:** 1,091 `extract` verdicts, **74 approved / 1,017 rejected**; 0 tier-1 rows pending. Several same-conversation episodes were hand-merged via direct SQL; the procedure and the size limit are recorded under "Backlog — closed items → Recall coverage for manually merged episodes" below.
- **Promotion:** **73/74 succeeded** into `mem-fabric-local`. The one `failed` row is the oversized 39-point merge, replaced by two split episodes that both promoted. It's excluded from future promotion, because its `derived_memories.approval_state` is `rejected` (superseded by the split).
- **Doc proposals:** **0 pending.** 46 applied to `LLM_Wiki` and 211 rejected, across all harnesses. Some early proposals had a wrong `target_path` and no frontmatter and were fixed by hand after apply (filed as a Backlog item).
- **Left unreviewed:** 24 tier-2 `extract` rows (12 finding, 9 investigation, 3 hypothesis), outside tier-1 scope.

### Exit gate: answered by measurement

**How much of a coding session is worth keeping?** Keep full text for `user`/`assistant text`/`thinking`. Keep bounded 1000-char summaries only for `tool_result`/`tool_use`. That's ~140 kept events/MB. After extraction and review, 74 of 1,091 extracted episodes (~7%) were worth promoting.

### Ongoing capture: hooks, launchd poller, first live run (2026-09-22 → 09-23)

- **Live hook-applicability test (2026-09-22).** Desktop's Code tab fires `SessionStart`/`Stop` hooks live with no restart. `install_sentinel_hooks()` ran against the real `~/.claude/settings.json`, and `~/.claude/cmf-hook-sentinel.log` picked up `Stop` timestamps mid-session and `SessionStart` on a fresh session.
- **launchd poller.** `com.cmf.claude-code-poller` (`~/Library/LaunchAgents/local.cmf.claude-code-poller.plist`; the `local.` filename prefix is cosmetic) runs `.venv/bin/python3 -m server.adapters.claude_code.cli tail` every 900s, `RunAtLoad` on, no `KeepAlive`, with consolidation on. `hooks.install_hooks()` as a Stop/SessionStart accelerant was left uninstalled.
- **Wrong policy on the first run, corrected same day.** The poller's first invocation swept all 871 never-tailed conversations on the machine, but under `ReasoningEpisodePolicyV1` (episode-only). `worker.py::process_pending()` was switched to `ExtractPolicyV1`, the ~477 throwaway `reasoning-episode` rows were rejected by exact `memory_id` via `apply_verdicts()`, and the same 872 conversations were re-derived per `conversation_id`. Results: 0 errors, ~2h56m, 599 raw episode candidates + 53 doc proposals. After thread-merge (431 constituents auto-rejected), that left **171 episodes (114 tier-1 + 57 tier-2) + 53 doc proposals across 23 conversations in 5 projects**.
- **Two bugs fixed while setting up that review (2026-09-23).** (1) Episode mirrors were written to `imports/journal/episode-proposals/`, because `process_pending()` built `ConsolidationStore(journal_store.db_path)` rather than `None`. `cli.py` now constructs the store itself, and 538 misplaced mirror files were moved after a backup. (2) `derived_memories.project` was NULL for every `claude_code` row ever produced, because `record_reasoning_episode()` never took a `project`. The new `server/adapters/claude_code/project_slug.py` derives it from the transcript's `project_path` at extraction time; the 599 rows and their mirrors were backfilled. This sweep also covered the backfill of every non-CMF project on the machine (project-gamma, project-delta, ProjectAlpha, ProjectBeta, project-epsilon, …), which closed that backlog item.

### Ongoing-capture batch review (2026-09-23 → 09-24, closed out)

All 5 projects reviewed by project via the `project` filter added to the three list tools on 2026-09-23. Verdicts checked against `journal.db` on 2026-09-28:

| Project | Approved | Rejected |
|---|--:|--:|
| `project-gamma` | 48 | 193 |
| `project-delta` | 17 | 73 |
| `ProjectAlpha` | 9 | 24 |
| `ProjectBeta` | 3 | 14 |
| `context-memory-fabric` | verified clear 2026-09-24 | |

The 24 tier-2 `extract` leftovers from the manual-backfill batch were also cleared on 2026-09-22 (3 approved and promoted as `claude-code-context-memory-fabric-074`..`076`, 21 rejected).

### Closing condition: met (2026-09-28)

The milestone was held open until the ongoing-capture batch was fully reviewed (User, 2026-09-22), not just until the poller had captured a session end-to-end (met 2026-09-23). Both are now true. What the poller has captured since (78 pending `claude_code` episodes from about 12 conversations, 2026-09-23 → 09-27, as of 2026-09-28) belongs to the recurring review pass, not to this milestone.

---

## MS4b-antigravity — Antigravity IDE transcript adapter (unplanned, 2026-09-24)

**Closed 2026-09-28.** Added out of sequence (MS4c = OpenClaw and MS4d = Codex/Gemini came next) because User was already running real review sessions in Antigravity, and the only thing reaching the journal from it was MS4a's thin MCP-boundary capture: one row per MCP tool call, unusable for `ExtractPolicy` windowing.

### What was built

- **`server/adapters/antigravity/`** (commit `c5e5b55`) mirrors `claude_code`'s parser → tail-state → worker → CLI shape. It reads Antigravity's own plain-JSON `transcript.jsonl` per conversation. Antigravity's separate proprietary SQLite/protobuf store was explicitly ruled out, so there is no parsing of undocumented binary schemas.
- **Project tagging, two fixes.** (1, `31204c3`) The extraction pipeline's project derivation was hardcoded to `harness=="claude_code"`. (2, `32c9e31`) 10 of 15 conversations still resolved to `unknown` because the secondary `antigravity-ide` install's `conversation_summaries.db` is a 0-byte file on this machine. A fallback now takes the conversation's dominant `/Users/<user>/{Dev,Documents}/<project>` path from its own `transcript.jsonl`, measured at 24–380 hits for the true workspace against a handful for anything else. The 130 rows and 56 mirror files were backfilled.
- **Ongoing capture (2026-09-24).** `server/adapters/antigravity/hooks.py` installed a `Stop` hook into `~/.gemini/config/hooks.json` (`e40e33d` added the install helper), and `com.cmf.antigravity-poller` (`~/Library/LaunchAgents/local.cmf.antigravity-poller.plist`, 900s) is loaded. The accelerant plus backbone is the same dual pattern as `claude_code`.

### Backfill

The first run was capped (`--until <local midnight>`, leaving live sessions alone): 19 conversations, 6,298 events, 0 errors, giving 31 tier-1 + 12 tier-2 episodes + 13 doc proposals. A second uncapped `backfill --all-app-dirs` picked up 2 more conversations, also with 0 errors.

### Closing decision (User, 2026-09-28)

Closed on build plus operational capture, **without** waiting for review. As of 2026-09-28, 51 `antigravity` episodes are pending, and they join the recurring review pass alongside `claude_code`'s ongoing capture. The `antigravity-ide` empty `conversation_summaries.db` question was closed as not worth investigating, since the transcript-path fallback makes it cosmetic rather than a data gap.

---

## MS4e — Entity extraction quality (forward-only, 2026-09-28)

**Closed 2026-09-28.** `typed-recall` + debris filter adopted in `.env`; exit gate answered in the decisions log. Phase 5 (read-only report on the existing graph) was left optional and belongs to the backlog cleanup item it links to.

**Goal:** New episodes produce a small set of durable, re-findable entities: people, organizations, software and services, AI models, hardware, projects, places and named methods. Implementation debris stays out. Existing nodes are not touched; retroactive cleanup is at most a read-only report (Phase 5).

**Why now:** Measured read-only on `mem-fabric-local` (2026-09-28), `project-gamma` has 398 entities from 49 episodes, and **315 (79%) are mentioned by exactly one episode**. Most of those are file names (`build_traces20.py`), identifiers (`head_idx`, `defaultK`), numbers (`layer 40`, `317MB`, `PID 12955`), local labels (`Option C`, `Phase 7`), generic nouns (`scores`, `structure`) and fragments (`'Depends'`). There are three causes:

1. **CMF's own prompt asks for it.** The pre-MS4e `EXTRACTION_INSTRUCTIONS` asked for "every specific named entity… files and path patterns… settings or parameters… even when mentioned only briefly". It was tuned in Spark Phase 7 for terse ~200-char episodes where qwen returned nothing ~45% of the time, and was never re-tuned as episodes grew. It also said "CURRENT MESSAGE", but `remember()` sends `EpisodeType.text`, whose prompt calls the input `<TEXT>`.
2. **There is no ontology.** `add_episode` never passed `entity_types` or `excluded_entity_types`, so nothing structural filters the output.
3. **Entity yield scales with episode length.** Episodes average 2.6 entities under 1K chars, 8.4 at 1–3K, 16.6 at 3–8K and 42.8 over 8K. Thread merges had no size bound: `claude-code-project-gamma-048` is a 23.5K-char merge of a 74-turn thread and produced 85 entities.

Separately, `correct_memory` called `add_episode` with no instructions at all, so corrected episodes extracted under Graphiti's bare defaults.

### Phases

- [x] **1. Extraction-profile plumbing.**
  - New module `server/providers/extraction_profile.py` owns every extraction kwarg.
  - `CMF_EXTRACTION_PROFILE` selects a profile (validated fail-fast in `server/core/config.py`, default `legacy`):
    - `legacy`: the pre-MS4e text, verbatim.
    - `selective`: new instructions that name the debris to skip, using User's keep/drop examples.
    - `typed`: `selective` plus an 8-type ontology (Person, Organization, Software, AIModel, Hardware, Workstream, Place, Method) with Graphiti's generic `Entity` excluded, so anything that fits no type is dropped.
  - Type names become FalkorDB labels. A project-like entity is therefore `Workstream`, not `Project`, which is CMF's own `IN_PROJECT` target.
  - `remember()` and `correct_memory()` both route through `extraction_kwargs()`.
  - Tests: `tests/test_ms4e_extraction_profile.py`.
- [x] **2. Thread-merge size budget.**
  - `_merge_tier1_by_thread` splits a thread whose statement + driving question + rationale exceeds `THREAD_MERGE_CHAR_BUDGET` (3,000 chars; overridable through `run_reasoning_consolidation(merge_char_budget=...)`) into consecutive `::part<N>` merges.
  - A run of one stays as the original episode. A thread within budget keeps its unsuffixed ids, and a thread already merged whole is not re-split.
  - Tests: `tests/test_ms4e_thread_merge_budget.py`.
- [x] **3. A/B replay (scratch graphs only).**
  - `scripts/rebuild_graph_from_ledger.py` gained `--project` / `--memory-ids-file`. The read-only scorer is `scripts/ms4e_entity_quality.py`, with gold in `tests/fixtures/ms4e/` (User's keep/drop lists plus a held-out drop list the prompt never quotes).
  - **Sample:** 30 fixed episodes (20 project-gamma including project-gamma-048, 10 control). User hand-marked a per-episode keep set, 93 names after revision.
  - **Round 1** (A legacy, B selective, C typed, D typed without `Reasoning:`):
    - B, C and D cut the noise but over-filtered. C found 36 of the keeps, against A's 71.
    - The misses were techniques, formats, generic roles and subject topics. User ruled all of them keeps.
  - **Round 2** added the `typed-recall` profile:
    - A coverage-first prompt that names each thing once, in its canonical name.
    - A 10-type ontology (`PersonOrRole`, `OrganizationOrGroup`, `SoftwareOrService`, AI model, hardware, `Workstream`, place, `TechniqueOrMethod`, `Format`, and `Topic` with at most two per episode).
    - A project entity taken from the episode's `source_description`.
    - A post-extraction **debris filter** (`server/providers/entity_filter.py`, `CMF_ENTITY_DEBRIS_FILTER`). It deletes path, identifier, exception, number, local-label and quoted entities that only the new episode mentions.
    - Episode-content embedding at write time (`CMF_EMBED_EPISODES`, default on), so new episodes join the `recall_mem` vector arm immediately.
  - **Results.** Legacy output swings run to run: A found 71 keeps with 47 noise, and A2 found 49 with 79.

    | Run | Keeps found | Noise | Notes |
    |---|---|---|---|
    | F (typed-recall + filter) | 76 / 93 | 1 | project-gamma entities per episode 12.7 → 6.2 (−51%) |
    | G (production copy: the same episodes replayed onto a copy of `mem-fabric-local`, so resolution sees the real graph) | 71 vs 59 for the production originals | 6 vs 97 | `recall_mem` at parity |
    | H (v2.1 tightening: Topic capped at two overall-subject topics, base64 kept as a Format, docs/README/tests kept out of `Workstream`) | 76 / 93 | 4 | 0 empty episodes (F had 3); unmatched extras 76 → 57; the filter caught 13 |
  - **Production backfill.** All 634 production episodes got `content_embedding` (backup: `mem-fabric-local.pre-epvec-20260928`).
- [x] **4. Exit gate and default flip (2026-09-28).**
  - `.env` sets `CMF_EXTRACTION_PROFILE=typed-recall` and `CMF_ENTITY_DEBRIS_FILTER=1`, and the MCP server was restarted.
  - The `ms4e-*` scratch graphs were dropped.
- [→] **5. Optional. Moved to [MS9 task 1b](plan-active.md#ms9--graph-quality-for-retrieval) (2026-09-30).** Run the Phase 3 report over all of `mem-fabric-local` and hand a noise-candidate CSV to [Entity/edge cleanup pass](plan-active.md#entityedge-cleanup-pass-for-the-cmf-graph-found-2026-09-20-reviewing-conversation-b23f6f7d). No deletions in MS4e.

### Acceptance tests (Phase 3 sample, C or D against A)

1. Entities per project-gamma episode fall by ≥50%.
2. Regex-flagged noise is <5% of new entities.
3. ≥90% of the held-out gold "drop" names are not created.
4. 100% of the gold "keep" names present in the sample are still created.
5. The empty-extraction rate is reported. An episode with no entities still gets its `IN_PROJECT` link via `tag_promoted_episode`.
6. The `recall_mem` side-by-side is no worse than A (User's judgment).
7. `uv run pytest` passes.

### Exit gate

**Which profile becomes the default — `selective`, `typed`, or `typed` without `Reasoning:` in the Graphiti body?**

**Answer (User, 2026-09-28):**
- None of the three. `typed-recall` + debris filter (variant F) won, after one tightening pass (v2.1, confirmed by H).
- `Reasoning:` stays in the body.
- Acceptance #4 (100% of keeps) is not met by any variant, legacy included: the best is 76/93. User accepted that trade for noise falling from ~50–100 to ~1–6 per 30 episodes.

**Effort:** 3–4 sessions, mostly Spark replay time.
**Risk:** Low-medium.
- qwen may swing back to under-extracting (the Phase 7 empty-list problem). The empty-extraction rate is measured for that reason, and `selective` is the fallback if `typed` over-filters.
- The schema change is additive. Typed nodes carry `Entity` plus a type label, and when a typed extraction resolves onto an old untyped node, Graphiti adds the label to it. Old nodes stay valid and the vector dimension is unchanged.

---

## MS5 — Knowledge-provider generalization (2026-09-28)

**Closed 2026-09-28.** Exit gate answered in the decisions log.

**Goal:** Make the LLM Wiki *one* supported knowledge provider rather than a requirement. Roadmap MS5.

**Scope change (User, 2026-09-28):** the second provider that proves the contract is **Gmail** instead of GitHub, tested on the mail User sent in the last month. The plan's earlier note still holds: email is evidence, not curated knowledge. The provider returns what was *said*, dated and attributed, and never presents it as what is true.

### What was built

- **Core contract.**
  - `KnowledgeResult` (`server/core/models.py`) is now load-bearing, with provenance on every result: `provider`, `document_id`, `source_version`, `scope` (access scope), `uri`, `source_timestamp`.
  - New `KnowledgeSource` protocol (`server/core/protocols.py`): `name`, `is_configured()`, `query()`.
  - The wiki provider implements it alongside its existing `search()`, which still backs `search_wiki`.
- **Multi-provider fan-out** (`server/knowledge.py`).
  - Sources come from `CMF_KNOWLEDGE_PROVIDERS` (`module:Class` paths; default is the wiki).
  - Each provider is queried separately and the results are interleaved by rank. Nothing is merged or deduplicated across providers, and no provider outranks another.
  - A failing or unconfigured provider is reported and skipped.
- **Other surfaces.**
  - `get_context` gives every non-wiki provider its own attributed section.
  - New MCP tools: `search_knowledge` (all providers) and `propose_knowledge_change`. The latter routes to a provider's own proposal path; read-only providers say so. `search_wiki` and `propose_doc_update` are unchanged.
- **Gmail provider** (`server/providers/gmail/`).
  - Reads a local snapshot, one JSON file per message, and ranks with BM25 on subject and body.
  - Scope is `private:gmail:<account>`, `source_version` is the send date, and each result links to its thread.
  - It is read-only. CMF holds no Gmail credentials, so the snapshot is filled from outside: `scripts/import_gmail_mbox.py` for a Google Takeout export, or an assistant session with a Gmail connector.
- **Conformance suite** (`tests/conformance/knowledge_source.py`). One set of contract checks, run unchanged against both providers:
  - identity, and the cheap `is_configured()` check;
  - attributed results with scope and version;
  - `max_results`, best-first ordering, stable ids;
  - blank and odd queries;
  - read-only behavior;
  - participation in the fan-out.

### Acceptance

1. ✅ **Second provider passes conformance without changing core.**
   - Gmail passes the same `KnowledgeSourceConformance` suite as the wiki (`tests/test_ms5_gmail_provider.py`, `tests/test_ms5_knowledge_core.py`).
   - The 18 core files (`server/core/*`, `server/knowledge.py`, `server/context.py`, `server/mcp.py`, `server/providers/__init__.py`, `server/providers/wiki/*`) were fingerprinted before the Gmail package existed and were byte-identical after it was added.
   - Gmail is enabled by configuration alone.
2. ✅ **Conflicting documents stay separately attributable.** A fixture test covers this, and the real data has two examples:
   - A wiki job-search note (2026-08-23) reads as an active application, while a 2026-09-18 sent email records its outcome. Both come back with their own provider and date.
   - The 2026-09-04 and 2026-09-10 emails disagree about which Spark models are resident; their version dates show the change.
3. ✅ **Real sent mail.**
   - 61 messages sent 2026-08-28 to 2026-09-28 across 33 threads went into a gitignored snapshot (`imports/gmail/snapshot/`). Only User's own words were kept: quoted history, card and itinerary numbers, and a password in a quoted reply were left out.
   - 9 real queries were run across wiki + Gmail, and Gmail's top hit was on-topic for all 9.
4. ✅ `uv run pytest` passes.

Follow-ons deliberately left out of MS5 (enabling Gmail, a GitHub provider, incremental Gmail sync) moved to [MS11](plan-active.md#ms11--knowledge-provider-follow-ons-from-ms5).

---

## MS8 — Replay and evaluation (2026-09-28 → 09-29)

**Closed 2026-09-29 (User).**

**Goal:** Use accumulated evidence to improve agents and CMF itself. Roadmap MS8.

### What was built

- **Historical snapshots by subtraction** (`server/replay/snapshot.py`).
  - `GRAPH.COPY` of `mem-fabric-local` into a `replay-*` graph, then Graphiti's own `remove_episode` for every episode whose memory had not reached production at the cut-off.
    - "Reached production" is the memory's first successful promotion into any `PRODUCTION_LINEAGE` graph (`mem-fabric-gemini`, `mem-fabric-local` and its two rebuilds).
    - Each episode is matched to its memory by the `memory_id=` in its own `source_description`, not by name (see "Gold keyed by memory id" below).
    - An episode with no memory id (a direct `remember()`) falls back to its `created_at`.
  - Facts that a removed episode invalidated are made current again. The invalidator is the episode whose ingestion was running at the fact's `expired_at`, since ingestion is sequential.
  - MS7b `Note` nodes are kept only if their file exists in the wiki export, and entities only the removed notes mentioned go too.
  - The wiki is exported with `git archive` at the last commit before the cut-off. Its commit window (`wiki_window`) is recorded, and gold wiki files changed inside it are graded `wiki_uncertain`. A next commit that is an `Auto-sync` commit made well after the cut-off counts as exact.
  - The target name must start with `replay-` and can never be a protected graph.
- **Policies** (`server/replay/policies.py`): `edge-only` (the MS7 baseline) and `edge+episode-vector` (production default), switched by env inside a context manager.
  - `SWEEP_POLICIES` also vary the two fusion knobs `recall_mem` reads at call time: the vector arm's size (`vector_k`, production 6) and facts allowed per source episode (`max_per_episode`, production 1 as of 2026-09-29; formerly 2).
  - Sweeps run isolated via temporary patch; production `memory_graphiti.py` is untouched during sweeps.
- **Grading** (`server/replay/grading.py`):
  - gold rank, Hit@k and MRR for memory and wiki;
  - temporal leaks (facts from after the cut-off);
  - superseded facts returned;
  - provenance rate;
  - graded abstention before the gold existed.
- **Export** (`scripts/replay_eval.py`): a report plus a text-free `trajectories.jsonl` by default. Fact text is exported only with a flag.
- **Replay efficiency and progress (2026-09-29):** wiki search paths, including empty results, are cached per query within each snapshot and reused across policies. Memory retrieval still runs for every case/policy pair. Each completed snapshot/policy pair emits a flushed progress line with its labels and case count; questions and retrieved text are not printed.
- **Guards:** production node and edge counts are checked before and after every run, and the script exits 3 on any change.
- **Query timeout:** the run pins a 30 s per-query FalkorDB timeout (`--query-timeout-ms`, docker-compose's documented value), preventing dependency on live server timeout drift.
- **Eval suite expansion:** 30 MS7 eval questions expanded with 30 approved additions (10 each for A, B, C; 60 cases total), over 4 cut-offs (09-09 16:00, 09-10 00:00, 09-20 00:00 UTC, now).

### Fidelity fixes

- **09-09 snapshot empty (fixed):** Memories dated by first promotion into any production-lineage graph keyed by `memory_id` in `source_description`, populating the snapshot with 280 episodes.
- **Eval gold drift (fixed):** Rebuilding reordered episode names (167 of 634 renamed). `queries.json` was re-keyed to `gold_memory_ids` and resolved per graph at runtime.
- **Later invalidations leaked (fixed):** Facts expired during an ingested episode are un-expired when that episode is subtracted.
- **Wiki notes not pruned (fixed):** Note nodes follow the wiki export at the cutoff; gold wiki files inside commit gaps are flagged uncertain.

### Results & Validation (60 cases × 4 cutoffs)

Comparing production (`max_per_episode=2`) vs candidate (`max_per_episode=1`):

| Cutoff | Production Hit@8 (MRR) | Candidate Hit@8 (MRR) | Superseded (Prod / Cand) | Notes |
|---|---|---|---|---|
| **09-09 16:00** | 94.1% (0.591) | 94.1% (0.596) | 23 / 20 | 0 losses, superseded reduced by 3 |
| **09-10 00:00** | 94.4% (0.558) | 97.2% (0.567) | 22 / 19 | +2.8% lift; C9 net win |
| **09-20 00:00** | 94.6% (0.530) | 97.3% (0.543) | 23 / 17 | +2.7% lift; C9 net win |
| **now** | 92.5% (0.501) | 92.5% (0.507) | 39 / 34 | 0 losses; superseded reduced by 5 |

- **Zero regressions** across all 480 case evaluations.
- **Noise reduction:** Superseded facts returned dropped across all four cutoffs (-3, -3, -6, -5).
- **Production default:** Adopted `max_per_episode=1` in `server/providers/memory_graphiti.py`.

### Exit gate — ANSWERED

**Can a historical case be rerun with its original available context, and can two policies be compared on the same cases? Replay must not mutate production memory by default.**

**Yes.** Historical snapshots reconstruct past graph and wiki state by subtraction with zero forward temporal leaks. Policies (`edge-only`, `edge+episode-vector`, fusion parameter sweeps) can be run and compared across the expanded 60-case eval suite. Production graph is protected by pre/post node/edge count checks and strict `replay-*` prefix guards (verified 4,111 nodes / 9,835 edges unchanged). The sweep identified one-fact-per-episode fusion cap as beating production, validated with zero regressions across 480 evaluations (+2.8% at 09-10, +2.7% at 09-20, C9 net win, reduced superseded facts at every cutoff); adopted `max_per_episode=1` as production default. (2026-09-29)

---

## MS4d — Codex transcript adapter and ongoing capture (2026-09-30)

**Closed 2026-09-30 (User).**

**Goal:** Round out coding-harness coverage by building continuous evidence capture for OpenAI Codex (Desktop & CLI) using local transcripts and lifecycle hooks. Note: Gemini CLI was removed from the milestone scope per user steer (User, 2026-09-30).

### What was built

- **Incremental transcript reader** (`server/adapters/codex/transcript_reader.py`):
  - Traverses `~/.codex/sessions/**/*.jsonl` recursively;
  - Tracks file-level read offsets and line cursors in SQLite `codex_tail_state` with bounds and shrinkage checks;
  - Handles subagent and fork transcripts with subagent-to-parent provenance tagging;
  - Supports both full-history and cutoff-based scanning.
- **Transcript schema parser** (`server/adapters/codex/parser.py`):
  - Maps Codex's JSON-RPC and message record formats (`session_meta`, `turn_context`, `user_message`, `assistant_message`, `tool_call`, `tool_return`, `compacted_summary`) into canonical `SourceEvent` records (`harness="codex"`);
  - Extracts and formats tool calls into human-readable compact summaries (capped at 1,000 chars);
  - Preserves token counts, model identifiers, and turn sequence numbers;
  - Redacts sensitive credentials (e.g., API keys, bearer tokens) before journaling.
- **Project attribution** (`server/adapters/codex/project.py`):
  - Resolves session working directories to registered repository slugs (`context-memory-fabric`, `project-gamma`, `project-delta`, `project-epsilon`, etc.), falling back to path-derived directory names.
- **Queue store & worker** (`server/adapters/codex/worker.py`):
  - Staged journal-first processing: writes source events to `imports/journal/journal.db`, then executes `run_reasoning_consolidation()` under `ExtractPolicy`;
  - Enforces `CMF_LLM_PROVIDER=local` during consolidation to protect remote rate limits and API budgets;
  - Multi-process safe: uses an exclusive file lock (`/tmp/cmf_codex_worker.lock`) to prevent concurrent poller or hook runs from colliding;
  - Retry queue: tracks pending consolidation attempts in `codex_pending_consolidation`, automatically retrying un-consolidated sessions on subsequent tail passes.
- **Lifecycle hooks** (`server/adapters/codex/hooks.py`):
  - Idempotent installer/uninstaller managing `~/.codex/hooks.json`;
  - Installs non-blocking `SessionStart` and `Stop` hooks executing background `tail --once` passes detached from the interactive CLI.
- **Background poller** (`local.cmf.codex-poller.plist`):
  - Launchd 900-second interval agent running `server.adapters.codex.cli tail --once` under `.venv/bin/python3`;
  - Logs to `~/Library/Logs/cmf-codex-poller.log` and `cmf-codex-poller-err.log`.
- **Management CLI** (`server/adapters/codex/cli.py`):
  - Subcommands: `status` (inspect active sessions, offsets, and pending queues), `preview` (read-only transcript dump), `tail` (run journal and consolidation passes), and `hooks` (`status`, `install`, `uninstall`).
- **Fixture isolation & privacy**:
  - `tests/fixtures/codex/` added to `.gitignore`;
  - Dynamic mock generator `_ensure_fixture()` in `tests/test_codex_parser.py` ensures tests remain completely self-contained and reproducible without storing personal local transcript data in Git.

### Verification & Validation

- **Unit and regression tests:** 40 dedicated Codex tests across 5 modules (`tests/test_codex_*.py`), passing with 100% success (97/97 across broader test suite).
- **Pilot validation:** Real local Codex session (`01a0eef3-bb1e-78e1-b959-48721c7929ad`) processed live: 155 events journaled to `journal.db` and 13 reviewable proposals staged under `ExtractPolicy` with zero production graph mutations and zero duplicates on rerun.
- **Live mobile test:** A remote mobile Codex session (`01a0f24b-5e3d-7bf0-a619-18b440206e1c`) was automatically picked up by the launchd poller, journaled (3 events), and consolidated without manual intervention.

### Exit gate — ANSWERED

**Does the transcript-tailer and hook-accelerant pattern extend cleanly to OpenAI Codex (Desktop & CLI)?**

**Yes.** Built `server/adapters/codex/` with incremental JSONL reader, schema parser, project attribution, and SQLite queue store. Enforced local LLM consolidation and multi-process file locking. Hooks installer added `SessionStart` and `Stop` non-blocking hooks into `~/.codex/hooks.json`. Backbone 15-minute launchd poller (`local.cmf.codex-poller`) deployed and active. Real pilot (155 events) and live mobile session (3 events) verified end-to-end into reviewable proposals with zero graph side-effects. Gemini CLI dropped per user steer. (2026-09-30)

---

## Backlog — closed items

Completed backlog items, moved out of [plan-active.md](plan-active.md)'s Backlog section once done. Grouped under the same topic headings used there.

### FalkorDB TIMEOUT drift and full-text join optimization (found 2026-09-29, closed same day)

- [x] **Container recreated with `TIMEOUT 30000` surviving restarts.** Verified with node and edge count parity before and after. Persistence verified with volume mounted at `/data`.
- [x] **Detect timeout drift (`server/providers/falkor_driver.py`).** The MCP server logs `FalkorDB TIMEOUT: <ms>` at startup and warns below 30000 ms. `capture_health` reports the timeout status and warnings.
- [x] **Optimized edge fulltext search (`CMFFalkorDriver` + `FalkorSearchInterface`).** Binds yielded relationship and reads `startNode(e)`/`endNode(e)`. Search across all 30 MS7 questions dropped from 212.7 s to 0.56 s (A1 dropped from 10.85 s to 0.03 s) with identical results and ordering.

### Corpus & review backlog (found 2026-09-11, closed out same day)

MS6b's governance tooling ([plan-history.md](plan-history.md#ms6b--governance)) surfaced this once `explain --graph`, `correct-memory`, and the review CLI could actually be pointed at the corpus. Measured directly against the live journal, not the MS6a/MS3.5-era estimates (several bulk actions and ongoing capture had moved these numbers since 2026-09-08):

- [x] **27 tier-1-shaped v0.1 orphans, reviewed (2026-09-11).** All were `decision`×16/`plan`×8/`rejected_alternative`×3, `policy_version=0.1`, correctly excluded from `tier1_review_queue()`'s v0.2 filter but never formally retired. Checked each against v0.2 for actual evidence-event overlap rather than assuming duplication: **19 confirmed duplicates** (same evidence, reprocessed under v0.2, `rejected` with reason citing the duplication) and **8 with no v0.2 counterpart**, individually read in full (statement + rationale + evidence) — **4 approved** (specific, confirmed-accurate technical/narrative decisions: FeatureX print resolution, moon/sun/PlanetFeature mask config, Java-over-Kotlin for the Android project, integrating the fine-arts narrative into the project-epsilon cover letter) and **4 rejected** (two were literal task instructions, not durable facts, one still `status=open`; two were thin one-off wording edits on a resume/LinkedIn post with no lasting reference value).
- [x] **969 tier-2 episodes, reviewed and promoted (2026-09-11) — closed out.** `finding`×16, `hypothesis`×33, `experiment`×138, `investigation`×755 (the live count moved from the 981 estimate — ongoing capture). Read individually — statement, rationale, status, and evidence turns for ambiguous ones — against one standard: promote only if the statement itself states a durable, specific, resolved conclusion; reject pure process narration, open unresolved threads, or task instructions misclassified as reasoning; defer anything genuinely uncertain or sensitive rather than guessing. First pass: **163 approved, 797 rejected, 9 deferred**. Promote rate varied by kind as expected (`decision`-adjacent kinds like `finding`/`hypothesis` ran ~35-50%; `investigation`/`experiment`, which are mostly exploration without a stated resolution, ran ~12-31%) — confirms MS3.5's own observation that `reasoning_kind` is a routing hint, not a keep/drop gate; individual content had to be read either way. Verdicts applied via `apply_verdicts` (the same chokepoint MS6a's tier-1 pass used).
  - **The 9 deferred, resolved by User (2026-09-11):** *Sensitive/personal (5)* — personal-health and household-governance items, including third-party names — **User approved all 5**. *Genuinely uncertain (4)* — two home-AV troubleshooting items (a mid-troubleshooting symptom and a power-draw measurement the user questioned) and two home-project hypotheses — User rejected the two home-AV items and approved both home-project hypotheses.
  - **Final tally: 171 approved, 799 rejected, 0 deferred**, all 171 promoted into `mem-fabric-local` across three batches, **171/171 succeeded, 0 failed** (real qwen3.5-122b extraction per episode, local/unmetered). Graph grew **295 → 465 Episodic nodes** (verified via direct Cypher count), entities 393→665, `RELATES_TO` edges →501.
  - **A real bug surfaced when User asked why the first batch was 164, not 163** (2026-09-11): one of the 164 wasn't a tier-2 approval at all — it was the *original, pre-correction* 360-cam/eclipse episode (the misattribution `correct_memory` fixed earlier in the MS6b work), silently re-promoted with its stale wrong content. Root cause: `reviews` is last-writer-wins per memory_id and `correct_memory` never touches it, so the old memory_id's `approved` verdict from 2026-09-08 stayed on record after the correction superseded it; `correct_memory` separately clears the old memory_id's `PromotionStore` row (the graph identity moved to the new memory_id). Those two facts together made `actions.promote_approved`'s "approved and not yet promoted" query — which had no idea `derived_memories.approval_state` existed — treat the superseded old memory_id as freshly eligible. **Fixed:** the query now excludes any memory_id whose `derived_memories.approval_state` is `rejected`/`superseded_by_reasoning`/`superseded_by_correction`, joining against `derived_memories` rather than reading `reviews` alone (`server/review/actions.py`). New regression test `test_superseded_by_correction_is_not_reeligible` (`tests/test_ms6_review.py`) reproduces the exact sequence and passed on the very next real promotion batch (the 2 final home-project approvals). **Cleanup:** the wrongly-revived `chatgpt-photo-006` episode (uuid `2734ac71-...`) removed from `mem-fabric-local`, its stray `PromotionStore` row deleted.
- [x] **25,961 heuristic-pattern rows still `queued_for_review`, resolved for real (2026-09-18).** Resurfaced while scoping MS4a2/MS4b's episode-proposals file mirror. Root cause: the same underlying events reclassified three times as the policy version bumped 1.0 (9,658 rows) → 1.1 (9,721) → 1.2 (6,582), older versions never formally retired. Ran `python -m server.review.cli retire-stale-versions --apply` (implementation `bulk_reject_stale_policy_versions()`, `server/review/actions.py:238` — one tool, not two; there is no separate `bulk-reject-stale-policy-versions` CLI command) — dry-run first confirmed the tool's own docstring numbers exactly (19,379 stale rows / 9,757 distinct events), then applied: **19,379 v1.0/v1.1 rows rejected** (`batch_id: 67a0f5dbe07945b39799ad75ca8a11cd`, reversible via `revert-batch`), leaving heuristic-pattern's real `queued_for_review` pile at **6,582** (v1.2 only) — down from 25,961.

- [x] **MS4b's 379 `reasoning-episode` rows (2026-09-18 consolidation run) — closed out 2026-09-22.** Not reviewed as-is: rejected by exact `memory_id` 2026-09-19, re-derived from source journal events under `ExtractPolicy` (436 episodes + 69 doc proposals, later extract 1.1–1.3 re-runs), and the result fully reviewed by conversation — 74 approved / 1,017 rejected, 73 promoted, 46 doc proposals applied. See [MS4b](#ms4b--claude-code-transcript-adapter-build-backfill-re-derivation-review-2026-09-18--09-22).

- [x] **Extend MS4b's Claude Code backfill beyond the CMF-only scope — closed 2026-09-28.** Done as a side effect of the launchd poller's first run, which swept all 872 conversations on the machine (project-gamma, project-delta, ProjectAlpha, ProjectBeta, project-epsilon, …) under `ExtractPolicy`. The "default `project` to the repo slug" half landed as `server/adapters/claude_code/project_slug.py`. See [MS4b → Ongoing capture](#ongoing-capture-hooks-launchd-poller-first-live-run-2026-09-22--09-23).

(The remaining items under this heading, the growing-corpus framing note and the `cmf_test` vector-dimension mismatch, are still open; see [plan-active.md](plan-active.md#backlog).)

### Proposal-directory housekeeping (found 2026-09-18, scoping MS4a2/MS4b)

Both proposal-review surfaces stored everything flat with no move-on-review, and MS4a2's `capture_session` was about to add volume to both:

- [x] **`wiki-proposals/` (was 78 files, flat, all statuses mixed) — fixed (2026-09-18).** `server/proposals.py`'s `_save_proposal()` now locates a proposal wherever it currently lives (root/`approved/`/`rejected/`) and moves it to match its status on every save; `get_proposal()`/`list_proposals()` search all three locations. `applied` stays under `approved/` (sub-state, not a third folder, per MS6d's own status vocabulary). One-time migration run for real against the existing backlog: **76 moved to `rejected/`, 2 to `approved/`** (both `applied`), flat root now empty. 4 new tests in `tests/test_ms6d_proposal_review.py::TestProposalSubfolders`.
- [x] **Staged reasoning episodes had no file representation at all — fixed (2026-09-18).** `server/episode_proposals.py`: a write-through mirror hooked into `ConsolidationStore.record_reasoning_episode()` (unconditional — heuristic-pattern rows physically can't reach that method, they go through `record_consolidation()` instead, so no policy_name filter was needed) and into `approve_episode`/`reject_episode` (`server/review/actions.py`) for the approved/rejected move. SQLite stays authoritative; the file is a read-only projection. Filename is `sha256(memory_id)` rather than the raw id — a real bug surfaced backfilling production: some windowed-episode memory_ids embed long composite event ids and exceed the filesystem's filename length limit. **Real backfill run**, scoped as planned (tier1 first, tier2 second, `heuristic-pattern`/stale `reasoning-episode@0.1` excluded): **301 tier1 + 942 tier2 = 1,243 files written**, verified against `SELECT count(*)`. 10 new tests in `tests/test_episode_proposals.py`, including a regression test for the long-memory_id filename bug. Test isolation: `ConsolidationStore`/`ReviewStore` derive the mirror directory from their own `db_path` (a sibling `episode-proposals/` next to whatever db_path a test passes), so no existing test needed updating to avoid polluting the real project directory.
  - **Follow-up bug, found and fixed the same day:** User spotted a rejected test episode's mirror file sitting in `rejected/` while its own `approval_state` field still read `queued_for_review` — `move_episode_mirror()` was a pure filesystem rename, never a content rewrite. Fixed: it now rewrites `approval_state` to the terminal value plus `reviewed_at`/`reviewer`/`review_reason` on move, matching `WikiProposal.status`'s own single-evolving-field convention (which is why the wiki side never had this bug). 6 new test assertions.

### Episode-proposals review MCP tools (found 2026-09-18, parity gap with MS6d)

MS6d built a full `list`/`get`/`review`/`apply`/`bulk_reject` MCP lifecycle for wiki proposals. The episode side never got the equivalent — `approve_episode`/`reject_episode`/`tier1_review_queue()` were CLI-only. Found the same day a live `capture_session` test needed rejecting and no MCP client could do it.

- [x] **Built:** `list_episode_proposals(tier, approval_state)`, `get_episode_proposal(memory_id)` (both read the `episode-proposals/` file mirror, not `derived_memories` directly — see the filed gap below for why), `review_episode(memory_id, verdict, reason, reviewer)` (approve/reject/defer, never calls `remember()`), `bulk_review_episodes(verdicts, reviewer)` (mixed verdicts in one call, wraps the already-existing `apply_verdicts()` — strictly more capable than `bulk_reject_wiki_proposals`'s single-reason-only shape). Tool count 17 → 21. 6 new tests in `tests/test_episode_proposals.py` covering the read helpers; the MCP wrapper functions themselves are registration-checked only (same convention `promote_auto_accepted_memories` already follows for tools with no test-injectable path — the underlying logic gets the real unit coverage, the thin wrapper doesn't get exercised against production state).

- [x] **Hardcoded `policy_name = 'reasoning-episode'` — fixed 2026-09-19 ([PR #16](https://github.com/username/context-memory-fabric/pull/16), commit `6530a8d`).** Two independent instances (`ConsolidationStore.query_reasoning_episodes()`/`tier1_review_queue()`, and `server/review/queue.py`'s `review_queue()`) plus a third found while touching the same code (`cli.py`'s `_hint_other_versions()`). Solved more simply than the per-policy-name version map originally sketched: `policy_name` is a parameter everywhere, defaulting to `'reasoning-episode'` so existing callers/tests are unaffected, with a new `--policy-name` CLI flag on `stats`/`queue`/`export`. Verified live: `queue --tier 1 --project context-memory-fabric --policy-name extract --policy-version 1.0` returned the 227 tier-1 rows.

### Post-apply staleness (found 2026-09-16, fixed same day, MS6d)

`apply_wiki_proposal` is the first tool that writes into `LLM_WIKI_PATH`, but nothing downstream that assumes the corpus is static was getting invalidated when it ran: `search_wiki`'s filesystem-scan cache, and (at the time) [MS7b](plan-history.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13)'s offline-built wiki-derived entity/section graph. Confirmed concretely, not just theoretically — the real apply of `prop_20260916_125736_a33f295a` created `WIKI/projects/Context-Memory-Fabric/Context-Layers-as-the-Next-Frontier.md` (commit `6dfce163`) and it did not surface via `search_wiki` until this fix.

- [x] `search_wiki`'s side fixed: `apply_wiki_proposal` now calls `invalidate_corpus_cache()` (`server/wiki.py`) on every real (non-dry-run) apply — lazy invalidation, drops the cached engine/assets rather than forcing an immediate rescan, since applies are rare and a rescan can be non-trivial cost. Tested (`tests/test_ms6d_proposal_review.py::TestPostApplyStaleness`, 3 cases: pure invalidation, real-apply wiring, dry-run does *not* invalidate). **Live server restarted 2026-09-16** to pick this up — confirmed live via subsequent `search_wiki` calls from Code mode and Cowork.
- [x] The wiki-derived graph's own staleness question is moot now that MS7b closed onto a static, no-longer-rebuilt `mem-fabric-local` — see below.

### Recall coverage for manually merged episodes (found 2026-09-21, review-by-conversation manual merge)

During conversation `47ef54a2`'s review, 7 staged tier-1 episodes were hand-merged into one combined episode via a direct sqlite3 insert (no MCP tool merges already-staged episodes), with the 7 originals rejected and linked via `superseded_by`/`supersedes`. The originals stay queryable in the ledger; only the merged row becomes a graph node.

- [x] **Resolved 2026-09-21, but the wrong way for the right reason.** The 39-point/6,364-char merged episode never promoted — first a hard context-length error (`n_keep: 19622 >= n_ctx: 16384`) against local qwen3.5-122b, then, after reloading the model at 32768 context, repeated silent stalls (root cause: a stale week-old `com.cmf.spark-tunnel` SSH tunnel plus orphaned in-flight requests in the CMF server). Split into two ~20-point episodes (`session-2026-09-17-graph-schema-decisions`, `session-2026-09-17-process-tooling-decisions`), which promoted cleanly. **Lesson: keep manual merges under ~20 points / ~3,600 chars.**
- [x] **Resolved 2026-09-21.** Hand-inserted merged rows have no `episode-proposals/` mirror file. Confirmed low-impact: `review_episode`/`promote_approved_episodes` both work without one. A future `merge_episodes` MCP tool should still write the mirror — filed as open in [plan-active.md](plan-active.md#backlog).

### RediSearch `group_id` syntax error on promotion (found 2026-09-21, resolved 2026-09-22)

- [x] `promote_approved_episodes` failed deterministically on thread `ms4b-automated-ingest-plan` with `RediSearch: Syntax error at offset N near group_id`, inside Graphiti's internal full-text dedup query on the `RELATES_TO` index. Root cause: statement text containing literal `group_id: ""` / `group_id: "_"` — Graphiti strips quotes, leaving a bare `_` token that is invalid inside a RediSearch OR-list. Resolved by rewording the content in `derived_memories` (`group_id empty` / `group_id underscore`); promoted cleanly as `claude-code-context-memory-fabric-073`. `graphiti_core` left unmodified.
- [x] **Defensive input sanitization in CMF — done 2026-09-28.** Probed against live FalkorDB: a bare `_` is the only token that breaks (Graphiti maps every other ASCII punctuation character to a space; `""` yields no token). `neutralize_fulltext_hazards()` (`server/providers/memory_graphiti.py`) replaces each `_` that would end up as a bare token with U+FF3F FULLWIDTH LOW LINE, which reads the same and passes as a query token. It runs at every place text enters Graphiti: `remember()` (the promotion path), `recall` search, `correct_memory`, and the ChatGPT export importer. It deliberately does not subclass the driver: Graphiti builds fulltext queries through two separate paths, and `FalkorDriver.clone()` drops subclasses. `tests/test_fulltext_sanitizer.py` includes a drift guard against Graphiti's separator map. **Limit:** a lone `_` the extraction model invents on its own (not present in the source text) isn't covered; that would need a `graphiti_core` patch.

### Promotion hang with no error output (found 2026-09-21, reviewing conversation `8406b5c3`)

- [x] Thread `ms7b-implementation-priority` hung with zero log output twice, across fresh server restarts with a healthy tunnel and the model `IDLE`. **Retried cleanly 2026-09-22** and promoted as `claude-code-context-memory-fabric-072`. Workaround that unblocked the batch: restart the CMF server process to abandon the stuck coroutine (FalkorDB/ledger untouched). Root cause still open in [plan-active.md](plan-active.md#backlog).

### Episode-proposals mirror out of sync with pre-mirror verdicts (found and fixed 2026-09-22)

- [x] **1,243 `reasoning-episode@0.2` mirror files sat in the `episode-proposals/tier1/` (301) and `tier2/` (942) roots reading `queued_for_review`, although every one already had a terminal verdict in `reviews`** (tier-1: 295 approved + 6 rejected from MS6a; tier-2: 166 approved + 776 rejected from the 2026-09-11 pass). Cause: the mirror was backfilled 2026-09-18, after those verdicts, and `move_episode_mirror()` only runs on new verdicts. The effect was that `list_episode_proposals` and `list_review_conversations(include_tier2_only=True)` over-reported pending work by 1,243. **Fixed** with a one-time script: for every root-level mirror with a terminal `reviews` row, call the real `move_episode_mirror()` (which rewrites `approval_state`/`reviewer`/`review_reason` along with the location), then restore the historical `reviewed_at` from `reviews` rather than the sync time. Dry-run first; the pre-sync directory was snapshotted. **Result:** 1,243 moved. The `tier1/` root is empty, and the `tier2/` root holds exactly the 24 genuinely unreviewed MS4b `extract` rows. `list_episode_mirrors(approval_state='queued_for_review')` now returns 24.
- [x] **The 7 rows that are `rejected` in `reviews` but have a `succeeded` promotion: no action needed.** All 7 were promoted 2026-09-07 (MS3.6) into the since-retired `mem-fabric-gemini` graph and then rejected in the 2026-09-08/11 review passes. None exist in `mem-fabric-local`, which was rebuilt from approved rows only in the Spark migration; a direct Cypher check confirmed that. The ledger rows are historical and scoped to the rollback graph.


### Persisted backfill cutoff (found 2026-09-18, closed 2026-09-28)

- [x] **`--since` no longer has to be passed by hand.** `server/adapters/claude_code/cli.py`'s `backfill` already falls back to the `CMF_CLAUDE_CODE_BACKFILL_SINCE` env var when `--since` is absent. The unattended path (the launchd poller) runs `tail`, which resumes from `claude_code_tail_state` byte offsets and never takes a date cutoff. The only case left is a deliberate one-off manual backfill, where choosing the cutoff by hand is correct.

### Promotion/apply rough edges (found 2026-09-20 → 22, closed 2026-09-23 → 24)

- [x] **`format_promotion_report()` raised `KeyError` on an empty result.** Fixed in `985de51` (2026-09-24): every optional key is now read with `.get(...)`, including `dry_run`.
- [x] **Episodes promoted with NULL `project` were permanently named `<harness>-misc-NNN`. Root cause fixed for `claude_code` 2026-09-23.** `record_reasoning_episode()` never accepted a `project`, so every `claude_code` row was NULL by construction. `pipeline.py` now derives it at extraction time via `server/adapters/claude_code/project_slug.py`, and antigravity got the equivalent on 2026-09-24. `_semantic_episode_name()` still falls back to `misc` for any harness that never sets `project`. 23 `claude-code-misc-*` episodes had been renamed by hand on 2026-09-21 under the old behavior.
- [x] **Doc proposals could carry a wrong `target_path` or missing frontmatter. Fixed at generation 2026-09-23 (`EXTRACT_POLICY_VERSION = "1.4"`).** For `claude_code`/`claude_desktop`, the conversation's known project overrides the model's folder choice (`_canonicalize_doc_project_folder`, matching existing WIKI folders case- and punctuation-insensitively). Each required frontmatter key is filled individually, with legacy `date:` → `created:`, and the prompt now asks for `created`/`updated`. **Not covered:** `apply_doc_proposal()` still doesn't validate proposals from other sources (another policy, or a manual `propose_doc_update`). Revisit if that becomes a real problem.

### Single-episode promotion in `promote_approved_episodes` (requested 2026-09-22, done 2026-09-28)

- [x] **The MCP tool now promotes exactly one episode per call.** It takes a required `memory_id` and `dry_run`; `limit` and bulk mode were removed from MCP, per User: bulk runs overloaded Spark and outlived client tool-call timeouts at 1–4 minutes per episode. `actions.promote_approved()` gained an optional `memory_id` that applies the same eligibility rules (approved, not superseded, not already promoted). An ineligible id comes back as `not_eligible` with a note, rather than being mislabeled as a bad id. Bulk promotion is CLI-only: `python -m server.review.cli promote`.

### Auth hardening (deferred out of MS6c, 2026-09-16; closed 2026-09-28)

Raised in review on [PR #6](https://github.com/username/context-memory-fabric/pull/6) and merged without fixing at the time. Still open in [plan-active.md](plan-active.md#auth-hardening-deferred-out-of-ms6c-2026-09-16): `exchange_refresh_token` dropping `resource`, deliberately deferred.

- [x] **Test coverage — done 2026-09-28 (`tests/test_oauth.py`; 17 tests, 24 after the hashing and minor fixes below).** Provider-level: the full consent → code → token → refresh → rotate → revoke round trip, tokens surviving a provider restart, and every refusal case (wrong password, expired pending request, expired/reused/other-client code, other-client/expired/rotated refresh token, expired/unknown access token). End-to-end: the real `server.mcp` app over HTTP in a subprocess with a temp DB (register, authorize with PKCE, consent page with wrong and right password, wrong `code_verifier`, code reuse, `/mcp` without/with a bad/with a good bearer token, refresh rotation). Plus `BearerTokenAuthMiddleware`. Mutation-checked: disabling refresh rotation fails both layers. **Found while testing:** the MCP SDK's `/revoke` handler requires a `client_secret` field, so a public client (`token_endpoint_auth_method: none`) can't revoke over HTTP; that's SDK behavior, and revocation is covered at the provider level.
- [x] **Tokens hashed at rest — done 2026-09-28.** `OAuthStore` stores SHA-256 hex digests and looks up by hash; callers still pass and receive raw tokens. Migration keeps connected clients working: `main()` runs `hash_legacy_tokens()` once at server startup, and a lookup that hits a legacy plaintext row rehashes it on the spot. The bulk pass is deliberately not in the store constructor, because tests import `server.mcp` against the real `journal.db` and would lock out a still-running pre-hashing server. **Note:** backups made before this change (e.g. `imports/journal/journal-9.22.26-bak.db`) still contain plaintext tokens.
- [x] **No rate limiting on the consent password — addressed in docs 2026-09-28.** [CLIENTS.md §0](CLIENTS.md) now states that `openssl rand -hex 16` (or longer) is required, because the endpoint has no rate limiting or lockout and the password's entropy is the whole defense. It also covers how to rotate the password and force all clients to re-consent. No code-level rate limiting was added; at 128 random bits it doesn't change the risk.
- [x] **Minor — done 2026-09-28.** `http_auth.py`'s docstring now describes it as the static-token fallback, mutually exclusive with OAuth, instead of saying OAuth was rejected. `_codes` is pruned of expired entries every time a new code is issued. Both `compare_digest` call sites (consent password, static bearer token) compare UTF-8 bytes, so a non-ASCII value is cleanly denied instead of raising `TypeError`.

### Per-conversation SQLite/HTTP client leak in `process_pending` (found 2026-09-22, first launchd poller run)

- [x] **Fixed 2026-09-28. There were two separate leaks, neither in `worker.py`'s loop itself.** (1) `run_reasoning_consolidation()` (`server/consolidation/pipeline.py`) opened a `ThreadIndex` and a `ReviewStore` on every call when the caller didn't pass them, and never closed either, which accounts for 2 sqlite handles per conversation. It now closes whatever it opened itself in a `finally`, while caller-passed stores stay the caller's. (2) `_local_generate()` (`server/policies/reasoning_episode.py`) built a new `LMStudioCompatClient` (with its own httpx pool) for every model call under `asyncio.run()` and left it for the garbage collector after the loop had closed, which is the source of the `Event loop is closed` / `Task exception was never retrieved` tracebacks. The client is now created and closed inside the same `asyncio.run()`. Both workers (`claude_code`, `antigravity`) also now close the fallback `ConsolidationStore` they construct. `tests/test_consolidation_resource_cleanup.py`: 4 of its 5 tests fail on the pre-fix code.

### Claude surface provenance and Cowork capture (found 2026-10-02, closed 2026-10-03)

Unplanned work, found by User during MS9 Phase 5 while looking into Claude's low cross-model score. Working checklist with every count and commit: [CLAUDE-HARNESS-PROVENANCE-PLAN.md](CLAUDE-HARNESS-PROVENANCE-PLAN.md). The mislabel turned out **not** to explain the Phase 5 score (no gold episode was `claude_code`).

- [x] **Provenance.** The MS4b parser hard-coded `harness="claude_code"`, yet 1,394 of 1,395 transcripts were Desktop Code-tab sessions. Harness now comes from each line's `entrypoint` (`cli`/`sdk-*` → `claude_code`, `claude-desktop` → `claude_desktop_code`, `local-agent` → `claude_cowork`); `event_id` keeps its `claude_code:` prefix so nothing re-ingests. The worker had also consolidated under a hard-coded harness and would have silently skipped every Desktop event; fixed. Cowork's MCP client slug maps to `claude_cowork`.
- [x] **Eval junk out.** 1,330 of 1,413 `claude_code` conversations were `answer_eval.py` `claude -p` runs in temp dirs (2,664 events, no derived memories). Temp-dir projects are no longer discovered (1,395 → 65 files), `answer_eval.py` drops the inherited entrypoint, and the migration deleted them. 30,269 events (63 conversations) were relabeled `claude_desktop_code`, plus 2,902 episode mirrors, 338 doc proposals and 265 thread harness lists (backed up first).
- [x] **Episode rename.** The 161 Code-tab episodes in `mem-fabric-local` and `fixgraph-p4` were renamed `claude-desktop-code-*` with label `:Claude_Desktop_Code`; other graphs keep the old names as frozen records. Its pre-check exposed MS9 Phase 2b's episode damage, which was repaired first ([FIX-GRAPH-PLAN.md → Phase 2b repair](FIX-GRAPH-PLAN.md#phase-2b-repair-production-2026-10-02)).
- [x] **Lost transcripts.** Claude Code's default 30-day `cleanupPeriodDays` had deleted 56 CLI and 39 Code-tab transcripts; it is now 36500. No local or claude.ai-export copy exists; the typed prompts of the 56 CLI sessions were journaled from `history.jsonl` as `user_prompt.history` events (424, partial, never extracted). The wider search is parked (plan-active Backlog).
- [x] **Cowork adapter** (`server/adapters/claude_cowork/`): discovers `local-agent-mode-sessions` transcripts, stamps sidecar metadata (title, session type, scheduled task, cloud handoff), project from the first selected folder, own tail state; extraction for interactive/dispatch sessions, scheduled sessions opt-in by task id. Poller `com.cmf.cowork-poller` enabled 2026-10-03.
- [x] **Cowork backfill.** 69,798 events from 944 transcripts journaled; all 420 interactive/dispatch conversations extracted (plus the 40 sub-floor stubs via `extract --stubs`), then 8 of 12 scheduled-task channels via the allowlist and `extract --retriage` (triage withholds automated windows, which have no genuine user turns). Totals: 460 conversations, **1,706 episodes (725 likely keepers), 211 doc proposals, 0 errors**. A 9-hour unattended push used `scripts/cowork_extract_driver.py`: event-budgeted batches ordered by assistant prose per event (r=+0.61 with likely keepers per event in the first 40 conversations; typed-turn density +0.29; titles no guide), speed seeded from history, slowdown/health/error guards, live control and status files, interruption recovery.
- [x] **One Spark job at a time.** `server/adapters/spark_lock.py`: all pollers and backfills take `imports/journal/spark_job.lock` and skip a pass (no offsets moved) while it's held or the Phase 4 wiki extraction runs. The Code-tab poller held it ~5 of every 15 minutes while re-extracting a long live session; pausing it during the push cut Cowork from ~1.2 to ~0.7 s/event.
- [x] **Review ordering, not rejection.** From User's 732 reviewed `extract` episodes: decision/plan/rejected_alternative with ≥3 evidence turns were approved 57% of the time; investigation/hypothesis/experiment 0 of 176; model confidence useless (≥0.9 approved 9%). Doc proposals: only length matters (<1.5k chars applied 4%). `scripts/review_priority_report.py` writes the ordered queue.
- **Still open** (plan-active Backlog): the lost-transcript search, cloud-run transcripts (≥8 Code sessions, 6 Cowork handoffs), the 4 recurring scheduled channels (journal-only by decision), and the 4 [double-promoted memories](plan-active.md#double-promoted-memories-in-mem-fabric-local-found-2026-10-02-provenance-step-5a).

### extract@1.6 backfill review: Phases 0–3 and the Phase 4 pilot (2026-10-03)

Phase 4 (applying approved docs and promoting approved episodes) continues in [plan-active.md](plan-active.md#extract16-backfill-review-phase-4-apply-and-promote). The rules that came out of this review for the next extractor are in [extract@1.7](plan-active.md#extract17-scoped-thread-candidates-approved-2026-10-03-build-after-the-extract16-review).

Full review of the queue left by the extract@1.6 backfill; brief and rules from User's 2026-10-03 session (batches need his go-ahead; review per conversation, smallest project first; promote one episode at a time, never with the bulk promoter).

**Phase 1 (code), done and pushed unless noted:** test isolation for proposal/mirror dirs (`144d610`); thread merges keep their project + `CMF_PROJECT_ALIASES` (`149d5c0`); codex mirrors to the real review dir (`7341ce2`); Cowork scheduled-task project map and `Documents/Claude/Projects/<p>` rule (`8849245`); repair scripts `backfill_threadmerge_project.py` / `retag_review_projects.py` (`2f94931`); proposal path casing normalizer (`bc036e0`); doc-update apply guard, >30% line removal refused (`b874cc9`); folder-prefix project map, per-item `--set` (`3d71d68`, local). extract@1.7 (thread scoping) approved, built after this review.

**Phase 2 (queue hygiene), done:**
- 2.1 rejected 23 test-fixture proposals.
- 2.2 18 duplicate doc targets consolidated; all 45 pending 1.6 `update` proposals are whole-page rewrites from a snippet, so updates are rebuilt non-destructively on the live page; transcript-dated frontmatter (America/Chicago) on every created/edited page; `RAW/` writes allowed.
- 2.3 canonical Title-Case project folders (`Career-Navigator`, `Context-Memory-Fabric`); 134 paths normalized.
- 2.4 projects: 368 thread merges filled from their children; `?` conversations overridden per User; `project-epsilon` dissolved into `domain-epsilon` (job search) / `project-epsilon-dev` (plugin dev) by folder for pending items (per-item fixes during Phase 3 via `retag_review_projects.py --set`); slug merges astro->astrophotography, obsidian-brain->obsidian, project-delta->finance, claude->claude-tooling, interlock-core->interlock. Promoted items: see the slug-migration backlog item.

**Phase 3 (review) progress** (pending at start: 747 tier-1 episodes, 263 doc proposals, 268 conversations):

| Project | Convs | Tier-1 | Docs | Cleared |
|---|---|---|---|---|
| claude-tooling | 1 | 1 | 0 | ✅ 2026-10-03: 0 approved, 1 rejected |
| backup | 1 | 3 | 1 | ✅ 2026-10-03: 2 approved, 1 rejected; doc approved (corrected) |
| driverescueattempt1 | 1 | 3 | 1 | ✅ 2026-10-03: 2 approved, 1 rejected; doc approved (corrected) |
| driverescueattempt2 | 1 | 6 | 1 | ✅ 2026-10-03: 5 approved, 1 rejected; doc approved (corrected) |
| personal | 2 | 7 | 0 | ✅ 2026-10-03: 5 approved, 2 rejected |
| finance (was marriagecomparison) | 1 | 5 | 3 | ✅ 2026-10-03: EP 4 approved, 1 rejected; DOC 3 → 2 pages approved (fixed/merged) |
| home-project | 2 | 8 | 1 | ✅ 2026-10-03: EP 3 approved, 5 rejected (duplicates); DOC 1 rejected (already in wiki) |
| obsidian (now llm-wiki) | 3 | 5 | 7 | ✅ 2026-10-03: EP 4 approved (2 retagged domain-epsilon), 1 rejected; DOC 1 fixed + 2 Phase-2.2 rebuilds approved, 4 rejected (already in wiki) |
| astrophotography | 2 | 11 | 2 | ✅ 2026-10-03: EP 4 approved, 7 rejected (duplicate singles); DOC 2 fixed (art-projects, Household) |
| dev | 4 | 7 | 7 | ✅ 2026-10-03: EP 7 approved (retagged capacities-mcp / mac-infra / project-epsilon-dev); DOC 5 merged into 2 pages, 1 rejected, 1 moved to project-epsilon-dev |
| archive | 1 | 13 | 2 | ✅ 2026-10-03: EP 10 approved, 3 rejected; DOC 2 fixed (Household; consolidation lessons added) |
| interlock | 3 | 15 | 5 | ✅ 2026-10-03: EP 9 approved, 6 rejected; Forge/Max EPs retagged openclaw; DOC 3 → 1 decisions page, Forge env merged into OpenClaw handoff |
| openclaw | 9 | 15 | 8 | ✅ 2026-10-03: EP 15 approved, 2 rejected (+ Forge/Max EPs from interlock); DOC 10 → 6 pages (Gateway, Max, Tools, Morning Brief, Forge handoff, OpenClaw.md links) + overflow page approved |
| project-epsilon-dev | 12 | 20 | 10 | ✅ 2026-10-03: EP 14 approved, 6 rejected; DOC 15 → 6 pages + 2 as-is approved (plugin docs live in WIKI/projects/Career-Navigator/); 16 domain-epsilon-tagged EP + 3 DOC in the same conversations deferred to the domain-epsilon batches |
| llm-wiki | 26 | 73 | 22 | ✅ 2026-10-03 (batch 6, autonomous): a folder bucket (sessions run in the vault); items retagged by topic (home-project 28, domain-epsilon 23, mac-infra 18, exhibition 3, interlock 2; vault tooling stays llm-wiki). EP 67 approved, 6 rejected; DOC 6 new/rebuilt + 3 Phase-2.2 rebuilds approved, 16 rejected (mostly whole-page rewrites already covered; one EV timeline rewrite misdated events) |
| context-memory-fabric | 26 | 174 | 63 | ✅ 2026-10-03 (batch 7, autonomous): EP 86 approved, 87 rejected (process narration, transient steps, singles duplicating merges), 1 deferred (flag below); 1 retagged claude-tooling. DOC 63 → 13 new/additive pages verified against the code (FalkorDB Browser guide, retrieval config, ChatGPT import, eval methodology, graph quality, wiki extraction, typed-recall profile, backlog triage; additive to Cross-Harness, Graph-Deduplication, Antigravity, Clients, OpenClaw.md) + 3 Phase-2 rebuilds approved, 60 rejected |
| domain-epsilon | 172 | 379 | 123 | ✅ 2026-10-03 (batch 8, autonomous; includes the 16 EP + 3 DOC deferred from batch 5): EP 175 approved, 202 rejected (workflow narration, transient steps, five copies of one recruiter-outreach session), 2 deferred (flags below); 11 retagged (9 project-epsilon-dev for voice-MCP/dashboard/packaging, 1 claude-tooling, 1 interlock). DOC 123 → 7 new/additive pages (Career-Voice-MCP, Resume-Guidelines, Job-Search-Playbook, CorpD role page; additive to Alternate roles, DBVendorA, CorpE), 122 rejected, 1 deferred |

**From batch 6 on** (User, 2026-10-03): Claude applies its own recommendations per batch and lists uncertain items under *Flagged for User* below instead of waiting for a per-batch go-ahead.

**Flags raised during review and how User resolved them (2026-10-03):**
- *Project slugs (batch 6):* `mac-infra` (NAS, home network) and `exhibition` (Now & Then framing) kept.
- *Wiki homes (batch 6):* `WIKI/projects/LLM-Wiki/` kept.
- *Review-format preference (batch 7):* approved. It has been valid since 2026-09-24 19:02 UTC (Antigravity review session) with no later reversal, so it supersedes the 2026-09-20 lean-table rule (memory updated).
- *Celestial bodies as Place:* production `RecallPlace` (typed-recall) now matches the `typed` profile's `Place`.
- *Eval privacy in the live wiki:* the Cross-Harness-Memory-Comparison update is redacted to case IDs and scores (`prop_20261003_214844_5f5c4481`, superseding batch 7's `..._9106a5e2`).
- *OpenClaw.md:* only batch 7's update (`prop_20261003_211559_40154e1d`) applies; batch 4's `..._2f967da7` is marked superseded.
- *Stale doc:* `docs/SETUP.md`'s FalkorDB timeout section was rewritten to match `docker-compose.yml`.
- *Career doc policy:* the 29 rejected role pages and 7 outreach notes with new facts became 36 approved EPs (`reason:reviewdoc:*`), attributed to their source conversations and dated from the transcripts. The batch-5 Market-Brief-Process page is the single brief-overview page.
- *Black-hole glass sculpture:* folded into the Neighbors series page (`prop_20261003_214844_df5e6072`); the EP was retagged `exhibition` and approved.
- *Mirror-Hanging-Plan proposal:* closed (its change was already live).
- **Still open:** the "email to ColleagueA about lab hosting" EP is deferred until User picks a project slug (`openclaw`, `mac-infra` or `personal`).

**Phase 3 (review) closed 2026-10-03.** One EP was left pending: "email to ColleagueA about lab hosting", waiting on a project slug.

**Review follow-ups (User, 2026-10-03, commit `99ae3ad`):**
- The 29 rejected role pages and 7 outreach notes with new facts became 36 approved EPs (`reason:reviewdoc:<conv>:<slug>::extract@1.6`). They were staged by `ConsolidationStore.record_reasoning_episode`, cite real events from their source conversation, and are dated from the transcript. Journal backup: `imports/journal/bak/journal-20261003-pre-reviewdoc-eps.db`.
- `RecallPlace` (typed-recall) now includes celestial bodies, matching the `typed` profile. The MCP server was restarted.
- `docs/SETUP.md`'s FalkorDB timeout section was rewritten (persisted at 30000 ms by `docker-compose.yml`).
- Two approved proposals were marked superseded through `server.proposals._save_proposal`, because `review_proposal` refuses to re-review: batch 4's OpenClaw.md update and batch 7's Cross-Harness update. The Mirror-Hanging-Plan proposal was closed.

**Phase 4 (completed 2026-10-04, all via MCP, dry run first):**
- **DOCs applied:** 59 applied (3 in pilot, 56 in main session: 43 creates, 16 updates); 1 superseded (`prop_20260923_022621_97766243` replaced by `prop_20261003_224921_cb5972d9` against live page).
- **EPs promoted:** 443 successfully promoted across 23 batches (3 in pilot + 440 across project batches; 8 failed on timeout after retry; 1 extract@1.2 superseded skipped; 0 remaining unattempted).
- **Retagged & approved:** "email to ColleagueA about lab hosting" EP retagged with `openclaw` and approved (2026-10-03).
- **Over-broad threads closed:** `scripts/close_overbroad_threads.py --apply` closed 12 attractor threads spanning >10 conversations (896 open, 507 resolved).
- **MS8 replay spot check:** verified (`scripts/replay_eval.py`; Hit@8=0.850 static, 0.875 with gold updates; zero production regressions).
- **Session captured & promoted:** recorded as `antigravity-misc-001` and `antigravity-misc-002`.

