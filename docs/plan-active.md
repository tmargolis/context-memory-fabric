# Implementation Plan — Active milestones

The milestones still to do, in execution order. Index and decisions log: [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). Completed milestones: [plan-history.md](plan-history.md).

---

## MS4a2 — Cowork live-session episode/entity/wiki capture (current priority, 2026-09-18)

**Goal:** Auto-generate real episodes (and durable wiki content) from live Claude Desktop/Cowork conversations, without a manual export/import round trip.

**Why now:** Todd's actual near-term priority, ahead of MS4b. Cowork has no local transcript and no hook API (`docs/ROADMAP.md`'s Milestone 4 section) — confirmed architectural limit, not a gap to design around — so the only levers are server instructions and new tools the live model can call using what it already has in context.

**Terminology note (worth stating once, since the names collide):** `capture_note` (existing MCP tool) writes a bare marker to the *journal* only, not an episodic memory. A `:Note` *graph node* (wiki-derived layer, `seed_wiki_graph.py`) is a different concept — it represents one actual Markdown file under `LLM_WIKI_PATH`, a real durable wiki doc. `capture_session` (below) is a third, new thing.

### Design: one `capture_session` tool, routes to either destination

One tool call at a checkpoint; the model tags each item `destination: "episode" | "wiki_proposal"` rather than needing two separate tool calls:

- `destination="episode"` — reuses `ReasoningEpisodePolicyV1`'s existing extraction rubric (`server/policies/reasoning_episode_v1.py`'s `_SYSTEM` prompt, lines 54-105: 8-way `reasoning_kind` taxonomy, WHAT COUNTS exclusions, confidence bands) adapted into the tool's own docstring, so live self-extraction applies the same bar offline windowing does. Implementation, per item: (1) journal the model's own `evidence_text` as a lightweight source event first (harness=`claude_desktop`, redacted via `server/capture/filters.py`) — Cowork's raw turns are never otherwise journaled, so this is the only trace that reaches the journal and gives the episode something real to cite; (2) build a `ReasoningEpisode` (`server/policies/protocols.py:113`) from the item's fields; (3) `ConsolidationStore.record_reasoning_episode()` (`server/consolidation/store.py:267`) with `policy_name="cowork_live_v1"`, `policy_version="0.1"` (distinct provenance from offline windowing, same staging path) and `approval_state` from the shared `reasoning_auto_accept_threshold` config (see Review posture below).
- `destination="wiki_proposal"` — no new plumbing, an internal call to the existing `propose_wiki_update(target_path, proposed_content, rationale, source_context=evidence_text)`. Same review path as every other proposal.
- Routing rule (stated in both the tool docstring and `SERVER_INSTRUCTIONS`): *episodic* = something that happened/was decided/was concluded in this conversation; *wiki-worthy* = durable, reusable, still-true-read-cold-later knowledge.

### Review posture (Todd, 2026-09-18)

Start with full manual review via the existing `tier1_review_queue()` to gauge quality and tune the rubric. Once trusted, flip `reasoning_auto_accept_threshold` to a real confidence value and have a scheduled job call the existing `promote_auto_accepted()` — both pieces already exist, this is a config change made after calibration, not new code. **This threshold is shared with MS4b** (below) — one calibration, not two.

### Honesty constraint

Stays **interaction-triggered, not automatic** in the cron/hook sense — no session-end signal exists for Cowork. Per `docs/ROADMAP.md`'s own principle, docs must not imply continuous capture this mechanism can't deliver: this is a better-structured checkpoint than today's bare `capture_note` marker, not a background daemon.

### Tasks

- [ ] `capture_session` tool registered in `server/mcp.py`, docstring carries the adapted rubric + routing rule.
- [ ] `server/capture/session_capture.py` — per-item journal-then-stage logic.
- [ ] Update `SERVER_INSTRUCTIONS` (`server/mcp.py:66-86`) to point "session wrapping up" at `capture_session`, not `capture_note`.
- [ ] Tests: per-item staging, evidence-event creation, secret redaction in `evidence_text`, shared-threshold approval-state behavior.
- [ ] Docs: `docs/CLIENTS.md`/`docs/ROADMAP.md` corrected to reflect the new tool and the interaction-triggered honesty constraint.
- [ ] (Todd, outside this repo) Update his own custom Claude instructions to reference `capture_session` by name once shipped.

