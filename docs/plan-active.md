# Implementation Plan — Active milestones

The milestones still to do, in execution order. Index and decisions log: [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). Completed milestones: [plan-history.md](plan-history.md).

---

## MS6a — Review surface — **built**

**Goal:** Make the staged backlog reviewable at all, and get real material into the graph.

**What the corpus actually says.** Three measurements taken before writing code changed the design:

1. **The thread is the wrong review unit.** The 315 unpromoted tier-1 episodes fall into 215 threads — a 1.47x reduction, 73% of them singletons. `thread_key` is a free-text slug the extraction model invents per window and matches by exact equality (`consolidation/threads.py`), so `openclaw-gateway-connection` and `openclaw-gateway-setup` are two threads. Corpus-wide that is 1.9 episodes per thread and no queue design improves it. **Grouping by project bucket instead gives 301 -> 20 buckets (15.8x), median bucket 11, one singleton.** Reading is unchanged; what drops by an order of magnitude is *re-orientation*.
2. **There is no dedup shortcut.** Near-duplicate detection over the tier-1 statements finds one pair. These are 314 genuinely distinct claims across 180 topics and 10 months.
3. **The heuristic pile is a quarter the size it looks.** 25,961 `queued_for_review` heuristic rows cover only **9,757 distinct events** — the same turns were re-judged under policy versions 1.0, 1.1 and 1.2 and every pass was left queued; 16,303 rows carry an explicit `supersedes` pointer. The original plan's "~9,800 heuristic candidates" and "6,582" (the v1.2 count) were both correct. Retiring stale versions is bookkeeping, not review.

### Built

- **`server/review/projects.py`** — the project taxonomy (21 ordered first-match rules) plus `backfill()`. `thread_key` and `project` are now real columns on `derived_memories`; `thread_key` had only ever been serialised into the `reason` text.
- **`server/review/store.py`** — `reviews` + append-only `review_audit`. **Every mutation routes through `ReviewStore.record()` / `record_bulk()`** — one chokepoint, not per-action discipline. A bulk action writes one audit row carrying the filter and prior-state histogram, plus per-row verdicts.
- **`server/review/queue.py`** — `review_queue()` returning project buckets ordered by tier-1 density, UI-ready dicts, evidence optionally inlined.
- **`server/review/explain.py`** — the journal half of `explain()`: statement, unpacked rationale, resolved evidence turns, thread. The Graphiti half is MS6b.
- **`server/review/actions.py`** — `approve/reject/defer_episode`, `apply_verdicts`, `bulk_reject`, `bulk_reject_stale_policy_versions`, `bulk_confirm_superseded`, `sample_audit`, `revert_batch`, `promote_approved`.
- **`server/review/cli.py`** — `backfill`, `stats`, `queue`, `export`, `apply`, `explain`, `retire-stale-versions`, `bulk-reject`, `confirm-superseded`, `sample-audit`, `revert-batch`, `promote`. Every mutating command is dry-run by default and needs `--apply`.
- **Review artifact** — keyboard-driven, project-batched, evidence inlined, verdicts persisted to the artifact's own store so state survives across devices. It reports running keep rate and wall-clock, which is what the exit gate measures.
- **`tests/test_ms6_review.py`** — 41 tests.

### Cut from the original plan, deliberately

- **`approve_thread` / `reject_thread`** — 73% of threads hold one tier-1 episode; a thread action is an episode action with extra machinery.
- **`retier`** — tier 2 is "never promoted"; retiering then approving is just approving.
- **Scopes (`personal` / `project`)** — one person, one graph, and no `recall` caller that would be scoped. Acceptance test 6 tested a feature with no user. Deferred to MS9 access control.
- **`correct_memory` + Graphiti re-issue, deletion propagation** — deferred to MS6b. Both serve the 22 promoted rows, and the correction path cannot be designed well before watching a real review pass.

### Acceptance tests — as built

1. `explain()` returns statement, unpacked rationale, resolved evidence turns and thread. ✅
2. Approval promotes exactly the approved set, idempotently; verdicts survive a promotion that stops early on quota (two ledgers: `reviews` and `promotions`). ✅
3. Every mutation writes an audit row with actor, time, reason and prior state — asserted per action type. ✅
4. A bulk action writes **one** audit row, not one per memory, and `revert_batch` restores the prior `approval_state`. ✅
5. A date-scoped bulk action never sweeps a row whose `event_date` is unknown. ✅
6. ~~scoped recall~~ — cut, see above.
7. **Rewritten.** The plan's "decisions-made vs episodes-reviewed" passes at 215-vs-315 while saving nothing. The property that matters is the grouping: few buckets, no singleton piles. Asserted directly, and the real gate is wall-clock, instrumented by the review surface.

