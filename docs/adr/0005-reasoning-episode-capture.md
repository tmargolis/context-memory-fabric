# ADR 0005: Reasoning-episode capture — a `reasoning_kind` property, not new classification types

**Status:** Proposed (design only — no code in this ADR)
**Implemented:** No. `HeuristicPatternPolicyV1` (Milestone 3) remains the only classification policy in production. This ADR scopes a model-based companion policy and a new consolidation stage, landing as **MS3.5 — Reasoning-episode consolidation**, the next milestone, run before the MS4 capture adapters (see IMPLEMENTATION-PLAN.md amendment A5).
**Date:** 2026-09-05
**Relationship to ADR 0004:** extends decision 2, reframes decision 4; decisions 1 and 3 carry forward unchanged. See the amendment block at the top of [ADR 0004](0004-message-classifier-v2-and-work-journal.md).

**Execution order (set 2026-09-05 with Todd):** MS3.5 runs *before* the MS4b–MS4d capture adapters. The ~19,000 events already in the journal (native ChatGPT, Claude, Gemini) are the substrate for getting episode-reasoning identification right; realtime ingestion of new Claude Code / OpenClaw traffic is secondary and inherits a working consolidation layer rather than driving its design.

## Context

The classification pipeline recognizes **closure**, not **thinking**. `CandidateClassifier` (`server/importer.py`), wrapped by `HeuristicPatternPolicyV1` (`server/policies/heuristic_v1.py`), decides "episodic" from three lexical signals: an episodic section heading (`decision|milestone|changelog|incident|release`), an episodic verb (`decided`, `chose`, `shipped`, `fixed`, `migrated`, `deployed`, `resolved`, `launched` — all past-tense completion verbs), or an explicit preference change. Auto-accept (`server/consolidation/pipeline.py`, threshold `0.75`) additionally requires an exact/day-precision date, so in practice only a **dated decision** clears it.

Everything that is *thinking in progress* has no positive signal and lands in `ambiguous` → `queued_for_review` → the 9,658-row pile nothing drains:

| Content | Current fate |
|---|---|
| "I'm trying to work out whether the thread key should be a string or an embedding" | ambiguous — no closure verb |
| "the reason promotion produced junk is Case C trusting a bare date" | ambiguous — analysis, not a decision |
| "tried the 500-char guard, auto_accepted dropped 119→7, 3 still wrong" | ambiguous / too-long downgrade |
| "considered Postgres, staying on SQLite because single-writer is fine" | the rejected alternative is lost entirely |
| "I want a broader classifier" (stated intent) | ambiguous |

This is the same gap the "deriving work-session memories instead of only suppressing pasted technical content" discussion in IMPLEMENTATION-PLAN.md's promotion milestone was circling, and a superset of it: not just "summarize the debugging session" but "keep the exploration, the analysis, the experiments, the dead ends."

Three constraints shape the fix:

1. **No new classification types.** `episodic` / `durable_candidate` / `ambiguous` / `non_memory` stay exactly as they are. A `technical` or `reasoning` category was already rejected in ADR 0004 decision 4 for siloing the content most worth keeping; this ADR reaffirms that and goes further — the *kind of thinking* is a **property on the derived memory**, not a fifth category and not a proliferation of `memory_type` values.
2. **A regex cannot see reasoning.** "decided" is a keyword match; "the reason X fails is Y" is not, and summarizing an exploration requires understanding what the text is *about*. ADR 0004 already accepted a hybrid (lexical pre-filter + model step); this ADR makes the model step do the substantive work.
3. **The unit of analysis is wrong.** One turn rarely contains a whole thought. An investigation spans a dozen turns; a hypothesis is raised in one and tested three turns later. Single-event classification (what the pipeline does today) structurally cannot assemble that.

## Decision

### 1. Add a `reasoning_kind` property to derived memories — a starter vocabulary, not an enum of categories

`DerivedMemory` (`server/core/models.py`) gains one optional field, `reasoning_kind: Optional[str]`, and `derived_memories` gains the corresponding nullable column. It names the kind of thinking a memory represents. Starter vocabulary (working set — extend by adding a string, not by migrating a schema):

| `reasoning_kind` | What it marks |
|---|---|
| `decision` | A choice made between options (what closure-detection already finds — now tagged, same `episodic` category) |
| `investigation` | Actively working to understand something not yet understood; has an open/resolved status |
| `hypothesis` | A proposed explanation or prediction, not yet tested |
| `experiment` | A deliberate trial and what it showed |
| `finding` | Something concluded or learned, usually from an investigation or experiment |
| `rejected_alternative` | An option considered and consciously set aside, with the reason |
| `retrospective` | An after-the-fact assessment of how something went |
| `plan` | A stated intention or approach for work not yet done |

