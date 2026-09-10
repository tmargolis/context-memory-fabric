# Context Memory Fabric — Implementation Plan

**Companion to** [ROADMAP.md](ROADMAP.md) — the roadmap states *what and why*; this plan states *how, in what order, and how we know it worked*.

This file is the **index**. Detail lives in two companions:

| File | Contents |
|---|---|
| **[plan-active.md](plan-active.md)** | the milestones still to do — full task lists, acceptance tests, exit gates |
| **[plan-history.md](plan-history.md)** | completed milestones (MS0.5–MS3.6, MS4a, MS6a) + the Spark local-inference migration — decisions, corrections found while building, exit-gate answers |

## How to use

Work proceeds **one milestone at a time**. Each milestone in `plan-active.md` has a Goal + Why now, a task list, Files touched, Acceptance tests (mechanically checkable), an Exit gate (a decision answered before the next milestone starts), Effort, and Risks. Nothing in a milestone begins until the previous exit gate is answered and the milestone is approved.

Task-list checkboxes: `[ ]` not started · `[/]` in progress · `[x]` done.

---

## Milestone sequence

**Execution order (resequenced 2026-09-06 with Todd).** Section numbers are stable identifiers — they no longer run in numeric order. The driving change: adding more *capture* (MS4b–MS4d) is dead weight while the retrievable graph is near-empty, so the near-term path drives the existing history all the way to *retrieval* first, then adds the other adapters.

