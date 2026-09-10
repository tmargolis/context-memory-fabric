# MS7 spike — episode-content vector retrieval for `recall_mem`

**Status:** spike done, result strong — code landed (gated, `64fffdd`); live-graph backfill pending Todd (2026-09-10)
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

**Regressions to follow up:** B7 `+memory` 1→0 (RRF pushed a routing fact below the cut); A10 `+both` stayed 0 (the `recall_mem` arm surfaces it but `get_context`'s own fan-in RRF doesn't propagate it — `_select_memory` / RRF interaction); A1 `+memory` now asserts a wrong backend (NAS + M5 MacBook Air — a conflated episode).

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
