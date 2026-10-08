# Implementation Plan — Active milestones

The milestones still to do, in execution order. Index and decisions log: [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). Completed milestones: [plan-history.md](plan-history.md).

**MS9 (graph quality for retrieval) closed 2026-10-07**; its record is in [plan-history.md](plan-history.md#ms9--graph-quality-for-retrieval-2026-09-30--10-07). Next in order is [MS10a](#ms10a--second-user-readiness), then [MS10](#ms10--distribution-and-ecosystem). What MS9 left open is in the [Backlog](#merge-related-episodes-career-tailoring-threads-deferred-out-of-ms9-2026-10-07).

**MS4b and MS4b-antigravity closed 2026-09-28**, and their record is in [plan-history.md](plan-history.md#ms4b--claude-code-transcript-adapter-build-backfill-re-derivation-review-2026-09-18--09-22). **MS4d (Codex) closed 2026-09-30 (Gemini CLI dropped per user steer)**, and its record is in [plan-history.md](plan-history.md#ms4d--codex-transcript-adapter-and-ongoing-capture-2026-09-30). **MS4c (OpenClaw) remains on hold (User, 2026-09-28).** Its plan is kept intact below but not scheduled. What the active coding adapters keep capturing goes to the recurring review pass ([Backlog](#backlog)).

**MS4e (entity extraction quality) closed 2026-09-28**; its record is in [plan-history.md](plan-history.md#ms4e--entity-extraction-quality-forward-only-2026-09-28). `typed-recall` + debris filter is the production extraction profile.

**MS5 (knowledge-provider generalization) closed 2026-09-28**; its record is in [plan-history.md](plan-history.md#ms5--knowledge-provider-generalization-2026-09-28). What it deliberately left out is [MS11](#ms11--knowledge-provider-follow-ons-from-ms5).

**Claude surface provenance + Cowork capture (unplanned, found in MS9 Phase 5) closed 2026-10-03**; its record is in [plan-history.md](plan-history.md#claude-surface-provenance-and-cowork-capture-found-2026-10-02-closed-2026-10-03). Capture now separates the Claude CLI (`claude_code`), Desktop Code tab (`claude_desktop_code`) and Cowork (`claude_cowork`). What it left open is in the [Backlog](#claude-surface-provenance-lost-transcripts-and-cowork-capture-found-2026-10-02-ms9-phase-5).

**extract@1.6 backfill review: Phases 0–4 closed 2026-10-04** (record in [plan-history.md](plan-history.md#extract16-backfill-review-phases-03-and-the-phase-4-pilot-2026-10-03); 60 approved docs applied, 443 approved episodes promoted across 23 batches, 8 failed on timeout, 1 superseded skipped). Post-Phase-4 items queued.

**MS8 (replay and evaluation) closed 2026-09-29**; its record is in [plan-history.md](plan-history.md#ms8--replay-and-evaluation-2026-09-28--09-29). One-fact-per-episode candidate validated (0 regressions across 60 cases × 4 cutoffs) and adopted as production default.

---

## MS4c — OpenClaw adapter and `cmf-http`

**⏸ On hold (2026-09-28).**

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

## MS10a — Second-user readiness

**Status:** 🟡 in progress (started 2026-10-08). Order: 1 → 5 → 6b → 6 → 2 → 3 → 4 → 7.

**Goal:** A second person can run CMF on their own machine with their own data. Their setup: Claude Code (CLI and the desktop app's Code tab) as the main harness, Codex CLI alongside it, and a custom agent team running in both. They have no Spark and no LM Studio, and they prefer Claude or OpenAI models to Gemini or local ones. Operating system unknown. This is MS10's goal, scoped down to one real user. What it finds feeds MS10.

**Decided (User, 2026-10-08):**
- Claude and OpenAI are reached with **API keys** (Anthropic / OpenAI), not through subscription CLIs. If the user has neither key, they fall back to the Gemini free tier, which needs no new code.
- Embeddings: **OpenAI** by default (Anthropic has no embeddings API). A small local embedder, nomic via Ollama, stays the documented fallback (SETUP.md, "Run a Small Local Embedder"). A frontier LLM with a local embedder becomes a first-class pairing via separate LLM and embedder endpoint settings (task 2).
- Personal names in the extraction prompt move to `.env` rather than being swapped for other hardcoded examples (task 6).
- Tasks 2–5 are already open as general gaps: the background-capture override, the poller install, the reviewer default and the project-root pattern all assume this deployment. Fixing them is generic work, not special-casing one user.

### Tasks

- [x] **0. Commit the 2026-10-08 env cleanup** on its own (done in `f37d3f2`): `.env.example` merged with the removed `.env.preview.example`, the `CMF-local` default graph, and doc references updated.
- [x] **1. Background-capture provider as a setting.** *Done 2026-10-08: `server/adapters/capture_provider.capture_llm_provider()`, read by `config.capture_llm_provider_from_env()` with the same strict validation as the other provider switches; tests in `tests/test_capture_provider.py`.* The four workers force `CMF_LLM_PROVIDER=local` during unattended extraction (`claude_code`, `codex`, `antigravity`; `claude_cowork` imports the `claude_code` copy). Replace the four `_forced_local_llm_provider` copies with one helper driven by a new `CMF_CAPTURE_LLM_PROVIDER`. Default `local`, so this deployment's Spark safeguard is unchanged.
- [x] **2. `anthropic` and `openai` providers** *Done 2026-10-08 (code + offline tests; the live round-trips are acceptance tests 1-2, which need real keys). Found: Graphiti 0.29.3's `AnthropicClient` forces a tool call and sends `temperature`, both rejected (400) by `claude-opus-5-5` / `claude-sonnet-5-5`, so `server/providers/anthropic_client.py` subclasses it and uses structured outputs (`output_config.format`) plus server-side refusal fallbacks; `anthropic.transform_schema` rejects type lists, so nullable fields become `anyOf`. OpenAI uses Graphiti's own `OpenAIClient` (default `gpt-5.5`) and reranker; embeddings send `dimensions=EMBEDDING_DIM` (`server/providers/openai_embedder.py`); worker extraction uses the Responses API with a strict schema (`server/providers/openai_extraction.py`). The Gemini limiter is unmetered for both hosted providers, and the worker generate functions read the configured model rather than the limiter's (the limiter is a per-process singleton). Defaults: `claude-sonnet-5-5` (User, 2026-10-08; `CMF_ANTHROPIC_MODEL` / `CMF_ANTHROPIC_EFFORT` to change), `gpt-5.5`, `text-embedding-3-small`. SDK: `anthropic` 1.12.1 via Graphiti's `anthropic` extra.* for `CMF_LLM_PROVIDER` / `CMF_EMBED_PROVIDER` / `CMF_CAPTURE_LLM_PROVIDER`:
  - Config: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, per-provider model settings, and a `memory_enabled` credential check per selected provider.
  - Promotion: Graphiti 0.29.3's `AnthropicClient` / `OpenAIClient`; add the `anthropic` SDK dependency (`openai` is already installed).
  - Worker extraction: Anthropic and OpenAI branches in `reasoning_episode._select_generate_fn` with schema-constrained output. Check the current Claude structured-output API and model ids before writing it.
  - Embeddings: `openai` (text-embedding-3-small, `dimensions=EMBEDDING_DIM`). `anthropic` as an embed provider fails at startup with a clear message.
  - Reranker: passthrough on the `anthropic` path (Graphiti's reranker needs logprobs); `OpenAIRerankerClient` on `openai`.
  - Retries on 429/529. The Gemini rate limiter does not apply.
  - **Frontier LLM + local embedder as a supported pairing.** Today one `CMF_LOCAL_BASE_URL` / `CMF_LOCAL_API_KEY` serves both local roles. That makes Gemini + Ollama embeddings work only by accident: nothing else reads the URL. Split it into **`CMF_LOCAL_LLM_BASE_URL`** and **`CMF_LOCAL_EMBED_BASE_URL`** (each with its own `_API_KEY`), each falling back to `CMF_LOCAL_BASE_URL` when unset so existing `.env` files keep working. Then any frontier LLM (`anthropic` / `openai` / `gemini`) can pair with a laptop embedder (Ollama `nomic-embed-text`, 768). An LM Studio LLM and an Ollama embedder can also run on different hosts or ports. The `memory_enabled` credential check reads whichever URL each selected role uses. Update the SETUP.md "Run a Small Local Embedder" section and the `.env.example` Ollama block, both written 2026-10-08, to drop their "not yet built" caveats.
- [x] **3. Capture Claude subagent transcripts.** *Done 2026-10-08: discovery also reads `<session>/subagents/agent-*.jsonl`; each becomes conversation `<session>:agent-<id>` with `parent_conversation_id`, `is_subagent`, `subagent_type` and `subagent_description` in event metadata (`session_id` stays the parent's, as in the transcript lines), and consolidation runs on it as its own conversation. Backfilled after all (User, 2026-10-08, reversing forward-only): the 21 existing transcripts (2026-09-07 → 09-28) were first marked read, then unmarked so the poller captures them as a live test of subagent capture and of how their `user` turns extract. First extraction attributed the parent agent's instructions to the user, so subagent windows now label those turns `(parent agent)` / `(tool result)` with an attribution note (3 of 46 re-extracted episodes still slipped). Review of the 21 (2026-10-08): 2 of 20 EP approved, 1 of 10 DOC approved after re-filing, 2 more rebuilt additively; most output was process narration and stale snapshots. **Keep harvesting subagents, doc proposals included (User, 2026-10-08):** other users may lean on subagents more.* *Stays in MS10a (User, 2026-10-08): the second user's custom agent team runs inside Claude Code, so subagent transcripts are where much of their work lands, and nothing in MS10 covers capture.* Discovery in `claude_code/transcript_reader.py` only globs `<project>/*.jsonl`, so `<project>/<session>/subagents/agent-*.jsonl` is never read. A subagent becomes its own conversation (`<session>:agent-<agentId>`) with its parent session as `parent_conversation_id`, matching the Codex adapter's `parent_thread_id` handling. The agent type and description come from the sibling `.meta.json`. Codex already captures subagents and forks; no change there. **Decided (User, 2026-10-08): backfill.** First decided forward-only, then reversed the same day: this deployment's 21 existing subagent transcripts (2026-09-07 → 09-28, nearly all CMF build exploration) are captured and reviewed, as the test of task 3.
- [x] **4. Cross-platform poller templates** in `deploy/pollers/`. *Done 2026-10-08: `macos/` (launchd plist templates + `install.sh`), `linux/` (systemd --user service/timer templates + `install.sh`), `windows/install.ps1` (Task Scheduler, S4U so no console window), and a README covering providers, `CMF_PROJECT_ROOTS` and logs. Portability fixes found on the way: `fcntl` was imported unconditionally by the Codex reader, which `spark_lock` imports, so every poller would have failed to start on Windows; a portable `server/adapters/file_lock.py` (`fcntl` / `msvcrt`) now backs both. Codex honours `CODEX_HOME`; Cowork's session root is per-OS (`CMF_COWORK_SESSIONS_ROOT` overrides). Tested: the rendered macOS plist matches this deployment's production plist except its label, and two test agents (`com.cmf.mstest.*`, running read-only `status`) installed, ran under launchd, were refused a duplicate install, and uninstalled cleanly. Linux and Windows are reviewed and dry-run/parse-tested only (no machine here).* *All three platforms stay in MS10a (User, 2026-10-08): the second user's OS is unknown, so build them all now. A brief move of Linux/Windows to MS10 the same day was reverted.*
  - macOS: launchd `.plist` templates for the MCP server and the Claude Code and Codex pollers.
  - Linux: systemd `--user` `.service` + `.timer`.
  - Windows: a PowerShell Task Scheduler registration script.
  - README with placeholders (repo path, venv Python, interval). The Spark tunnel is deployment-specific and stays out.
  - Check Windows portability: `spark_lock`'s file locking, `project_slug.py` on Windows transcript folder names, `~/.claude` and `~/.codex` resolution.
- [x] **5. Env vars.** *Done 2026-10-08: `config.default_reviewer()`; `server/core/project_roots.py` shared by the Claude Code, Codex and Antigravity adapters (unset keeps each adapter's built-in pattern; old vs new slugs identical on all 39 Claude Code folders and every Codex cwd here). `CMF_PROJECT_ALIASES` was already honoured for every harness except doc-proposal folder routing, now fixed; `CMF_PROJECT_FOLDER_MAP` was Cowork-only and now also applies to Claude Code (on the encoded folder name, so a sibling whose name extends a mapped folder also matches) and Codex. Tests in `tests/test_project_roots.py`.* `CMF_REVIEWER`, replacing the hardcoded `"todd"` default in `server/mcp.py` and `server/proposals.py`; it defaults to the OS login name, so this deployment is unchanged. `CMF_PROJECT_ROOTS`: parent folders whose children are projects, replacing `project_slug.py`'s hardcoded `Dev|Documents|Volumes` pattern. Confirm the Claude Code and Codex adapters honour the existing `CMF_PROJECT_FOLDER_MAP` / `CMF_PROJECT_ALIASES`.
- [x] **6. Extraction examples from `.env`, not code.** *Done 2026-10-08, revised with User:* the settings are `CMF_EXTRACTION_WORKSTREAMS` / `_HARDWARE` / `_TOPICS` (no `EXAMPLE`), plus `CMF_EXTRACTION_PERSON` (the Person docstring's name) and `CMF_EXTRACTION_DEBRIS_FILES` (the never-extract file names). The entity-type docstrings are part of the prompt (Graphiti sends `__doc__`), so they are templates too, rendered at import by `render_recall_profile()`. The `mcp.py` tool descriptions were genericized instead of read from `.env` (also the Spark mentions), so the contract fixture stays machine-independent. **Production prompt change (reviewed by User before commit):** the topic examples became User's own topics, taken from the most-mentioned `Topic` entities in `mem-fabric-local`, and one device left the hardware examples; everything else renders byte-identical (checked locally against `HEAD`, not in a tracked test, so the names stay out of git). The profile has no version marker, so this note is the record. Still hardcoded: debris identifiers (`head_idx`, `top_probs`, ...) and the Organization docstring's board/HOA examples; left for the scrub item in the Backlog.*  The `typed-recall` prompt in `server/providers/extraction_profile.py` hardcodes this deployment's names: lines 96, 133, 143, 172, 226 (J-Space, nanospark, Career Navigator, NVIDIA Spark, astrophotography, …). So do the `server/mcp.py` tool descriptions (lines 321, 420). Read them instead from three comma-separated settings in the gitignored `.env`, next to `CMF_COWORK_SCHEDULED_TASK_PROJECTS`: `CMF_EXTRACTION_EXAMPLE_WORKSTREAMS`, `CMF_EXTRACTION_EXAMPLE_HARDWARE` and `CMF_EXTRACTION_EXAMPLE_TOPICS`. Unset = generic built-ins, so a new user's prompt never carries someone else's projects. Placeholders went into `.env.example` and this deployment's real values into `.env` on 2026-10-08. They're not read yet.
  - Each prompt site takes the first few names it needs from the list, in order. Line 226 uses five workstream names, line 133 three, line 96 one plus hardware.
  - Snapshot test: with this deployment's `.env`, the rendered prompt equals today's text byte for byte, so production extraction does not shift. Any unavoidable wording change is reviewed before merge.
  - Bump the profile's version marker only if the snapshot differs.
- [x] **6b. `typed-recall` as the code default; retire the legacy profiles.** *Done 2026-10-08 (User: keep `legacy` opt-in): unset = `typed-recall`; `selective` and `typed` removed and now rejected; `legacy` kept only when set explicitly. Found while doing it: the `typed-recall` prompt quotes 4 of the MS4e `drop_heldout` names, so held-out scores against it are flattered for those four; the leak test only ever covered `selective` and was removed, with the gold file's note corrected.* `extraction_profile_from_env()` (`server/core/config.py`) still returns `legacy` when `CMF_EXTRACTION_PROFILE` is unset. `.env.example` now sets `typed-recall` explicitly (2026-10-08), but a deleted line silently drops back to the pre-2026-09-28 prompt. Make `typed-recall` the default. Then drop `legacy`, `selective` and `typed`, or keep them reachable only for replay/eval scripts, after checking what `scripts/replay_eval.py` and the MS4e/MS8 tests still exercise. Update SETUP.md's config table (currently says default `legacy`).
- [x] **7. Docs.** *Done 2026-10-08: SETUP.md gains a Model Providers section (which provider does LLM vs embeddings, common pairings, API-key and credit notes), an own-machine checklist, Background Capture and Reviewing What Was Captured sections, and corrected config-table rows; `.env.example` gains the same provider table, says the wiki is optional, and that the Gemini key is only for gemini providers; CLIENTS.md's per-client capture table is current (pollers, remote Cowork). Found on the way: the Docker preview passed only a Gemini key into the container, so Anthropic/OpenAI keys never reached it (now passed through), and empty model variables from compose overrode the defaults (config now treats empty as unset). Wiki-less extraction no longer asks for doc proposals (`_EXTRACT_SYSTEM_NO_DOCS`; the with-wiki prompt is byte-identical).*
  - `.env.example`: new providers and keys; `LLM_WIKI_PATH` "leave empty to run without a wiki". Unset already skips the wiki tools, but the template's default is `./starter-wiki`.
  - `docs/CLIENTS.md` / `docs/SETUP.md`: a second-user setup path covering providers, Ollama embedder fallback, poller templates and the review loop.
  - Note that the Docker preview always mounts a wiki and cannot run wiki-less.

### Acceptance tests

1. With `CMF_LLM_PROVIDER=anthropic`, `CMF_EMBED_PROVIDER=openai`, `CMF_CAPTURE_LLM_PROVIDER=anthropic` and no Gemini key or local endpoint, a Claude Code session and a Codex session are captured, extracted into reviewable proposals, approved and promoted, and `recall_mem` finds them.
2. The same with `openai` for all three.

   *Passed 2026-10-08 (second run, after both accounts were funded). Both: no Gemini key, every local URL pointed at a dead port, synthetic Claude Code + Codex sessions, scratch journal and `ms10a-accept-*` graphs. Test 1 (anthropic / openai / anthropic): 6 + 4 turns journaled, `claude-sonnet-5-5` extracted 2 episodes (one a thread merge), both approved and promoted (`claude-code-acme-billing-001`, `codex-acme-billing-001`; promotion 34 s), and `recall_mem` returned each as the top fact for its question. Test 2 (openai for all three): the same flow with `gpt-5.5`, 3 episodes promoted (170 s), both recalled first. The first run (same day) was blocked by unfunded accounts and found two bugs, fixed in `adc3fdc`: billing errors retried as transient quota, and a fixture missing Codex `ordinal`. Open for task 7: with no wiki configured, extraction still asks for doc proposals and then drops them ("LLM_WIKI_PATH is not set", counted as `doc_proposals_failed`); a wiki-less setup should not request them.*
3. This deployment with no `.env` change behaves exactly as before: capture still forced local, reviewer still `todd`, existing project slugs unchanged. Full test suite green.
4. A Claude Code subagent's turns land in the journal as their own conversation linked to the parent session, and are extracted separately from the parent's windows.
5. A clean `.env` from `.env.example` with `LLM_WIKI_PATH` emptied starts with no wiki tools and no errors. *Passed 2026-10-08: a clean working-tree copy with the template `.env` started over stdio in an emptied environment: 18 tools, none wiki/doc, no errors or warnings (25 tools with the starter wiki).*
6. Poller templates install and run on macOS. Linux and Windows are reviewed, and tested if a machine is available.

### Exit gate

**Can the second user go from clone to captured, reviewed and recalled memory on their own machine, using only their own API keys, with no edits to code?** And has this deployment come through unchanged?

**Effort:** 2–3 sessions (the providers in task 2 are most of it).
**Risk:** Medium. New providers touch both LLM call paths. The extraction-prompt change (task 6) is guarded by a byte-for-byte snapshot against today's prompt, and the default flip (6b) is a no-op for this deployment, which already sets `typed-recall`.

---

## MS10 — Distribution and ecosystem

**Goal:** Make CMF useful beyond the original personal deployment. Roadmap [Milestone 10](ROADMAP.md#milestone-10--distribution-and-ecosystem).

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

## MS11 — Knowledge-provider follow-ons (from MS5)

**Status:** ⚪ not started. MS5 deliberately left these out, and none of them block anything.

**Goal:** Take the second knowledge provider from proof to everyday use, and add the next provider when one is wanted.

### Tasks

- [ ] **Decide whether to enable Gmail in `.env`** (`CMF_KNOWLEDGE_PROVIDERS`). Enabling it puts email into `get_context` for every connected client, including ChatGPT and Gemini, so it is a privacy decision for User. Options:
  - leave it off;
  - enable it for everyone;
  - first add per-client or per-scope gating (this ties into MS10 scopes).
- [ ] **GitHub repository provider.** Gmail replaced it as MS5's proof, and it remains the next document-shaped source.
- [ ] **Incremental Gmail sync.** Today the snapshot is refreshed by re-running `scripts/import_gmail_mbox.py` over a fresh Takeout export or a connector pull. Replace that with an incremental refresh that has a cursor.

### Exit gate

**Is a second provider live in User's everyday `get_context` without a privacy surprise, and does its snapshot stay current without a manual re-import?**

**Effort:** 1–3 sessions, depending on which tasks are taken.
**Risk:** Low for the code. The privacy decision is the real gate.

---

## Backlog

Two unrelated piles, previously kept as separate top-level sections (the corpus one used to sit at the top of this file) — merged here since neither is an active milestone with its own exit gate.

### Corpus & review backlog (found 2026-09-11, closed out same day)

MS6b's governance tooling ([plan-history.md](plan-history.md#ms6b--governance)) surfaced this once `explain --graph`, `correct-memory`, and the review CLI could actually be pointed at the corpus. Measured directly against the live journal, not the MS6a/MS3.5-era estimates (several bulk actions and ongoing capture had moved these numbers since 2026-09-08):

- [ ] **The corpus is growing, not static.** The reasoning-episode pool alone grew from 1,243 rows (the 2026-09-05/06 reprocess) to 1,310 by 2026-09-11 — capture (MCP-boundary + imports) kept running after the 2026-09-08 review pass. A recurring/periodic tier-1 review pass is probably the more accurate framing going forward, rather than treating any fixed count as a target to eventually finish. `uv run python -m server.review.cli queue --tier 1` shows what's currently outstanding. **Current recurring pile (2026-09-28), all ongoing capture since the MS4b/MS4b-antigravity batches closed:** 78 `claude_code` episodes (~12 conversations, 2026-09-23 → 09-27; mostly `context-memory-fabric`, 14 `project-epsilon`, 8 with NULL `project`), 51 `antigravity` episodes, and 23 pending doc proposals. Review via `list_review_conversations(harness=..., policy_name="extract", include_tier2_only=True)`. **2026-10-03:** after the Cowork backfill the pending pile was 1,189 episodes and 319 doc proposals across all harnesses; the extract@1.6 review cleared its tier-1 and doc part the same day (see the Phase 4 item below). `claude_code` rows before 2026-10-02 are now `claude_desktop_code`. `scripts/review_priority_report.py [--harness …]` orders it priority / maybe / low from User's review history (decision/plan/rejected_alternative with ≥3 evidence turns first) without rejecting anything.
- **Sourced from three transcript adapters now:** MS4b (Claude Code), MS4b-antigravity (both closed 2026-09-28), and MS4d (Codex, closed 2026-09-30). MS4c (OpenClaw) remains on hold; Gemini CLI dropped. The extending-the-backfill-beyond-CMF item closed as a side effect of the poller's first 872-conversation sweep.
- **Also found, unrelated to the review pass itself:** the `cmf_test` FalkorDB graph's vector index is still 1024-dim (Gemini-era) while the configured embedder produces 768-dim (local/nomic) — every `live`-marked test that calls `remember()` against `cmf_test` currently fails with a vector-dimension mismatch, independent of any of this session's code changes (confirmed by re-running before/after). `cmf_test` was never migrated alongside `mem-fabric-local` in the Spark migration; needs the same treatment (`docs/spark-phase7-ab-log.md`'s migration steps, applied to the test graph).

### Auth hardening (deferred out of MS6c, 2026-09-16)

Raised in review on [PR #6](https://github.com/username/context-memory-fabric/pull/6) and consciously merged without fixing (User, 2026-09-16) — the OAuth layer works and these are hardening, not blockers, on a single-user personal server. The design itself reviewed clean: PKCE correct, codes single-use, refresh tokens rotate on exchange, expiry enforced on both token types, consent password compared with `secrets.compare_digest`. Four of the five items were closed 2026-09-28 ([plan-history.md](plan-history.md#auth-hardening-deferred-out-of-ms6c-2026-09-16-closed-2026-09-28)); the one left is deliberately deferred.

- [ ] **`exchange_refresh_token` drops `resource`.** `exchange_authorization_code` persists `authorization_code.resource` (`server/core/oauth_provider.py:161`); the refresh path hardcodes `None` (`:202`), so a token's audience binding silently disappears the first time it refreshes. Not exploitable today — the SDK's `ProviderTokenVerifier` only calls `load_access_token` and never checks `resource` — but it becomes a real bug the moment RFC 8707 audience validation is enabled, and it would surface ~30 days after a client first connects. `RefreshToken` needs to carry the resource forward for this to be fixable at all. **Deferred (User, 2026-09-28):** `resource` names which server a token was issued for, not which client or IP holds it, so this only bites with more than one server behind the same auth *and* audience validation turned on. Neither applies to this single-server deployment.

### Wiki entity-extraction quality (found 2026-09-17, closing MS7b)

Surfaced while visually inspecting the pruned `mem-fabric-local` graph in FalkorDB Browser — a real gap in `build_wiki_entities.py`'s heading+lede decomposition, not fixable by editing the graph directly:

- [ ] **Topic-level entities never form when no section heading names the topic.** `WIKI/art-projects/Cityscapes/Cityscape-View the shadows.md` — all 13 section headings are camera-technique-specific (`TS-E Mechanical Setup`, `Shift-Stitch Configuration`, `Post-Processing Pipeline (Photoshop / ACR)`, ...); none contains the word "Cityscape," so the note never links to the `Cityscapes`/`cityscape` entities that exist from other notes in the same folder. Root cause: Phase 2's original finding that note titles match only 2% of entities (documented in [MS7b](plan-history.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13)) meant titles were deliberately excluded as a decomposition source — correct call in aggregate, but it leaves notes like this one topic-orphaned. Compounding: `cityscape` (domain) and `Cityscapes` (project) are themselves near-duplicates Phase 4's merge pass never caught, since `_norm()` doesn't handle singular/plural.
- [ ] **Per-section decomposition can attribute a concept to the wrong section, fragmenting it.** Same note: `Tilt` and `Shift` were both extracted from the section titled **"Zero/Static Configuration"** — specifically the section about using *neither* — while the actual `Tilt Configuration` section produced `Scheimpflug` instead, and a third section produced `Tilt-Shift` as a separate tool entity. Three sections, three fragments of one lens concept, none cross-linked.
- **Options, not yet chosen between:** (a) re-run `build_wiki_entities.py` with the note title/folder path added to each section's decomposition context; (b) extend Phase 4's merge pass beyond literal near-duplicates to catch singular/plural and compound-vs-parts cases; (c) accept it as a known cost of this approach and fix only manually, case by case. Whichever is chosen, `mem-fabric-local`'s current ~1,219 entities mentioned by a note but by no episode/fact/project were deliberately left unpruned specifically so this evidence isn't destroyed before a fix is decided.

### Entity/edge cleanup pass for the CMF graph (found 2026-09-20, reviewing conversation b23f6f7d)

Surfaced visually inspecting `mem-fabric-local` in FalkorDB Browser after promoting the "review by conversation" batch's approved episodes — the same graph-quality question the item above raises for wiki-derived entities, but from the episode-extraction side and scoped as an actual cleanup pass rather than a root-cause fix:

- [ ] **Near-duplicate entities from qwen-122b extraction** (e.g. "MemPalace-like memory substrate layer" vs. "memory palace layer", "Obsidian URI schemes" vs. "Obsidian") need merging, not just cosmetic renaming. Mechanically: redirect every `MENTIONS`/`RELATES_TO`/`IN_PROJECT` edge from the duplicate onto the canonical node (`MATCH (dup)<-[r:MENTIONS]-(ep) MERGE (ep)-[:MENTIONS]->(canonical) DELETE r`, same pattern each direction/edge type), then delete the duplicate. **Not a bare `MERGE`-and-delete**: graphiti's edges carry real content (`fact` text, embeddings, `valid_at`/`created_at`) that a fresh bare edge won't have — properties must be copied onto the new edge first, or the redirect silently drops the fact each edge represents. No candidate list built yet.
- [ ] **Confirmed unsafe to judge by eyeballing the graph.** Two entities that looked orphaned by visual inspection (`100MB limit`, `120-second OpenClaw timeouts`) both turned out to be real, correctly-connected data on closer query — `100MB limit` is a genuine fact from an unrelated Obsidian-project episode (`gemini-obsidian-009`) that surfaces in CMF's view only because it shares the `GitHub` entity (projects are many-to-many by `tag_projects.py`'s own design); `120-second OpenClaw timeouts` had two mentioning episodes, one from this very CMF batch, just not visible in a dense 127-node force layout. Any cleanup pass needs to query connectivity directly, not read the rendered graph.
- [ ] **The real candidate set is graph-wide zero-`MENTIONS` entities — measured at 1,219**, which is very likely the same population the "Wiki entity-extraction quality" item above already names (`~1,219 entities mentioned by a note but by no episode/fact/project`) — worth confirming they're the same set before treating this as two separate backlog items. Even a zero-`MENTIONS` entity can carry real `RELATES_TO` fact text (as `100MB limit` shows) — "safe to delete" here means "won't break graph structure or dangling references," not "zero information loss." Needs a categorization pass (genuine standalone facts vs. junk — config values, restated instructions, extraction artifacts) before deleting anything, not a bulk sweep.
- **Planned in [MS9 — Graph quality for retrieval](plan-history.md#ms9--graph-quality-for-retrieval-2026-09-30--10-07)** (2026-09-29): the merge tool, the candidate report and the wiki-only entity categorization live there.
- **Forward fix:** [MS4e](plan-history.md#ms4e--entity-extraction-quality-forward-only-2026-09-28) stops new noise at extraction time. Its optional Phase 5 report, this item's candidate list, moved to MS9 task 1b.
- **Relates to:** [Wiki entity-extraction quality](#wiki-entity-extraction-quality-found-2026-09-17-closing-ms7b) (root-cause side of the same noise) and [Project nodes vs. entity property](#project-nodes-vs-entity-property-open-since-before-ms7b) (paused partly *because of* this same noise).

### Manual episode merge tooling (found 2026-09-21, review-by-conversation)

The review pass merged same-conversation episodes 3 times via hand-rolled sqlite3 scripts. The resolved parts, including the ~20-point / ~3,600-char size limit, are in [plan-history.md → Backlog — closed items](plan-history.md#backlog--closed-items).

- [ ] **A real `merge_episodes` MCP tool.** It would wrap: insert the consolidated `derived_memories` row, set `supersedes`/`superseded_by`, write the `episode-proposals/` mirror file (hand-inserted rows have none, so `get_episode_proposal` can't see them), bulk-reject the constituents, and enforce the size limit up front.

### Wiki Note dates (found 2026-09-29, MS8 fidelity)

MS7b's `Note` nodes carry no dates (`scripts/seed_wiki_graph.py` seeds them once from registries that have none). Replay snapshots therefore prune notes by the wiki export, and `get_context` can't say how old a note is.

- [ ] **Give every `Note` a `created_at`, an `updated_at` and an `ingested_at`**, set by `seed_wiki_graph.py` and `sweep_wiki_graph.py`, each with a `*_source` field saying where the date came from.
  - **`created_at`:** the note's `created:` frontmatter when present (90 notes have one), otherwise the commit that first added the file (`git log --diff-filter=A --follow`).
  - **The 705 files from the 2026-04-22 initial import:** git can only say "on or before 04-22" for these, so use the file's disk timestamp instead (User, 2026-09-29).
    - Measured the same day, of the 671 still on disk, **126** have a disk birth time before the import, which is real pre-git history.
    - **545** have one after it, because a copy or sync reset it (442 show May 2026), and would date notes weeks after git shows they existed.
    - Proposed rule, to confirm with User: the disk time when it's earlier than the import, otherwise the import date with `created_at_source = "git-import-upper-bound"`.
  - **`updated_at`:** the last commit that touched the file. It's exact since Auto-sync resumed on 2026-09-11. Inside the 2026-08-31 → 09-11 watcher outage it's only as good as the hand-made commits.
  - **Not disk birth time in general:** it's reset by rewrites and syncs (`Print-Framing-Now-And-Then.md`: disk 05-27, git first-add 04-23).
- [ ] **Use them.** Replay snapshots prune notes by `created_at`, and `get_context` shows a note's age next to it, which feeds the staleness signals under Assembly refinements.

### Promotion hang with no error output (found 2026-09-21, reviewing conversation `8406b5c3`)

`promote_approved_episodes` hung with **zero log output** on thread `ms7b-implementation-priority`. It reproduced twice across fresh server restarts, with the Spark tunnel healthy and `lms ps` showing the model `IDLE`, so the request apparently never reached LM Studio. A clean retry on 2026-09-22 promoted it ([plan-history.md](plan-history.md#backlog--closed-items)). Restarting the CMF server abandons a stuck coroutine safely.

- [ ] **Not root-caused.** Content length isn't the explanation. Reproduce with request-level tracing (log immediately before the `openai_generic_client` call, not just around it) to tell whether the hang is in Graphiti's pre-processing or in the HTTP call to LM Studio.

### Project nodes vs. entity property (open since before MS7b)

User's question, not yet settled: do `:Project`/`IN_PROJECT` nodes and edges (33 `Project` nodes and 2,090 `IN_PROJECT` edges in `mem-fabric-local` as of 2026-09-30 (1,946 from entities, 144 from episodes, 0 from notes), built by `tag_projects.py`) earn their place as first-class graph structure, or would `entity.project = ["proj1", "proj2"]` as a plain property serve the same purpose more simply? Paused rather than decided — visualizing the current graph in FalkorDB Browser showed the project layer isn't wrong, just not obviously pulling its weight next to the noise from the wiki-only entity layer above. Revisit once the extraction-quality items above are resolved, since they're currently confounding how legible the project layer looks.

**Moved out of MS9 task 7 (User, 2026-10-06); undecided.** What the code shows:

- **Retrieval never reads it.** `server/context.py` and `server/retrieval_expansion.py` don't touch `:Project` or `IN_PROJECT`; Phase 3's expansion follows typed `MENTIONS` only. Today the layer is for Browser viewing, not retrieval.
- **Episodes already use a property** (`e.project` + a project label, `server/consolidation/graph_tagging.py`), which fits since an episode belongs to one project. Only entities get project membership, through `IN_PROJECT`, and an entity can belong to several projects.
- **Upkeep:** every promotion writes the edges, `tag_projects.py` recomputes them, and `merge_entities.py`, `retract_episodes.py` and `repair_p2b_episodes.py` each have to handle them. MS4e's `Workstream` label exists only to avoid clashing with `Project`. Notes and the Phase 4 wiki entities have no `IN_PROJECT` edges at all.

**The options:**

- **A. Keep the hub nodes.** Renaming or merging a slug changes one node, and a node can hold project metadata later. But the layer adds 33 hubs and about 2,090 edges that retrieval never reads, stores projects one way for episodes and another for entities, and becomes a risk if any traversal is ever untyped.
- **B. Use `entity.project = [...]`.** One model for episodes and entities, a cheap `WHERE 'x' IN n.project` filter, no hub nodes, and fewer edge types for the maintenance tools. But a slug rename becomes a bulk `SET`, and project metadata would need a small registry. Analysis leaned B.

**Separate question: does project help retrieval at all?** Only if ranking uses it, and then A and B are equivalent. Candidates:

- a soft boost for results in the query's inferred project(s), which works fine with entities in several projects;
- a project-bounded addition to the Phase 3 expansion;
- an explicit `get_context(project=...)` parameter (ties to MS10 scopes).

Test it as an MS8 replay policy (Hit@8 / MRR) before building. A recent person-and-contact lookup miss (2026-10-06) was missing data in CMF, not a ranking problem, so project tagging wouldn't have helped. The Gmail provider (MS11), person entities, and the entity-summary arm in Assembly refinements are the more relevant levers there.

**Evidence that project-aware ranking could help (2026-10-06).** The replay drop at `now` after 10-02 (Hit@8 90.0% → 82.5%, MRR 0.515 → 0.434; see [MS9 task 7](plan-history.md#ms9--graph-quality-for-retrieval-2026-09-30--10-07)) is one project crowding out others. 219 new `claude-cowork-career-*` episodes now take a large share of the 130 slots that episodes added since 10-03 hold, out of 480 across the 60 cases.

- **A6** (a dataviz decision) and **C1** (Interlock) lost their gold answers to career and other off-target episodes.
- **C5, C15 and C19** slipped from rank 1.

A soft boost for results in the query's inferred project(s), or a per-project cap on the memory slots, would target exactly this. Measure it on these cases first, after refreshing stale gold such as C16, whose new career episodes are valid answers.

### Nightly auto-review of poller output (User, 2026-10-08)

The pollers stage episodes and doc proposals around the clock; today every one waits for a hand review. A nightly pass could do the first cut and leave only the final call to the user.

- [ ] **A scheduled task, nightly after the pollers,** reviews what they staged since its last run, in batches of at most 50, using the calibrated rules:
  - reject process narration, duplicates and stale snapshots;
  - for a doc update that trips the 30% check, compare it with the live page and rebuild it additively rather than reject (see the extract@1.7 item);
  - flag rather than decide: project-slug questions, new wiki folders, legal/financial/personal facts that conflict, deletions of substantive content.
- [ ] **Recommend, never apply.** It records proposed verdicts (approve / reject / flag, with reasons) without changing any episode, doc proposal, wiki page or graph node.
- [ ] **Then message the user** with the batch: EP and DOC tables with inline reasoning, flagged items first. The user confirms or changes the verdicts; only then are they recorded, docs applied and episodes promoted.
- **Open:** where it runs (a Cowork scheduled task, a launchd job driving a headless Claude session, or an LLM call inside CMF); which model and what it costs; how proposed verdicts are stored (a `recommended` review state or a sidecar file); how the message is sent (push notification, email draft, or an artifact with approve/reject controls) and how the reply comes back.

### Merge related episodes: career tailoring threads (deferred out of MS9, 2026-10-07)

The MS8 replay at `now` fell from Hit@8 90.0% / MRR 0.515 to 82.5% / 0.434 after 447 episodes arrived on 10-03 and later (see [MS9 task 7](plan-history.md#ms9--graph-quality-for-retrieval-2026-09-30--10-07)). User ruled it episode-volume dilution and closed MS9, and wants to know whether merging related episodes helps or hurts. **No prune experiment (User, 2026-10-07), and "removing approved episodes is not the remedy" stands.**

- **Why a merge could help.** A word-overlap check found no near-duplicates among the 237 `claude-cowork-career-*` episodes, but sibling clusters exist: one company's resume tailoring (`career-078/079/080`), a second requisition at another (`090/091/092`), and an employer-sections consolidation (`068/075`). One roll-up episode per cluster could free slots without losing the information.
- **How, sequentially on scratch graphs (never production):**
  - Scope to the 447 episodes added since 2026-10-03 first; widen to all production episodes only if it works.
  - Take a fresh `GRAPH.COPY` (wait for `rdb_bgsave_in_progress:0` between copies), group the career episodes by company or application, write one summary episode per group on Spark (check `spark_job.lock` is free), and retract the originals in that copy.
  - Replay all 66 cases of `ms7_eval/queries.json` with `scripts/replay_eval.py`. Group `D-prune` (D1–D6) is already in the file: D1–D3 are the merge-sensitive tailoring clusters and D4–D6 are single-fact decisions that a merge must not lose. Compare Hit@8, MRR, Wiki@8 and `provenance_rate`, and watch A6, C1, C5, C15, C19 and D1–D3 case by case.
  - Any production change needs a new ruling from User, a backup first, and before/after counts.
- **Risk.** A roll-up cuts per-event precision and can hurt cases that pass today; the replay is how to find out.

### Project-aware ranking (deferred out of MS9, 2026-10-07)

Goal: stop one project from taking most of the 8 memory slots on a query about something else. The evidence is in the [Project nodes vs. entity property](#project-nodes-vs-entity-property-open-since-before-ms7b) item (A6, C1, C5, C15, C19). Test each variant as an MS8 replay policy before building; none is built.

- **Soft boost.** Score bonus for episodes in the query's inferred project(s). Cannot hide a right answer, but depends on the guess.
- **Per-project cap.** No project may hold more than N of the 8 slots. Needs no guess, but can cut a right answer when a question really is about one project (D1–D3 are exactly that), so consider a cap that applies only when the extra results score far below the top ones.
- **Which signal (open, User to decide).** Keep **project** (a tag) and **workstream** (an extracted entity type, kept apart from `:Project` on purpose, see [FIX-GRAPH-PLAN.md](FIX-GRAPH-PLAN.md)) separate:
  - *Project tag:* `e.project` is on 1,073 of 1,074 episodes (19 are `misc`), but only 147 episodes have an `IN_PROJECT` edge and 2,157 of 4,919 entities do, so ranking must read the property. Granularity is uneven: `career` is a whole area (238 episodes) while small slugs behave like workstreams.
  - *Workstream entities:* finer (a company or an effort) and already followed by Phase 3's `MENTIONS` expansion, but only as good as extraction, and not measured.
  - Run both variants in the replay and compare.
- **Related.** `:Project` nodes vs. a property (same item above), and `get_context(project=...)` ties to [MS10](#ms10--distribution-and-ecosystem) scopes.

### Phase 6: full ledger re-ingest into a fresh graph (paused, carried out of MS9, 2026-10-07)

Optional post-MS9: rebuild from transcripts using the current extraction policies (`typed-recall` + debris filter) across all historical episodes. **Paused at 432 of 1,071 episodes (40.3%).** It was paused on 2026-10-05 10:55 CDT to free Spark for Public Preview Phase 2 grading. The isolated scratch graph `mem-fabric-rebuild-scratch` holds 432 committed episodes, 1,017 nodes, 2,756 edges and 721 facts, and can resume immediately with `scripts/rebuild_graph_from_ledger.py` (replay into a new graph name, or everything is skipped). Needs Spark for many hours and takes `spark_job.lock`.

### Found during MS6c Phase 1 (Cowork functional pass, 2026-09-16)

- [x] **Fixed 2026-10-06 ([MS9 task 7](plan-history.md#ms9--graph-quality-for-retrieval-2026-09-30--10-07)): `new_content` now re-extracts the episode's facts, and the one polluted production episode was removed.** Original finding: **`edit_memory` doesn't re-run fact extraction, so corrected episodes leave stale facts behind.** Confirmed by direct Cypher query against `mem-fabric-local-wiki`: both MS6c verification episodes (`ms6c_verification_test_2026_09_16`, `gemini_verification_test_2026_09_16`) still carry their pre-correction `RELATES_TO` fact edges (`"Initial version... test value set to ALPHA"`, `"...test value is ALPHA-GEMINI"`) with no post-correction facts added alongside them. `edit_memory` updates the episode's own content node in place — that part is correct and immediate, `recall_mem`/`get_context` both surface the corrected body text — but the entity-relationship facts Graphiti derived at original ingestion are untouched. Since `recall_mem`/`get_context` render *both* the episode body and separately-listed facts, this is what User's Cowork pass surfaced as apparent "duplicates": two or more distinct, differently-worded facts from the same original extraction pass, one of them now describing a state the episode no longer says. Not literal duplicate indexing — each fact edge has a distinct uuid and wording — but confusing and worth fixing: `edit_memory` should either re-run extraction on `new_content` or explicitly invalidate/mark superseded the old fact edges the way `correct_memory`'s original design intended.
- [ ] **Still open — also found 2026-09-21 (review-by-conversation pass): `edit_memory`'s target search misses some already-promoted episodes entirely.** Every query tried (full statement text, short phrases, episode name, single keywords) returned "no matching episodes, entities, or facts found" for content `recall_mem` finds instantly. Not diagnosed. Right now there is no working MCP path to patch a promoted episode's content. A direct sqlite write fixes only the ledger's `derived_memories.statement`, not the graph node.
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
- [ ] **Test an entity-summary arm in recall (MS8 replay policy).** `recall_mem` reads fact edges and episode text, never entity nodes. Graphiti keeps an LLM-merged summary on each entity, updated on every mention.
  - **The test:**
    - Add a replay policy that runs Graphiti's node search (entity name + summary) alongside the two existing arms.
    - Surface each matched entity's summary as a result, labelled as synthesized, with its mentioning episodes as provenance.
    - Grade it against the current production policy on every eval case, for every memory case, not just the targets.
  - **Targets:** the entity-centric misses that no arm reaches today: A1 (what OpenClaw runs on), C2 and C10 (OpenClaw's model backend).
  - **Adopt only if** memory Hit@8 rises at "now" without new misses elsewhere.
  - **Caveat:** historical cut-offs will read today's summaries, which can describe later events. Treat those runs as optimistic, or build summary history first (below).
  - **If adopted, add summary history:** a journal table of (entity, old summary, new summary, episode, time), written by `postprocess_episode`. It gives the replay the summary as of a cut-off, and gives `explain` the episode behind each change.
- [ ] **Residual eval misses.** A1 — `gemini-openclaw-002` (the friend's-Spark decision) is retrieved by neither the edge nor the vector arm; needs the extraction gap closed or a broader vector recall. B7 — `+memory` regressed 1→0 after the vector arm (wiki-domain query, `+both` unaffected); accepted. C6 — see `search_wiki` semantic retrieval above.

### Double-promoted memories in `mem-fabric-local` (found 2026-10-02, provenance step 5a)

Found while repairing Phase 2b. These four memories were promoted twice, once on 2026-09-13 and again on 2026-09-23. Each now has two episode nodes with the same content and memory_id but different names and separately extracted facts. They predate Phase 2b and don't collide on names. The ledger can record only one name per (memory, graph), so its row names one of the two nodes.

| memory's nodes (2026-09-13 / 2026-09-23) |
|---|
| `chatgpt-photo-006` / `chatgpt-photo-009` |
| `claude-project-epsilon-004` / `claude-project-epsilon-029` |
| `claude-project-epsilon-005` / `claude-project-epsilon-030` |
| `claude-project-epsilon-006` / `claude-project-epsilon-031` |

- [x] **Done 2026-10-06 — see the fold record below.** Original task: **Remove the 09-23 duplicate of each,** keeping the 09-13 node, which the eval gold and ledger are more likely to reference. Before deleting, check which node carries more `MENTIONS`/facts. `-005` has 10 mentions and `-030` has 0, but for `-004`/`-029` the newer node has more, so the choice is per pair, not always the newer one. Use `graphiti.remove_episode` (no LLM) on a backup-first, scratch-rehearsed run, like `scripts/repair_p2b_episodes.py remove-wrong`, then align the ledger.
- [x] **Also `claude-cowork-interlock-005` (found 2026-10-06), folded the same day.** Two identical nodes, created 2026-10-04 00:10 and 00:13 (`f710c9a8…`, `0b030fc3…`), probably a retried batch in the extract@1.6 Phase 4 promotion. It is the only duplicate episode name in production; remove one the same way.
- **Fold record (2026-10-06).** The four memories above were really `claude-career-navigator-004/-029`, `-005/-030`, `-006/-031` and `chatgpt-photo-006/-009`; the doc names were scrubbed placeholders. All 5 pairs were folded, not deleted. Each copy was extracted on its own and carried facts the other lacked, so the duplicate's facts and mentions moved onto the kept node before the duplicate was removed (`scripts/fold_duplicate_episodes.py`, ledger `imports/fixgraph/fold_duplicates_ledger.jsonl`).
  - **Kept:** `-006` (C16 gold), `-005`, `photo-006`, `-029` (`-004` was empty), and interlock `0b030fc3` (14 facts vs 9).
  - **Process:** rehearsed on `fixgraph-folddup`; production backed up to `mem-fabric-local.pre-folddup-20261006`.
  - **Result:** −5 episodes, facts unchanged at 7,879, −18 redundant mentions; 0 duplicate contents or names left. The ledger's `promotions` rows now name the kept nodes.
  - **Replay at `now`:** unchanged (Hit@8 82.5%, MRR 0.434; only A10 moved, 5 → 6).
- [ ] **Find why they were promoted twice on 09-23.** All four were promoted within a minute (18:56). The 09-23 promotion pass didn't see the 09-13 promotions as already promoted, probably the same stale-ledger problem as Phase 2b, since 09-13 rows went to `mem-fabric-local-wiki` and not `mem-fabric-local`.

### Claude surface provenance, lost transcripts and Cowork capture (found 2026-10-02, MS9 Phase 5)

**Done 2026-10-03; record in [plan-history.md](plan-history.md#claude-surface-provenance-and-cowork-capture-found-2026-10-02-closed-2026-10-03)**, working checklist in [CLAUDE-HARNESS-PROVENANCE-PLAN.md](CLAUDE-HARNESS-PROVENANCE-PLAN.md). In short: harness now comes from each transcript line's `entrypoint` (`claude_code` / `claude_desktop_code` / `claude_cowork`); eval and temp-dir runs are out of the journal; the Phase 2b episode damage it uncovered in `mem-fabric-local` is repaired and the Code-tab episodes are renamed `claude-desktop-code-*`; Cowork has an adapter, a 15-minute poller and a full backfill (964 conversations journaled, 460 extracted); all pollers share one Spark slot. Still open:

- [ ] **Lost transcripts (provenance steps 6a/6b).** Claude Code's 30-day cleanup deleted 56 CLI and 39 Code-tab transcripts before capture (`cleanupPeriodDays` is now 36500). No local copy exists; the typed prompts of the 56 CLI sessions are journaled as partial `user_prompt.history` events. Searching further needs Full Disk Access (Time Machine depth), the NAS shares mounted, and User's answer on whether "Todds-Air" was another machine. Manifest: `imports/source/claude-restore/manifest.json` (local).
- [ ] **Cloud-session transcripts (deferred, User 2026-10-02).** At least 8 Code sessions ran in the cloud, and 6 Cowork sessions were handed off to it after starting locally; none of that is on disk.
- [ ] **Cowork sessions are no longer on disk (found 2026-10-08, MS10a).** The Cowork poller runs cleanly every 15 minutes but has journaled nothing since 10-06: the newest local transcript under `local-agent-mode-sessions/` is from 2026-09-22, while `remote-session-spaces.json` (updated 2026-10-08) lists 73 remote sessions. Cowork appears to run sessions remotely now, so their transcripts never reach the adapter. Today's Cowork review pass shows up only as MCP-boundary `mcp_tool_call` events, attributed to `claude_code`. Checked 2026-10-08: nothing under `~/Library/Application Support/Claude/` holds them. The only trace is the claude.ai web cache (`IndexedDB/https_claude.ai_0.indexeddb.leveldb`), a partial, private-format copy of what's on screen: not a capture source. Options: (a) MCP-boundary capture, already running, but attributed to `claude_code`, so give those events the real client surface; (b) `capture_session` checkpoints, already asked for in the server instructions; (c) a claude.ai data export, if it includes Cowork sessions (check with one export first).
- [ ] **Recurring Cowork scheduled tasks (decided 2026-10-03: journal-only).** `project-epsilon-daily-schedule`, `daily-research-sweep`, `project-epsilon-dashboard-refresh` and `social-media-analytics` (~470 sessions, ~7 Spark-hours) stay unextracted; the 8 other channels were extracted. Open option: add `weekly-status-brief` to `CMF_COWORK_EXTRACT_SCHEDULED_TASKS` in the Cowork poller's plist so new briefs are extracted as they land.
- [x] **The 6 pre-existing `remember`-path test failures** (`test_mcp_contract_fixtures`, `test_ms4e_extraction_profile`), present since `6fa4056`'s naming automation. *No longer failing as of 2026-10-08 (full non-live suite: 898 passed, 0 failed), after MS9's close-out test repairs.*
- **Relates to:** [MS9](plan-history.md#ms9--graph-quality-for-retrieval-2026-09-30--10-07) (Phase 5 finding; the Phase 2b repair) and [Double-promoted memories](#double-promoted-memories-in-mem-fabric-local-found-2026-10-02-provenance-step-5a).

### extract@1.7: scoped thread candidates (approved 2026-10-03, build after the extract@1.6 review)

extract@1.6 reuses thread labels across unrelated conversations: `open_threads()` hands the model the 20 most recently active open threads corpus-wide, `thread_key` is a required string, and the prompt never says what a thread is. 47 of 509 keys span more than one conversation; the worst spans 123 conversations in 5 projects. Spot checks show thread *merges* are coherent within a conversation; it is the *label* that is borrowed. The label is not promoted into graph text (the promotion parser stops at `| thread=`).

- [ ] **A. Scope candidates:** this conversation's threads plus same-project open threads seen recently (default 14 days). Additive `reasoning_threads.projects_json` column, backfilled from `derived_memories`.
- [ ] **B. Prompt:** define a thread (one specific decision or problem); reuse an open key only on a clear continuation, otherwise mint a new specific key; show each candidate's project and latest statement.
- [ ] **Replay check first:** a fixed sample of ~20 conversations replayed under 1.6 and 1.7 in a scratch journal (no promotion). Compare keys spanning >1 project (target ~0), reuse rate of the top-5 keys, merges per conversation, plus a 10-merge spot check each.
- [ ] **Collect review findings here** that should also go into 1.7.
  - Doc routing: new project folders under `WIKI/projects/` must be Title-Case and reuse an existing folder's casing (1.6 used the lowercase project slug; 134 pending paths normalized 2026-10-03 with `scripts/normalize_proposal_paths.py`).
  - **Destructive updates (critical):** the model sees only a ~300-char snippet of an existing page (`_retrieve_relevant_wiki_docs`) but is told to write the complete page, so 43 of 45 pending 1.6 updates would remove >50% of their page. 1.7 must give the model the full live page for a target it updates, or switch updates to section-level additions merged by code. Since 2026-10-03 `apply_doc_proposal` refuses updates removing >30% of a page's lines unless `force=True`; the 45 pending updates are triaged and rebuilt non-destructively in review. **The 30% check is a flag, not a reject rule (User, 2026-10-08):** when it trips, compare the proposal with the live page; reject only if the page already holds everything relevant, otherwise rebuild it as an additive update. Whatever reviews proposals (a person, the nightly auto-review below, or 1.7 itself) should run that comparison instead of stopping at the refusal.
  - Unmerged duplicate singles: in some conversations, window-level episodes repeat verbatim as numbered parts of that conversation's thread merge without having been merged away (astro: 7 of 11). The merge should reject every child it absorbs, including children from re-runs.
  - Doc targets: new topics belong on new pages, not inserted into an existing page as a whole-file rewrite (both OpenClaw.md proposals were unrelated topics). Writing into `RAW/` is allowed (User, 2026-10-03: they are his own memories), keeping each RAW page's own frontmatter schema.
  - Frontmatter dates: `created`/`updated` must come from the source transcript dates (America/Chicago), never the extraction date; 1.6 used the extraction day as `updated` and guessed `created`. Every created or edited page needs complete frontmatter (title, created, updated, status, source when known, tags).
  - Eval privacy: 1.6 doc proposals quoted eval question text and gold answers into wiki pages (found in batch 7). Extraction should treat `tests/fixtures/ms7_eval/` content as private: case IDs and scores only.
  - Near-duplicate pages within one conversation: one CMF session proposed 8 overlapping FalkorDB Browser pages under different titles, and another proposed 4 graph-repair procedure pages. 1.7 should see its own earlier proposals in the conversation and update those instead of minting new titles.
  - Career routing (User, 2026-10-03): a role/application and a per-person outreach note are **episodes**, not doc proposals. One overview page explains what a weekly market brief contains; dated briefs never become pages. Personal job-search pages belong under `WIKI/Job-Search/`, plugin pages under `WIKI/projects/Career-Navigator/`, and art work under its `WIKI/art-projects/` series page.
  - Resume rules: never write a wiki copy as if it were the source; the canonical rules are `profile.md` and ExperienceLibrary `resume_writing_note` fields.
  - Duplicate doc targets across conversations: each `update` is a full-file rewrite on the same base, so only one can ever apply; 18 paths / 52 proposals were consolidated by hand in Phase 2.2. 1.7 (or the pipeline) should see other conversations' pending proposals for the same target.
- [x] **After User completes the 1.6 review:** dry-run script that closes over-broad open threads (e.g. spanning >10 conversations) in `reasoning_threads` -- status only, no episode/merge/review-queue change; apply with User's approval. *Done 2026-10-04: `scripts/close_overbroad_threads.py` closed 12 attractor threads spanning >10 conversations (896 open, 507 resolved).*
- Rejected: merge-time statement similarity (lexical overlap did not separate attractor-key merges from the rest, 0.114 vs 0.114). Not doing: relabeling existing 1.6 rows.

### Promoted-episode project-slug migration (found 2026-10-03, extract@1.6 review)

The review split the old `project-epsilon` project into a job-search project and a plugin-development project, and folded several near-duplicate slugs together (mappings live in the gitignored `.env`: `CMF_PROJECT_ALIASES`, `CMF_PROJECT_FOLDER_MAP`, `CMF_COWORK_SCHEDULED_TASK_PROJECTS`). Pending items were retagged with `scripts/retag_review_projects.py`; episodes already **promoted** still carry the old slugs in FalkorDB and the journal (as of 2026-10-03: 89 job-search/plugin, 114 vault, 26 finance).

- [ ] Inventory and classify the promoted episodes; User approves the table.
- [ ] Dry-run retag script for graph tags + `derived_memories.project` + approved mirrors; rehearse on a graph copy, back up, apply with approval, MS8 replay.
- [ ] Decide on episode names that embed an old slug (recommend: keep them as identifiers).

### Scrub personal mentions from tracked files (found 2026-10-03, review-queue cleanup)

Code, tests, docs and commit history pushed to GitHub name User's own projects, scheduled tasks, folders, people and graph episode names (test fixtures, docstring examples, plan and result docs). From 2026-10-03 on, new code uses placeholder names and keeps real mappings in the gitignored `.env` (`CMF_COWORK_SCHEDULED_TASK_PROJECTS`, `CMF_PROJECT_ALIASES`).

- [ ] **Inventory** tracked files for personal project, task, folder and person names; decide per file whether to genericize, move to a gitignored local file (as `tests/fixtures/ms7_eval/` already is), or keep.
- [ ] **Genericize tests and code first**, then docs. Rewriting already-pushed history is a separate decision.
- [ ] **Guard:** a pre-commit or test check against a local, gitignored denylist so new mentions don't creep back.

### Gemini Apps records missing from the April Takeout (found 2026-10-08, export cleanup)

The only Gemini import ran on 2026-09-04, from the September Takeout (`Takeout 3`). The older April 22 export (`Takeout 5`, unpacked 2026-09-08) was never imported, and 226 of its 4,452 Gemini Apps records (Oct 2024 to Apr 2026) have no matching event in the journal, apparently because Google left them out of the later export. Matching was done by timestamp. The exports moved to the NAS on 2026-10-08: `/Volumes/nas-data/docs/Takeout`.

- [ ] **Run `journal_gemini_apps_export` (`server/importers/gemini.py`) on `Takeout 5/My Activity/Gemini Apps/MyActivity.json`** from the NAS copy. It has no CLI; call it from Python as the original import did. Event IDs are a hash of content plus conversation ID, so the ~4,200 records already in the journal should come back as `*_deduped`. Check that before trusting it: `*_journaled` should be about 226 prompts plus their responses, not thousands. Then run the normal review and promotion pass on the new rows.
- Not imported from any export and not planned: AI Mode (107 records in `Takeout 3`, 88 in `Takeout 5`), NotebookLM (480 files), Workspace Studio, gems/scheduled actions, and the attachments in `Takeout 4`. Each would need its own importer.

---

## Two standing notes

- **MS6 is the differentiation milestone.** *"Why does the system believe this?"* is the capability competitors do not offer. It should not slip indefinitely behind adapter work.
- **MS7 is done** (2026-09-10). The answer-quality eval (`tests/fixtures/ms7_eval/`, Artifact `ms7-answer-grader`) is the reusable instrument — re-run `capture.py` + `answer_eval.py` after any retrieval or assembly change.
