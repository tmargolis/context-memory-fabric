# Implementation Plan — Active milestones

The milestones still to do, in execution order. Index and decisions log: [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). Completed milestones: [plan-history.md](plan-history.md).

---

## MS4b — Claude Code (Desktop's Code tab) transcript adapter — current priority (2026-09-18)

**Status:** [MS4a2](plan-history.md#ms4a2--cowork-live-session-episodewiki-capture-2026-09-18) closed out and shipped (live exit-gate dry run passed 2026-09-18). **Built and evidence-layer-verified live 2026-09-18** (unattended AFK session) — parser/reader/worker/hooks/cli all real and tested, a real scoped backfill ran against production, PR merged 2026-09-19 ([PR #12](https://github.com/tmargolis/context-memory-fabric/pull/12)). **Reasoning-episode consolidation over that backfill ran live 2026-09-18** (see "Real consolidation run" below) — 379 episodes now `queued_for_review`, tagged `project='context-memory-fabric'` 2026-09-19. **Not being reviewed as-is** — per Todd's revised plan (2026-09-19, see "Not done" below and "Wiki→doc rename and doc-proposal extraction"), they'll be re-derived from the source journal via the new `ExtractPolicyV1` once it exists, for a real episode-vs-doc split, rather than reviewed in their current episode-only shape. Two items still intentionally left for Todd: the live hook-applicability test, and actually installing the launchd poller / hooks against his real settings.json (see Tasks below for why).

**Goal:** Highest-fidelity capture available in the stack — full local transcripts and lifecycle hooks.

**Scope correction (2026-09-18):** Originally scoped as "the standalone Claude Code CLI" only. A direct filesystem check found Desktop's **Code tab** (Todd's actual daily driver, standalone CLI used occasionally) writes the *same* JSONL transcript format to the *same* `~/.claude/projects/<project-slug>/<session-uuid>.jsonl` path — real multi-MB files confirmed. The parser reads by file path/format, not by which binary wrote it, so both are covered by one adapter. Cowork itself still has no transcript; that gap is MS4a2's territory, not this one's.

### Available surfaces (measured against 8 real transcript files, 0.3MB–8.0MB)

