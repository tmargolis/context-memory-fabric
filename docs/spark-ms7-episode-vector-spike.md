# MS7 spike — episode-content vector retrieval for `recall_mem`

**Status:** landed on `mem-fabric-local` 2026-09-10 (`64fffdd` + follow-up); backup `mem-fabric-local.pre-epvec-20260910`
**Owner:** MS7 · related: [plan-active.md](plan-active.md) MS7, [SPARK-MIGRATION-PLAN.md](SPARK-MIGRATION-PLAN.md)

## Why

The MS7 answer eval (see `plan-active.md` MS7 § "Answer-quality eval") pinned
`recall_mem` as the ceiling for the queries CMF exists to serve:

| answer-eval mean (+both) | before spike |
|---|---|
| Group A (memory-domain) | 0.80 |
| Group C (spanning) | 0.80 |
| overall | 1.07 |

`recall_mem` does graphiti `EDGE_HYBRID_SEARCH_RRF` over RELATES_TO edge facts
only. When the signal is in the episode's synthesized statement but not in any
extracted edge — or when qwen extracted **zero entities** for that statement
(~28% of tier-1) — the gold episode is unreachable. A3/A5/A8/A9/A10 scored 0 in
every arm for exactly this reason (A5/A9 retrieved a *contradictory* episode).

## Spike

Add an episode-content vector arm to `recall_mem`, RRF-blended with the edge
search, **auto-gated** on a `Episodic.content_embedding` vector index existing on
the target graph (so it is a safe no-op on `mem-fabric-local` until/unless the
live graph is backfilled, and can merge independently).

## Throwaway graph — rollback

- **Created:** `mem-fabric-spike-eps` via `GRAPH.COPY mem-fabric-local mem-fabric-spike-eps` (2026-09-10).
- **Live graphs `mem-fabric-local` / `mem-fabric-gemini`: not touched.** `.env` not edited — the spike capture runs with `FALKORDB_DATABASE=mem-fabric-spike-eps` set in the environment only.
- **Mutations on the clone:** `SET e.content_embedding` (nomic 768-d) on 295 `:Episodic` nodes + one `CREATE VECTOR INDEX` on `:Episodic(content_embedding)`.
- **Rollback:** `redis-cli DEL "mem-fabric-spike-eps" "telemetry{mem-fabric-spike-eps}"` (or `GRAPH.DELETE mem-fabric-spike-eps`). Nothing else to undo.

## Result — clears the bar

Spike graph `mem-fabric-spike-eps` (295 episodes nomic-embedded + vector index):

| | before | spike |
|---|---|---|
| gold episode in `recall_mem` top-8 | 7/20 | **17/20** |
| answer-eval `+memory` mean | 0.47 | **0.90** |
| answer-eval `+both` mean | 1.07 | **1.53** |
| lift (`+both` − `model_only`) | +1.00 | **+1.47** |
| Group A `+both` | 0.80 | **1.30** |
| Group C `+both` | 0.80 | **1.60** |

The three contradictory-retrieval cases flipped to correct — A5 (combine Now & Then + Delayed Vision), A9 (skip Schedule C, no expenses), A10 (MFJ vs MFS). C4/C8/C10 total misses now answered.

**Follow-ups (applied):** `_rrf_merge` caps facts at 2 per source episode; the vector arm feeds only its top 6 into the fusion; `get_context` renders `recall_mem`'s full ranked output instead of re-truncating to 8.
- **A10 `+both` 0 → 2** — fixed. The rank-8 MFS episode now reaches the answer.
- **B7 `+memory` 1 → 0** — not recovered. B7 is a wiki-domain query; `+both` answers it fully (2). Accepted.
- **A1 `+memory` / `+both`** — still conflates a NAS + M5-MacBook-Air backend. `gemini-openclaw-002` (the friend's-Spark decision) is not retrieved by the edge *or* vector arm; a distance ceiling that would drop the conflation would also drop A10's gold (d≈0.38), so left as-is.
- **C6** — the site-eval panel numbers share no words with the query; unreachable by lexical snippet. Needs vector retrieval in `search_wiki` — the next MS7 lever.

Post-follow-up answer-eval: `+both` **1.60** (80% of a complete answer), `+memory` **1.00**, lift **+1.53**.

## Landing

- **Code:** `feat(recall): episode-content vector arm` (`64fffdd`) — committed, **gated** (`_graph_has_episode_vector_index`), so it is a no-op on `mem-fabric-local` until the backfill below runs. Nothing changed for live callers yet.
- **Live backfill (pending Todd):**
  ```
  redis-cli GRAPH.COPY mem-fabric-local mem-fabric-local.pre-epvec-20260910   # backup
  .venv/bin/python tests/fixtures/ms7_eval/backfill_episode_vectors.py --graph mem-fabric-local --live
  ```
  ~1 min. Then re-capture + re-run the answer eval against live to confirm parity with the spike.
- **Rollback:** `redis-cli GRAPH.QUERY mem-fabric-local "DROP VECTOR INDEX FOR (e:Episodic) ON (e.content_embedding)"` — instant; the vector arm auto-disables. Or restore from `mem-fabric-local.pre-epvec-20260910`.
- **Spike graph:** `mem-fabric-spike-eps` left in place for inspection — drop with `redis-cli DEL "mem-fabric-spike-eps" "telemetry{mem-fabric-spike-eps}"`.
