# Context Memory Fabric — Implementation Plan

**Companion to** [ROADMAP.md](ROADMAP.md) — the roadmap states *what and why*; this plan states *how, in what order, and how we know it worked*.

This file is the **index**. Detail lives in two companions:

| File | Contents |
|---|---|
| **[plan-active.md](plan-active.md)** | the milestones still to do — full task lists, acceptance tests, exit gates |
| **[plan-history.md](plan-history.md)** | completed milestones (MS0.5–MS3.6, MS4a, MS6a, MS6b, MS6c, MS6d) + the Spark local-inference migration — decisions, corrections found while building, exit-gate answers |

## How to use

Work proceeds **one milestone at a time**. Each milestone in `plan-active.md` has a Goal + Why now, a task list, Files touched, Acceptance tests (mechanically checkable), an Exit gate (a decision answered before the next milestone starts), Effort, and Risks. Nothing in a milestone begins until the previous exit gate is answered and the milestone is approved.

Task-list checkboxes: `[ ]` not started · `[/]` in progress · `[x]` done.

---

## Milestone sequence

**Execution order (resequenced 2026-09-06 with Todd).** Section numbers are stable identifiers — they no longer run in numeric order. The driving change: adding more *capture* (MS4b–MS4d) is dead weight while the retrievable graph is near-empty, so the near-term path drives the existing history all the way to *retrieval* first, then adds the other adapters.