Every line is JSON with a `type` field: `user` (string content = real turn, or `tool_result` list = tool output fed back), `assistant` (`text`/`thinking`/`tool_use` blocks — `thinking` often holds a coding session's real substance), plus several Desktop-only bridging types (`bridge-session`, `ai-title`, `atis-latch`, `frame-link`, `pr-link`, etc.) absent from the plain CLI's schema — parser must skip-and-log unrecognized types, never raise. Measured "kept" density: **~50–75 real turns per MB** (~80–85% of raw lines are metadata/tool-output noise) — this answers the exit gate's own "which raw event types reach the journal" question concretely. `~/.claude/settings.json` `hooks` block exists (`Notification` already in use) but **whether Desktop's Code tab actually fires `SessionStart`/`Stop` hooks is unverified** — nothing in this repo demonstrates it; needs a live test before depending on it.

### Dedup against already-imported exports (Todd, 2026-09-18)

Real risk, confirmed against the live journal: `claude`-harness events (the already-imported Claude export) run through `2026-09-04T00:36:13Z`; `gemini`/`chatgpt` exports through `2026-09-04`/`2026-08-31`. A full-history backfill would re-walk the same conversations under harness `claude_code` instead of `claude` — **not** caught by `compute_event_id`'s existing dedup (harness is part of the hash). Mitigation: `backfill --all-projects` defaults to a date cutoff (`CMF_CLAUDE_CODE_BACKFILL_SINCE`, read from the import registry) rather than attempting content-hash matching across differently-shaped importers.

### Review posture

Same as MS4a2 above — start manual via `tier1_review_queue()`, same shared `reasoning_auto_accept_threshold`, flip to automatic once calibrated.

### Tasks

- [x] `server/adapters/claude_code/parser.py` — line → canonical `SourceEvent`, keep/skip rule per the measured types above, secret redaction via `server/capture/filters.py` before hashing, harness hardcoded to `claude_code`.
- [x] `server/adapters/claude_code/transcript_reader.py` — byte-offset incremental tailing via a new `claude_code_tail_state` table in `journal.db`, plus `CMF_CLAUDE_CODE_PROJECT_ALLOW`/`_DENY`. Handles partial trailing lines (file being written mid-poll) and a truncated/replaced file (offset resets to 0 rather than seeking past EOF).
- [x] `server/adapters/claude_code/worker.py::process_pending()` — tails changed files, journals new events, calls `run_reasoning_consolidation(..., reasoning_auto_accept_threshold=<shared config>)` per touched conversation, under a forced `CMF_LLM_PROVIDER=local` context manager that restores whatever was set before on exit.
- [x] `server/adapters/claude_code/hooks.py` — settings.json merge-installer (deep-merge only under `hooks.<EventName>`, backs up the file first with a timestamped sibling, refuses to touch `Notification`/`statusLine`/`enabledPlugins`); hook body launches a detached (`nohup ... & disown`) worker and returns.
- [x] `server/adapters/claude_code/cli.py` — `status`, `tail --once`, `backfill --all-projects [--since ISO8601]`. **Known gap:** `--since`/`CMF_CLAUDE_CODE_BACKFILL_SINCE` is wired through to `process_pending(since=...)` and does skip pre-cutoff events from being journaled (verified live, see below) — but there's no persisted "registry" of per-harness cutoffs the CLI reads automatically; the caller must pass `--since` explicitly each time. A real import-registry-backed default (as the original task line described) wasn't built — flagging rather than pretending it's automatic.
- [x] Tests against real local transcript files (`tests/test_ms4b_claude_code_parser.py`, `tests/test_ms4b_transcript_reader.py`, `tests/test_ms4b_worker.py`, `tests/test_ms4b_hooks.py` — 27 new tests, all passing; hooks tests always target a `tmp_path` settings.json, never the real `~/.claude/settings.json`). The parser test module runs against every `*.jsonl` file actually present under `~/.claude/projects/-Users-todd-Dev-context-memory-fabric/` on this machine (24 files at time of writing, not just the original 8 — capture kept running) rather than a fixed set, since new sessions keep landing there.
  - **Real bug found and fixed while testing:** the parser's emptiness check dropped a pure-`tool_result` user turn entirely (joined `text` was empty, so the whole event was discarded) — directly contradicting this doc's own exit-gate answer that `tool_result` should be kept as a bounded summary. Fixed in `parser.py`'s `parse_line`: now checks both `text` and `blocks` before deciding a turn is empty. Regression-covered by `test_user_tool_result_kept_as_bounded_summary`.
  - **Real bug found and fixed while testing:** `worker.py` imported `_open_tail_state_db`/`iter_transcript_files`/`tail_file` from `transcript_reader.py`, none of which exist there (`transcript_reader.py`'s real API is `TailStateStore`/`discover_transcript_files`/`read_new_lines`) — an API mismatch between two different work sessions on this build, `import server.adapters.claude_code.worker` raised `ImportError` before any fix. Rewrote `process_pending()`'s tailing loop against the real `transcript_reader` API; `worker.py` now imports and runs cleanly, covered by `tests/test_ms4b_worker.py`.
- [x] **Measured against real data, not estimated:** ran the parser over every `*.jsonl` file under `~/.claude/projects/-Users-todd-Dev-context-memory-fabric/` (24 files, 94.0MB total) — 33,353 lines seen, 13,143 events emitted (**~140 kept events/MB**, revising the original "~50-75 turns/MB, measured against 8 files" estimate now that more real data exists), 0 unparseable lines. Skip breakdown by type: `attachment` 6,142, `bridge-session` 1,764, `atis-latch` 1,740, `custom-title` 1,717, `last-prompt` 1,767, `queue-operation` 1,099, `system` 459, `pr-link` 491, `mode` 441, `agent-name` 270, `file-history-snapshot` 184, `artifact-autoreact-ledger` 109, `frame-link` 397, `file-history-delta` 116, `ai-title` 141, `artifact-comment-monitor` 36 — matches the module docstring's `KNOWN_SKIPPED_TYPES` list exactly, no genuinely-unrecognized type encountered.
- [ ] Live hook-applicability test: trivial `SessionStart`/`Stop` sentinel hook, open a real Code-tab session, confirm whether it fires — record the answer here. **Not attempted this session.** `hooks.install_sentinel_hooks()` is built and unit-tested against a tmp settings.json, but actually installing it against the real `~/.claude/settings.json` and then needing a real interactive Code-tab session to open afterward isn't something an unattended AFK session can complete or verify — deliberately left for Todd to run by hand (`python -c "from server.adapters.claude_code.hooks import install_sentinel_hooks; install_sentinel_hooks()"`, then open a fresh Code-tab session, then `check_sentinel_fired()`). The launchd-poller path (below) doesn't depend on the answer either way.
- [ ] Trigger: a launchd agent polling every ~10-15 min (precedent: `com.cmf.spark-tunnel.plist`). **Not built this session** — `cli.py tail` exists and is the command such an agent would invoke, but the actual `launchd` plist + `load`/install step wasn't created, since installing a new always-on background service unattended felt like it warranted Todd's sign-off rather than being done silently while he's AFK. `hooks.install_hooks()` (the Stop/SessionStart accelerant) exists and is tested but was likewise **not run against the real settings.json** for the same reason — both are one command away once Todd reviews this.

### LLM provider: local only, not Gemini

`ReasoningEpisodePolicyV1.evaluate_window()` draws from the same shared Gemini rate-limiter ledger live interactive `remember()` calls depend on. With the chosen `TimeGapWindower`, one 8MB/595-turn session is already ~30 extraction calls — meets-or-exceeds the tightest candidate model tier (`rpd=20`) in a single unattended session, with no human gate in front of it (unlike promotion, naturally throttled by review pace). Local Spark has zero quota cost; wall-clock doesn't matter since this runs entirely in the background. **Decision: `CMF_LLM_PROVIDER=local` for this worker, always** — real per-window wall-clock time is unmeasured (only promotion's ~25-38s/episode exists, which includes ~20 embedding calls this step never makes) and is the first thing to measure once built, to set the real poller cadence.

### Exit gate

**How much of a coding session is worth keeping?** Answered concretely by the measurement above, not left as a hand-wave: full text for `user`/`assistant text`/`thinking` blocks, bounded 1000-char summaries only for `tool_result`/`tool_use`.

**Effort:** 3–4 sessions — actual: 1 unattended session (2026-09-18, AFK build) got parser/reader/worker/hooks/cli built, tested, and a real evidence-only backfill run, followed same day by an interactive reasoning-episode consolidation run (379 episodes staged for review); the live hook test and the launchd/hook install itself are the two items still deliberately left for Todd (see their task notes above).
**Risk:** Low-medium. Volume and the export-overlap dedup risk (mitigated below) are the real considerations, not hooks (which have a local, zero-cost, hook-independent fallback).

### Real backfill run (2026-09-18, this session)

Scoped deliberately narrow given the time available unattended, per Todd's own "don't waste time/effort/tokens on what's already been ingested" instruction: `CMF_CLAUDE_CODE_PROJECT_ALLOW=context-memory-fabric`, `--since 2026-09-04T00:36:13Z` (the `claude`-harness importer's own cutoff, per the dedup section above), `--no-consolidation` (evidence only — no reasoning-episode LLM extraction was run, see below for why).

