# Implementation Plan — Active milestones

The milestones still to do, in execution order. Index and decisions log: [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). Completed milestones: [plan-history.md](plan-history.md).

---

## MS7b — Wiki-derived entity layer + enriched episode bodies (experiment, 2026-09-13)

**Status:** Experiment on branch `ms7b-wiki-entities`. Builds into a new graph (`mem-fabric-local-wiki`); the current graph is preserved untouched as `mem-fabric-local-ep`. Adopt-or-discard is decided at the exit gate, not before.

**Restored to `main`'s plan 2026-09-16.** This section was written on the `ms7b-wiki-entities` branch and went with it when MS6c's work was split onto `main`, so for a few days the milestone existed in code and in no plan anyone reading `main` would see. The branch stays parked — **the adopt-or-discard decision is still open** (Phase 5 found `mem-fabric-local-wiki` does not beat `mem-fabric-local-ep` on retrieval, and Todd has not ruled). `FALKORDB_DATABASE` points at `mem-fabric-local-wiki` as the *interim* default in the meantime, which is safe because it is a strict superset of `-ep`, not because the decision went that way.

**2026-09-13 (Todd):** hold the 415 non-wiki singletons in a candidate area (not delete); do the Phase 1 rename; proceed with the full plan. In progress — see per-phase status below.

**Goal:** Stop episodes defining the graph's vocabulary. Derive entity nodes from the LLM Wiki's heading hierarchy, keep the hierarchy itself as structure, and use wikilinks as edges — then attach episodes onto that backbone. Separately, and first, stop discarding two-thirds of each episode's extracted reasoning at the promotion boundary.

**Why now:** Todd observed that many graph entities are irrelevant. Measured on `mem-fabric-local` (465 episodes / 701 entities / 523 `RELATES_TO`): **585 of 701 entities (83%) are mentioned exactly once**, and sampling them returns `table`, `claim`, `set -e`, `window margin`, `search icon`, `defined paths` — episode-local nouns that contribute nothing to traversal.

### What the measurements changed about the approach

Four findings, in the order they were made. Each one redirected the design, so they are recorded rather than just their conclusions.

1. **Wiki titles/links are not an entity source.** Note titles + `[[wikilink]]` targets (785 distinct terms) match **11 of 701** graph entities — 2%. The wiki's link graph is *topic*-level (`Interlock`, `EV-Charging`, `Observables 2026`), not *thing*-level. Seeding from titles alone would discard `Photoshop`, `macOS`, `GitHub`, `Mac Pro`, `Obsidian`, `Cursor`, `Anthropic`.

