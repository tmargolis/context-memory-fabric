# ADR 0004: Conversation-aware classification, metadata-grounded dates, and the project work journal

**Status:** Proposed (design only — no code in this ADR)
**Implemented:** No. `HeuristicPatternPolicyV1` (Milestone 3) remains the only classification policy in production; this ADR scopes its successor.
**Date:** 2026-09-05

> **Amended by [ADR 0005](0005-reasoning-episode-capture.md) (2026-09-05).** ADR 0005 broadens this ADR's scope from "recognize technical project work" to "capture the reasoning itself — exploration, analysis, experiments, dead ends."
> - **Decision 1 (dates from metadata):** carries forward unchanged.
> - **Decision 2 (hybrid classification):** *extended.* The model step no longer just returns an episodic/durable/ambiguous verdict — it derives typed reasoning episodes over a window of turns. The four categories are unchanged; the kind of thinking is a new `reasoning_kind` **property**, not a category.
> - **Decision 3 (cross-conversation context):** carries forward unchanged as the mechanism.
> - **Decision 4 (linked thread, no new memory type):** *reframed.* The "no new memory type / no new category" half is reinforced. The "thread = `intent → in-progress → blocked → done` task-progression timeline" half is replaced: the thread is the container for the reasoning narrative (driving questions, hypotheses and their fate, findings, rejected alternatives, dead ends), and its entries are `reasoning_kind`-tagged episodes.
>
> Implementation is **MS3.5 — Reasoning-episode consolidation**, the next milestone, run before the MS4 capture adapters (IMPLEMENTATION-PLAN.md amendment A5).

## Context

`HeuristicPatternPolicyV1` (`server/policies/heuristic_v1.py`) wraps `server.importer`'s `CandidateClassifier` + `TemporalExtractor`: pure regex/lexical pattern matching against one `SourceEvent`'s text, plus a little same-turn context (`PolicyContext.preceding_assistant_text`/`conversation_title`/`section_heading`). It is deliberately conservative and, per the Milestone 3 exit-gate numbers in IMPLEMENTATION-PLAN.md, measurably precise on the corpus it was tuned against (imported markdown notes and historical chat exports). Extending it to live capture of day-to-day technical work (Claude Code sessions, MS4b) surfaced three mismatches between how that content is actually shaped and what a single-event regex classifier can see.

**1. Dates live in event metadata, not in message text, for live conversation.** `TemporalExtractor.extract_date()` mines a date out of the text itself. That is a real signal in retrospective prose ("On March 3 I switched jobs") — which is why `heuristic_v1.py` already special-cases it for backfilled/imported events (`is_backfilled`, gated to `metadata.provenance_reconstructed=True`) after MS3 measured that applying it unconditionally produced 50% false positives on live turns. The actual usage pattern this ADR is scoped for is more specific than "no date in the text": *intent and outcome are reported in separate messages, often days apart, and neither one contains a date string* — "I'm thinking about migrating the journal store to v3" now, "that's live" weeks later. Text-mining returns nothing for either message; both messages' own `observed_at` already has the correct answer to "when was this said," it's just the wrong question being asked of the wrong field.

**2. A regex/lexical layer, applied to one isolated message, cannot link intent to outcome.** This is a unit-of-analysis problem, not only a method problem: even a stronger per-message classifier still cannot recognize that two messages, possibly in different conversations, concern the same undertaking. `PolicyContext` today only carries same-turn/same-conversation hints; there is no notion of a thread that persists across conversations and across time.

**3. The category vocabulary has no home for ongoing project work.** An early sketch for v2 considered adding a `technical` category to route developer/tool chatter out of the main classification lanes. That is the wrong fix: it would silo exactly the content most worth remembering — migrations, decisions, bugs found and fixed, milestones hit are prime episodic and durable material, not a separate, lesser lane. What's missing is not a category but an axis: *is this message part of a tracked project thread*, independent of whether it lands in `episodic`/`durable_candidate`/`ambiguous`.

## Decision

**1. Live-captured event dates always resolve from `SourceEvent` metadata, never from text-mining.** `event_date` for a `turn.completed` (or equivalent live) event is its own `observed_at`/`event_date`/`date_precision` as journaled — full stop. `TemporalExtractor.extract_date()` stays scoped to the import/backfill path, where source content is retrospective narrative and an in-text date is a deliberate authorial signal (generalizing the existing `is_backfilled` gate from an exception into the rule: text-mined dates are an *import-format* concern, never a *live-conversation* concern). Concretely, the v2 policy must not call `TemporalExtractor` against live turn content at all.