### Exit gate — answered by the 2026-09-08 pass (see Update 2026-09-09 below)

- **Wall clock.** Target was under 3 hours of measured review time. Not recoverable from the journal (bulk verdict write) — the review artifact holds it.
- **Tier-1 keep rate.** ~98% on the tier-1-routed slice (295/301). Settles the ranker question: the `reasoning_kind` router already does the triage a ranker would, so a ranker is only worth building for a future source that lacks one.

**Effort:** ~2 sessions. **Status:** surface built; **tier-1 pass completed 2026-09-08.**

### Update 2026-09-09 — the tier-1 pass is done, and it fed the Spark rebuild

The `reviews` table records reviewer `todd`, 2026-09-08: **295 tier-1 episodes approved, 6 rejected**, of the ~301 routed to tier 1 by `reasoning_kind`. Those 295 approved episodes are exactly what was promoted — into `mem-fabric-gemini` (285 succeeded + 24 transient-failed) and then, on the [Spark local-inference migration](SPARK-MIGRATION-PLAN.md)'s Phase 6, into the fresh **`mem-fabric-local`** (295 succeeded) on GLM-4.7-Flash + `nomic-embed`. So `mem-fabric-local` is the reviewed tier-1 corpus re-run through local models, not raw material.

- **Keep-rate exit gate — answered.** ~98% kept **on the tier-1-routed slice** (295/301). The routing did the filtering; within tier 1 almost everything was a keep. The earlier 25–68% guesses were over *all* statements, not the routed subset — so an LLM triage *ranker* for the next corpus is only worth building if a future source lacks a comparable `reasoning_kind` router. Not urgent.
- **Wall-clock exit gate.** Verdicts were bulk-written from the review artifact (all 301 `reviewed_at` within ~0.05s), so measured review time isn't in the journal — read it off the artifact's own instrumentation if the number still matters.
- **Verdicts are graph-independent.** They live in `reviews` / `derived_memories`, not FalkorDB, so re-promoting the same set into whichever graph wins the Phase 7 A/B is cheap (`promote_reviewed`, local models, no quota).
- **Open decision: which graph is production.** `.env` still points at `mem-fabric-gemini`. Making `mem-fabric-local` the live graph needs the deliberate `CMF_LLM_PROVIDER` + `CMF_EMBED_PROVIDER` + `FALKORDB_DATABASE` + `EMBEDDING_DIM` flip (they move together — SPARK plan §"Note for Phase 6"), and Phase 7's A/B is what settles whether to make it.

**Update 2026-09-09 (Phase 7 answered + executed).** The A/B ([docs/spark-phase7-ab-log.md](spark-phase7-ab-log.md)) rejected GLM-4.7-Flash and adopted **`unsloth/qwen3.5-122b-a10b` + an `EXTRACTION_INSTRUCTIONS` nudge**: Gemini-class hygiene, ~5-pt recall gap, fully local. **Migration executed same day:** GLM graph → `mem-fabric-local-glm`; fresh `mem-fabric-local` re-promoted from the 295 tier-1 episodes on qwen3.5-122b + nomic (295/295, 0 failed); episodes renamed `<harness>-<project>-NNN`; `.env` flipped — `mem-fabric-local` is now the live graph. `mem-fabric-gemini` retained for rollback. So the tier-1 corpus is retrievable on fully-local inference; MS7 evaluation now runs against that graph.
- **MS6a and Spark Phase 7 share a sample.** Both want a hand-graded set of promoted episodes compared across the two graphs — run them together.

---

## MS6b — Governance — **next**