Rules:

- `reasoning_kind` is **meaningful on `episodic`** memories. It is permitted but optional on `durable_candidate` and `ambiguous` (e.g. an unresolved `investigation` that is currently `ambiguous`). It is never set on `non_memory`.
- It is **not** a classification outcome and does not affect the category. An event is still classified `episodic`/`durable_candidate`/`ambiguous`/`non_memory` exactly as before; `reasoning_kind` is added alongside.
- Single-valued for now. If real data shows genuinely multi-kind episodes are common (an `experiment` that is also its own `finding`), it becomes a list later — additive.
- The name `reasoning_kind` is provisional; settle it when the field is actually added in MS3.5.

### 2. Extend ADR 0004 decision 2 — the model step derives typed reasoning episodes

The cheap lexical pass (`HeuristicPatternPolicyV1`) is kept, with two jobs: (a) unambiguous exclusions (empty content, `actor_type != 'user'`, obvious trivia), unchanged; (b) **triage** — flag spans that contain substantive thinking (deliberation markers, question language, technical/log-shaped content — the same signal currently *causing* false positives) and route only those to the model. Everything else is not worth an extraction call. This is the sampling/triage posture the MS4a privacy/cost gate already chose.

The model step is a new policy, `ReasoningEpisodePolicyV1`, implementing the existing `ExtractionPolicy` protocol (`server/policies/protocols.py`) unchanged in shape. It is asked for a small structured record per reasoning episode: `{category, reasoning_kind, statement, driving_question?, rationale?, alternatives?, status?, thread_key?, confidence}`. `category` is still one of the four. `statement` is a concise synthesis ("investigated why promotion produced junk; root cause was Case C trusting a bare date"), not the raw turns.

**User turns only seed an episode; assistant turns are context, for now.** The per-event `actor_type != 'user'` guard (ROADMAP principle 9) is kept unchanged: a reasoning episode is derived from what the *user* said and thought, and assistant-authored text in the window is read as supporting context but never establishes a fact or stands alone as an episode. **Flagged for review (Todd, 2026-09-05):** much real reasoning — root-cause analysis, trade-off articulation, a stated finding — is authored by the assistant in reply to a terse user prompt, so "ignore assistant replies" may systematically under-capture exactly the thinking this milestone exists to keep. Sticking with user-only for MS3.5; revisit once MS3.5 has real output to measure against (see the open question below and the MS3.5 exit gate).

### 3. The unit of analysis is a topical window, not a turn

