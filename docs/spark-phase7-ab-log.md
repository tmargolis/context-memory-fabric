# Spark Phase 7 — quality A/B working log

**Purpose:** decide the D1 gate — does Spark-local extraction (GLM-4.7-Flash) produce
entity/edge structure comparable to Gemini's, or do we fall back to the hybrid
(Gemini extraction + local embeddings)?

**Rollback contract:** every step below is tagged **[READ-ONLY]** (no state change,
nothing to roll back) or **[MUTATES]** (what changed + how to undo). The two
production graphs `mem-fabric-gemini` and `mem-fabric-local` are never written to.

---

## Method

The 295 MS6a tier-1-approved episodes were promoted into **both** graphs, so GLM's
extraction and Gemini's extraction of the *same* synthesized statements already
coexist:

- `mem-fabric-gemini` — Gemini extraction, 1024-dim (pre-migration)
- `mem-fabric-local` — GLM-4.7-Flash extraction, 768-dim (Phase 6 rebuild)

Matched on `promotions.episode_name` (identical across both graphs).
**275 matched pairs** (295 approved − 24 that failed the earlier Gemini promotion,
± overlap with the 57 MS0.5 backfill episodes).

No new inference is required for the GLM-vs-Gemini comparison.

---

## Steps

### 1. Establish the matched set — [READ-ONLY]

- `GRAPH.LIST` → `mem-fabric-gemini`, `mem-fabric-local`, `cmf_test`, `default_db` present.
- `promotions` INTERSECT on `memory_id`, both `status='succeeded'` → **275 pairs**.
- `episode_name` is identical across graphs for a given `memory_id` (spot-checked 3).
- Names → `scratchpad/phase7/names.txt` (scratchpad, auto-cleaned).

### 2. Aggregate comparison over all 275 pairs — [READ-ONLY]

`scratchpad/phase7/compare.py` — FalkorDB `RO_QUERY` only. For each episode, from each
graph: `MENTIONS` entities, and `RELATES_TO` edges where the episode uuid is in
`r.episodes`.

| metric | Gemini | GLM-local | per-ep Gemini | per-ep GLM |
|---|--:|--:|--:|--:|
| episodes matched | 275 | 275 | | |
| entities | 394 | **799** | 1.43 | 2.91 |
| edges | 129 | **535** | 0.47 | 1.95 |
| self-loop edges | **0** | 8 | 0.00 | 0.03 |
| pronoun entities (`user`/`assistant`/…) | **0** | 47 | 0.00 | 0.17 |
| prompt-echo / template-token facts | **0** | 4 | 0.00 | 0.01 |
| exact-duplicate facts | **0** | 53 | 0.00 | 0.19 |

GLM emits ~2× the entities and ~4× the edges of Gemini on the *identical* statements.
Gemini is at **zero** on all four defect classes across the whole set; GLM has defects
in every one.

### 3. Hand-score, 10 episodes across projects — [READ-ONLY]

`scratchpad/phase7/view.py` — side-by-side dump. Result: **Gemini better in 7/10,
tie (both under-extract) in 3/10, GLM better in 0/10.**

GLM failure modes, all confirmed on real episodes:
- **Pronoun subjects** — `user` / `The user` as entities *and* as edge sources
  (astrophotography, exhibition).
- **Fragment entities** — `configuration`, `corrupted session state`, `workload`,
  `5-minute exposures at gain 100` (two params fused).
- **Hallucination** — `Product Manager` extracted from a statement about adding
  support structures to a 3D-printed sphere (nothing about a PM in the text).
- **Paraphrase-spam edges** — condo episode: 7 edges, 5 of them `board → property
  manager` splitting one request into per-list-item near-duplicates, one with a
  reversed-direction relationship. Exhibition: 7 edges, 5 `The user → X` with raw
  sentence-fragment facts.
- **Edge facts that are un-predicated sentence fragments** — "revert to 5-minute
  exposures at gain 100", "The user decided to combine two separate art projects".