MS7 is done (see [plan-history.md](plan-history.md#ms7--context-assembly-quality)); MS6b is the active milestone. Deferred from MS6 on the grounds that all of it serves the promoted rows, and the correction path should be designed after a real assembly pass rather than before it.

- [x] **`explain()` into Graphiti** — `server/review/graph_explain.py`'s `explain_graph()` resolves `memory_id` -> `episode_name` via `PromotionStore`, then walks the FalkorDB `Episodic` node, its `MENTIONS` entities and the `RELATES_TO` edges citing it. Returns `None` for a never-promoted memory (fall back to the journal-only `explain()`) and `found_in_graph: False` when the ledger says promoted but the episode is actually absent, rather than raising. Wired into the CLI as `explain <memory_id> --graph`. 11 tests against a hand-rolled `FakeDriver`/`FakeGraphiti` (no real FalkorDB) — `tests/test_ms6b_governance.py`.
- [x] **`correct_memory`** — `server/review/correction.py`. Re-issues via `remove_episode` + `add_episode` under the *original* episode's `valid_at` (preserved from the graph read, not re-derived), with a fresh episode name (a removed episode's name isn't reusable) and re-run extraction against the corrected text. Updates `PromotionStore` to the new episode name so it stays the resolvable identity for this `memory_id`; audited via `ReviewStore.note()` (not `.record()` — this isn't a keep/drop verdict). No-ops when the new content matches the graph's current content. CLI: `correct-memory <memory_id> --content "..." --reason "..."`.
- [x] **Deletion propagation** — `delete_memory` in the same module: removes the Graphiti episode and deletes the `PromotionStore` row (new `PromotionStore.delete()`), leaving `derived_memories` and the journal untouched — takes back a promotion, doesn't un-happen the reviewed event. Tolerates the episode already being absent from the graph (logs and clears the ledger anyway) rather than failing. Re-promoting the same `memory_id` (`promote_reviewed`) is the recovery path. CLI: `delete-memory <memory_id> --reason "..."`.
- [ ] **Scopes** — revisit as access control alongside MS9, or MS4c if remote ingest needs it first. Not attempted here — see MS6a's original cut rationale (one person, one graph, no scoped caller yet).

**Effort:** 2-3 sessions. **Risk:** Medium — the correction re-issue path is the one genuinely fiddly piece. **Status (2026-09-10):** the three graph-touching pieces are built and unit-tested against fakes; not yet exercised against a real FalkorDB graph or wired into the review artifact's UI. Scopes deliberately deferred.

### Exit gate — live round-trip (not yet run)

The plan never defined acceptance tests for MS6b the way MS6a did; `tests/test_ms6b_governance.py`'s 11 tests prove the logic against a hand-rolled `FakeDriver`/`FakeGraphiti` — real correctness of the Cypher against Graphiti's actual schema, and of `remove_episode`/`add_episode` sequencing, is unverified until this runs against a real graph. Two checks, in order:

1. **Read-only, against production, zero mutation risk.** `explain --graph` never writes. Pick any real promoted memory_id (a `reviews` row with `review_state='approved'`) and confirm the entities/edges it returns look right:
   ```
   uv run python -m server.review.cli explain <memory_id> --graph
   ```
2. **Full round-trip, fully isolated from production.** `scripts/ms6b_exit_gate.py` seeds one real episode into its own scratch FalkorDB graph (`cmf-ms6b-exit-gate` by default — refuses to run against `mem-fabric-local`/`mem-fabric-gemini`/`mem-fabric-local-glm`) and a scratch SQLite file (a tempfile, never `imports/journal/journal.db`), then runs `explain_graph` → `correct_memory` (dry run, then applied) → `delete_memory`, asserting at each step against the real graph state (old episode actually gone, new content actually present, `PromotionStore` actually cleared) rather than mocks. Exits non-zero on the first failed assertion:
   ```
   uv run python scripts/ms6b_exit_gate.py --cleanup
   ```
   Passing both is what would flip this section's status to "done."

---

## MS4a — MCP-boundary capture — live verification

**Built and unit-tested** (see [plan-history.md](plan-history.md#ms4a--mcp-boundary-capture-claude-desktop)). What remains is the roadmap's five-step live cross-harness test, which needs a live Claude Desktop connected to a build carrying the capture middleware:

1. Record a decision in Claude Desktop.
2. Verify it consolidates into memory with source provenance naming Claude Desktop.
3. Retrieve it from a second harness (Claude Code, or ChatGPT via HTTP transport).
4. Correct it from that second harness.
5. Verify Claude Desktop sees current state while history stays inspectable.

Not a blocker for MS6 / MS7. Do it opportunistically the next time Claude Desktop is on a middleware build.

---

## MS4b — Claude Code adapter

**Goal:** Highest-fidelity capture available in the stack — Claude Code writes full local transcripts and supports lifecycle hooks.

**Why now:** After the retrieval loop is proven. Second capture priority once capture resumes.

### Available surfaces (verified)

- **Transcripts:** `~/.claude/projects/<path-slug>/<session-uuid>.jsonl` — full turn-by-turn including tool calls. ~15 projects present.
- **Hooks:** `~/.claude/settings.json` `hooks` block (already in use for `Notification`). `SessionStart`, `Stop`, `PostToolUse` available.
- **History:** `~/.claude/history.jsonl`.

### Tasks

- [ ] JSONL transcript parser → canonical source events, preserving native session / turn / tool / model ids.
- [ ] Hook installer that **merges** a CMF block into `~/.claude/settings.json` — never overwrites the existing `Notification` hook or statusline config.
- [ ] `SessionStart` hook: open a CMF session, optionally inject prior context.
- [ ] `Stop` hook: enqueue the completed transcript for consolidation.
- [ ] Backfill mode across all existing project directories, idempotent on re-run.
- [ ] Per-project allow/deny.
- [ ] Hooks must complete fast and fail open — never block or slow a turn.
- [ ] Secret filtering over tool payloads (Claude Code payloads frequently contain file contents and command output).

### Exit gate

**How much of a coding session is worth keeping?** A transcript is mostly file reads and tool output. Per ADR 0005, "summaries" is no longer a hand-wave — MS3.5's reasoning-episode derivation already runs on the journal. The MS4b decision is narrower: **which raw Claude Code event types reach the journal at all** (full turns vs. tool-output-elided), given MS3.5's stage derives the synthesis on top. Keep enough raw turns that reasoning extraction has substrate; elide pure file-read / tool-output noise.

**Effort:** 3–4 sessions.
**Risk:** Low-medium. Local files, documented hooks. Volume is the real risk — coding transcripts are large and everything consolidated costs an extraction call.

---

## MS4c — OpenClaw adapter and `cmf-http`

**Goal:** Capture the Studio Network's operational events as project-state context — the first genuinely cross-host, cross-agent source, and the strongest demonstration of the fabric thesis. OpenClaw runs on separate hardware from CMF, which forces `cmf-http` (unscheduled in the roadmap) into this milestone.

### Tasks

- [ ] **`cmf-http`:** HTTP ingest endpoint accepting canonical source events — authentication, request validation, idempotency keys.
- [ ] Multi-actor event model — the named agents each with their own identity; `actor.id` carries the agent identity, not just `type: agent`.
- [ ] Map OpenClaw's messaging-mediated messages and approvals into canonical events.
- [ ] Retry + queue on the OpenClaw side so a CMF outage drops nothing.
- [ ] Scope model: which agents write to which context scope (depends on MS6 scopes).
- [ ] Record the CMF ↔ Interlock boundary as an ADR — CMF holds context, Interlock governs agent control.

### Acceptance tests

1. An OpenClaw agent event ingested over HTTP from the other host appears in the journal with agent identity preserved.
2. Duplicate delivery with the same idempotency key produces one event.
3. CMF down for 10 minutes loses zero OpenClaw events (queued + retried).
4. An OpenClaw-originated memory is retrievable from another harness.

### Exit gate

**Authentication and tenancy for remote ingest.** Shared secret, mTLS, or SPIFFE-based? OpenClaw already runs SPIRE — reusing it is coherent but couples CMF to that infrastructure. Decide explicitly, and threat-model before anything listens beyond loopback.

**Effort:** 4–5 sessions (HTTP transport is most of it).
**Risk:** Medium. First network-exposed surface.

---

## MS4d — Codex and Gemini CLI

**Goal:** Round out coding-harness coverage. Deliberately last — by this point the adapter pattern is proven three times.

### Tasks

- [ ] Survey each harness's actual local surfaces first — do not assume a Claude Code-shaped transcript exists.
- [ ] Implement against the adapter development kit that falls out of MS4a–MS4c.
- [ ] Conformance tests shared with the other adapters.

**Effort:** 2–3 sessions each.
**Risk:** Low, assuming usable local surfaces. If a harness offers no capture surface, say so and fall back to export ingestion rather than building something fragile.

---

## MS5 — Knowledge-provider generalization

**Goal:** Make the LLM Wiki *one* supported knowledge provider rather than a requirement. Roadmap MS5.

**Why after the adapters:** The wiki already serves retrieval today (`FileKnowledgeProvider`, verified working). MS7 builds assembly against it as-is. MS5 is only needed when a *second* knowledge source (GitHub) or a provider-neutral proposal API is actually wanted.

### Tasks

- [ ] Formalize the normalized `KnowledgeResult` contract (the dataclass exists in `server/core/models.py`, forward-declared).
- [ ] Keep the existing local file / Markdown provider.
- [ ] Add GitHub repository retrieval as a separate provider.
- [ ] Allow multiple providers in one query; preserve provider provenance, source version, access scope; never collapse results or assign a fixed authority hierarchy.
- [ ] Provider-neutral `propose_knowledge_change(...)`; retain `search_wiki` / `propose_wiki_update` as backward-compatible aliases when the wiki provider is configured.

### Exit gate

**Can a second knowledge provider pass conformance tests without changing core?** Prove it with the GitHub provider. Conflicting documents from different providers must stay separately attributable.

**Effort:** 3–4 sessions.
**Risk:** Low-medium. Mostly additive.

---

## MS8 — Replay and evaluation

**Goal:** Use accumulated evidence to improve agents and CMF itself. Roadmap MS8.

### Tasks

- [ ] Convert selected event sequences into replayable cases; snapshot the context available at a historical time.
- [ ] Counterfactual comparison of retrieval / memory policies on the same cases.
- [ ] Regression suites built from real corrected failures.
- [ ] Grade temporal accuracy, provenance, relevance, harmful retention, task outcomes.
- [ ] Export trajectories for compatible evaluation systems; keep private source data out of exported cases unless explicitly authorized.

### Exit gate

**Can a historical case be rerun with its original available context, and can two policies be compared on the same cases?** Replay must not mutate production memory by default.

**Effort:** 4–5 sessions.
**Risk:** Medium.

---

## MS9 — Distribution and ecosystem

**Goal:** Make CMF useful beyond the original personal deployment. Roadmap MS9.

### Tasks

- [ ] One-command local Docker Compose deployment; documented minimal (memory-only) and full (journal + memory + knowledge) configs.
- [ ] Python SDK + OpenAPI description; MCP server package.
- [ ] Adapter and provider development kits + conformance tests.
- [ ] Migration and backup tools (journal identity, provenance, config).
- [ ] Threat model and privacy guide.
- [ ] A web review/governance UI on top of the MS6 APIs.
- [ ] Sample deployments: individual, developer-team, self-hosted server.

### Exit gate

**Can a new user start without an LLM Wiki, and can a third party implement a provider without modifying core?** Backups must restore journal, memory identity, provenance, and configuration.

**Effort:** 4–6 sessions.
**Risk:** Low-medium; breadth, not depth.

---

## Backlog — deferred assembly refinements

Out of MS7 with the exit gate met (`get_context` at 80% of a complete answer, beating every single-provider baseline — [plan-history.md](plan-history.md#ms7--context-assembly-quality)). These push the number higher but were not blockers. Roughly in value order:

- [ ] **`search_wiki` semantic retrieval.** The largest remaining retrieval lever. Lexical search now puts the gold doc in the top-5 on 18/20 because the wiki is well-titled, but it can't cross a vocabulary gap ("garage panel capacity" ↔ "400A 3-phase service" — the C6 miss). A one-time nomic embed of the corpus (~1000 files, chunked) into a persisted vector store, hybrid lexical+vector in `search_wiki`, incremental re-embed on rescan. **Not** per-`remember` work — `remember` writes the graph, the wiki is curated files.
- [ ] **Explicit conflict + staleness signals** (MS7 acceptance test 3). Today `get_context` tags `SUPERSEDED` and leans on the interpretation block; it works when the retrieved set happens to contain both sides (A9), not by design. Add a dedicated "these two facts disagree / this one is likely stale" callout off the `supersedes` / `superseded_by` lineage.
- [ ] **Truncation / omission disclosure** (MS7 acceptance test 4). `get_context` should name what it dropped — N lower-ranked items, an empty provider, a below-threshold wiki tail.
- [ ] **Query-intent classification + per-intent retrieval budgets.** current-state / historical / troubleshooting / research → different hit counts from memory vs knowledge vs journal.
- [ ] **Time-aware modes.** A current-state query prefers valid facts without erasing history; a "what did I decide in March" query reconstructs the prior state.
- [ ] **Context templates** — project-continuation / decision-history / troubleshooting / research, each a different assembly shape.
- [ ] **Token-budget allocation** across evidence / memory / knowledge.
- [ ] **Retrieval explanations** — a debug mode showing why each item was included.
- [ ] **Cross-encoder reranker** (`CMF_RERANKER=bge`, Spark plan D3). Deprioritized: a reranker reorders a candidate set, and the eval showed the candidate set was the problem, not its order. Revisit only if intent-routing surfaces a real ordering gap.
- [ ] **Residual eval misses.** A1 — `gemini-openclaw-002` (the friend's-Spark decision) is retrieved by neither the edge nor the vector arm; needs the extraction gap closed or a broader vector recall. B7 — `+memory` regressed 1→0 after the vector arm (wiki-domain query, `+both` unaffected); accepted. C6 — see `search_wiki` semantic retrieval above.

---

## Two standing notes

- **MS6 is the differentiation milestone.** *"Why does the system believe this?"* is the capability competitors do not offer. It should not slip indefinitely behind adapter work.
- **MS7 is done** (2026-09-10). The answer-quality eval (`tests/fixtures/ms7_eval/`, Artifact `ms7-answer-grader`) is the reusable instrument — re-run `capture.py` + `answer_eval.py` after any retrieval or assembly change.