A new consolidation stage segments a conversation/session into bounded topical windows (a span of consecutive turns on one subject) and runs `ReasoningEpisodePolicyV1` per window, producing 0–N reasoning episodes. Segmentation strategy — fixed turn-count windows, embedding-similarity boundaries, explicit topic-shift cues, or model-driven segmentation inside the same call — is deferred to implementation and needs real Claude Code transcripts to choose (this is the same open problem as ADR 0004's thread-keying question, and should be settled together).

### 4. Reframe ADR 0004 decision 4 — the cross-conversation thread carries the reasoning narrative

ADR 0004's thread (a small index of open project threads, checked before extraction) is kept as the mechanism, but its purpose changes: it is not a bare `intent → in-progress → blocked → done` status machine, it is the container for the *narrative of thinking* on a topic — its driving questions, the hypotheses raised and their fate, the findings, the alternatives weighed and rejected, the dead ends kept on purpose, and the decisions if any. `reasoning_kind`-tagged episodes are its entries. Thread membership remains metadata (a `thread_id`-shaped reference on `DerivedMemory`), never a category.

### 5. Loosen the MS3 auto-accept gate

The `0.75` threshold and its de-facto "must be a dated decision" requirement were set for personal-fact precision against a ChatGPT corpus. Reasoning episodes are inherently lower-confidence and frequently undated in the text — but their `event_date` is the window's time span, resolved from event metadata (ADR 0004 decision 1), which *is* reliable. So:

- Reasoning episodes produced by `ReasoningEpisodePolicyV1` may auto-accept on the model's own confidence, without requiring an in-text or curated date.
- The numeric threshold is re-derived from the rebuilt fixture set (decision 7) during MS3.5, not carried over.
- **Accepted consequence:** the review backlog grows. Todd's call (2026-09-05): acceptable for now; better backlog-trimming is later work and does not block this. MS6 review tooling becomes load-bearing sooner as a result — noted, not solved here.

### 6. Extraction runs on hosted Gemini now, moves to Spark local inference later

Reasoning extraction sends multi-turn windows (larger prompts than single turns) through the rate-limited Gemini path (`server/core/rate_limiter.py`). Content-class filtering stays deferred (unchanged from the MS4a gate). Todd's direction (2026-09-05): acceptable in the interim; the reasoning workload is the intended first tenant of the Spark local-inference stack once its validation sequence runs, at which point sensitive-class routing is revisited.

### 7. Downstream artifacts derived from the ChatGPT *summary* are abandoned, not rebuilt in place

The 95 reconstructed/backfilled ChatGPT events (57 `memory-fabric` backfill + 38 `default_db` recovery) were already deleted from the journal. Their downstream artifacts — including the positive side of `tests/fixtures/memory_quality/labeled_events.json` — are **not** reconstructed from the summary. The full native ChatGPT logs (6,343 journaled events) plus Claude and Gemini are the substrate MS3.5 works from; Claude Code transcripts join later (MS4b) but MS3.5 does not wait on them. The memory-quality fixture's positive side is rebuilt during MS3.5 with `reasoning_kind`-labeled examples drawn from that native content.

## Consequences

- `DerivedMemory` and the `derived_memories` table grow one optional field/column (`reasoning_kind`). Additive; existing rows read as `NULL`. This is MS3.5 task 1.
- `ReasoningEpisodePolicyV1` is a new `ExtractionPolicy` implementation; the protocol itself does not change. `PolicyContext` grows to carry the topical window and open-thread index (additive optional fields, as ADR 0004 already anticipated).
- A new consolidation stage (windowing + model extraction) sits alongside the existing per-event pass. `HeuristicPatternPolicyV1` keeps running; the two are not mutually exclusive — an event can get a v1 lexical derivation and be part of a v-next reasoning episode.
- Model calls now happen in consolidation. Bounded by the existing rate limiter; measured against the MS4a gate's ledger, not a new cost decision.
- Segmentation is the hard, unsolved part and needs real data. Do not build it blind — prototype `ReasoningEpisodePolicyV1` offline against the Claude/Claude-Code slice of the existing journal and inspect output before committing the window schema.
- The review backlog grows (decision 5). MS6 review/governance tooling is now on the critical path for this feature to be usable, not a "should not slip."
- No new classification category anywhere. Anyone reaching for a fifth `ExtractionCategory` value should be redirected to `reasoning_kind` (a steer note is added to `server/policies/protocols.py`).
- **Known limitation carried in on purpose:** user turns seed episodes, assistant turns are context only (per-event `actor_type != 'user'` guard unchanged). This may under-capture reasoning that lives in assistant replies to short prompts. Not fixed in MS3.5; measured there and reviewed after (see decision 2 and the open question).

## Alternatives considered

- **Add `reasoning` / `technical` / `exploration` as classification categories.** Rejected — ADR 0004 decision 4's reasoning (siloing the content most worth keeping) still holds, and Todd explicitly wants the four categories kept.
- **Proliferate `memory_type` values (`investigation`, `finding`, `hypothesis`, …).** Rejected — `memory_type` is the coarse evidence/memory/knowledge distinction; overloading it with cognitive modes conflates two axes. One `reasoning_kind` property keeps them separate and extends without schema churn.
- **Keep regex-only, add more guards.** Rejected — the residual false positives in the promotion milestone (short log snippets with an incidental episodic verb and an embedded timestamp) are already documented as unfixable by shape/length heuristics; and this cannot capture exploration or analysis at all, only suppress noise.
- **One-line work-session labels only** (the earlier "deriving work-session memories" sketch). Rejected as too lossy — a label ("debugged the altitude bug") keeps that work happened but not what was learned, what was tried, or why a direction was taken. That is the part Todd wants kept.
- **Per-turn model classification, no windowing.** Rejected — ADR 0004 decision 3's unit-of-analysis argument: a stronger per-turn classifier still cannot assemble an investigation that spans a dozen turns.

## Open questions (deferred to implementation)

- Topical-window segmentation strategy — resolve together with ADR 0004's thread-keying question, against real Claude Code transcripts.
- **User-only vs. include assistant reasoning.** MS3.5 keeps the `actor_type != 'user'` guard — episodes derive from user turns, assistant text is context only. Review after MS3.5's first real run: measure how often an episode's substance is only in assistant turns, and if it's material, decide whether assistant-authored reasoning can seed an episode (marked assistant-originated, still not a "personal fact" under principle 9, still queued for review — not auto-accepted).
- Final `reasoning_kind` name and whether the vocabulary is closed, open-with-a-registry, or free text with a documented starter set.
- The re-derived auto-accept threshold for reasoning episodes, from the rebuilt fixture.
- Whether a reasoning episode's evidence is the full turn span or a sampled subset, for journal-link fidelity vs. storage.
- Interaction with promotion: does a `finding` promote to durable knowledge differently than a `decision`? (Likely yes — a `finding` is closer to Wiki-shaped content — but out of scope until MS5's `propose_knowledge_change`.)