2. **Headings are the node source; links are edges** (Todd's correction). `WIKI/` + `REPORTS/` + `TO-RESEARCH/` carry 2,355 headings (H1 195 / H2 1,300 / H3 724 / H4 105 / H5 31) → **1,736 sections after boilerplate filtering** (`Sources` ×121, `Open Questions` ×56, `Summary / TL;DR` ×34) → 1,469 distinct labels. Edges: **2,049 structural parent→child**, plus **1,728 wikilink references** of which 93% resolve to a real note and **99.8% are anchored to a specific section**. That is ~3,700 edges over ~1,700 nodes, against today's 523 over 701.

3. **Full headings make bad node names; decompose them** (Todd's correction). A heading like `Why Gemma 4 12B is especially suitable artistically` cannot name-match an episode, which would force episode attachment onto embedding KNN and abandon Graphiti's native resolution. Decomposed (`Gemma 4 12B` (model) + `Art` (domain)) it matches directly. Verified on real headings: `Apple Vision Pro Status (May 2026)` → `Apple Vision Pro`; `3A. Install Docker Desktop` → `Docker Desktop`; `Protocol Layer: MCP + A2A` → `MCP`, `A2A`; `What to raise with Charles` → `Charles`. Numbering, dates and framing words strip cleanly.

   **Decompose from heading + section lede, not the heading alone.** Coverage of the 701 existing entities:

   | Source text | all | ≥2 mentions | ≥3 mentions |
   |---|---|---|---|
   | heading only | 16% | 36% | 57% |
   | **heading + first 25 words** | **28%** | **58%** | **80%** |
   | full section body | 34% | 62% | 86% |

   Section bodies are median 74 words (81% between 21–400), so they are also a natural chunking of the corpus — which incidentally serves the Backlog's `search_wiki` semantic-retrieval item.

4. **Episode capture is lossy at the promotion boundary, not at extraction.** `ReasoningEpisodePolicyV1` extracts `statement`, `driving_question`, `rationale`, `alternatives`, `status`, `thread_key`, and all of it is persisted (the latter fields packed into `derived_memories.reason`). But both promotion paths call `content=row["statement"]` ([promotion.py:413](../server/consolidation/promotion.py#L413), [promotion.py:583](../server/consolidation/promotion.py#L583)). Across all 465 promoted episodes: `statement` averages **188 chars**, the never-sent `reason` averages **406** — 98% carry a driving question, 98% a rationale. **~68% of extracted reasoning never reaches the graph.**

   Concretely: an episode whose `statement` ends "...an artifact of the SMB/NAS filesystem" drops a `reason` naming **QNAP** — and `QNAP TS-264` is in the `Storage-NAS` wiki note. The cross-channel link this whole milestone depends on was being severed by one field selection.

### Decisions taken (Todd, 2026-09-13)

- **Enriched episode body = `statement` + driving question + rationale. Alternatives excluded** — "options considered and rejected" reads as fact once it is in a graph.
- **Extraction model = `openai/gpt-oss-20b`** for now. See the A/B below.
- **No episode-mention threshold for wiki-supported entities.** Todd's objection: a mention threshold applied to terse summaries measures extraction failure, not relevance. Confirmed — 116/465 episodes (25%) extracted **zero** entities and 43% extracted ≤1. Crossing wiki support against recurrence:

  | | singleton (1 ep) | recurring (≥2) |
  |---|---|---|
  | in wiki | **170** | 73 |
  | not in wiki | 415 | 43 |

  A symmetric "wiki AND ≥2" rule keeps 73 of 701. The asymmetric "wiki OR ≥2" keeps 286 — the 213-entity difference is almost entirely wiki-backed singletons (`Kotlin`, `Illinois Electric Vehicle Charging Act`, `pyproject.toml`, `UCSD`, `IRS`, `Google Workspace`, `NVMe drive`, `Image Stacking`), singleton only because the episode channel dropped them.

### Model A/B (30 episodes, enriched template, 2026-09-13)

| | `openai/gpt-oss-20b` | `qwen3.5-122b-a10b` |
|---|---|---|
| Parsed | 30/30, 0 failures | ~20/30 attempted, **6 truncated** at 3,000 tok |
| Entities/episode | **4.97** | no data |
| **Zero-entity episodes** | **0 (0%)** | no data |
| In wiki prose | 50/149 (34%) | no data |
| Speed | **4.2s/ep → ~33 min for 465** | 131s/ep → **~17 h for 465** |

The 25% zero-entity rate disappears on the 20b. The 122b run was **stopped before completion at Todd's instruction**, so there is **no quality comparison between the two models** — only the throughput and truncation profile, which was already decisive (it reasons in proportion to input length, so enriched bodies make it worse). Revisit with a 6,000-token cap on ~10 episodes if extraction quality is ever suspected.

**Known limitation of the 20b result:** the "0% >4-word fragments" metric overstates quality. The junk changed shape rather than disappearing — it now emits generic single nouns (`wall`, `rumors`, `planet`, `tabs`, `vendor`, `Activities`, `staff group`) alongside good entities (`NVIDIA Spark`, `OCLP installer`, `Ars Electronica`, `Slack`). That is exactly the shape the wiki registry and stoplist are meant to catch, so it is a known input to Phase 4, not an unmeasured risk.

### Architecture

Three layers in `mem-fabric-local-wiki`:

- **`:Section`** — 1,736 nodes from the heading hierarchy, joined by 2,049 `CONTAINS` edges. Carries `wiki_path`, heading level, and the lede.
- **`:Entity`** — decomposed from heading + lede. This is the surface episodes attach to, by name, via Graphiti's native resolution.
- **`:Episodic`** — the 465 promoted episodes, replayed from the journal with enriched bodies. Unchanged 1:1 with promoted reasoning episodes, so `recall_mem`'s vector arm, `_resolve_episode_index`, `tag_projects.py`, `entity_audit.py` and ledger-replay DR all keep working.

Edges: `Section-[:CONTAINS]->Section`, `Section-[:MENTIONS]->Entity`, `Section-[:REFERENCES]->Note`, and the existing `Episodic-[:MENTIONS]->Entity` / `RELATES_TO`. Retrieval path becomes `episode → entity → section → wiki note`.

**Wiki notes are NOT ingested as episodes.** Entities are seeded directly (`EntityNode.save()` / `add_triplet`), so the episodic layer stays pure.

**On provenance:** the wiki is itself AI-generated (Todd, 2026-09-13), so this is LLM output extracting from LLM prose. The quality argument is not provenance but **redundancy** — an entity earns retention by appearing in a curated section *or* recurring across episodes, two independently-generated channels. Neither is trusted alone.

### Phases

**Phase 0 — enriched episode bodies — done**
- [x] Enriched content template — `enriched_episode_content()` in [promotion.py](../server/consolidation/promotion.py), wired into both call sites (`promote_auto_accepted`, `promote_reviewed`). Statement + driving question + rationale; alternatives excluded, parsed from `derived_memories.reason`'s `Q:`/`why:` fields; falls back to the bare statement on any unfamiliar `reason` shape (e.g. `promote_auto_accepted`'s heuristic-policy rows) rather than raising.
- [x] Reasoning policy not re-run — confirmed, this only changes what `promote_reviewed`/`rebuild_graph_from_ledger.py` send to `remember()`.
- [x] Model set to `openai/gpt-oss-20b` for the Phase 3 replay — via `CMF_LOCAL_LLM_MODEL` at invocation time, not a global `.env` change (the global default stays `qwen3.5-122b` for live production capture; see Phase 1 note below on why that distinction turned out to matter).
- [x] Unit tests — [tests/test_ms7b_enriched_content.py](../tests/test_ms7b_enriched_content.py), 9 cases (all fields, alternatives-never-included, missing/empty/malformed `reason`, question-only, and an end-to-end `promote_reviewed`/`promote_auto_accepted` check). All pass; no regressions in the existing 23 promotion tests.

**Phase 1 — graph rename — done**
- [x] `GRAPH.COPY mem-fabric-local mem-fabric-local-ep`; verified 465/701/31/523 match exactly; confirmed `BGSAVE` landed on the mounted `/data` volume before deleting the source. `mem-fabric-local` deleted.
- [x] `mem-fabric-local-restore-20260912` (0 nodes) left as-is — not explicitly authorized to remove, and harmless.
- **Found during this step, not anticipated in the plan:** live `server.mcp` processes (Claude Desktop's Cowork/Code connections) were still running against `.env`'s old `FALKORDB_DATABASE=mem-fabric-local`. Deleting that graph name meant their next call would have silently recreated an empty shell there (same "index-only ghost" mechanism ADR 0003 documented for `default_db`, just triggered by this delete instead of Graphiti's internal default) — and any real `remember()` from a live session before the fix would have started writing into that empty graph instead of the real one. Fixed by updating `.env`'s `FALKORDB_DATABASE` to `mem-fabric-local-ep` immediately. **Those already-running processes won't see this until restarted** — same restart requirement ADR 0003 already documents for this class of change.

**Phase 2 — section + entity registry builder — done**
- [x] [scripts/build_wiki_sections.py](../scripts/build_wiki_sections.py) — deterministic, zero LLM calls. Real run over `WIKI/`+`REPORTS/`+`TO-RESEARCH/`: **2,355 sections** (matches the earlier measurement exactly), **1,939 non-boilerplate**, **434 notes** (201 scanned + 233 stub targets outside scope), **1,725 wikilinks, 93% resolved**. Refinement over the exploratory measurement this plan was based on: boilerplate sections (`Sources`, `Open Questions`, ...) are kept as real `:Section` structure nodes and keep their own wikilinks (a "Sources" section is often a bibliography) — only flagged `is_boilerplate` so Phase 2b skips decomposing them. The earlier 1,736/1,728 figures had come from two differently-filtered passes over the same data; this is one consistent pass.
- [x] [scripts/build_wiki_entities.py](../scripts/build_wiki_entities.py) — batched (25 headings/call) heading+lede decomposition via `gpt-oss-20b`, text-mode + schema-in-prompt (same LM Studio reasoning-model quirk documented in `lmstudio_client.py`). Batching measured live at **~0.6–0.7s/section** (vs. ~4.2s/episode unbatched in the earlier A/B). One real bug caught by a full run and fixed: the model occasionally returns a bare string instead of `{"name","type"}`; now tolerated rather than crashing.
- **The Spark SSH tunnel dropped mid-run** (`ConnectionResetError`, ~525/1,939 sections in) — the old code only wrote output once at the end, so this lost all prior work. Fixed before doing anything else: `build()` now checkpoints to `--out` after every batch and `--resume` continues from it; `_call_model` retries a transient network error with backoff first. This was the first of three tunnel drops in this session (see below) — no longer a one-off risk to design around.
- **`--provider gemini` fallback added** (Todd, 2026-09-13, while the Spark was down): this step is pure text generation, no embeddings, so it isn't provider-locked the way seeding/replay is — reusable regardless of which model embeds the results later. Builds its own `GeminiRateLimiter` (real chain + budgets) rather than `get_default_rate_limiter()`, which is provider-aware and returns an unmetered stand-in whenever `CMF_LLM_PROVIDER=local` — correct for production, useless here. Off by default; spends Todd's real quota only when passed explicitly. **Used for real once** to unblock this step: full 1,939-section corpus in ~6 minutes, **1,345 distinct entities**, 5 stoplist hits, 131 of 500 daily calls spent (split across the two-chain models) — comfortably inside budget. Output: `imports/state/wiki_entities.json`.

**Phase 3 — seed + replay — done**
- [x] [scripts/seed_wiki_graph.py](../scripts/seed_wiki_graph.py) — full seed into `mem-fabric-local-wiki`: 434 Note / 2,355 Section / 1,345 Entity nodes, verified directly in FalkorDB.
- [x] **Root-cause diagnosis, not guesswork.** The first calibration attempt (10 episodes, `gpt-oss-20b`) hit a tunnel drop mid-run — genuinely inconclusive at the time. Rather than re-running blind, used `graphiti_core.utils.maintenance.node_operations.extract_nodes()` directly (read-only, no graph writes) to inspect the model's raw output for all 7 zero-entity episodes: it was cleanly, quickly returning `{"extracted_entities": []}` — not truncating, not erroring. Found the likely cause inside graphiti's own baked-in prompt: *"When in doubt, do not extract the entity"* — directly conflicting with this codebase's `EXTRACTION_INSTRUCTIONS` nudge, which was tuned against `qwen3.5-122b`/Gemini and never validated against `gpt-oss-20b`.
- [x] **Memory diagnosis, also not guesswork.** Three real tunnel drops traced to `sshd` never logging a close (client-side keepalive giving up, not a server crash — confirmed via `journalctl`/`dmesg`/`uptime` on the Spark itself, 25 days uptime, no reboot) plus a 15-min load average spike with three models already pinned resident (~95.6GB, swap 14/15 GB full per Alex's own status). Root cause: loading `gpt-oss-20b` cold, on top of that, had nowhere to go. Fixed by unloading `unsloth/qwen3.5-122b-a10b` (freeing 73.5GB) and warming `gpt-oss-20b` deliberately before any real run.
- [x] **Model comparison, measured in-graph, not assumed.** A calibration re-run under `gpt-oss-20b` (10 episodes) vs `qwen3.5-122b` (first 24 of the full run) on the *same real pipeline*: gpt-oss-20b — 0.3 entities/episode, 70% zero-entity, **0** `RELATES_TO` fact edges. qwen3.5-122b — 2.7 entities/episode, ~21% zero-entity, 43+ real fact edges. Not subtle; switched to 122b for the full replay despite the ~15-30x speed cost, since a fast replay with zero fact edges would have defeated the point of the migration.
- [x] **Full 465-episode replay**, `qwen3.5-122b`, batched (25/batch, timestamped progress — added to `rebuild_graph_from_ledger.py`): **455 newly promoted, 10 already-promoted (skipped, no dup), 0 failed.** 16,618s (4.6h), 35.7s/episode. Final (pre-Phase-4-merge): 2,180 entities, 809 `RELATES_TO` edges, 25.8% zero-entity rate overall (close to the pre-migration ~25% baseline for this model — the enrichment's real benefit shows up in *richness per successful episode*, not in cutting the zero-entity rate for this particular model).

**Phase 4 — sweep + re-tag — done**
- [x] **Duplicate-entity root cause found and fixed.** Browser inspection surfaced 94+ duplicate-name entity groups. Traced to `seed_wiki_graph.py` seeding entities with `group_id=""` while `add_episode()` defaults to `group_id="_"` — Graphiti's dedup search is scoped *by* `group_id`, so a wiki-seeded node was never a merge candidate regardless of name match (confirmed directly: two byte-identical `"Anthropic"` nodes, `group_id` `""` vs `"_"`). Fixed in `seed_wiki_graph.py` for future reseeds; the existing graph needed a repair, not a redo.
- [x] [scripts/sweep_wiki_graph.py](../scripts/sweep_wiki_graph.py) extended with a merge pass ahead of retention tagging (same `_norm()` used for IDF vouching, so it catches the separate non-breaking-space case too — `NVIDIA Spark` vs `NVIDIA␠Spark`). Run for real: **123 duplicate groups, 126 redundant entities merged, 788 edges redirected, zero data loss** (verified node/edge counts before/after; "Anthropic" now resolves to exactly 1 node).
- [x] Retention sweep on what's left (2,054 entities post-merge): **97 confirmed by recurrence (≥2 episodes), 624 confirmed by IDF-vouching, 16 held as candidates** — never deleted. Far below the 415-non-wiki-singleton number the plan was originally written against; the richer `qwen3.5-122b` extraction plus the merge fix meant most entities now clear one bar or the other on their own.
- [x] `tag_projects.py` re-run: 120 entity-less episodes linked directly to 23 project hubs — matches the measured 120 zero-entity-episode count exactly (independent cross-check).
- [x] `entity_audit.py` re-run: 68 entities span ≥2 projects. Read through the actual list rather than just counting it — every one at the top (Mac Pro across 4 projects, Photoshop across 4, GitHub across 4) reads as legitimate shared infrastructure per the script's own docstring standard, not sense-collapse. No entity found meaning two different things across its project list.

**Phase 5 — A/B against the old graph — done**
- [x] Ran the real MS7 instrument: `capture.py` against both graphs (30 queries × `recall_mem`/`search_wiki`/`get_context`), `answer_eval.py` generating real answers in 4 conditions via `claude -p` (240 answers total), graded by hand against `gold_needs` the same way the original MS7 verdicts were — [tests/fixtures/ms7_eval/verdicts_ms7b_phase5.json](../tests/fixtures/ms7_eval/verdicts_ms7b_phase5.json) has the full per-query notes.
- [x] **Result: `mem-fabric-local-wiki` did not beat `mem-fabric-local-ep` on this instrument.**

  | arm | `-ep` | `-wiki` |
  |---|---|---|
  | memory (recall_mem alone) | **0.70** | 0.43 |
  | wiki (search_wiki alone) | 0.73 | 0.73 *(graph-independent by construction — reads LLM_Wiki files directly, never touches FalkorDB; identical score is the expected sanity check, not a coincidence)* |
  | both (get_context fusion) | **1.40** | 1.33 |

  The memory arm is the real story: `-wiki` is notably *weaker* despite objectively richer graph structure (2.7 entities/episode vs `-ep`'s pre-enrichment baseline, 809 vs 523 `RELATES_TO` edges). More graph structure did not translate into better ranked retrieval — `recall_mem`'s RRF fusion (`_rrf_merge`, the per-episode cap, the vector-arm top-6 cutoff) was tuned against `-ep`'s shape, and a denser, differently-resolved entity graph changes which facts get surfaced without those tuning constants having been revisited. The `both` arm is close but `-wiki` still trails, driven by concrete misses: **C9** (build-sequencing + differentiation) — `-ep`'s fusion produces a full match by combining two facts neither single arm surfaced alone (exactly the behavior the C-group exists to test); `-wiki`'s fusion doesn't replicate it, scoring 0. **C1** (Interlock registry) and **A10** (filing status) show the same pattern. Genuine `-wiki` wins exist too — **C2** (partially recovers the friend's-Spark fact `-ep` misses entirely) and **C3** (surfaces specific camera-gear detail `-ep`'s fusion leaves generic) — so this isn't one-sided, but the aggregate doesn't clear the bar.
- **Grading caveat, stated plainly:** single-pass, by me, not independently cross-checked the way the original MS7 draft was reviewed by Todd before being treated as final. A few borderline calls (partial-credit judgment on incomplete-but-not-wrong answers) could each move the mean by ~0.03; the gap between the two `both` means (0.07) is within range of that noise. The **memory-arm gap (0.27) is larger and reads as a real effect**, not grading noise.

### Acceptance tests

1. ✅ Enriched bodies measurably raise entity yield on the real corpus: 2.55 entities/episode across all 465 (2.7 for the `qwen3.5-122b` cohort specifically) — the A/B's 4.97 prediction was on 30 episodes via a simpler prompt than graphiti's real extraction path; the real-pipeline number is lower but the direction holds. Zero-entity rate (25.8%) did **not** improve over the pre-migration baseline for this model — recorded honestly in Phase 3, not glossed over.
2. ✅ `mem-fabric-local-ep` untouched since Phase 1 — never re-opened by any Phase 2–5 script (all of which target `mem-fabric-local-wiki` explicitly).
3. ✅ Phase 2 builders re-run clean; `build_wiki_sections.py` is zero-LLM and deterministic, `build_wiki_entities.py` checkpoints/resumes.
4. ✅ Duplicate-entity rate measured directly (8.6%, 188 entities) and fixed via the Phase 4 merge — not just measured, corrected.
5. ❌ **MS7 eval on `-wiki` did not reach `-ep`** — the one acceptance test that didn't clear, and the one the exit gate below turns on.

### Exit gate

*"Does a wiki-structured entity layer plus enriched episode bodies retrieve better than the episode-derived graph on the same graded queries — and is the entity set one Todd recognises as relevant? If the answer is only 'enriched bodies helped,' that is a real result: ship Phase 0 to the existing graph and discard the rest."*

**That is where this landed.** The entity set is real and recognizable (Phase 4's audit confirmed no sense-collapse), but retrieval quality on the graded instrument did not improve — if anything, the memory arm alone measurably regressed. Per the exit gate's own pre-committed criterion, the honest recommendation is: **adopt Phase 0 (enriched episode bodies) on `mem-fabric-local-ep` directly** — that part is model-agnostic, already validated end-to-end in Phase 3's real replay, and costs nothing to keep — **and treat the wiki-structured entity/section layer as a documented, working, but not-yet-adopted experiment.** Whether to pursue tuning `recall_mem`'s fusion constants against the new graph shape (a real, separate follow-on, not a quick fix) or to set `mem-fabric-local-wiki` aside as-is is Todd's call, not a default this doc should assume.

**Effort:** 2–3 sessions estimated; actual was closer to 4, almost entirely in Phase 3's diagnosis work (two real infrastructure failures — a flaky Spark tunnel, a memory-pressure model-eviction issue — and one real architecture bug — the `group_id` mismatch) rather than in the phases themselves.
**Risk:** Realized, not just estimated. The dedup-search-timeout risk flagged going in never manifested (0 failures across the full 465-episode replay); the risks that did bite weren't on the original list, which is itself a useful note for scoping the next experiment like this one.

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

### Auth hardening (deferred out of MS6c, 2026-09-16)

Raised in review on [PR #6](https://github.com/tmargolis/context-memory-fabric/pull/6) and consciously merged without fixing (Todd, 2026-09-16) — the OAuth layer works and these are hardening, not blockers, on a single-user personal server. The design itself reviewed clean: PKCE correct, codes single-use, refresh tokens rotate on exchange, expiry enforced on both token types, consent password compared with `secrets.compare_digest`.

- [ ] **The auth layer has no test coverage at all.** No test references `OAuthStore`, `CMFOAuthProvider`, or `BearerTokenAuthMiddleware`; the suite's 383 passing tests do not execute one line of `server/core/oauth_provider.py`, `oauth_store.py`, or `http_auth.py`. For the code standing between the open internet and `remember`/`edit_memory`/`import_chatgpt_exports`, that is the gap worth closing first — the flow has enough state transitions (consent → code → token → refresh → rotate) that a regression would be silent and would present as a client-side bug. Highest value: a full round-trip test through the provider, plus the refusal cases (wrong password, expired code, reused code, wrong client_id on exchange).
- [ ] **`exchange_refresh_token` drops `resource`.** `exchange_authorization_code` persists `authorization_code.resource` (`server/core/oauth_provider.py:161`); the refresh path hardcodes `None` (`:202`), so a token's audience binding silently disappears the first time it refreshes. Not exploitable today — the SDK's `ProviderTokenVerifier` only calls `load_access_token` and never checks `resource` — but it becomes a real bug the moment RFC 8707 audience validation is enabled, and it would surface ~30 days after a client first connects. `RefreshToken` needs to carry the resource forward for this to be fixable at all.
- [ ] **Tokens are stored in plaintext.** `oauth_access_tokens.token` / `oauth_refresh_tokens.token` are raw values used as PRIMARY KEY, in the same `journal.db` the journal writes. A stray copy or backup is working credentials for 30 and 180 days. Storing SHA-256 and looking up by hash is one line per save/get pair.
- [ ] **No rate limiting on the consent password**, which is the entire security boundary by design, on an endpoint reachable by anyone who finds the URL. `openssl rand -hex 16` as documented makes brute force infeasible — so the real action is making CLIENTS.md say that recommendation is load-bearing rather than advisory.
- [ ] **Minor.** `http_auth.py`'s docstring says the SDK's OAuth machinery is "deliberately not" used, which the same PR reversed — a reader hitting that file first concludes OAuth was rejected. `_codes` is pruned only when an entry is read, so approved-but-never-exchanged codes persist for the process lifetime. `secrets.compare_digest` raises `TypeError` on a non-ASCII password rather than cleanly denying.

### Post-apply staleness (found 2026-09-16, fixed same day, MS6d)

`apply_wiki_proposal` is the first tool that writes into `LLM_WIKI_PATH`, but nothing downstream that assumes the corpus is static was getting invalidated when it ran: `search_wiki`'s filesystem-scan cache, and MS7b's offline-built wiki-derived entity/section graph (`mem-fabric-local-wiki`, parked but still the interim `FALKORDB_DATABASE`). Confirmed concretely, not just theoretically — the real apply of `prop_20260916_125736_a33f295a` created `WIKI/projects/Context-Memory-Fabric/Context-Layers-as-the-Next-Frontier.md` (commit `6dfce163`) and it did not surface via `search_wiki` until this fix.

- [x] `search_wiki`'s side fixed: `apply_wiki_proposal` now calls `invalidate_corpus_cache()` (`server/wiki.py`) on every real (non-dry-run) apply — lazy invalidation, drops the cached engine/assets rather than forcing an immediate rescan, since applies are rare and a rescan can be non-trivial cost. Tested (`tests/test_ms6d_proposal_review.py::TestPostApplyStaleness`, 3 cases: pure invalidation, real-apply wiring, dry-run does *not* invalidate). **Live server restarted 2026-09-16** to pick this up — confirmed live via subsequent `search_wiki` calls from Code mode and Cowork.
- [ ] MS7b's wiki-derived graph is parked, not live — still unaddressed, but no live consumer depends on it today. Revisit only if/when MS7b is adopted.

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