`python -m server.adapters.claude_code.cli backfill --all-projects --since 2026-09-04T00:36:13Z --no-consolidation` against 26 real transcript files (every project-slug directory whose name contains "context-memory-fabric" — this repo plus a couple of differently-named worktree/clone variants) found on this machine:

```
files_scanned: 26, files_with_new_bytes: 25
events_journaled: 12,612, events_deduped: 0, events_skipped_before_cutoff: 700
conversations_touched: 25, errors: []
```

Verified against `python -m server.journal.cli stats`: the live production `imports/journal/journal.db`'s `by_harness` breakdown now shows `claude_code: 12,620` real events (a small delta above the 12,612 reported by this run, from a handful of events journaled by the earlier ad-hoc parser/worker testing in this same session before the real scoped backfill ran) — confirms the backfill landed for real, not just a dry-run count.

**Deliberately not run in the 2026-09-18 AFK session: reasoning-episode consolidation** (`run_consolidation=True`, which extracts `DerivedMemory` candidates via local qwen3.5-122b per touched conversation-window). Per this file's own LLM-provider math above, one 8MB/595-turn session is already ~30 extraction calls; 25 real conversations touched here — the actual per-window wall-clock time was still unmeasured going into that session and running it unattended risked consuming the whole remaining time budget on an unverified-duration step with no easy way to check progress or abort cleanly if something hung. The safer choice at the time: land the evidence layer for real (done, verified above), leave consolidation as a deliberate next step run interactively with visible progress — see below, this was then done the same day.

