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

---

## Rollback ledger

| Step | State change | Undo |
|---|---|---|
| 1–3 | none (all `GRAPH.RO_QUERY`) | n/a |
| 4 | FalkorDB graph `spark-phase7-q122` created | `redis-cli GRAPH.DELETE spark-phase7-q122` |

Production graphs `mem-fabric-gemini` / `mem-fabric-local` and `imports/journal/journal.db`
were never written to. Scratch under `scratchpad/phase7/` is session-isolated / auto-cleaned.