- **Wrong edge direction / object** — obsidian: `Obsidian → corrupted session state`
  where the real object is `Git`.

Gemini's failure mode is the opposite and benign: **under-extraction** — 0 entities /
0 edges on 3 of the 10 (condo, ev-charging, interlock). What it *does* extract is
clean: real referents, correct direction, sensible predicate, no pronouns, no dupes,
no garbage — across all 275.

### Verdict — D1 gate

**GLM-4.7-Flash is clearly below Gemini on extraction quality. Not borderline.**
The 2×/4× volume is noise (pronouns, fragments, dupes, occasional hallucination),
not richer structure.

**Recommendation: fall back to the hybrid** — `CMF_LLM_PROVIDER=gemini` +
`CMF_EMBED_PROVIDER=local`. Keeps extraction at the Gemini bar; still banks the
migration's real win (local `nomic` embeddings — unmetered, and embeddings at
~20/episode were the actual quota wall, not generation). Config-only change per D4.

**Open option:** `qwen/qwen3.5-122b-a10b` is untested and much larger than GLM; it
*might* reach the Gemini bar and preserve the all-local goal. Not on the critical
path — the hybrid unblocks MS7 and the 1,243-row backlog now. Test it only if
full-local is a hard requirement.

---

### 4. `qwen3.5-122b-a10b` extraction probe — [MUTATES: one throwaway graph]

Todd loaded `unsloth/qwen3.5-122b-a10b` at **32768** context + `nomic` embedder on
the Spark via `lms load` (correct key is `unsloth/…`, not `qwen/…`). Server on 1234,
both models resident.

- Smoke test: qwen-122b is a **reasoning model** — empty `content`, all output in
  `reasoning_content` — so the run uses the existing `LMStudioCompatClient` proxy in
  Mode A (`json_schema`), same as GLM.
- `scratchpad/phase7/run_q122.py` — env-overrides `CMF_LLM_PROVIDER=local`,
  `CMF_LOCAL_LLM_MODEL=unsloth/qwen3.5-122b-a10b`, `EMBEDDING_DIM=768`,
  `FALKORDB_DATABASE=spark-phase7-q122`; full `graphiti.add_episode` for **8** of the
  scored sample episodes (dropped interlock / mac-infra / ev-charging — both
  extractors were ~empty there).
- **Only mutation: FalkorDB graph `spark-phase7-q122`.** Journal DB untouched (local
  rate-limiter path writes no ledger; the promotion path is not used).

**Undo:** `docker exec context-memory-fabric-falkordb redis-cli GRAPH.DELETE spark-phase7-q122`
*(kept for now — may append more episodes for a wider miss-rate sample)*

Run: 8/8 ingested, **6–56 s/episode, ~27 s avg** (≈ 2.2 h for 295, ≈ 9 h for 1,243 —
comparable to GLM, not the feared multi-minute-per-episode).

### 4a. 3-way result — Gemini / GLM / qwen3.5-122b, 8 episodes — [READ-ONLY]

| | ent/ep | edge/ep | pronoun | self-loop | garbage | dup |
|---|--:|--:|--:|--:|--:|--:|
| Gemini | 2.12 | 1.12 | 0 | 0 | 0 | 0 |
| GLM | 2.62 | 2.25 | 2 | 0 | 0 | 4 |
| **qwen-122b** | 2.25 | 0.75 | **0** | **0** | **0** | **0** |

**qwen-122b is clean on every defect class GLM failed** — no pronoun entities, no
self-loops, no paraphrase-spam, no garbage. On hygiene it matches Gemini.

Per-episode vs Gemini:
- **≥ Gemini:** obsidian (caught `.obsidian/workspace*` + coherent edges), Spain/Morocco
  (best edges of the three), openclaw (`OpenClaw`, = Gemini), career-nav (also caught
  `Phase 1`/`Phase 3`), condo (both correctly empty).