**Scope not attempted:** the other ~859 transcript files under `~/.claude/projects/` for different projects (`python -m server.adapters.claude_code.cli status` with no project filter reports 885 files total across this machine) — MS4b's own goal is capture for *this* project's context; backfilling every other project's Claude Code history was out of scope for an unattended run and not something this milestone's design asked for.

### Real consolidation run (2026-09-18, same day, interactive)

Ran `ReasoningEpisodePolicyV1` consolidation directly via `run_reasoning_consolidation()` (not through `cli.py backfill`, since the backfill's tail offsets had already fully advanced — a second `backfill` invocation would find no new bytes and touch zero conversations) against every distinct `conversation_id` on file for harness `claude_code` in the live `imports/journal/journal.db`, forcing `CMF_LLM_PROVIDER=local` for the run (same pattern as `worker.py`'s `_forced_local_llm_provider`). Progress was printed per-conversation as it ran, addressing the two concerns that had left this step for Todd: unmeasured/unbounded duration and no visibility mid-run.

**34 conversations** processed (not 25 — the query picked up a handful more than the scoped backfill touched, from the earlier ad-hoc worker/hook testing in the same session), **0 errors, 0 `quota_exhausted` stops**:

```
windows_seen: 660, windows_triaged_out: 440, windows_sent_to_model: 219
episodes_created: 379, all queued_for_review (0 auto_accepted)
by_reasoning_kind: skews decision / investigation / plan, smaller finding / hypothesis / rejected_alternative / experiment
total wall-clock: 3194s (~53 min)
```

**This answers the doc's own "first thing to measure once built" line:** ~14.6s/model-call average (219 calls / 3194s); per-conversation wall-clock ranged 0s (fully triaged out, no model calls) to ~423s (60 windows / 28 model calls / 52 episodes, the largest single conversation). That's the real number to set the launchd poller cadence against, once the poller itself is installed (still open, see Tasks above).

**Not done:** the 379 episodes are staged in the consolidation store as `queued_for_review` — none reviewed or promoted into `mem-fabric-local` yet. `project='context-memory-fabric'` was applied directly 2026-09-19 (see the corpus backlog entry above).