### Exit gate

Manual dry run: at the end of a real Cowork session, ask the model to call `capture_session` with a few real items, inspect `queued_for_review` output via the review CLI, confirm evidence/taxonomy/confidence look right before trusting it on real volume.

**Effort:** 1-2 sessions (one new tool + docstring + tests, no new subsystem).
**Risk:** Low. No new storage, reuses `record_reasoning_episode`/`propose_wiki_update` as-is.

---

## MS4b — Claude Code (Desktop's Code tab) transcript adapter — parked, fully designed (2026-09-18)

**Status:** Not the current priority (see MS4a2 above), but fully designed and ready to build when picked up.

**Goal:** Highest-fidelity capture available in the stack — full local transcripts and lifecycle hooks.

**Scope correction (2026-09-18):** Originally scoped as "the standalone Claude Code CLI" only. A direct filesystem check found Desktop's **Code tab** (Todd's actual daily driver, standalone CLI used occasionally) writes the *same* JSONL transcript format to the *same* `~/.claude/projects/<project-slug>/<session-uuid>.jsonl` path — real multi-MB files confirmed. The parser reads by file path/format, not by which binary wrote it, so both are covered by one adapter. Cowork itself still has no transcript; that gap is MS4a2's territory, not this one's.

### Available surfaces (measured against 8 real transcript files, 0.3MB–8.0MB)

