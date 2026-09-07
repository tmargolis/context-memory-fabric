# Implementation Plan — Active milestones

The milestones still to do, in execution order. Index and decisions log: [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). Completed milestones: [plan-history.md](plan-history.md).

---

## MS6 — Review and governance

**Goal:** Make staged context inspectable and correctable, and move it into the graph at scale. MS3.5/MS3.6 produced ~1,221 unpromoted reasoning episodes + ~9,800 heuristic candidates that all need human judgement before promotion. MS6 is the surface for that — plus the *"why does the system believe this?"* capability, which is the core differentiation competitors don't offer.

**Why now:** Promotion (MS3.6) works but only 22 episodes are through it. MS7 (assembly quality) can't be evaluated until there's real material in the graph, and that requires bulk review. Separately: every `remember()` / `edit_memory` today is unaudited — no actor / time / reason / prior-state record.

**Scope — three things, in priority order:**

1. **Bulk promotion review** for the staged backlog — the unit of review is a **thread**, then a **kind**, not an episode. Per-episode modals do not scale to 1,200.
2. **Evidence trace** — *"why does this memory exist"* walks episode → derived_memory → source events → journal turns → thread.
3. **Correction + audit** — edit / reject / re-tier with a recorded reason and prior state; deletion that propagates without falsifying history.

### Tasks

- [ ] **Review data model.** A `reviews` table in the journal DB (same one-file precedent as `ConsolidationStore` / `PromotionStore`): per `memory_id` — `review_state` (`pending` / `approved` / `rejected` / `deferred`), `tier` (1/2/3, overridable; seeded from `default_tier()`), `reviewer`, `reviewed_at`, `reason`, `prior_state_json`. Append-only audit rows for every mutation.
- [ ] **`review_queue()` API.** Returns **threads**, not episodes — each with its episodes, their `reasoning_kind`s, evidence counts, current auto-tier, and thread status (`open` / `resolved`). Ordered by tier-1-episode density (most decisions-per-look first). Filters: harness, kind, date range, thread status. Structured output (dicts), not formatted strings — must be UI-ready.
- [ ] **Bulk actions**, each writing an audit row:
  - `approve_thread(thread_key)` — promotes every tier-1 episode in the thread via `promote_reviewed`, marks the rest `deferred` (stay in-thread as work journal).
  - `reject_thread(thread_key, reason)` — marks all its episodes `rejected`.
  - `retier(memory_id, tier, reason)` — move an episode between tiers.
  - `approve_episode` / `reject_episode` — single-episode escape hatches.
- [ ] **`explain(memory_id)`.** Returns the reasoning episode's `statement` + `reason` fields + its `evidence_event_ids` resolved to the actual journal turns + the `reasoning_thread` it belongs to + (if promoted) the Graphiti episode name and the entities/edges Graphiti extracted. This is the differentiation feature — a complete answer to "why".
- [ ] **`correct_memory(memory_id, {statement? | reasoning_kind? | event_date?}, reason)`.** Updates the staged `derived_memories` row; if the memory is already promoted, re-issues to Graphiti (`remove_episode` + `add_episode` with the correction, carrying the original `reference_time`). Audit row with prior state. **Journal evidence is never touched.**
- [ ] **Deletion propagation.** `delete_memory(memory_id, reason)` — removes from Graphiti (`graphiti.remove_episode`, which cleans up entities/edges mentioned only by that episode and leaves shared ones), reverts the `PromotionStore` row, leaves `derived_memories` + journal intact, writes an audit row. Turns the MS2/MS3.6 "architecturally satisfied" invariant into a *tested* one.
- [ ] **Heuristic pile surfacing.** The 6,582 `queued_for_review` heuristic rows *and* the 3,251 `superseded_by_reasoning` rows appear in the queue; superseded rows link to the covering reasoning episode so a reviewer confirms (one click) rather than re-judges.
- [ ] **Scopes.** `personal` / `project` tags on memories (extend later to `team` / `org`); `recall` / `get_context` never cross a configured scope. Minimal for MS6 — personal vs project, enforced at retrieval.
- [ ] **Surface: CLI first** (`cmf review …` subcommands over the APIs above), matching the `server/journal/cli.py` precedent. A web UI is MS9 (distribution), not MS6 — but the APIs are built UI-ready from the start.

### Files touched

New: `server/review/{__init__,store,queue,actions,explain,scopes}.py`, `server/review/cli.py`, `tests/test_ms6_review.py`. Modified: `server/consolidation/promotion.py` (called from `approve_*`), `server/providers/memory_graphiti.py` (correction re-issue + deletion path), `server/context.py` (scope filter at retrieval), `docs/CLIENTS.md` / `README.md`.