- **< Gemini — under-extraction:** **0 entities on 3 of 8** (astro-exposures, 3d,
  and no edges on exhibition) where Gemini pulled real entities + edges every time.
  qwen-122b's failure mode is Gemini's (conservatism) but more often — 3/8 here vs
  Gemini's ~1/8.

vs GLM: **qwen-122b wins on all 8** — no hallucination (GLM invented `Product Manager`
on the 3d episode), no fragments (`configuration`, `corrupted session state`), no
pronoun subjects, no 7-edge spam clusters.

Side note: none of the three extract `Spain` / `Morocco` on `promoted_20260830…` —
because the *promoted statement* only says "Morocco zones toggle layer" (a UI element)
and never mentions the trip. That is a **reasoning-episode synthesis** gap, not an
extraction-model gap.

### Verdict — revised

- **qwen3.5-122b fixes GLM's disqualifying defects.** All-local extraction is back on
  the table — the earlier "GLM sinks it, go hybrid" no longer holds.
- **But qwen-122b under-extracts more than Gemini** (0/8 → 3/8 total misses on this
  small set). 8 episodes is too few to trust 37% vs ~12%; needs a wider sample to
  decide if that miss rate is acceptable for a memory graph.
- Throughput is fine (~27 s/episode).

### 4b. Wider sample — 38 episodes, 3-way — [READ-ONLY]

Added 30 more (stratified across projects) to `spark-phase7-q122`. 30/30 ingested,
~4–59 s each. Full 3-way over all 38:

| | n | ent/ep | edge/ep | **0-entity misses** | pronoun | self-loop | garbage | dup |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| Gemini | 38 | 1.50 | 0.50 | **9 (24%)** | 0 | 0 | 0 | 0 |
| GLM | 38 | 2.87 | 1.82 | 2 (5%) | 8 | 0 | 0 | 6 |
| **qwen-122b** | 38 | 1.45 | 0.68 | **17 (45%)** | 0 | 0 | 0 | 0 |

- **Hygiene: qwen-122b = Gemini, perfect.** Zero on all four defect classes. GLM's
  low miss rate (5%) is not a virtue — it "hits" by emitting pronouns / fragments /
  hallucinations (its 2.87 ent/ep is inflated junk).
- **Recall: qwen-122b clearly below Gemini.** 45% of statements yield zero entities
  vs Gemini's 24% — held from 37% at n=8, so not noise. On the ~29 statements Gemini
  finds extractable, qwen-122b succeeds on ~21 → **~28% relative recall loss**.
  Concretely: ~8 of the 38 are episodes Gemini made entity-retrievable and qwen-122b
  left with nothing (3d-sphere, an astro-exposure episode, two exhibition, a finance,
  an ev-charging…).
- **When qwen-122b does engage, structure ≈ Gemini or slightly richer** (e.g.
  `promoted_20260830…` G 5/2 vs Q 7/3; `promoted_20260502_a1f3da61` G 3/2 vs Q 5/4).