Every line is JSON with a `type` field: `user` (string content = real turn, or `tool_result` list = tool output fed back), `assistant` (`text`/`thinking`/`tool_use` blocks — `thinking` often holds a coding session's real substance), plus several Desktop-only bridging types (`bridge-session`, `ai-title`, `atis-latch`, `frame-link`, `pr-link`, etc.) absent from the plain CLI's schema — parser must skip-and-log unrecognized types, never raise. Measured "kept" density: **~50–75 real turns per MB** (~80–85% of raw lines are metadata/tool-output noise) — this answers the exit gate's own "which raw event types reach the journal" question concretely. `~/.claude/settings.json` `hooks` block exists (`Notification` already in use) but **whether Desktop's Code tab actually fires `SessionStart`/`Stop` hooks is unverified** — nothing in this repo demonstrates it; needs a live test before depending on it.

### Dedup against already-imported exports (Todd, 2026-09-18)

Real risk, confirmed against the live journal: `claude`-harness events (the already-imported Claude export) run through `2026-09-04T00:36:13Z`; `gemini`/`chatgpt` exports through `2026-09-04`/`2026-08-31`. A full-history backfill would re-walk the same conversations under harness `claude_code` instead of `claude` — **not** caught by `compute_event_id`'s existing dedup (harness is part of the hash). Mitigation: `backfill --all-projects` defaults to a date cutoff (`CMF_CLAUDE_CODE_BACKFILL_SINCE`, read from the import registry) rather than attempting content-hash matching across differently-shaped importers.

### Review posture

Same as MS4a2 above — start manual via `tier1_review_queue()`, same shared `reasoning_auto_accept_threshold`, flip to automatic once calibrated.

### Tasks

- [ ] `server/adapters/claude_code/parser.py` — line → canonical `SourceEvent`, keep/skip rule per the measured types above, secret redaction via `server/capture/filters.py` before hashing, harness hardcoded to `claude_code` (never resolved via MCP `client_info` — sidesteps the known-broken Cowork/Code-tab identity ambiguity in `server/capture/identity.py` entirely).
- [ ] `server/adapters/claude_code/transcript_reader.py` — byte-offset incremental tailing (files grow live during a session; new `claude_code_tail_state` table in `journal.db`), plus `CMF_CLAUDE_CODE_PROJECT_ALLOW`/`_DENY`.
- [ ] `server/adapters/claude_code/worker.py::process_pending()` — tails changed files, journals new events, calls `run_reasoning_consolidation(..., reasoning_auto_accept_threshold=<shared config>)` per touched conversation. **Verified**: this function never calls `promote_reviewed`/`promote_auto_accepted` under any configuration — threshold only controls `queued_for_review` vs `auto_accepted`. `CMF_LLM_PROVIDER=local` for this worker specifically (see LLM provider decision below), independent of the interactive server's config.
- [ ] Trigger: a launchd agent polling every ~10-15 min (precedent: `com.cmf.spark-tunnel.plist`) as the reliable default, working regardless of hook applicability. A `Stop`-hook accelerant is optional, built last, only if the live hook test (below) passes.
- [ ] `server/adapters/claude_code/hooks.py` — settings.json merge-installer (deep-merge only under `hooks.<EventName>`, back up the file first, never touch `Notification`/`statusLine`/`enabledPlugins`); hook body must do nothing but launch a detached (`nohup ... & disown`) worker and return — no synchronous LLM work.
- [ ] `server/adapters/claude_code/cli.py` — `backfill --all-projects`, `tail --once`, `status`.
- [ ] Tests against the 8 real transcript files already available under `~/.claude/projects/-Users-todd-Dev-context-memory-fabric/` — no need to wait for a new session.
- [ ] Live hook-applicability test: trivial `SessionStart`/`Stop` sentinel hook, open a real Code-tab session, confirm whether it fires — record the answer here.

### LLM provider: local only, not Gemini

`ReasoningEpisodePolicyV1.evaluate_window()` draws from the same shared Gemini rate-limiter ledger live interactive `remember()` calls depend on. With the chosen `TimeGapWindower`, one 8MB/595-turn session is already ~30 extraction calls — meets-or-exceeds the tightest candidate model tier (`rpd=20`) in a single unattended session, with no human gate in front of it (unlike promotion, naturally throttled by review pace). Local Spark has zero quota cost; wall-clock doesn't matter since this runs entirely in the background. **Decision: `CMF_LLM_PROVIDER=local` for this worker, always** — real per-window wall-clock time is unmeasured (only promotion's ~25-38s/episode exists, which includes ~20 embedding calls this step never makes) and is the first thing to measure once built, to set the real poller cadence.

### Exit gate

**How much of a coding session is worth keeping?** Answered concretely by the measurement above, not left as a hand-wave: full text for `user`/`assistant text`/`thinking` blocks, bounded 1000-char summaries only for `tool_result`/`tool_use`.

