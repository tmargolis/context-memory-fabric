# B16 execution plan — project inference comparison + career episode merge

Written 2026-10-13 for handoff to another harness. Backlog context: [plan-active.md § B16](plan-history.md#b16--project-aware-retrieval-closed-2026-10-15). Prior result this builds on: [plan-history.md § B08](plan-history.md#b08--project-organization-and-retrieval-dilution-closed-2026-10-13).

## Goal

Answer two questions with measurements, without changing production:

1. **Which query-time project inference method is good enough to drive B08's project boost?** B08 measured the ceiling with an oracle (MRR 0.485 → 0.740 on all 66 eval cases). A real method must get close to that without confidently guessing wrong.
2. **Does rolling up related career episodes help retrieval**, alone and combined with the best inference method?

Constraint from User: inference must add essentially no time at retrieval. No extra LLM call per query; at most cheap lookups or math on vectors already computed.

## Hard rules

- **Never write to `mem-fabric-local`.** All writes go to scratch graphs made with `GRAPH.COPY`. Any production change needs a new ruling from User, a backup first, and before/after counts.
- **Spark is single-tenant.** Every Spark-heavy step (roll-up writing, ingestion) must hold `imports/journal/spark_job.lock` via `server/adapters/spark_lock.py`. If the lock is held, wait; don't break it.
- **`GRAPH.COPY` hygiene:** between copies, wait for `rdb_bgsave_in_progress:0` (`INFO persistence`). See memory note on the 2026-09-12 FalkorDB persistence incident.
- **Never print secrets.** Don't echo `.env` lines containing `PASSWORD`, `KEY`, `SECRET`, `TOKEN`. Load `.env` with `set -a; . ./.env; set +a` and pass variables through.
- **Eval question text stays local.** `tests/fixtures/ms7_eval/` is the eval set; tracked docs and commit messages carry case IDs and scores only, never question text.
- **Imports:** run from the main checkout with `uv run`. In a worktree, set `PYTHONPATH` to the worktree, or the venv's `.pth` imports main's `server/`.
- **Other sessions may be active on this tree.** Check `git log -5` and `git status` before committing; commit only your own files.
- **Stop for User at the two marked gates** (⛔). Don't continue past them on your own.

## Part A — inference method comparison (no Spark lock needed)

New read-only script: `scripts/experiment_project_inference.py`. Base it on `scripts/experiment_project_rerank_replay.py` (same case loading, same 24-wide pool from `recall_mem`, same boost rerank), and replace the oracle with each method's guess.

| ID | Method | Signal | Added cost per query |
|---|---|---|---|
| A | Name/alias match | Query text contains a project slug or an alias from `CMF_PROJECT_ALIASES` (normalize case, hyphens, plurals) | ~0 |
| B | Entity match | Entities whose `name` appears in the query → their `entity.project` lists; score projects by matched-entity count, weighted by inverse project size | 1 FalkorDB read, a few ms |
| C | Candidate vote | Rank-weighted vote (e.g. 1/rank) over the `project` of the episodes/facts already in the 24-wide pool | ~0 (data already fetched) |
| D | Combined | B if it is confident, else C; abstain if neither is | a few ms |
| E | Project centroid (reference only) | Cosine of the query embedding against a per-project mean of stored episode `content_embedding`s, built once offline | ~0 at query time; offline build |

**Every method must be able to abstain.** Return `(project, margin)`; abstain when the top score doesn't beat the runner-up by a margin. Sweep the margin threshold (e.g. 0, 0.1, 0.2, 0.3 of normalized score) and report each setting.

**Do not add a per-query embedding or LLM call.** For E, reuse the query embedding the vector arm already computes (`server/providers/memory_graphiti.py` ~line 833). In the experiment you may embed queries once and cache them.

**Metrics per method × threshold** (gold project = the project of each case's gold episodes, exactly as the oracle in the B08 script defines it):

- accuracy when it guesses; abstain rate; **confident-wrong rate** (the costly case);
- Hit@8 and MRR after the boost, using the guess (abstain = no rerank);
- per-case regressions vs baseline, listed by case ID;
- reference rows: baseline (no rerank) and oracle (B08 ceiling).

Output: `imports/replay/b16-inference-comparison-<YYYYMMDD>.json` plus a short markdown summary next to it. Run it on `mem-fabric-local` (read-only).

## Part B — career episode merge on a scratch graph (Spark)

Scope: `claude-cowork-career-*` episodes in `mem-fabric-local` with `project = 'career'` created on or after 2026-10-03 (221 at time of writing). Widen to all career, then to `context-memory-fabric`, only if this works and User agrees.

1. **Scratch copy.** `GRAPH.COPY mem-fabric-local mem-fabric-b16-merge-<YYYYMMDD>`; wait for bgsave; record node/edge/episode counts of both graphs.
2. **Cluster (no Spark).** Group the scoped episodes by company/requisition/thread. Signals: the `Organization` entities each episode `MENTIONS` (B08 found 134 distinct companies in career), thread/conversation IDs, and name/date proximity. Known example clusters: `career-078/079/080`, `career-090/091/092`, `career-068/075`. Singletons stay as they are. **At most 10 episodes per cluster:** split anything larger by requisition, thread or date. Don't cluster on a hub organization that many unrelated threads mention (2026-10-13: 4 org buckets had 10+ episodes, the largest 43; these are hubs, not threads). Write `imports/replay/b16-merge-clusters-<YYYYMMDD>.json` (cluster id, member episode names, shared org, one-line rationale) and a review table with the columns: cluster, members, org/thread, why.
3. ⛔ **Gate 1: User reviews the clusters.** Present the table in batches of at most 50 clusters. Apply User's edits. Nothing runs on Spark before approval.
4. **Write roll-ups (Spark, take the lock).** One summary episode per approved cluster with qwen-122b on Spark. It must keep every distinct decision, date, company, role and outcome from its members; nothing new. **At most 4,000 characters per roll-up** (members average ~915 chars, max 2,544). If a cluster can't fit, split it rather than compress facts away. Context is not the constraint: inputs stay well under qwen-122b's 64K window, so don't raise it. Name: `claude-cowork-career-rollup-<NNN>`; `project = 'career'`; `valid_at` = latest member's; provenance lists the member episode names. Save drafts to `imports/replay/b16-rollups-<YYYYMMDD>.jsonl` before ingesting so they can be inspected or reused.
5. **Ingest into the scratch graph only (Spark, take the lock).** Use the same Graphiti `add_episode` path production promotion uses, with the production extraction profile (`typed-recall` + debris filter), pointed at the scratch graph. Expect about 2–2.5 min per roll-up.
6. **Retract members in the scratch graph.** `scripts/retract_episodes.py --graph mem-fabric-b16-merge-<YYYYMMDD>` (dry run first, then `--apply`), with an episodes file listing only the merged members. Never pass the production graph name.
7. **Replay.** `scripts/replay_eval.py --now --source mem-fabric-b16-merge-<YYYYMMDD>` (all 66 cases, k=8), and the same on `mem-fabric-local` the same day for a controlled before/after. Compare Hit@8, MRR, Wiki@8, `provenance_rate`; list case-level changes for A6, C1, C5, C15, C19 and D1–D6. D1–D3 are merge-sensitive tailoring clusters; D4–D6 are single-fact decisions the merge must not lose. If `replay_eval.py` can't target the scratch graph through `--source`, check how it selects the graph and adapt; don't point it at production for writes.
8. **Combined run.** Re-run Part A's best one or two methods against the scratch graph to measure merge + inference together.

**Time estimate** (from 49 logged promotions: median 61 s, mean 114 s, p90 196 s): clustering 30–45 min plus review; roll-up writing ~45–75 s × 45–60 clusters ≈ 1 h; ingest ≈ 1.5–2.5 h; replay 15–20 min. **About 2.5–3.5 h of Spark.** Part A can run during the Spark steps; only its query embeddings touch Spark, briefly.

## Deliverable

⛔ **Gate 2: report to User and stop.** One results table:

| Variant | Hit@8 | MRR | Wiki@8 | Confident-wrong | Regressed cases |
|---|---|---|---|---|---|
| baseline (production) | | | | — | — |
| oracle boost (B08 ceiling) | | | | 0 | |
| best inference method (+ threshold) | | | | | |
| career merge only | | | | — | |
| merge + best inference | | | | | |

Plus: a recommendation (which method, which threshold, merge yes/no), the scratch graph names to keep or drop, and artifact paths. Add a dated results paragraph under B16 in `docs/plan-active.md` (IDs and scores only). Commit the scripts and doc update on `main` with message prefix `B16:`; don't push unless User asks. Don't wire anything into `recall_mem`. That is a separate ruling after this report.

---

## Prompt for the executing harness

> You're working in `/Users/todd/Dev/context-memory-fabric` (Context Memory Fabric). Read `CLAUDE.md`, then execute `docs/plan-b16.md` exactly. Do Part A and Part B, running Part A while Part B waits on User or on Spark.
>
> Before starting, present a short task list of what you'll build and run, then proceed. Follow every hard rule in the plan, especially: never write to `mem-fabric-local`, take `imports/journal/spark_job.lock` for Spark work, never print secrets from `.env`, and keep eval question text out of tracked files.
>
> Stop at Gate 1 (cluster review) and show me the cluster table in batches of at most 50. Stop at Gate 2 with the results table and a recommendation. If something in the plan doesn't match the code (a flag, a path, a script behavior), say so and adapt conservatively rather than guessing. If an adaptation would touch production, stop and ask.
