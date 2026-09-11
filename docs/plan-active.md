# Implementation Plan — Active milestones

The milestones still to do, in execution order. Index and decisions log: [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). Completed milestones: [plan-history.md](plan-history.md).

---

## MS6c — MCP server cross-agent verification (2026-09-11)

**Goal:** Verify the CMF MCP server actually works, end-to-end, as an installed connector inside the real client apps Todd uses. Produce a corrected, followable install/configure guide for each. Order: **Claude Desktop — Cowork mode → Claude Desktop — Code mode → Gemini Spark (web/mobile) → ChatGPT.**

**Corrected 2026-09-11 (Todd):** Todd does not run the standalone Claude Code CLI/TUI — "Claude Code" here means **Claude Desktop's Code tab**, the same app as Cowork, not a separate product. This session is itself running in that tab. And "Gemini" means the consumer **Gemini web/mobile app** (`gemini.google.com/app`, `gemini.google.com/spark/apps`) — not the Gemini CLI, which Todd doesn't use either. Both corrections changed the actual mechanism below, not just the label.

**Why now:** The graph now holds 465 real episodes worth retrieving (the [Backlog](#backlog)'s review pass just finished) and MS6b's governance tools are done — there's finally something substantive to verify retrieval/correction *against* from a second context. This subsumes [MS4a](#ms4a--mcp-boundary-capture--live-verification)'s outstanding five-step live cross-harness test as Phase 1+2's combined acceptance test; once that passes, MS4a's exit gate is answered too, not just this milestone's.

**What this is not:** new capability. `docs/CLIENTS.md` already documents stdio config for Claude Desktop/Cursor/VS Code and a generic remote-HTTP path (`--transport streamable-http`/`sse`) for remote clients — the server already supports all three transports (`server/mcp.py`'s `--transport {stdio,sse,streamable-http}`). This milestone is about *actually running* those configs against each real client and fixing whatever CLIENTS.md gets wrong.

### Phase 1 — Claude Desktop: Cowork mode

- [ ] Install per `docs/CLIENTS.md` §1 (stdio, `claude_desktop_config.json`); restart; confirm tool discovery (12 tools with `LLM_WIKI_PATH` set, 10 without — verify the count actually matches, CLIENTS.md's claim is untested).
- [ ] Functional pass: call each read-only tool (`get_context`, `search_wiki`, `recall_mem`, `capture_health`) from Cowork; confirm sane, correctly-formatted output (Markdown rendering inside Cowork's UI is a real risk, not just "does the call succeed").
- [ ] `remember()` a real test decision from Cowork.
- [ ] Correct CLIENTS.md §1 against what actually happened (restart behavior, `LLM_WIKI_PATH` gating, anything undocumented).

### Phase 2 — Claude Desktop: Code mode

- [ ] Check whether the Code tab shares Desktop's global `mcpServers` config automatically, or needs its own `.mcp.json`/project-level config the way the standalone Claude Code CLI does — this is a real open question, not an assumption either way, since Desktop's Code tab may not behave identically to the CLI it's built on.
- [ ] Same tool-discovery + functional pass as Phase 1.
- [ ] **MS4a's 5-step test, run for real, using Cowork and Code as the two harnesses:** the `remember` call from Phase 1 (Cowork) lands in the journal tagged with a harness identity and a synthesized session id (stdio has none natively) → from Code mode, retrieve it (`recall_mem`/`explain`) and confirm content + provenance → `correct-memory` it from Code mode → confirm Cowork's next `get_context`/`recall_mem` call reflects the corrected version while `explain` still shows the original superseded.
- [ ] Capture-identity check: confirm `server/capture/`'s harness-identity resolution actually distinguishes the Code tab from Cowork — same top-level app, so this is a real risk of both collapsing to one `claude-desktop` identity, not a formality. If they do collapse, that's a finding worth recording, not a bug to silently work around.
- [ ] Add a dedicated **Claude Desktop Code mode** note to `docs/CLIENTS.md` §1 (currently silent on whether Cowork and Code need separate configuration).

### Phase 3 — Gemini Spark (web + mobile)

- [ ] **Requires a public MCP server URL** — Gemini Spark's Connected Apps only take a hosted MCP endpoint, no local/stdio option (confirmed 2026-09-11 against Google's own support docs: [support.google.com/gemini/answer/17209137](https://support.google.com/gemini/answer/17209137)). Same underlying requirement as Phase 4 (ChatGPT) — **share one tunnel setup across both phases** rather than building it twice: run `server/mcp.py --transport streamable-http --port 8000` and expose it via a persistent tunnel (Cloudflare Tunnel/ngrok), ideally as a `launchd` service mirroring `com.cmf.spark-tunnel.plist` from this session.
- [ ] Connect: `gemini.google.com` → Settings & help → Connected Apps → "Custom apps for Spark" → Add a custom app → paste the MCP server URL. If the server doesn't support Dynamic Client Registration (CMF's doesn't yet), use "Show more" under Advanced features to enter credentials manually.
- [ ] Decide auth for this path specifically — DCR/OAuth is the primary expected flow per Google's docs; verify whether the manual-credentials fallback actually accepts a simple bearer token in practice, or requires something closer to real OAuth. Don't assume; this is exactly the kind of detail that changes on contact with the real UI.
- [ ] **Note the real constraints:** personal Google account only (no work/school account), 18+, US region, and — per Google's own guidance — custom third-party MCP servers are "outside Google's control," so this is Todd's own server he already trusts, not a third-party risk.
- [ ] Usage is `@`-mention-scoped (`@context-memory-fabric` or whatever name it registers as) inside a Spark task, not available in plain Gemini chat outside Spark — confirm this doesn't silently limit which of CMF's tools actually get invoked in practice.
- [ ] Same functional + capture-identity pass.
- [ ] Add a **Gemini Spark** section to `docs/CLIENTS.md` (today's §4 is generic "remote harnesses" language; this replaces the Gemini-CLI-shaped assumption that was here before the correction).

### Phase 4 — ChatGPT

- [ ] Reuse Phase 3's tunnel — same `streamable-http` endpoint, no separate infra needed.
- [ ] Connect: Settings → Apps & Connectors → enable Developer Mode → Create connector → server URL must end in `/mcp` → OAuth or bearer token (confirmed 2026-09-11).
- [ ] Decide auth: a bearer token is almost certainly the right call for personal single-user use over full OAuth — flag as the recommended default, don't build OAuth unless it's actually needed.
- [ ] Same tool-discovery + functional + capture-identity pass.
- [ ] Update CLIENTS.md §4 with the concrete, current ChatGPT steps — today it's generic where it could be exact.

### Acceptance tests

1. All 4 contexts (Cowork, Code, Gemini Spark, ChatGPT) can list and successfully call every registered CMF tool, with correctly-rendered output.
2. MS4a's 5-step cross-harness capture+correction test passes end-to-end using Cowork and Code mode as the two harnesses.
3. Capture-journal harness identity is checked for all 4 — and it's an explicit finding, not a silent assumption, whether Cowork/Code collapse to one identity or resolve distinctly.
4. `docs/CLIENTS.md` is corrected/expanded so a new user could follow it start-to-finish for any of the 4 without hitting an undocumented gap.
5. One shared tunnel serves both Gemini Spark and ChatGPT without reconfiguration.

### Exit gate

Does the MCP server actually work, end-to-end, inside Claude Desktop (both modes), Gemini Spark, and ChatGPT — and does `docs/CLIENTS.md` reflect that reality closely enough for a stranger to follow? Which of MS4a's outstanding live-verification steps does this pass answer for free?

**Effort:** 2-3 sessions — live/interactive testing plus doc corrections; the tunnel (shared by Phases 3-4) is the one piece of real new infra work.
**Risk:** Low-medium. The tunnel is the main new operational surface — same class of concern as the Spark SSH tunnel (needs to stay up reliably, exposes a port that needs real auth, not "trust the network"). Gemini Spark's DCR/OAuth requirement is an unknown until tested — may need real OAuth implementation work, not just a bearer token, unlike ChatGPT.

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
- [ ] **Scopes (`personal` / `project`)** — access control model, deferred from MS6b (one person, one graph, no scoped caller yet). MS4c may need it sooner if OpenClaw's multi-agent writes require per-agent scoping.
- [ ] A web review/governance UI on top of the MS6 APIs.
- [ ] Sample deployments: individual, developer-team, self-hosted server.

### Exit gate

**Can a new user start without an LLM Wiki, and can a third party implement a provider without modifying core?** Backups must restore journal, memory identity, provenance, and configuration.

**Effort:** 4–6 sessions.
**Risk:** Low-medium; breadth, not depth.

---

## Backlog

Two unrelated piles, previously kept as separate top-level sections (the corpus one used to sit at the top of this file) — merged here since neither is an active milestone with its own exit gate.

### Corpus & review backlog (found 2026-09-11, closed out same day)

MS6b's governance tooling ([plan-history.md](plan-history.md#ms6b--governance)) surfaced this once `explain --graph`, `correct-memory`, and the review CLI could actually be pointed at the corpus. Measured directly against the live journal, not the MS6a/MS3.5-era estimates (several bulk actions and ongoing capture had moved these numbers since 2026-09-08):

- [x] **27 tier-1-shaped v0.1 orphans, reviewed (2026-09-11).** All were `decision`×16/`plan`×8/`rejected_alternative`×3, `policy_version=0.1`, correctly excluded from `tier1_review_queue()`'s v0.2 filter but never formally retired. Checked each against v0.2 for actual evidence-event overlap rather than assuming duplication: **19 confirmed duplicates** (same evidence, reprocessed under v0.2, `rejected` with reason citing the duplication) and **8 with no v0.2 counterpart**, individually read in full (statement + rationale + evidence) — **4 approved** (specific, confirmed-accurate technical/narrative decisions: Saturn-mode print resolution, moon/sun/Saturn mask config, Java-over-Kotlin for the Android project, integrating the fine-arts narrative into the career-navigator cover letter) and **4 rejected** (two were literal task instructions, not durable facts, one still `status=open`; two were thin one-off wording edits on a resume/LinkedIn post with no lasting reference value).
- [x] **969 tier-2 episodes, reviewed and promoted (2026-09-11) — closed out.** `finding`×16, `hypothesis`×33, `experiment`×138, `investigation`×755 (the live count moved from the 981 estimate — ongoing capture). Read individually — statement, rationale, status, and evidence turns for ambiguous ones — against one standard: promote only if the statement itself states a durable, specific, resolved conclusion; reject pure process narration, open unresolved threads, or task instructions misclassified as reasoning; defer anything genuinely uncertain or sensitive rather than guessing. First pass: **163 approved, 797 rejected, 9 deferred**. Promote rate varied by kind as expected (`decision`-adjacent kinds like `finding`/`hypothesis` ran ~35-50%; `investigation`/`experiment`, which are mostly exploration without a stated resolution, ran ~12-31%) — confirms MS3.5's own observation that `reasoning_kind` is a routing hint, not a keep/drop gate; individual content had to be read either way. Verdicts applied via `apply_verdicts` (the same chokepoint MS6a's tier-1 pass used).
  - **The 9 deferred, resolved by Todd (2026-09-11):** *Sensitive/personal (5)* — a finding connecting current binocular vision instability to a past brain injury + neuro-ophthalmologic history; the matching hypothesis and investigation episodes from the same thread (astigmatism theory, single-eye-vs-both testing); an investigation seeking medical guidance on OTC pain relievers after a head injury; an investigation analyzing a condo board-meeting transcript evaluating specific named candidates (Ken, Kevin, Brian) for board openings — **Todd approved all 5**. *Genuinely uncertain (4)* — a home-AV finding describing a symptom mid-troubleshooting (Shield/projector power state); a hypothesis about whether current homeowners insurance covers required EV-charger terms; a hypothesis interpreting the condo board's resistance motive as capacity-hoarding rather than genuine cost concern; an experiment with real measured data (Jackery AC-vs-DC power draw) the user themself questioned the accuracy of — Todd rejected the home-AV symptom and the power-draw measurement, approved both EV-charging hypotheses.
  - **Final tally: 171 approved, 799 rejected, 0 deferred**, all 171 promoted into `mem-fabric-local` across three batches, **171/171 succeeded, 0 failed** (real qwen3.5-122b extraction per episode, local/unmetered). Graph grew **295 → 465 Episodic nodes** (verified via direct Cypher count), entities 393→665, `RELATES_TO` edges →501.
  - **A real bug surfaced when Todd asked why the first batch was 164, not 163** (2026-09-11): one of the 164 wasn't a tier-2 approval at all — it was the *original, pre-correction* 360-cam/eclipse episode (the misattribution `correct_memory` fixed earlier in the MS6b work), silently re-promoted with its stale wrong content. Root cause: `reviews` is last-writer-wins per memory_id and `correct_memory` never touches it, so the old memory_id's `approved` verdict from 2026-09-08 stayed on record after the correction superseded it; `correct_memory` separately clears the old memory_id's `PromotionStore` row (the graph identity moved to the new memory_id). Those two facts together made `actions.promote_approved`'s "approved and not yet promoted" query — which had no idea `derived_memories.approval_state` existed — treat the superseded old memory_id as freshly eligible. **Fixed:** the query now excludes any memory_id whose `derived_memories.approval_state` is `rejected`/`superseded_by_reasoning`/`superseded_by_correction`, joining against `derived_memories` rather than reading `reviews` alone (`server/review/actions.py`). New regression test `test_superseded_by_correction_is_not_reeligible` (`tests/test_ms6_review.py`) reproduces the exact sequence and passed on the very next real promotion batch (the 2 final EV-charging approvals). **Cleanup:** the wrongly-revived `chatgpt-photo-006` episode (uuid `2734ac71-...`) removed from `mem-fabric-local`, its stray `PromotionStore` row deleted.
- [ ] **25,961 heuristic-pattern rows still `queued_for_review`**, none ever routed through `ReviewStore` (0 have a `reviews` row — every heuristic-pattern state change so far, including the 27,516 already `rejected` and 3,251 `superseded_by_reasoning`, was a direct bulk `UPDATE`, not an individually reviewed verdict). MS3.6's own assessment stands: mostly re-judged duplicates across policy versions and low-signal raw turns, not undiscovered content. `retire-stale-versions` / `bulk-reject-stale-policy-versions` already exist for this — the open question is whether it's worth running them again now, or whether this pile is simply not worth further attention.
- [ ] **The corpus is growing, not static.** The reasoning-episode pool alone grew from 1,243 rows (the 2026-09-05/06 reprocess) to 1,310 by 2026-09-11 — capture (MCP-boundary + imports) kept running after the 2026-09-08 review pass. A recurring/periodic tier-1 review pass is probably the more accurate framing going forward, rather than treating any fixed count as a target to eventually finish. `uv run python -m server.review.cli queue --tier 1` shows what's currently outstanding.
- **Not sourced from new adapters at all yet:** MS4b (Claude Code), MS4c (OpenClaw), MS4d (Codex/Gemini CLI) remain unbuilt — none of the above touches those.
- **Also found, unrelated to the review pass itself:** the `cmf_test` FalkorDB graph's vector index is still 1024-dim (Gemini-era) while the configured embedder produces 768-dim (local/nomic) — every `live`-marked test that calls `remember()` against `cmf_test` currently fails with a vector-dimension mismatch, independent of any of this session's code changes (confirmed by re-running before/after). `cmf_test` was never migrated alongside `mem-fabric-local` in the Spark migration; needs the same treatment (`docs/spark-phase7-ab-log.md`'s migration steps, applied to the test graph).

### Assembly refinements (deferred out of MS7)

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