**Effort:** 3–4 sessions.
**Risk:** Low-medium. Volume and the export-overlap dedup risk (mitigated above) are the real considerations, not hooks (which have a local, zero-cost, hook-independent fallback).

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
- [ ] **Future provider candidates** (not scoped for this milestone's own exit gate — GitHub is still the one that proves the contract; these are the next sources once it does): Gmail (threads/messages as source events, likely feeding the journal via MS4c's `cmf-http` pattern rather than as a `KnowledgeResult` provider directly, since email is evidence, not curated knowledge); Slack (channel history/threads — same evidence-vs-knowledge distinction applies, and multi-workspace scoping will need MS9's deferred scopes model). Both are conversational/event sources, not documents, so they likely enter as capture adapters (MS4-family) that feed the journal, with only a durable subset ever promoted into the knowledge layer — worth revisiting this split once GitHub's provider conformance test exists as a concrete comparison point.

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
- [ ] **25,961 heuristic-pattern rows still `queued_for_review`** across three reprocessed policy versions (1.0: 9,658, 1.1: 9,721, 1.2: 6,582) — the same underlying events reclassified three times, older versions never formally retired. **`retire-stale-versions`** (`python -m server.review.cli retire-stale-versions [--apply]`, implementation `bulk_reject_stale_policy_versions()` in `server/review/actions.py:238` — one tool, not two; there is no separate `bulk-reject-stale-policy-versions` CLI command) already exists for exactly this and keeps the newest version's row per source event, rejecting the rest — its own docstring: "25,961 queued rows cover only 9,757 distinct events, and 16,303 of them carry an explicit `supersedes` pointer." Resurfaced 2026-09-18 while scoping MS4a2/MS4b's episode-proposals file mirror (below) — running this is the actual fix, not file-mirroring 26k near-duplicates.
- [ ] **The corpus is growing, not static.** The reasoning-episode pool alone grew from 1,243 rows (the 2026-09-05/06 reprocess) to 1,310 by 2026-09-11 — capture (MCP-boundary + imports) kept running after the 2026-09-08 review pass. A recurring/periodic tier-1 review pass is probably the more accurate framing going forward, rather than treating any fixed count as a target to eventually finish. `uv run python -m server.review.cli queue --tier 1` shows what's currently outstanding.
- **Not sourced from new adapters at all yet:** MS4b (Claude Code), MS4c (OpenClaw), MS4d (Codex/Gemini CLI) remain unbuilt — none of the above touches those.
- **Also found, unrelated to the review pass itself:** the `cmf_test` FalkorDB graph's vector index is still 1024-dim (Gemini-era) while the configured embedder produces 768-dim (local/nomic) — every `live`-marked test that calls `remember()` against `cmf_test` currently fails with a vector-dimension mismatch, independent of any of this session's code changes (confirmed by re-running before/after). `cmf_test` was never migrated alongside `mem-fabric-local` in the Spark migration; needs the same treatment (`docs/spark-phase7-ab-log.md`'s migration steps, applied to the test graph).

### Proposal-directory housekeeping (found 2026-09-18, scoping MS4a2/MS4b)

Both proposal-review surfaces store everything flat with no move-on-review, and MS4a2's `capture_session` will only add volume to both:

- [ ] **`wiki-proposals/` (78 files, flat, all statuses mixed).** `server/proposals.py`'s `_save_proposal()`/`review_proposal()` always write back to the same flat location; `get_proposals_dir()`/`list_proposals()` glob one directory. Fix: move to `wiki-proposals/approved/` or `wiki-proposals/rejected/` on a terminal status (`applied` stays under `approved/` — sub-state, not a third folder), update lookups to check both locations, one-time migration script to sort the existing 78 by current status.
- [ ] **Staged reasoning episodes have no file representation at all** — they're `derived_memories` rows in `imports/journal/journal.db` (SQLite, already gitignored), not files. Real current volume (queried 2026-09-18): `reasoning-episode@0.2` (current policy) has **1,243 `queued_for_review`** — 301 tier1-shaped (decision/plan/rejected_alternative/retrospective), 942 tier2-shaped (investigation/experiment/hypothesis/finding) — every one accumulated *since* the 2026-09-11 review pass above, unreviewed. Fix: a write-through file mirror, `episode-proposals/{tier1,tier2}/{memory_id}.json`, moved to `.../approved/` or `.../rejected/` by `approve_episode`/`reject_episode` (`server/review/actions.py`) — SQLite stays authoritative, the file is a read-only projection. Explicitly excludes `heuristic-pattern` (that's the retirement item above's job) and stale `reasoning-episode@0.1` (66 rows). Backfill tier1 first (301, matches `tier1_review_queue()`'s own prioritization), tier2 as a second pass.

### Auth hardening (deferred out of MS6c, 2026-09-16)

Raised in review on [PR #6](https://github.com/tmargolis/context-memory-fabric/pull/6) and consciously merged without fixing (Todd, 2026-09-16) — the OAuth layer works and these are hardening, not blockers, on a single-user personal server. The design itself reviewed clean: PKCE correct, codes single-use, refresh tokens rotate on exchange, expiry enforced on both token types, consent password compared with `secrets.compare_digest`.

- [ ] **The auth layer has no test coverage at all.** No test references `OAuthStore`, `CMFOAuthProvider`, or `BearerTokenAuthMiddleware`; the suite's 383 passing tests do not execute one line of `server/core/oauth_provider.py`, `oauth_store.py`, or `http_auth.py`. For the code standing between the open internet and `remember`/`edit_memory`/`import_chatgpt_exports`, that is the gap worth closing first — the flow has enough state transitions (consent → code → token → refresh → rotate) that a regression would be silent and would present as a client-side bug. Highest value: a full round-trip test through the provider, plus the refusal cases (wrong password, expired code, reused code, wrong client_id on exchange).
- [ ] **`exchange_refresh_token` drops `resource`.** `exchange_authorization_code` persists `authorization_code.resource` (`server/core/oauth_provider.py:161`); the refresh path hardcodes `None` (`:202`), so a token's audience binding silently disappears the first time it refreshes. Not exploitable today — the SDK's `ProviderTokenVerifier` only calls `load_access_token` and never checks `resource` — but it becomes a real bug the moment RFC 8707 audience validation is enabled, and it would surface ~30 days after a client first connects. `RefreshToken` needs to carry the resource forward for this to be fixable at all.
- [ ] **Tokens are stored in plaintext.** `oauth_access_tokens.token` / `oauth_refresh_tokens.token` are raw values used as PRIMARY KEY, in the same `journal.db` the journal writes. A stray copy or backup is working credentials for 30 and 180 days. Storing SHA-256 and looking up by hash is one line per save/get pair.
- [ ] **No rate limiting on the consent password**, which is the entire security boundary by design, on an endpoint reachable by anyone who finds the URL. `openssl rand -hex 16` as documented makes brute force infeasible — so the real action is making CLIENTS.md say that recommendation is load-bearing rather than advisory.
- [ ] **Minor.** `http_auth.py`'s docstring says the SDK's OAuth machinery is "deliberately not" used, which the same PR reversed — a reader hitting that file first concludes OAuth was rejected. `_codes` is pruned only when an entry is read, so approved-but-never-exchanged codes persist for the process lifetime. `secrets.compare_digest` raises `TypeError` on a non-ASCII password rather than cleanly denying.

### Post-apply staleness (found 2026-09-16, fixed same day, MS6d)

`apply_wiki_proposal` is the first tool that writes into `LLM_WIKI_PATH`, but nothing downstream that assumes the corpus is static was getting invalidated when it ran: `search_wiki`'s filesystem-scan cache, and (at the time) [MS7b](plan-history.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13)'s offline-built wiki-derived entity/section graph. Confirmed concretely, not just theoretically — the real apply of `prop_20260916_125736_a33f295a` created `WIKI/projects/Context-Memory-Fabric/Context-Layers-as-the-Next-Frontier.md` (commit `6dfce163`) and it did not surface via `search_wiki` until this fix.

- [x] `search_wiki`'s side fixed: `apply_wiki_proposal` now calls `invalidate_corpus_cache()` (`server/wiki.py`) on every real (non-dry-run) apply — lazy invalidation, drops the cached engine/assets rather than forcing an immediate rescan, since applies are rare and a rescan can be non-trivial cost. Tested (`tests/test_ms6d_proposal_review.py::TestPostApplyStaleness`, 3 cases: pure invalidation, real-apply wiring, dry-run does *not* invalidate). **Live server restarted 2026-09-16** to pick this up — confirmed live via subsequent `search_wiki` calls from Code mode and Cowork.
- [x] The wiki-derived graph's own staleness question is moot now that MS7b closed onto a static, no-longer-rebuilt `mem-fabric-local` — see below.

### Wiki entity-extraction quality (found 2026-09-17, closing MS7b)

Surfaced while visually inspecting the pruned `mem-fabric-local` graph in FalkorDB Browser — a real gap in `build_wiki_entities.py`'s heading+lede decomposition, not fixable by editing the graph directly:

- [ ] **Topic-level entities never form when no section heading names the topic.** `WIKI/art-projects/Cityscapes/Cityscape-View the shadows.md` — all 13 section headings are camera-technique-specific (`TS-E Mechanical Setup`, `Shift-Stitch Configuration`, `Post-Processing Pipeline (Photoshop / ACR)`, ...); none contains the word "Cityscape," so the note never links to the `Cityscapes`/`cityscape` entities that exist from other notes in the same folder. Root cause: Phase 2's original finding that note titles match only 2% of entities (documented in [MS7b](plan-history.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13)) meant titles were deliberately excluded as a decomposition source — correct call in aggregate, but it leaves notes like this one topic-orphaned. Compounding: `cityscape` (domain) and `Cityscapes` (project) are themselves near-duplicates Phase 4's merge pass never caught, since `_norm()` doesn't handle singular/plural.
- [ ] **Per-section decomposition can attribute a concept to the wrong section, fragmenting it.** Same note: `Tilt` and `Shift` were both extracted from the section titled **"Zero/Static Configuration"** — specifically the section about using *neither* — while the actual `Tilt Configuration` section produced `Scheimpflug` instead, and a third section produced `Tilt-Shift` as a separate tool entity. Three sections, three fragments of one lens concept, none cross-linked.
- **Options, not yet chosen between:** (a) re-run `build_wiki_entities.py` with the note title/folder path added to each section's decomposition context; (b) extend Phase 4's merge pass beyond literal near-duplicates to catch singular/plural and compound-vs-parts cases; (c) accept it as a known cost of this approach and fix only manually, case by case. Whichever is chosen, `mem-fabric-local`'s current ~1,219 entities mentioned by a note but by no episode/fact/project were deliberately left unpruned specifically so this evidence isn't destroyed before a fix is decided.

### Project nodes vs. entity property (open since before MS7b)

Todd's question, not yet settled: do `:Project`/`IN_PROJECT` nodes and edges (31 projects, 1,083 edges in `mem-fabric-local`, built by `tag_projects.py`) earn their place as first-class graph structure, or would `entity.project = ["proj1", "proj2"]` as a plain property serve the same purpose more simply? Paused rather than decided — visualizing the current graph in FalkorDB Browser showed the project layer isn't wrong, just not obviously pulling its weight next to the noise from the wiki-only entity layer above. Revisit once the extraction-quality items above are resolved, since they're currently confounding how legible the project layer looks.

### Found during MS6c Phase 1 (Cowork functional pass, 2026-09-16)

- [ ] **`edit_memory` doesn't re-run fact extraction, so corrected episodes leave stale facts behind.** Confirmed by direct Cypher query against `mem-fabric-local-wiki`: both MS6c verification episodes (`ms6c_verification_test_2026_09_16`, `gemini_verification_test_2026_09_16`) still carry their pre-correction `RELATES_TO` fact edges (`"Initial version... test value set to ALPHA"`, `"...test value is ALPHA-GEMINI"`) with no post-correction facts added alongside them. `edit_memory` updates the episode's own content node in place — that part is correct and immediate, `recall_mem`/`get_context` both surface the corrected body text — but the entity-relationship facts Graphiti derived at original ingestion are untouched. Since `recall_mem`/`get_context` render *both* the episode body and separately-listed facts, this is what Todd's Cowork pass surfaced as apparent "duplicates": two or more distinct, differently-worded facts from the same original extraction pass, one of them now describing a state the episode no longer says. Not literal duplicate indexing — each fact edge has a distinct uuid and wording — but confusing and worth fixing: `edit_memory` should either re-run extraction on `new_content` or explicitly invalidate/mark superseded the old fact edges the way `correct_memory`'s original design intended.
- [ ] **`search_wiki` hit a transient 502 (Cloudflare, `origin_bad_gateway`) from Cowork, succeeded on retry.** Same OAuth/tunnel path every client (including Claude Desktop) now goes through post-MS6c, so this is the same class of flakiness behind Gemini's "error 1076"s and the first silent-failure `remember()` attempt — worth keeping an eye on if it recurs, not yet frequent enough to chase down.

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