**2. Classification stays hybrid, not regex-only.** The cheap lexical pass is kept as a fast pre-filter for unambiguous exclusions (empty content, `actor_type != 'user'`, obviously trivial turns) — the same job `HeuristicPatternPolicyV1` already does well and cheaply. Anything not confidently excluded is routed to a model-based classification step for the harder judgment (episodic/durable/ambiguous, and thread-membership per point 3). This follows the cost posture the MS4a privacy/cost gate already accepted — capture everything cheaply and locally, consolidate selectively and remotely — rather than opening a new privacy/cost decision; the model call should reuse the existing rate-limited Gemini path (`server/core/rate_limiter.py`), not a new one.

**3. Classification context must span conversations, not just one conversation's preceding turn.** `PolicyContext` (or a new, wider context passed alongside it) needs to expose previously-opened project threads a candidate event might continue or resolve — not full semantic search over the whole journal, but a small, purpose-built index of open threads (topic/project key, first-seen event, status, linked event IDs) that a new consolidation stage checks candidate events against before extraction. This is what makes "I'm thinking about X" (message 1) and "done, X is live" (message 400, different conversation, two weeks later) resolvable as the same thread.

**4. The output is a linked thread on top of ordinary derived memories, not a new memory type.** No `technical` category, no separate lane. A message about technical work is classified exactly as any other message would be (episodic/durable_candidate/ambiguous/non_memory) and, when it matches or opens a recognized project thread, is additionally linked into that thread via a `thread_id`-shaped reference on `DerivedMemory`. The thread itself is then queryable as a work journal, and individual events within it remain eligible to promote into durable knowledge (e.g., a decision made mid-thread) exactly as they would outside a thread. Being part of a thread is metadata, not a competing classification outcome. *(Amended by ADR 0005: the "no new memory type / no new category" principle stands and is reinforced by a `reasoning_kind` property rather than any new type. The originally-stated framing of the thread as a `intent → in-progress → blocked → done` task-progression timeline is replaced — the thread carries the full reasoning narrative, and its entries are `reasoning_kind`-tagged episodes.)*

## Consequences

- `ExtractionPolicy`/`PolicyContext` (`server/policies/protocols.py`) need to grow to carry thread context; this is additive (new optional fields/protocol) rather than a breaking change to v1, which can continue running unmodified since the protocol is already versioned per-policy (`name` + `version`).
- A new small persistent structure is required — open project threads — most naturally alongside the existing `derived_memories` staging table in `server/consolidation/`, not a new top-level store.
- Model-based classification is a new call site against the MS4a cost gate (previously scoped to entity extraction/embedding, not per-message classification); it must be measured and bounded before enabling by default, using the rate limiter already built rather than an unbounded new path.
- This ADR makes no code changes. `HeuristicPatternPolicyV1` keeps running in production. Implementation is **MS3.5**, ahead of the MS4 capture adapters — the existing ~19k-event journal (ChatGPT/Claude/Gemini) is the substrate, not live Claude Code traffic. *(This ADR originally proposed timing it "alongside or just before MS4b (Claude Code)"; superseded 2026-09-05 — see ADR 0005 and the amendment block at the top.)*

## Alternatives considered

- **Keep classifying one message at a time, just with a better (model-based) classifier.** Rejected: a stronger per-message classifier still cannot see across the time gap between intent and outcome; the limitation is the unit of analysis, not only the method.
- **Add a `technical` category to filter developer/tool chatter into its own lane.** Rejected: it would silo the content most worth remembering as episodic or durable knowledge, and loses the "why" behind a decision — exactly what a work journal needs to keep.
- **Text-mine dates uniformly regardless of source.** Rejected: already measured wrong for live-captured content during Milestone 3 (the 50%-false-positive incident recorded in IMPLEMENTATION-PLAN.md), and the intent/outcome usage pattern this ADR targets means text-mining would silently find no date in either the message that most needs one.

## Open questions (deferred to implementation)

- How threads are keyed and matched — exact project-name/topic string vs. embedding similarity — needs real conversation data to validate.
- Where the new model-classification and thread-matching call sites are bounded against the MS4a cost gate; this reopens that gate's scope and needs its own measurement, not an assumption that existing headroom covers it.
- Whether thread status (open/blocked/done) is inferred automatically or requires an explicit approval step under Milestone 6 governance.
