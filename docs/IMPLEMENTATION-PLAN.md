# Context Memory Fabric — Implementation Plan

**Companion to** [ROADMAP.md](ROADMAP.md) — the roadmap states *what and why*; this plan states *how, in what order, and how we know it worked*.

This file is the **index**. Detail lives in two companions:

| File | Contents |
|---|---|
| **[plan-active.md](plan-active.md)** | the milestones still to do — full task lists, acceptance tests, exit gates |
| **[plan-history.md](plan-history.md)** | completed milestones (MS0.5–MS3.6, MS4a) — decisions, corrections found while building, exit-gate answers |

## How to use

Work proceeds **one milestone at a time**. Each milestone in `plan-active.md` has a Goal + Why now, a task list, Files touched, Acceptance tests (mechanically checkable), an Exit gate (a decision answered before the next milestone starts), Effort, and Risks. Nothing in a milestone begins until the previous exit gate is answered and the milestone is approved.

Task-list checkboxes: `[ ]` not started · `[/]` in progress · `[x]` done.

---

## Milestone sequence

**Execution order (resequenced 2026-09-06 with Todd).** Section numbers are stable identifiers — they no longer run in numeric order. The driving change: adding more *capture* (MS4b–MS4d) is dead weight while the retrievable graph is near-empty, so the near-term path drives the existing history all the way to *retrieval* first, then adds the other adapters.

| # | Milestone | Focus | Status |
|--:|---|---|---|
| 1 | MS0.5 | Baseline correctness & wiring | done |
| 2 | MS1 | Provider interfaces | done |
| 3 | MS2 | Canonical event journal + importers + backfill | done |
| 4 | MS3 | Capture / consolidation split | done |
| 5 | MS3.5 | Reasoning-episode consolidation ([ADR 0005](adr/0005-reasoning-episode-capture.md)) | done 2026-09-07 |
| 6 | MS3.6 | Promotion: staged memories → retrievable graph | done 2026-09-07 |
| 7 | **MS6** | Review and governance | **next ← here** |
| 8 | MS7 | Context assembly quality | |
| | | *— retrieval loop proven end-to-end here —* | |
| 9 | MS4a | MCP-boundary capture — live cross-harness verification | built; verification pending |
| 10 | MS4b | Claude Code adapter | |
| 11 | MS4c | OpenClaw adapter (+ `cmf-http`) | |
| 12 | MS4d | Codex, Gemini CLI | |
| 13 | MS5 | Knowledge-provider generalization | |
| 14 | MS8 | Replay and evaluation | |
| 15 | MS9 | Distribution | |

**Why this order.** `get_context` / `recall` / `search_wiki` already work against whatever is in the graph plus the LLM Wiki. The ~1.2k reasoning episodes and the heuristic candidates are staged in SQLite and are **not retrievable** until reviewed and promoted. So the blocker for "test what CMF actually does" is MS6 (review the backlog) → MS7 (make assembly good) — not more capture adapters. MS4b–MS4d and MS5 come after the retrieval loop is proven.

---

## Current state (2026-09-07)

**Done through MS3.6.** The full pipeline loop is closed end-to-end:

```text
source exports → journal (19,012 events) → per-event classification (heuristic v1.2)
                                          → reasoning episodes (model v0.2): 1,243 staged / 659 threads
                                          → promotion (MS3.6): 22 human-reviewed keeps in the graph
                                          → recall() / get_context() return them, entity-extracted
```

- **Graph (`memory-fabric`):** 57 MS0.5-backfilled episodes + 22 promoted reasoning episodes = 79.
- **Staged, awaiting review:** ~1,221 unpromoted reasoning episodes (three-tier: ~301 tier-1 for promotion review, ~942 tier-2 work-journal, rest discard) + 6,582 heuristic `queued_for_review` + 3,251 `superseded_by_reasoning` (covered by an episode, confirm-only).
- **Live status:** `imports/ingest-pipeline-status.md` (gitignored; regenerate with `imports/tools/gen_ingest_report.py`).

**Next: MS6 — Review and governance** ([plan-active.md](plan-active.md#ms6--review-and-governance)). Build the surface to move the staged backlog into the graph — **bulk review by thread / kind / tier**, plus evidence-trace ("why does this memory exist"), correction, audit, and scopes.

### Parallel track — Spark local-inference migration (2026-09-09)

Tracked in its own file: [SPARK-MIGRATION-PLAN.md](SPARK-MIGRATION-PLAN.md). Replaces the Gemini Developer API as CMF's LLM + embedding backend with models served from the DGX Spark. **Phases 0-6 complete; Phase 7 (quality A/B) outstanding.** What changed that the milestone docs depend on:

- **The production graph is now split.** `memory-fabric` was renamed to **`mem-fabric-gemini`** (retained untouched, 1024-dim, 337/337/187) and a fresh **`mem-fabric-local`** built (768-dim; 559 Entity / 567 RELATES_TO). Its content is the **MS6a tier-1-approved set** — 295 episodes (reviewer todd, 2026-09-08, 295 approved / 6 rejected of ~301 routed to tier 1) re-promoted on GLM-4.7-Flash + `nomic-embed-text`, the same set that was also promoted into `mem-fabric-gemini`. `.env` still points at `mem-fabric-gemini` — flipping is a deliberate step, not yet taken.
- **The MS4a cost gate is superseded** (D5). Local inference has no per-call cost, so `server/core/rate_limiter.py` is now a *throughput* control on the Gemini path and unmetered on the local path — see "Privacy and cost" below.
- **Phase 7 is where the GLM-vs-Gemini extraction-quality decision gets made.** A first-look inspection (2026-09-09) found entity/edge quality below Gemini's — pronoun entities, self-referential and duplicate edges, surface-fragment entities. If GLM can't be recovered, the fallback is the hybrid (Gemini extraction, local embeddings), which Phase 1's split provider vars already allow.

---

## Exit-gate decisions log

The distilled output of the plan — every gate answered, newest first.

| Milestone | Question | Answer (date) |
|---|---|---|
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

Graphiti sends every ingested episode to the Gemini Developer API for entity extraction + embedding. At curated volume that's a bounded, deliberate exposure; continuous capture across Claude Desktop / Claude Code / OpenClaw changes the posture (every turn becomes an extraction call; the corpus already holds medical, financial, legal material; cost is unbounded and unmeasured).

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
| MS3.6 | 2–3 | ~27 | done — `promote_reviewed`, tier routing, coverage auto-resolve (−3,251), 22 keeps promoted, retrieval verified |
| MS6 (review & governance) | 5–6 | ~33 | **next** |
| MS7 (context assembly quality) | 4–5 | ~38 | |
| MS4a (live cross-harness verification) | 1 | ~39 | built; needs a live Claude Desktop session |
| MS4b (Claude Code) | 3–4 | ~43 | |
| MS4c (OpenClaw) | 4–5 | ~48 | |
| MS4d (Codex, Gemini CLI) | 4–6 | ~54 | |
| MS5 (knowledge-provider generalization) | 3–4 | ~58 | |
| MS8–MS9 (replay/eval, distribution) | 8–11 | ~68 | |