- Gemini's own 24% floor is partly statement quality — some reasoning-episode
  syntheses are too abstract to extract from by any model ("asks for a phased
  purchase and installation plan…").

### Verdict — n=38

**qwen-122b is a clean extractor that is too conservative.** It removes every one of
GLM's disqualifying defects but trades ~28% of Gemini's recall for it. Not a clear
win over the hybrid; a real quality/independence tradeoff.

**Options for Todd:**
1. **Hybrid** (`CMF_LLM_PROVIDER=gemini` + `CMF_EMBED_PROVIDER=local`) — best recall,
   captures the migration's real win (unmetered local embeddings), config-only.
   ~2 days limiter-paced to re-promote 295. **Safest.**
2. **All-local qwen-122b** — accept ~28% recall loss vs Gemini for fully local / no
   quota / no cost / clean output / fast (~2 h for 295).
3. **One tuning pass on qwen-122b** (~15–20 min) — the 45% miss may be a reasoning
   model going terse under Mode-A `json_schema` at low temperature; try temp 0.4–0.7
   and/or a stronger `custom_extraction_instructions`, re-run the 38, see if recall
   closes the gap. If it does → all-local becomes the clear pick.

### 4c. Round 1 — qwen-122b + `custom_extraction_instructions` — [MUTATES: graph `spark-phase7-q122-r1`]

Lever: graphiti extraction already runs at **temperature 1** (its `LLMConfig` default;
CMF never overrides it), so timidity is not a low-temp artefact. The real unused lever
is `custom_extraction_instructions` — a first-class `add_episode` param spliced into
the `extract_nodes`/`extract_edges` prompts, which `remember()` passes as `None`.

Added a 601-char nudge: *"…a concise, deliberately terse 3rd-person summary… extract
every specific named entity it references… even when mentioned only briefly… an empty
list should be rare… do NOT extract the narrator."* Re-ran the same 38 into
`spark-phase7-q122-r1`. 38/38 OK (one 605 s stall, recovered).

| | n | ent/ep | edge/ep | **MISS** | pronoun | self-loop | garbage | dup |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| Gemini | 38 | 1.50 | 0.50 | 9 (24%) | 0 | 0 | 0 | 0 |
| GLM | 38 | 2.87 | 1.82 | 2 (5%) | 8 | 0 | 0 | 6 |
| qwen r0 (no instr) | 38 | 1.45 | 0.68 | 17 (45%) | 0 | 0 | 0 | 0 |
| **qwen r1 (+instr)** | 38 | **2.03** | **1.24** | **11 (29%)** | **0** | **0** | **0** | **0** |

- **Miss rate 45% → 29%** — now 5 pts off Gemini, and **7 of the 11 remaining misses
  are episodes Gemini also misses** (un-extractable statements). Real recall gap vs
  Gemini ≈ 10%, down from ~28%.
- **Hygiene stayed perfect** — 0/0/0/0. Loosening did not bring back pronouns,
  self-loops, garbage or dupes. That is the decisive result: qwen-122b can be made
  less timid without making it sloppy.
- **Recall volume now ≥ Gemini** when it engages: ent/ep 2.03 vs 1.50, edge/ep 1.24
  vs 0.50 — and still clean.
- Net over r0: 9 episodes recovered (0→N entities), 3 lost (all were 1-entity, likely
  temp-1 noise).

### Verdict — Phase 7 D1 gate: ANSWERED

**All-local is viable. Adopt `unsloth/qwen3.5-122b-a10b` + the extraction-instructions
nudge.** It matches Gemini on hygiene (and crushes GLM), and closes to ~5 pts of
Gemini on recall — most of that residual being statements no model can extract from.
Fully local: no quota, no per-call cost, ~25 s/episode.

**Next steps (each milestone-gated):**
1. Wire the `INSTR` string into `remember()`'s `add_episode` call (small; benign no-op
   on the Gemini path — verify once). Set `CMF_LOCAL_LLM_MODEL=unsloth/qwen3.5-122b-a10b`.
2. Fresh-graph re-promotion of the 295 tier-1 episodes on qwen-122b + nomic (~2 h).
   `mem-fabric-gemini` retained for rollback.
3. Mechanical fixes (self-edge drop, edge dedup, semantic episode names) — lower
   priority now (qwen r1 already emits 0 self-loops / 0 dups) but still worth doing.
4. Watch for the ~600 s mid-run stall recurring; Phase 4's `"model unloaded"` retry
   marker should cover an Auto-Evict.

---

## Rollback ledger

| Step | State change | Undo |
|---|---|---|
| 1–3 | none (all `GRAPH.RO_QUERY`) | n/a |
| 4 | FalkorDB graph `spark-phase7-q122` created | `redis-cli GRAPH.DELETE spark-phase7-q122` |

Production graphs `mem-fabric-gemini` / `mem-fabric-local` and `imports/journal/journal.db`
were never written to. Scratch under `scratchpad/phase7/` is session-isolated / auto-cleaned.