| # | Milestone | Focus | Status |
|--:|---|---|---|
| 1 | [MS0.5](plan-history.md#ms05--baseline-correctness-and-wiring) | Baseline correctness & wiring | 🟢 done |
| 2 | [MS1](plan-history.md#ms1--extract-provider-interfaces-without-changing-behavior) | Provider interfaces | 🟢 done |
| 3 | [MS2](plan-history.md#ms2--canonical-event-journal-importers-and-backfill) | Canonical event journal + importers + backfill | 🟢 done |
| 4 | [MS3](plan-history.md#ms3--separate-capture-from-consolidation) | Capture / consolidation split | 🟢 done |
| 5 | [MS3.5](plan-history.md#ms35--reasoning-episode-consolidation-adr-0005) | Reasoning-episode consolidation ([ADR 0005](adr/0005-reasoning-episode-capture.md)) | 🟢 done 2026-09-07 |
| 6 | [MS3.6](plan-history.md#ms36--promotion-staged-memories-into-the-retrievable-graph) | Promotion: staged memories → retrievable graph | 🟢 done 2026-09-07 |
| 7 | [MS6a](plan-history.md#ms6a--review-surface) | Review surface — project-bucketed queue, audit chokepoint, bulk governance; tier-1 pass | 🟢 done 2026-09-08 |
| — | [*Spark migration*](SPARK-MIGRATION-PLAN.md) | Gemini → Spark-local inference | 🟢 done 2026-09-09 · [history](plan-history.md#spark-local-inference-migration--phases-0-6-2026-09-08--09) |
| 8 | [MS7](plan-history.md#ms7--context-assembly-quality) | Context assembly quality | 🟢 done 2026-09-10 |
| | | *— retrieval loop proven end-to-end here —* | |
| 9 | [MS6b](plan-history.md#ms6b--governance) | Governance — `explain()` into the graph, `correct_memory`, deletion propagation | 🟢 done 2026-09-11 |
| 10 | [MS6c](plan-history.md#ms6c--mcp-server-cross-agent-verification) | MCP server cross-agent verification — Claude Desktop (Cowork → Code mode) → Gemini Spark → ChatGPT | 🟢 done 2026-09-16 |
| 10.5 | [MS6d](plan-history.md#ms6d--durable-knowledge-proposal-review) | Durable-knowledge proposal review — list/get/review/apply over MCP | 🟢 done 2026-09-16 |
| 11 | [MS4a](plan-history.md#ms4a--mcp-boundary-capture-claude-desktop) | MCP-boundary capture — live cross-harness verification | 🟢 done 2026-09-16 |
| — | [**MS7b**](plan-history.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13) | *Experiment* — wiki-derived entity layer + enriched episode bodies | 🟢 done 2026-09-17 — enriched bodies adopted ([PR #7](https://github.com/tmargolis/context-memory-fabric/pull/7)), wiki-structured layer closed onto canonical `mem-fabric-local`; extraction-quality follow-ups in [Backlog](plan-active.md#backlog) |
| 11.5 | [**MS4a2**](plan-active.md#ms4a2--cowork-live-session-episodeentitywiki-capture-current-priority-2026-09-18) | Cowork live-session capture (`capture_session` tool) | 🟡 built + unit-tested 2026-09-18, live exit-gate dry run still to do |
| 12 | [MS4b](plan-active.md#ms4b--claude-code-desktops-code-tab-transcript-adapter--parked-fully-designed-2026-09-18) | Claude Code (Desktop's Code tab) transcript adapter | ⚪ parked, fully designed |
| 13 | [MS4c](plan-active.md#ms4c--openclaw-adapter-and-cmf-http) | OpenClaw adapter (+ `cmf-http`) | ⚪ |
| 14 | [MS4d](plan-active.md#ms4d--codex-and-gemini-cli) | Codex, Gemini CLI | ⚪ |
| 15 | [MS5](plan-active.md#ms5--knowledge-provider-generalization) | Knowledge-provider generalization | ⚪ |
| 16 | [MS8](plan-active.md#ms8--replay-and-evaluation) | Replay and evaluation | ⚪ |
| 17 | [MS9](plan-active.md#ms9--distribution-and-ecosystem) | Distribution | ⚪ |

All completed milestones' detail — what was built, corrections found, exit-gate answers — lives in [plan-history.md](plan-history.md). Open follow-on work from finished milestones (hardening, refinements, findings that didn't block) lives in [plan-active.md → Backlog](plan-active.md#backlog), not here.

**Why this order.** `get_context` / `recall` / `search_wiki` already work against whatever is in the graph plus the LLM Wiki. MS6a's tier-1 review is done — **295 reviewed episodes are now in the retrievable graph** (`mem-fabric-local`, Spark-local extraction) — MS7 (assembly quality) is **done** — `get_context` measurably beats both single-provider baselines (80% of a complete answer vs 50–53%), with the answer-quality eval kept as a reusable instrument. MS6b (correction / deletion / `explain()` into the graph) was sequenced *after* MS7: all of it serves the promoted rows, and the correction path should be designed after a real assembly pass, not before. MS6b's exit gate then unblocked a full corpus review pass (295 → 465 episodes) — with a real graph now worth retrieving from, **MS6c** checks that the MCP server actually works from the real client apps before more capture adapters (MS4b–MS4d) get built on top of it. MS5 comes after the retrieval loop is proven.

---

## Current state (2026-09-18)

**Done through MS4a/MS6c/MS6d/MS7b.** The pipeline runs end-to-end on Spark-local inference, retrieval is proven to beat single-provider baselines, governance (`explain`/`correct`/`delete`) and durable-knowledge proposal review both work live, and the MCP server is verified working inside all 4 real client apps Todd uses. **MS4a2's `capture_session` tool is built, unit-tested, and live** (18 registered tools) — its own exit gate (a live Cowork dry run) is still outstanding.

```text
source exports → journal (19,012 events) → per-event classification (heuristic v1.2)
                                          → reasoning episodes (model v0.3): ~1,310 staged / 659 threads
                                          → MS6a tier-1 + MS6b corpus review: 465 approved total
                                          → promotion → mem-fabric-local (qwen3.5-122b + nomic, 768-dim)
                                          → recall_mem() / get_context() / search_wiki() — verified live from
                                            Claude Desktop (Cowork + Code), Gemini Spark, and ChatGPT
```

- **Live graph:** `mem-fabric-local` — closed out by [MS7b](plan-history.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13) 2026-09-17: cloned forward from `mem-fabric-local-wiki` (which already held the enriched-content re-derivation of the full ledger) onto this canonical name, then pruned of its wiki-structured `:Section` layer (collapsed to `:Note`-level `MENTIONS`/`REFERENCES` first, so provenance survived). `Project`/`IN_PROJECT` and the un-corroborated wiki-only entities were deliberately left in place pending the two open items in [Backlog](plan-active.md#backlog). Rollback graphs retained untouched: `mem-fabric-local-ep`, `mem-fabric-local-wiki`, `mem-fabric-gemini`, `mem-fabric-local-glm`.
- **Live server:** `streamable-http` on `:8000` behind OAuth (DCR + PKCE, [PR #6](https://github.com/tmargolis/context-memory-fabric/pull/6)) — 18 registered tools (added `capture_session`, MS4a2), all 4 client families (Claude Desktop Cowork + Code, Gemini Spark, ChatGPT) connected and functionally verified. `docs/CLIENTS.md` reflects this as of 2026-09-18.
- **Review backlog, cleaned up 2026-09-18:** heuristic-pattern `queued_for_review` rows cut from 25,961 → 6,582 via `retire-stale-versions` (three reprocessed policy versions of the same events collapsed to the newest). `wiki-proposals/` and the new `episode-proposals/` mirror (1,243 files backfilled — 301 tier1 / 942 tier2) both now sort into `approved/`/`rejected/` subfolders on review instead of one flat directory. **Remaining:** OAuth hardening (no test coverage yet, plaintext token storage); `edit_memory` doesn't re-run fact extraction on correction; occasional transient 502s on the shared OAuth/tunnel path — all in [plan-active Backlog](plan-active.md#backlog), none blocking.
- **Live status:** `imports/ingest-pipeline-status.md` (gitignored; regenerate with `imports/tools/gen_ingest_report.py`).

Full build detail and corrections-found for every completed milestone — including the 2026-09-16 session that closed out MS6c/MS6d/MS4a (the OAuth build, the Cowork-vs-Code harness-identity finding, the Gemini Spark cross-harness write/correct test) — live in [plan-history.md](plan-history.md).

### Spark local-inference migration — done 2026-09-09

Own plan file: [SPARK-MIGRATION-PLAN.md](SPARK-MIGRATION-PLAN.md); Phase 7 A/B + rollback ledger: [spark-phase7-ab-log.md](spark-phase7-ab-log.md). Replaced the Gemini Developer API as CMF's LLM + embedding backend with DGX-Spark-served models.

- **Phase 7 / D1 answered:** GLM-4.7-Flash rejected on the A/B (pronoun entities, self-loops, paraphrase-spam, a hallucination — Gemini had zero of any). `unsloth/qwen3.5-122b-a10b` + a `custom_extraction_instructions` nudge matches Gemini on hygiene and closes to a ~5-pt recall gap. The hybrid (Gemini extract + local embed, via Phase 1's split provider vars) is the fallback if qwen regresses at scale.
- **The MS4a cost gate is superseded** (D5): local inference has no per-call cost — `server/core/rate_limiter.py` is unmetered on the local path, still a throughput control on the Gemini path.
- **Phase 7 checklist** is not fully closed — the D1 decision and re-promotion are done; discrete functional smokes (`remember()` round-trip, `search_wiki`, `get_context` both-provider) and the concurrency/throughput measurements are still open, tracked in the Spark log.

---

## Exit-gate decisions log

The distilled output of the plan — every gate answered, newest first.

| Milestone | Question | Answer (date) |
|---|---|---|
| **MS6c** | Does the MCP server actually work, end-to-end, inside Claude Desktop (both modes), Gemini Spark, and ChatGPT — and does CLIENTS.md reflect that? | **Yes.** All 4 client families connected via OAuth DCR+PKCE ([PR #6](https://github.com/tmargolis/context-memory-fabric/pull/6)) and functionally verified live (reads + write/correct). Required building a real auth server the milestone was explicitly scoped not to build — no connector UI (ChatGPT/Gemini/Claude Desktop) accepts a static bearer token. Two findings filed to Backlog, not blockers: `edit_memory` leaves stale fact edges on correction; the shared OAuth/tunnel path throws occasional transient 502s. (2026-09-16) |
| **MS6d** | Can durable knowledge be proposed, reviewed, and promoted into the corpus entirely through MCP, with no path from one tool call to a canonical write? | **Yes, live-verified against the real 76-proposal backlog**, not just tests: 1 applied (commit `6dfce163`), 75 rejected with recorded reasons. `apply_wiki_proposal`'s sha-guard + approved-only gate held throughout. (2026-09-16) |
| **MS4a** | Does the roadmap's 5-step live cross-harness test (write → verify provenance → retrieve from a 2nd harness → correct → verify current state) pass for real? | **Yes** — via MS6c: `remember()` from Gemini Spark, retrieved/corrected/re-verified from Claude Desktop Code mode, two independently-resolved harness identities. First attempt was a false positive (Gemini's chat reply claimed success with zero server-side trace) — take-away: verify server-side, not the client's own confirmation text. (2026-09-16) |
| **MS7** | Does cross-provider `get_context` measurably beat the single-provider baselines on graded real queries? | **Yes, decisively.** 30 queries, answers graded 0/1/2 vs hand-written gold: bare model 0.07 · `recall_mem` 1.00 · `search_wiki` 1.06 · **`get_context` 1.60 (80% of a complete answer)**; `+both` ≥ every single arm on all 30, lift +1.53. Progression 0.07 → 1.07 (search_wiki tokenizer, recall_mem render fidelity, get_context fan-in, multi-window snippets) → 1.60 (episode-content vector arm on `mem-fabric-local`). Same questions from Gemini/GPT/Claude's own memory: 10–18%, several confidently wrong. Instrument kept: `tests/fixtures/ms7_eval/`. (2026-09-10) |
| **Spark migration — Phase 7 / D1** | Which extractor for Spark-local inference — is a local model comparable to Gemini? | **`qwen3.5-122b-a10b` + `EXTRACTION_INSTRUCTIONS`.** GLM-4.7-Flash rejected on the A/B (47 pronoun entities, 8 self-loops, paraphrase-spam, a hallucination over the shared 275-statement set — Gemini had zero). qwen matches Gemini on hygiene; the instructions nudge cut its zero-entity rate 45% → 29% (Gemini 22–24%). 295 tier-1 episodes re-promoted into `mem-fabric-local` (768-dim); `.env` flipped. Hybrid (Gemini extract + local embed) is the fallback. (2026-09-09) |
| **MS6a** | Does project-bucketed bulk review clear the tier-1 backlog in usable time, and what is the keep rate? | **295 approved / 6 rejected** in one pass (verdicts bulk-written 2026-09-08). ~98% keep on the tier-1-*routed* slice — the `reasoning_kind` router already does the triage an LLM ranker would, so no ranker is worth building for this corpus. Wall-clock lives in the review artifact, not the journal. (2026-09-08) |
| **MS3.6** | Does a promoted reasoning episode survive `remember()` → `recall()` with provenance + entities intact? Does coverage auto-resolve shrink the heuristic pile safely? | Yes — round trip verified, Graphiti entities attached. Coverage auto-resolve: heuristic `queued_for_review` **9,833 → 6,582**, each superseded row keeps a `superseded_by` pointer. By-thread bulk-review speed is an MS6 question. (2026-09-07) |
| **MS3.5** | What is the reasoning-episode auto-accept threshold? Backlog burn-down plan? | **No threshold** — model confidence does not separate keep from drop (fixture: keep 0.95 vs drop 0.94). Auto-accept stays off; promotion is fully review-gated. Backlog handled by **three-tier routing** (promote / work-journal-in-thread / discard), reviewed **by thread** in MS6. `reasoning_kind` is a routing hint (`decision`/`plan` kept 63–78%, `experiment`/`hypothesis`/`finding` ~15%). Assistant-only-substance rate ~5% Claude / ~13–26% ChatGPT-Gemini — follow-on ("can assistant turns seed an episode") is a scoped-later mini-milestone. (2026-09-07) |
| **MS4a** | Privacy & cost for MCP-boundary capture | Gemini-only (no local routing yet), no content-class filtering, journal-everything / auto-consolidate-selectively, spend bounded by a hard free-tier RPM/RPD gate (`server/core/rate_limiter.py`). Capture registered unconditionally. (2026-09-04) |
| **MS3** | Which memories may be auto-accepted (per-event / lexical)? | Threshold **0.75** — requires *both* an episodic-shaped statement *and* an exact/day-precision date. Measured 100% precision on the fixture. Loosened for MS3.5 reasoning episodes (which then found no threshold works at all). (2026-09-03) |
| **MS2** | SQLite sufficient, or PostgreSQL? | **SQLite** — 19,012 events across 3 real sources, sub-second `stats`/`query`/`replay`. Revisit if MS4 volume grows an order of magnitude or MS4c cross-host writes make single-writer awkward. (2026-09-04) |
| **MS1** | Can a second memory provider be stubbed against the protocol without changing core? | **Yes** — `FakeMemoryProvider`/`FakeKnowledgeProvider` satisfy the protocols `isinstance`-verified, `get_context()` runs end-to-end against both with zero live deps, no Graphiti semantics leaked. (2026-09-03) |
| **MS0.5** | Which FalkorDB graph is production? | **`memory-fabric`**, pinned as `FALKORDB_DATABASE` in `.env`. `default_db` cleared, `cmf_chatgpt_000` deleted (snapshots taken first). (2026-09-03) |

---

## Cross-cutting concerns

### Privacy and cost

*(Historical framing — as of 2026-09-09 extraction + embedding run Spark-local, not on Gemini; see the amendment below.)* Graphiti sends every ingested episode to an LLM for entity extraction + embedding. At curated volume that's a bounded, deliberate exposure; continuous capture across Claude Desktop / Claude Code / OpenClaw changes the posture (every turn becomes an extraction call; the corpus already holds medical, financial, legal material). On the Gemini path cost was also unbounded and unmeasured — the reason for the rate-limiter gate.

**Standing policy (answered at the MS4a gate, 2026-09-04):**
1. **Journal everything locally** (cheap), **consolidate selectively** (expensive, remote) — the natural shape of the MS2/MS3 split, and the default.
2. **Budget ceiling** — a hard free-tier RPM/RPD gate (`server/core/rate_limiter.py`), not a soft dollar estimate; capture continues to the journal after the cap so nothing is lost.
3. **Local extraction** for sensitive content classes — deferred. The reasoning workload (MS3.5) is the intended first tenant of the Spark local-inference stack once its validation sequence runs; content-class routing is revisited then, not before.

**Amended 2026-09-09 (Spark migration D5).** The Spark local-inference stack is now built (Phases 0-6). On `CMF_LLM_PROVIDER=local` there is no per-call cost, so the item-2 ceiling stops being a spend control and becomes a pure throughput control; the ledger is not touched at all on the local path. On the Gemini path the ceiling stands, and Phase 4 closed a real gap — the embedder was never metered, and ~20 embeddings/episode (not the assumed ~3 LLM calls) was the constraint that actually exhausted the free tier. Content-class routing is still deferred; the whole reasoning workload now runs local regardless of class.

### Observability

Track from MS2 onward, not retrofitted: capture success/lag, consolidation latency/failure, extraction precision/rejection rate, duplicate/conflict rates, retrieval relevance, temporal correctness, provenance coverage, token cost per harness, provider latency, deletion/correction propagation.

### Compatibility

Version every canonical schema. Maintain backward-compatible MCP tool aliases through modularization. Keep CMF identifiers distinct from provider-native identifiers. Do not leak Graphiti types into the public contract.