**Revised plan (Todd, 2026-09-19), superseding "review these 379 as-is":** rather than reviewing the 379 in their current episode-only shape, re-derive them once `ExtractPolicyV1` (below) exists — re-running `run_reasoning_consolidation()` with the new policy over the same 34 `claude_code` conversation_ids, from the **source journal events**, not from the existing `derived_memories` rows (`ExtractPolicyV1.evaluate_window()` consumes windows of raw `SourceEvent`s, the same input the original run took — there's no path that takes already-derived episodes as input). This gives every window a proper episode-vs-doc split instead of forcing everything through as an episode. **Consequence to handle before that re-run's output is reviewed:** the existing 379 `reasoning-episode`-policy rows will substantively overlap the new `extract`-policy output once it exists — nothing here automatically marks the old rows superseded (unlike same-policy version bumps, which `retire-stale-versions` already handles; there's no equivalent for a *different* `policy_name` covering the same windows). The old 379 need an explicit reject/supersede pass tied to the re-run, or they'll sit in the queue as reviewable duplicates of the new candidates. Left as a task for the Phase 2 section below rather than decided here.

### Wiki→doc rename and doc-proposal extraction (found 2026-09-19, decided 2026-09-19)

**The gap:** `ReasoningEpisodePolicyV1.evaluate_window()` only ever asks the model for episodic candidates (`reasoning_kind` ∈ decision/plan/investigation/hypothesis/finding/rejected_alternative/experiment) — there is no branch anywhere in `server/policies/reasoning_episode_v1.py` or `run_reasoning_consolidation()` that considers whether a window's content is *durable, reference-shaped knowledge* better suited to a durable-knowledge proposal than an episode. That dual-destination judgment already exists, but only in one place: `capture_session` (MS4a2), where the calling LLM interactively routes each item to `destination='episode'` or `destination='wiki_proposal'`. The offline/batch windowed pipeline — the one MS3.5, MS4a, and MS4b's consolidation run all go through — has never had this option, so some of the 379 MS4b episodes are plausibly reference material a human reviewer would rather route to durable knowledge than accept as an episode, with no one-step way to do that today.

**Decided (Todd, 2026-09-19), superseding the three options previously listed here:**
- **A new policy, not a modification of the old one.** `ReasoningEpisodePolicyV1`/`reasoning_episode_v1.py` stays exactly as-is — the 1,689 existing `reasoning-episode`-policy rows keep their identity untouched. A new `ExtractPolicyV1(ReasoningEpisodePolicyV1)` in a new `server/policies/extract_v1.py` subclasses it, adding durable-knowledge-proposal candidates alongside episodes in the same `evaluate_window()` call (one model call, two possible output shapes — this was option (a) from the original three, now scoped as an addition rather than an in-place change). New policy identity: `name="extract"`, `version="1.0"` — a distinct `policy_name`, not a version bump of `reasoning-episode`, so old and new rows never mix.
- **Terminology: "wiki" → "doc" for the generic proposal/review layer, not the retrieval layer.** `WikiProposal`→`DocProposal`, `wiki_proposal` destination→`doc_proposal`, `wiki-proposals/`→`doc-proposals/`, and the MCP tools (`propose_wiki_update`, `list_wiki_proposals`, `get_wiki_proposal`, `review_wiki_proposal`, `apply_wiki_proposal`, `bulk_reject_wiki_proposals`) all hard-renamed to `*_doc_*` — no deprecated aliases, since the connected clients (Claude Desktop, Gemini Spark, ChatGPT) just re-discover tools on their next refresh. `search_wiki`, `LLM_WIKI_PATH`, `server/wiki.py`, `FileKnowledgeProvider` are explicitly **out of scope** — those name the actual LLM_Wiki corpus/provider, which really is wiki-specific today; generalizing that is MS5's job once a second knowledge provider exists to prove the abstraction against, not this rename's.
- **Light reorg alongside the rename:** `server/wiki.py` + `server/corpus.py` (the LLM_Wiki-specific corpus scanner, tightly coupled to `server/providers/knowledge_files.py`'s `FileKnowledgeProvider`) move under `server/providers/wiki/`, grouping the actual provider implementation in one place. `server/proposals.py` (the generic proposal engine — most of its functions are already named `list_proposals`/`get_proposal`/etc., only the `WikiProposal` class and `wiki-proposals/` default say "wiki") stays at `server/` root — it's not itself a provider, even though its `apply` step writes into the wiki corpus today; that seam is MS5's to generalize.
- **Sequencing:** landed as two separate PRs — rename+reorg first (pure refactor, no behavior change), then the new `ExtractPolicyV1` on top of the renamed names, so the new policy is never built against soon-to-be-renamed identifiers.
- **Follow-on, once `ExtractPolicyV1` lands (Todd, 2026-09-19):** re-run consolidation over the same 34 MS4b `claude_code` conversation_ids under the new policy — from source journal events, giving every window a real episode-vs-doc split, rather than reviewing the existing 379 `reasoning-episode` rows as-is. Before that output is reviewed, the old 379 need to be explicitly rejected/marked superseded (no automatic mechanism covers a cross-policy-name overlap the way `retire-stale-versions` covers same-policy version bumps) so they don't sit in the queue as reviewable duplicates of the new candidates.

**Also resolved:** the project-taxonomy question above — see the corpus backlog entry (`project='context-memory-fabric'` applied directly, keyword taxonomy not used for this batch; extending the same direct-tag approach to other repos is its own backlog item there).

### Phase 1 tasks — wiki→doc rename + light reorg (branch, PR, no behavior change)

- [x] `server/proposals.py`: `WikiProposal` → `DocProposal`; `create_wiki_proposal` → `create_doc_proposal`; `get_proposals_dir()`'s default subdirectory `wiki-proposals/` → `doc-proposals/`; internal docstrings/comments updated to match (module docstring now states explicitly why the class is named generically while `wiki_root`/"LLM_Wiki" stay as the real, current implementation target).
- [x] `server/mcp.py`: hard-renamed the six MCP tools — `propose_wiki_update`→`propose_doc_update`, `list_wiki_proposals`→`list_doc_proposals`, `get_wiki_proposal`→`get_doc_proposal`, `review_wiki_proposal`→`review_doc_proposal`, `apply_wiki_proposal`→`apply_doc_proposal`, `bulk_reject_wiki_proposals`→`bulk_reject_doc_proposals` — `title=` annotations, docstrings, `SERVER_INSTRUCTIONS`, and the `capture_session` cross-reference all updated; `search_wiki`/`LLM_WIKI_PATH`/`max_wiki_results` left untouched as planned (Layer 2).
- [x] `capture_session`'s `destination='wiki_proposal'` → `destination='doc_proposal'`, `wiki_rationale` → `doc_rationale` (tool description in `server/mcp.py` + `server/capture/session_capture.py`, including the internal `_capture_wiki_item` → `_capture_doc_item` helper).
- [ ] Reorg: `server/wiki.py` + `server/corpus.py` → `server/providers/wiki/`, update every importer. **Not yet done** — doing next.
- [x] Renamed the on-disk `wiki-proposals/` directory to `doc-proposals/` (plain `mv` — it's gitignored, not tracked) and updated `.gitignore` (`wiki-proposals/` → `doc-proposals/`) and the duplicated path-resolution logic in `server/core/config.py` (`_resolve_state_dir()`, which mirrors `get_proposals_dir()`'s computation — a real second place that would have silently pointed at the old, no-longer-written directory).
- [x] Updated tests: `test_ms6d_proposal_review.py`, `test_step6_mcp_tools.py`, `test_step6b_proposals.py`, `test_capture_session.py`, `test_step7_import_memories.py`, `test_ms1_provider_interfaces.py`, `test_mcp_contract_fixtures.py`, plus its `tests/fixtures/mcp_contracts/tool_schemas.json` snapshot (regenerated from the live `app.list_tools()` output, not hand-edited — diff scoped to exactly the 6 renamed tool names, verified). `docs/CLIENTS.md` still open. `plan-history.md`/closed ADRs deliberately left untouched — accurate historical record of what those tools were called at the time.
- [x] Full test suite green: **490 passed, 6 skipped, 8 deselected**, 0 failures.
- [ ] Live MCP server tool registration sanity-check (tool count, names) — pending, after the reorg below.
- [ ] PR opened, Todd reviews/merges.

### Phase 2 tasks — `ExtractPolicyV1` (branch off updated main, after Phase 1 merges)

- [ ] New `server/policies/extract_v1.py`, `class ExtractPolicyV1(ReasoningEpisodePolicyV1)` — `name="extract"`, `version="1.0"`, `reasoning_episode_v1.py` untouched.
- [ ] Extend `evaluate_window()` to also emit `DocProposal`-shaped candidates alongside episodes, one model call.
- [ ] New candidates route to the renamed `doc-proposals/` review surface from Phase 1.
- [ ] Tests; PR opened, Todd reviews/merges.

### Phase 3 tasks — re-derive the 34 MS4b conversations (after Phase 2 merges)

- [ ] Explicitly reject/mark-superseded the 379 existing `reasoning-episode`-policy rows tied to these 34 conversations, so they don't sit as reviewable duplicates once the re-run's output exists.
- [ ] Re-run `run_reasoning_consolidation()` with `ExtractPolicyV1` over the same 34 `claude_code` conversation_ids, from source journal events.
- [ ] Review the fresh episode + doc-proposal output via `list_episode_proposals` / `list_doc_proposals`.

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

- [ ] **The corpus is growing, not static.** The reasoning-episode pool alone grew from 1,243 rows (the 2026-09-05/06 reprocess) to 1,310 by 2026-09-11 — capture (MCP-boundary + imports) kept running after the 2026-09-08 review pass. A recurring/periodic tier-1 review pass is probably the more accurate framing going forward, rather than treating any fixed count as a target to eventually finish. `uv run python -m server.review.cli queue --tier 1` shows what's currently outstanding.
- [ ] **MS4b's real consolidation run (2026-09-18) added 379 more `queued_for_review` episodes** on top of that pile — see [MS4b → Real consolidation run](plan-active.md#ms4b--claude-code-desktops-code-tab-transcript-adapter--current-priority-2026-09-18). Tagged `project='context-memory-fabric'` directly 2026-09-19 (Todd: since the backfill itself was scoped to only that repo via `CMF_CLAUDE_CODE_PROJECT_ALLOW`, every row is that project by construction — the keyword-taxonomy `backfill --apply` would have misfiled 79% of them into `misc`, see the dry-run numbers in the linked section). Review of these resumes independently of the wiki/doc-proposal integration decision below, since they were extracted under the old episode-only policy and stay episode-only by design.
- [ ] **Extend MS4b's Claude Code backfill beyond the CMF-only scope (found 2026-09-19, Todd).** The 2026-09-18 backfill deliberately only walked `context-memory-fabric`-named project-slug directories (`CMF_CLAUDE_CODE_PROJECT_ALLOW=context-memory-fabric`); `cli.py status` with no filter reports 885 transcript files across ~this whole machine's other projects (`capacities-mcp`, `career-navigator-target`, `AstroAlert`, `DelayedVideoTablet`, and others outside `~/Dev`), none of them backfilled yet. Same dedup risk applies as the CMF run (each harness's own export-importer cutoff, see MS4b's "Dedup against already-imported exports" section) — `--since` needs setting per project, not once globally. Once backfilled, default `project` to the repo/slug name directly for `claude_code`-harness rows (same fix just applied to the CMF batch above) rather than running them through the personal keyword taxonomy, which wasn't built with coding-session `thread_key`s in mind.
- **Sourced from one new adapter now (MS4b, 2026-09-18); MS4c (OpenClaw), MS4d (Codex/Gemini CLI) remain unbuilt.**
- **Also found, unrelated to the review pass itself:** the `cmf_test` FalkorDB graph's vector index is still 1024-dim (Gemini-era) while the configured embedder produces 768-dim (local/nomic) — every `live`-marked test that calls `remember()` against `cmf_test` currently fails with a vector-dimension mismatch, independent of any of this session's code changes (confirmed by re-running before/after). `cmf_test` was never migrated alongside `mem-fabric-local` in the Spark migration; needs the same treatment (`docs/spark-phase7-ab-log.md`'s migration steps, applied to the test graph).

### Episode-proposals review MCP tools (found 2026-09-18, parity gap with MS6d)

MS6d built a full `list`/`get`/`review`/`apply`/`bulk_reject` MCP lifecycle for wiki proposals. The episode side never got the equivalent — `approve_episode`/`reject_episode`/`tier1_review_queue()` were CLI-only. Found the same day a live `capture_session` test needed rejecting and no MCP client could do it.

- [ ] **Filed, not fixed today:** `ConsolidationStore.query_reasoning_episodes()` hardcodes `policy_name = 'reasoning-episode'`, so `tier1_review_queue()` (and the CLI built on it) never sees `cowork_live_v1` rows at all — a `capture_session`-staged episode is invisible to `server/review/cli.py queue --tier 1` even though it shows up in the new MCP tools. Not a one-line fix: `reasoning-episode` and `cowork_live_v1` version themselves independently (0.3 vs 0.1), so the function's single shared `policy_version` parameter needs restructuring to a per-policy-name version map, which also touches `tier1_review_queue()`'s signature and several existing tests (`test_ms6_review.py`, `test_ms3_6_promotion.py`, `test_ms7b_enriched_content.py`).

### Auth hardening (deferred out of MS6c, 2026-09-16)

Raised in review on [PR #6](https://github.com/tmargolis/context-memory-fabric/pull/6) and consciously merged without fixing (Todd, 2026-09-16) — the OAuth layer works and these are hardening, not blockers, on a single-user personal server. The design itself reviewed clean: PKCE correct, codes single-use, refresh tokens rotate on exchange, expiry enforced on both token types, consent password compared with `secrets.compare_digest`.

- [ ] **The auth layer has no test coverage at all.** No test references `OAuthStore`, `CMFOAuthProvider`, or `BearerTokenAuthMiddleware`; the suite's 383 passing tests do not execute one line of `server/core/oauth_provider.py`, `oauth_store.py`, or `http_auth.py`. For the code standing between the open internet and `remember`/`edit_memory`/`import_chatgpt_exports`, that is the gap worth closing first — the flow has enough state transitions (consent → code → token → refresh → rotate) that a regression would be silent and would present as a client-side bug. Highest value: a full round-trip test through the provider, plus the refusal cases (wrong password, expired code, reused code, wrong client_id on exchange).
- [ ] **`exchange_refresh_token` drops `resource`.** `exchange_authorization_code` persists `authorization_code.resource` (`server/core/oauth_provider.py:161`); the refresh path hardcodes `None` (`:202`), so a token's audience binding silently disappears the first time it refreshes. Not exploitable today — the SDK's `ProviderTokenVerifier` only calls `load_access_token` and never checks `resource` — but it becomes a real bug the moment RFC 8707 audience validation is enabled, and it would surface ~30 days after a client first connects. `RefreshToken` needs to carry the resource forward for this to be fixable at all.
- [ ] **Tokens are stored in plaintext.** `oauth_access_tokens.token` / `oauth_refresh_tokens.token` are raw values used as PRIMARY KEY, in the same `journal.db` the journal writes. A stray copy or backup is working credentials for 30 and 180 days. Storing SHA-256 and looking up by hash is one line per save/get pair.
- [ ] **No rate limiting on the consent password**, which is the entire security boundary by design, on an endpoint reachable by anyone who finds the URL. `openssl rand -hex 16` as documented makes brute force infeasible — so the real action is making CLIENTS.md say that recommendation is load-bearing rather than advisory.
- [ ] **Minor.** `http_auth.py`'s docstring says the SDK's OAuth machinery is "deliberately not" used, which the same PR reversed — a reader hitting that file first concludes OAuth was rejected. `_codes` is pruned only when an entry is read, so approved-but-never-exchanged codes persist for the process lifetime. `secrets.compare_digest` raises `TypeError` on a non-ASCII password rather than cleanly denying.

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