### Acceptance tests

1. `explain(memory_id)` for a promoted episode returns statement, evidence turns, thread, and Graphiti entities — a complete "why".
2. `approve_thread` promotes exactly its tier-1 episodes; a second call is a no-op (idempotent via `PromotionStore`); its tier-2 episodes are marked `deferred`, not promoted.
3. Correcting a promoted episode's statement updates the graph, records prior state, and preserves `reference_time`; `recall` returns the corrected form; `explain` shows the correction in the audit trail.
4. Deleting a promoted episode removes it from the graph, reverts the promotion ledger, and leaves the journal + `derived_memories` row intact.
5. Every mutation (approve / reject / correct / delete / retier) writes an audit row with actor, time, reason, prior state — asserted for each action type.
6. A `recall` scoped to `personal` never returns a `project`-scoped memory.
7. Reviewing the ~301 tier-1 episodes by thread takes materially fewer decisions than 301 (measured: threads-touched vs episodes-approved).

### Exit gate

- **Did by-thread review actually scale?** Report decisions-made vs episodes-reviewed for the tier-1 pass. If threads don't usefully cluster episodes, the review unit is wrong — reconsider before MS7.
- **Correction re-issue with Graphiti.** Graphiti has no in-place update; `remove_episode` + `add_episode` must keep the original date and must not orphan shared entities. Confirm both.
- **Scope depth.** Is `personal` / `project` enough for now, or do `team` / `org` scopes need to land here rather than being pushed to MS9?

**Effort:** 5–6 sessions.
**Risk:** Medium. The correction → Graphiti re-issue path is the fiddly part (no in-place update). Audit-everything is easy to design and easy to forget to enforce — route every mutation through one chokepoint, not per-action discipline.

---

## MS7 — Context assembly quality

**Goal:** Deliver *useful* context, not a bag of retrieved items. Roadmap MS7.

**Why now:** Once MS6 has moved real material into the graph, `get_context` is the thing the whole system exists to produce — and today it is a flat concatenation of vector hits. This is where cross-provider retrieval, conflict signals, and token budgeting land.

**Depends on:** MS2 (journal, for time-aware modes), MS6 (real graph content to assemble). **Not** MS5 — the file knowledge provider is enough to build and evaluate assembly against; generalizing providers is independent.

### Tasks

- [ ] **Query intent classification** — current-state vs historical vs troubleshooting vs research; routes retrieval planning.
- [ ] **Provider-aware retrieval planning** — how many hits from memory vs knowledge vs journal, per intent; budget-allocated, not fixed.
- [ ] **Time-aware modes** — a "current state" query prefers valid current facts without erasing history; a "what did I decide in March" query reconstructs the prior state.
- [ ] **Conflict + staleness signals** — surface contradictory memories and likely-superseded ones rather than silently picking one. The `supersedes` / `superseded_by` lineage already exists; expose it in the assembled response.
- [ ] **Token-budget allocation** across evidence / memory / knowledge, with the response naming what was truncated or omitted.
- [ ] **Context templates** — project-continuation, decision-history, troubleshooting, research — each a different assembly shape.
- [ ] **Retrieval explanations** — a debug mode showing why each item was included.
- [ ] **Quality / latency / token-cost metrics**, and a **baseline measurement** (memory-only, knowledge-only) to show improvement against — done *during* MS7, not after.

### Acceptance tests

1. A current-state query prefers valid current facts without dropping history from the trace.
2. A historical query reconstructs a prior decision accurately (verified against a known thread).
3. A response with contradictory memories surfaces the conflict rather than resolving it silently.
4. Every response identifies omitted / truncated categories.
5. Evaluation shows measurable improvement over the memory-only and knowledge-only baselines on a fixed query set.

### Exit gate

**Does cross-provider context measurably beat the single-provider baselines?** On a fixed set of real queries with graded answers. If not, the assembly logic is adding cost without value — simplify.

**Effort:** 4–5 sessions.
**Risk:** Medium. "Better" needs a rubric and a query set before any code — build those first.

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

## Two standing notes

- **MS6 is the differentiation milestone.** *"Why does the system believe this?"* is the capability competitors do not offer. It should not slip indefinitely behind adapter work.
- **MS7 needs a baseline first.** "Improvement over memory-only and knowledge-only baselines" requires those baselines be measured — do that during MS7.
