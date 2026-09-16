# Context Memory Fabric — Implementation Plan

**Companion to** [ROADMAP.md](ROADMAP.md) — the roadmap states *what and why*; this plan states *how, in what order, and how we know it worked*.

This file is the **index**. Detail lives in two companions:

| File | Contents |
|---|---|
| **[plan-active.md](plan-active.md)** | the milestones still to do — full task lists, acceptance tests, exit gates |
| **[plan-history.md](plan-history.md)** | completed milestones (MS0.5–MS3.6, MS4a build, MS6a, MS6b) + the Spark local-inference migration — decisions, corrections found while building, exit-gate answers |

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
| 7 | [MS6a](plan-active.md#ms6a--review-surface--built) | Review surface — project-bucketed queue, audit chokepoint, bulk governance; tier-1 pass | 🟢 done 2026-09-08 |
| — | [*Spark migration*](SPARK-MIGRATION-PLAN.md) | Gemini → Spark-local inference | 🟢 done 2026-09-09 · [history](plan-history.md#spark-local-inference-migration--phases-0-6-2026-09-08--09) |
| 8 | [MS7](plan-history.md#ms7--context-assembly-quality) | Context assembly quality | 🟢 **done 2026-09-10** — answer-quality eval; `get_context` 0.07 → **1.60 / 80%** of a complete answer, beats every single-provider baseline. Refinements → [plan-active Backlog](plan-active.md#backlog) |
| | | *— retrieval loop proven end-to-end here —* | |
| 9 | [MS6b](plan-history.md#ms6b--governance) | Governance — `explain()` into the graph, `correct_memory`, deletion propagation | 🟢 **done 2026-09-11** — live-FalkorDB exit gate passed 6/6; a corpus review pass ([plan-active Backlog](plan-active.md#backlog)) then took the graph 295 → 465 episodes using it |
| 10 | [**MS6c**](plan-active.md#ms6c--mcp-server-cross-agent-verification-2026-09-11) | MCP server cross-agent verification — Claude Desktop (Cowork → Code mode) → Gemini Spark (web/mobile) → ChatGPT | 🔴 **in progress** — all 4 client families completed OAuth DCR (2026-09-14); the auth server MS6c was scoped *not* to build shipped in [PR #6](https://github.com/tmargolis/context-memory-fabric/pull/6) (2026-09-16). Functional passes evidenced for all 4 (ChatGPT 25 tool calls, Claude Desktop 7, Code 4, Gemini 2). **Not done:** MS4a's 5-step cross-harness write/correct test (never run — no `remember` from either Claude harness, no correction call from any), plus two CLIENTS.md sections. Hardening → [plan-active Backlog](plan-active.md#backlog) |
| 10.5 | [**MS6d**](plan-active.md#ms6d--durable-knowledge-proposal-review-2026-09-16) | Durable-knowledge proposal review — list/get/review/apply over MCP | 🔴 **in progress 2026-09-16** — closes MS6's unbuilt "proposing durable-knowledge changes" deliverable; 76 proposals inert since 2026-09-01. All 5 tools built, tested, merged to `main`; live server restarted to serve them. **Not done:** the real 76-proposal backlog is untouched by them, and no live MCP client has exercised the flow end-to-end yet |
| — | [**MS7b**](plan-active.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13) | *Experiment* — wiki-derived entity layer (`mem-fabric-local-wiki`) + enriched episode bodies | 🔴 **in progress**, branch `ms7b-wiki-entities` (parked, off `main`). Phases 1–5 done; **adopt-or-discard still undecided** — Phase 5 found `-wiki` does not beat `-ep`. `FALKORDB_DATABASE` points at `-wiki` as interim default only ← here |
| 11 | [MS4a](plan-active.md#ms4a--mcp-boundary-capture--live-verification) | MCP-boundary capture — live cross-harness verification | 🔴 built; verification folded into MS6c Phases 1-2 |
| 12 | [MS4b](plan-active.md#ms4b--claude-code-adapter) | Claude Code adapter | ⚪ |
| 13 | [MS4c](plan-active.md#ms4c--openclaw-adapter-and-cmf-http) | OpenClaw adapter (+ `cmf-http`) | ⚪ |
| 14 | [MS4d](plan-active.md#ms4d--codex-and-gemini-cli) | Codex, Gemini CLI | ⚪ |
| 15 | [MS5](plan-active.md#ms5--knowledge-provider-generalization) | Knowledge-provider generalization | ⚪ |
| 16 | [MS8](plan-active.md#ms8--replay-and-evaluation) | Replay and evaluation | ⚪ |
| 17 | [MS9](plan-active.md#ms9--distribution-and-ecosystem) | Distribution | ⚪ |

**Why this order.** `get_context` / `recall` / `search_wiki` already work against whatever is in the graph plus the LLM Wiki. MS6a's tier-1 review is done — **295 reviewed episodes are now in the retrievable graph** (`mem-fabric-local`, Spark-local extraction) — MS7 (assembly quality) is **done** — `get_context` measurably beats both single-provider baselines (80% of a complete answer vs 50–53%), with the answer-quality eval kept as a reusable instrument. MS6b (correction / deletion / `explain()` into the graph) was sequenced *after* MS7: all of it serves the promoted rows, and the correction path should be designed after a real assembly pass, not before. MS6b's exit gate then unblocked a full corpus review pass (295 → 465 episodes) — with a real graph now worth retrieving from, **MS6c** checks that the MCP server actually works from the real client apps before more capture adapters (MS4b–MS4d) get built on top of it. MS5 comes after the retrieval loop is proven.

---

## Current state (2026-09-11)

**Done through MS6b + the corpus review pass.** The pipeline runs end-to-end on Spark-local inference:

```text
source exports → journal (19,012 events) → per-event classification (heuristic v1.2)
                                          → reasoning episodes (model v0.3): ~1,310 staged / 659 threads
                                          → MS6a tier-1 review: 295 approved / 6 rejected (2026-09-08)
                                          → MS6b + corpus review pass: +170 more approved (2026-09-11)
                                          → promotion → mem-fabric-local (qwen3.5-122b + nomic, 768-dim)
                                          → recall() / get_context() return them, entity-extracted
```

- **Live graph, as of this section's date (2026-09-11):** `mem-fabric-local` — 465 Episodic · 665 Entity · 501 RELATES_TO · 768-dim (up from 295/393/263 as of MS6a). Extraction on `unsloth/qwen3.5-122b-a10b` + `EXTRACTION_INSTRUCTIONS`, embeddings on `nomic-embed-text`, both Spark-local. Episodes named `<harness>-<project>-NNN`. **Superseded 2026-09-13 by MS7b** — `FALKORDB_DATABASE` now points at `mem-fabric-local-wiki` (interim default; see [MS7b](plan-active.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13) for the current live graph's real shape and the still-open adopt-or-discard decision).
- **Retained graphs:** `mem-fabric-local-ep` (this section's `mem-fabric-local`, renamed and preserved untouched by MS7b Phase 1 — the rollback path if the wiki-derived layer is discarded); `mem-fabric-gemini` (pre-migration, 1024-dim, untouched — the older rollback path); `mem-fabric-local-glm` (rejected Phase 7 GLM build, kept as the A/B record).
- **Remaining backlog:** 25,961 heuristic `queued_for_review` rows, never individually reviewed (mostly re-judged duplicates per MS3.6's assessment) — [plan-active Backlog](plan-active.md#backlog).
- **Live status:** `imports/ingest-pipeline-status.md` (gitignored; regenerate with `imports/tools/gen_ingest_report.py`).

**MS6b — governance — done** ([plan-history.md](plan-history.md#ms6b--governance)): `explain()` into the graph, `correct_memory`, and deletion propagation built, unit-tested, and the live-FalkorDB exit gate passed 6/6 (2026-09-11). Exercising it for real against the corpus surfaced and fixed four bugs along the way (an `edit_memory` unscoped-write hazard, `explain()` surfacing the wrong approval field, a misattributed episode, and a `promote_approved` reversion hazard) — all in [plan-history.md](plan-history.md#ms6b--governance) and [plan-active Backlog](plan-active.md#backlog). Scopes remains deferred to MS9/MS4c, per MS6a's original cut rationale.

**MS6c — MCP server cross-agent verification — new** ([plan-active.md](plan-active.md#ms6c--mcp-server-cross-agent-verification-2026-09-11)): verify the MCP server actually works inside Claude Desktop's Cowork mode, Claude Desktop's Code mode (this is Todd's actual Claude Code usage — he doesn't run the standalone CLI), Gemini Spark (`gemini.google.com/app` / `gemini.google.com/spark/apps` — not the Gemini CLI, which Todd also doesn't use), and ChatGPT, in that order, and correct `docs/CLIENTS.md` against reality. Gemini Spark and ChatGPT share one prerequisite neither had before: a public HTTPS endpoint (the server's existing `streamable-http` transport + a tunnel), since neither supports a local/stdio server. Subsumes MS4a's outstanding live cross-harness test as its Phase 1+2 acceptance test (Cowork writes, Code mode reads/corrects). **In progress 2026-09-16:** the milestone explicitly scoped *not* to build OAuth ("a bearer token is almost certainly the right call") and had to — no connector UI among ChatGPT, Gemini, or Claude Desktop accepts a static header, so CMF grew a single-user DCR + PKCE authorization server, merged in [PR #6](https://github.com/tmargolis/context-memory-fabric/pull/6). All four client families registered on 2026-09-14; the per-client functional pass is still unevidenced. Two capture-identity questions came back answered: `claude_desktop` and `claude_code` resolve **distinctly**, not collapsed.

MS7 is done — its unfinished task-list items (intent routing, time-aware modes, explicit conflict signals, `search_wiki` semantic retrieval, …) are tracked in [plan-active Backlog](plan-active.md#backlog).

**Update 2026-09-16.** [PR #6](https://github.com/tmargolis/context-memory-fabric/pull/6) (the MS6c OAuth work above) merged to `main`; [MS6d](plan-active.md#ms6d--durable-knowledge-proposal-review-2026-09-16) shipped alongside it — five new MCP tools closing the review/apply gap on `propose_wiki_update`'s output, taking the registered tool count 12→17. [MS7b](plan-active.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13), whose plan section had briefly gone missing from `main` (it lived only on its branch), is restored above and still parked pending the wiki-vs-ep decision. The live `streamable-http` server (`cmf-mcp.log`) was restarted to pick up both, and its logging was quieted — short logger names, truncated query text in `recall_mem`'s per-call lines.

This session's own Claude Desktop connection to that server did not survive the restart and needs a manual reconnect — an HTTP MCP session doesn't recover on its own from the server process underneath it bouncing.

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