| # | Milestone | Focus | Status |
|--:|---|---|---|
| 1 | [MS0.5](plan-history.md#ms05--baseline-correctness-and-wiring) | Baseline correctness & wiring | done |
| 2 | [MS1](plan-history.md#ms1--extract-provider-interfaces-without-changing-behavior) | Provider interfaces | done |
| 3 | [MS2](plan-history.md#ms2--canonical-event-journal-importers-and-backfill) | Canonical event journal + importers + backfill | done |
| 4 | [MS3](plan-history.md#ms3--separate-capture-from-consolidation) | Capture / consolidation split | done |
| 5 | [MS3.5](plan-history.md#ms35--reasoning-episode-consolidation-adr-0005) | Reasoning-episode consolidation ([ADR 0005](adr/0005-reasoning-episode-capture.md)) | done 2026-09-07 |
| 6 | [MS3.6](plan-history.md#ms36--promotion-staged-memories-into-the-retrievable-graph) | Promotion: staged memories → retrievable graph | done 2026-09-07 |
| 7 | [MS6a](plan-active.md#ms6a--review-surface--built) | Review surface — project-bucketed queue, audit chokepoint, bulk governance; tier-1 pass | done 2026-09-08 |
| — | [*Spark migration*](SPARK-MIGRATION-PLAN.md) | Gemini → Spark-local inference | done 2026-09-09 · [history](plan-history.md#spark-local-inference-migration--phases-0-6-2026-09-08--09) |
| 8 | [**MS7**](plan-active.md#ms7--context-assembly-quality) | Context assembly quality | **in progress** — answer-eval instrument built; Step 1 (search_wiki tokenizer) + Step 2 (recall_mem rename/fidelity, get_context fan-in) + snippet/PDF fixes landed; Step 3 (episode-content vector retrieval) in spike, +both lift **+1.00**, gold-episode recall 7→17/20 |
| | | *— retrieval loop proven end-to-end here —* | |
| 9 | [MS6b](plan-active.md#ms6b--governance--after-ms7) | Governance — `explain()` into the graph, `correct_memory`, deletion propagation, scopes | after MS7 |
| 10 | [MS4a](plan-active.md#ms4a--mcp-boundary-capture--live-verification) | MCP-boundary capture — live cross-harness verification | built; verification pending |
| 11 | [MS4b](plan-active.md#ms4b--claude-code-adapter) | Claude Code adapter | |
| 12 | [MS4c](plan-active.md#ms4c--openclaw-adapter-and-cmf-http) | OpenClaw adapter (+ `cmf-http`) | |
| 13 | [MS4d](plan-active.md#ms4d--codex-and-gemini-cli) | Codex, Gemini CLI | |
| 14 | [MS5](plan-active.md#ms5--knowledge-provider-generalization) | Knowledge-provider generalization | |
| 15 | [MS8](plan-active.md#ms8--replay-and-evaluation) | Replay and evaluation | |
| 16 | [MS9](plan-active.md#ms9--distribution-and-ecosystem) | Distribution | |

**Why this order.** `get_context` / `recall` / `search_wiki` already work against whatever is in the graph plus the LLM Wiki. MS6a's tier-1 review is done — **295 reviewed episodes are now in the retrievable graph** (`mem-fabric-local`, Spark-local extraction) — so the active blocker for "make CMF's output actually good" is **MS7** (assembly quality). MS6b (correction / deletion / `explain()` into the graph) moved *after* MS7: all of it serves the promoted rows, and the correction path should be designed after a real assembly pass, not before. MS4b–MS4d and MS5 come after the retrieval loop is proven.

---

## Current state (2026-09-09)

**Done through MS6a + the Spark migration.** The pipeline runs end-to-end on Spark-local inference:

```text
source exports → journal (19,012 events) → per-event classification (heuristic v1.2)
                                          → reasoning episodes (model v0.3): ~1,243 staged / 659 threads
                                          → MS6a tier-1 review: 295 approved / 6 rejected (2026-09-08)
                                          → promotion → mem-fabric-local (qwen3.5-122b + nomic, 768-dim)
                                          → recall() / get_context() return them, entity-extracted
```

- **Live graph `mem-fabric-local`:** 295 Episodic · 393 Entity · 263 RELATES_TO · 768-dim. Extraction on `unsloth/qwen3.5-122b-a10b` + `EXTRACTION_INSTRUCTIONS`, embeddings on `nomic-embed-text`, both Spark-local. Episodes named `<harness>-<project>-NNN`. Hygiene matches Gemini (0 pronoun entities / 0 self-loops); 28% of statements yield no entities (vs Gemini's 22%).
- **Retained graphs:** `mem-fabric-gemini` (pre-migration, 1024-dim, untouched — the rollback path); `mem-fabric-local-glm` (rejected Phase 7 GLM build, kept as the A/B record).
- **Staged, not yet reviewed:** ~942 tier-2 reasoning episodes (work-journal, stay in-thread, not promoted) + 6,582 heuristic `queued_for_review` + 3,251 `superseded_by_reasoning` (confirm-only). Tier-1 is done.
- **Live status:** `imports/ingest-pipeline-status.md` (gitignored; regenerate with `imports/tools/gen_ingest_report.py`).

**Next: MS7 — context assembly quality** ([plan-active.md](plan-active.md#ms7--context-assembly-quality)). MS7 needs a graded query set + memory-only / knowledge-only baselines built *first*, then intent-routing, time-aware modes, conflict signals, and token budgeting on top of the 295-episode graph.

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

---

## Effort summary

Rows in **execution order**, not milestone-number order. Estimates assume agent-assisted implementation with review at each milestone boundary.

| Milestone | Sessions | Cumulative | Status |
|---|---:|---:|---|
| MS0.5 | 1–2 | 2 | done |
| MS1 | 3–4 | 6 | done |
| MS2 | 5–7 | 13 | done |
| MS3 | 4–5 | 18 | done |
| MS3.5 | 5–7 | ~24 | done — 1,243 episodes / 659 threads; no auto-accept threshold; three-tier routing |
| MS3.6 | 2–3 | ~27 | done — `promote_reviewed`, tier routing, coverage auto-resolve (−3,251); 22 keeps, then the full 295 tier-1 set promoted |
| MS6a (review surface + tier-1 pass) | 5–6 | ~33 | done 2026-09-08 — project-bucketed queue, audit chokepoint, 295 approved / 6 rejected |
| Spark migration (Gemini → Spark-local) | ~4 | ~37 | done 2026-09-09 — qwen3.5-122b + nomic; own plan file |
| MS7 (context assembly quality) | 4–5 | ~42 | **next** |
| MS6b (governance — correct / delete / `explain()`) | 2–3 | ~45 | after MS7 |
| MS4a (live cross-harness verification) | 1 | ~46 | built; needs a live Claude Desktop session |
| MS4b (Claude Code) | 3–4 | ~50 | |
| MS4c (OpenClaw) | 4–5 | ~55 | |
| MS4d (Codex, Gemini CLI) | 4–6 | ~61 | |
| MS5 (knowledge-provider generalization) | 3–4 | ~65 | |
| MS8–MS9 (replay/eval, distribution) | 8–11 | ~75 | |
