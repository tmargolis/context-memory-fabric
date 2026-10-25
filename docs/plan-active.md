# Implementation Plan — Active milestones

The milestones still to do, in execution order. Index and decisions log: [IMPLEMENTATION-PLAN.md](IMPLEMENTATION-PLAN.md). Completed milestones: [plan-history.md](plan-history.md).

**MS10a (second-user readiness) closed 2026-10-08**; its record is in [plan-history.md](plan-history.md#ms10a--second-user-readiness-2026-10-08). **B06 closed 2026-10-12** ([record](plan-history.md#b06--recurring-review-operations-and-outstanding-promotions-closed-2026-10-12)); review continues under [standing operations](#recurring-review-operations). Remaining work is in the [Backlog](#backlog), followed by [MS10](#ms10--distribution-and-ecosystem). **MS9 (graph quality for retrieval) closed 2026-10-07**; its record is in [plan-history.md](plan-history.md#ms9--graph-quality-for-retrieval-2026-09-30--10-07), and what it left open is in the [Backlog](plan-history.md#merge-related-episodes-career-tailoring-threads-deferred-out-of-ms9-2026-10-07).

**MS4b and MS4b-antigravity closed 2026-09-28**, and their record is in [plan-history.md](plan-history.md#ms4b--claude-code-transcript-adapter-build-backfill-re-derivation-review-2026-09-18--09-22). **MS4d (Codex) closed 2026-09-30 (Gemini CLI dropped per user steer)**, and its record is in [plan-history.md](plan-history.md#ms4d--codex-transcript-adapter-and-ongoing-capture-2026-09-30). **MS4c (OpenClaw) remains on hold (User, 2026-09-28).** Its plan is kept intact below but not scheduled. What the active coding adapters keep capturing goes to the recurring review pass ([Backlog](#backlog)).

**MS4e (entity extraction quality) closed 2026-09-28**; its record is in [plan-history.md](plan-history.md#ms4e--entity-extraction-quality-forward-only-2026-09-28). `typed-recall` + debris filter is the production extraction profile.

**MS5 (knowledge-provider generalization) closed 2026-09-28**; its record is in [plan-history.md](plan-history.md#ms5--knowledge-provider-generalization-2026-09-28). What it deliberately left out is [MS11](#ms11--knowledge-provider-follow-ons-from-ms5).

**Claude surface provenance + Cowork capture (unplanned, found in MS9 Phase 5) closed 2026-10-03**; its record is in [plan-history.md](plan-history.md#claude-surface-provenance-and-cowork-capture-found-2026-10-02-closed-2026-10-03). Capture now separates the Claude CLI (`claude_code`), Desktop Code tab (`claude_desktop_code`) and Cowork (`claude_cowork`). What it left open is in the [Backlog](plan-history.md#claude-surface-provenance-lost-transcripts-and-cowork-capture-found-2026-10-02-ms9-phase-5).

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

**Status:** Live Notion read-only retrieval closed 2026-10-21; [implementation, verification, and known limitations](plan-history.md#ms11--live-notion-knowledge-provider-closed-2026-10-21) moved to history. Notion staging/review passed live fixture acceptance; guarded application is implemented off by default and passed 214 offline regression tests and awaits review plus separately authorized write acceptance. Other follow-ons remain open. MS11 as a whole remains open.

**Goal:** Take the second knowledge provider from proof to everyday use, and add the next provider when one is wanted.

### Tasks

- [x] **Live Notion knowledge provider** — completed 2026-10-21. Actual CMF MCP retrieval, nested text, provenance, unchanged-page cache reuse, and edit-triggered cache invalidation verified. [Completed record and caveats](plan-history.md#ms11--live-notion-knowledge-provider-closed-2026-10-21).
- [ ] **Notion `propose_change` — after read-only retrieval** (User, 2026-10-21). [Design approved](notion-proposals-design.md) 2026-10-21. First bounded phase implemented: provider-aware envelope, bounded live block inspection, fresh single-paragraph proposal staging, digest-bound manual review, atomic local records, and explicit unsupported application. Legacy wiki JSON/callers remain compatible; automatic review/apply and consolidation exclude Notion proposals. Offline verification recorded in the design. Live fixture staging/review passed 2026-10-21 through refreshed MCP tools: nested paragraph, captured ancestry, CMF-sourced electrician shortlist, exact diff, digest-bound approval, and unsupported apply confirmed (proposal `notion-a4f30f46-6a65-440a-82b6-440d41ca6749`). User then authorized guarded-apply implementation. Off-by-default writer, dry run, lock, durable attempts, conflict checks, verification, cache generations and explicit reconciliation are implemented; the final isolated regression run passed 214 tests (four live tests deselected), including MCP contracts, wiki compatibility, automatic-review exclusion, concurrency and uncertain-outcome recovery. Review and live write acceptance remain open. No write token/configuration changes, additional service restarts, or Notion mutations were performed. Guarded live write acceptance remains open.
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

Execution groups reorganized 2026-10-09. Rank and close **whole groups**, not individual checkboxes. Existing findings, completed work, decisions, and deferrals are preserved below; the original section headings remain linkable. Grouping is not implementation approval or evidence that an old operational count is still current.

B01–B04, the first low-Spark shortlist, were closed 2026-10-10 and moved to plan-history.md. B15 may be a prerequisite for live checks. No group is complete merely because its code is written when its scope includes migration, deployment, or operational verification.

| Group | Complete scope | Planning disposition |
|---|---|---|
| B01 | Tracked-file privacy | **Closed 2026-10-10** → [plan-history.md](plan-history.md#b01--tracked-file-privacy-closed-2026-10-10) |
| B02 | Manual review merge tooling | **Closed 2026-10-10** → [plan-history.md](plan-history.md#b02--manual-review-merge-tooling-closed-2026-10-10) |
| B03 | Wiki date provenance and use | **Closed 2026-10-10** → [plan-history.md](plan-history.md#b03--wiki-date-provenance-and-use-closed-2026-10-10) |
| B04 | Client correction and transport reliability | **Closed 2026-10-10** → [plan-history.md](plan-history.md#b04--client-correction-and-transport-reliability-closed-2026-10-10) |
| B05 | Codex hook lifecycle verification | **Closed 2026-10-12** → [plan-history.md](plan-history.md#b05--codex-hook-lifecycle-verification-closed-2026-10-12) |
| B06 | Recurring review operations and outstanding promotions | **Closed 2026-10-12** → [plan-history.md](plan-history.md#b06--recurring-review-operations-and-outstanding-promotions-closed-2026-10-12) |
| [B07](#b07--promotion-reliability-and-identity) | Promotion reliability and identity | Uncertain reproduction/compute |
| B08 | Project organization and retrieval dilution | **Closed 2026-10-13** → [plan-history.md](plan-history.md#b08--project-organization-and-retrieval-dilution-closed-2026-10-13) |
| [B09](#b09--entity-extraction-quality-and-residual-graph-cleanup) | Entity extraction quality and residual graph cleanup | Reconcile completed work; remaining scope uncertain |
| B10 | Extraction policy 1.7 | **Closed 2026-10-12** → [plan-history.md](plan-history.md#b10--extraction-policy-17-closed-2026-10-12) |
| [B11](#b11--context-retrieval-and-assembly) | Context retrieval and assembly | Broad retrieval package; embedding/evaluation |
| B12 | Capture coverage and historical source recovery | **Closed 2026-10-13** → [plan-history.md](plan-history.md#b12--capture-coverage-and-historical-source-recovery-closed-2026-10-13) |
| [B13](#b13--historical-graph-rebuild) | Historical graph rebuild | Paused; many Spark hours |
| [B14](#b14--oauth-audience-preservation) | OAuth audience preservation | User-deferred |
| B15 | Test graph compatibility | **Closed 2026-10-13** → [plan-history.md](plan-history.md#b15--test-graph-compatibility-closed-2026-10-13) |
| B16 | Project-aware retrieval | **Closed 2026-10-15** → [plan-history.md](plan-history.md#b16--project-aware-retrieval-closed-2026-10-15) |
| B17 | Project taxonomy consolidation | **Closed 2026-10-14** → [plan-history.md](plan-history.md#b17--project-taxonomy-consolidation-closed-2026-10-14) |
| [B18](#b18--poller-consolidation-overflows-the-local-models-context) | Poller consolidation overflows the local model's context | Open; small code fix, short Spark check |

### Recurring review operations

B06’s finite activation/review package is [closed 2026-10-12](plan-history.md#b06--recurring-review-operations-and-outstanding-promotions-closed-2026-10-12). Nightly poller-output review and the user’s confirmation continue as standing operations. Use `uv run python -m server.review.cli stats`, `scripts/review_priority_report.py [--harness …]`, and [NIGHTLY-REVIEW.md](NIGHTLY-REVIEW.md).

At closure, five older holds remain in `rr_20261011_041319_c2ade2` (EP4, EP5, EP11, EP22) and `rr_20261012_041323_8e2fb5` (EP5). The newer EP10 was rejected. Of the six recently approved episodes, EP3 and EP5 in `rr_20261012_205058_b8b349` have succeeded; EP8, EP9, EP11 in that run and corrected EP2 in `rr_20261012_211841_a6ba35` remain queued/processing. Check their detached promotion logs under `imports/review/` before retrying; do not start duplicate promotions.

The JSON-link formatter becomes live after the next approved MCP restart. Temporary old-path proposal symlinks stay until all affected processes have restarted on the renamed paths. Other scheduled harnesses and unattended recurrence can be checked when needed; they are outside the accepted B06 closure scope.

### B07 — Promotion reliability and identity

Request tracing/root cause for the silent hang and the cause of double promotion belong together as promotion reliability work. Preserve the completed duplicate-fold record. Finish the outstanding diagnoses and any necessary prevention/verification; instrumentation alone does not close the hang. Spark usage is uncertain until reproduction is understood.

#### Promotion hang with no error output (found 2026-09-21, reviewing conversation `8406b5c3`)

`promote_approved_episodes` hung with **zero log output** on thread `ms7b-implementation-priority`. It reproduced twice across fresh server restarts, with the Spark tunnel healthy and `lms ps` showing the model `IDLE`, so the request apparently never reached LM Studio. A clean retry on 2026-09-22 promoted it ([plan-history.md](plan-history.md#backlog--closed-items)). Restarting the CMF server abandons a stuck coroutine safely.

- [ ] **Not root-caused.** Content length isn't the explanation. Reproduce with request-level tracing (log immediately before the `openai_generic_client` call, not just around it) to tell whether the hang is in Graphiti's pre-processing or in the HTTP call to LM Studio.

#### Double-promoted memories in `mem-fabric-local` (found 2026-10-02, provenance step 5a)

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

### B09 — Entity extraction quality and residual graph cleanup

Keep the extraction causes and resulting cleanup together to avoid repeatedly repairing the same artifacts. Reconcile the September findings with completed MS9 work, identify the remaining population, choose the extraction approach, and validate any approved repair. Historical counts are not a current deletion list. Compute and scope remain uncertain; no bulk cleanup is authorized by this grouping.

#### Wiki entity-extraction quality (found 2026-09-17, closing MS7b)

Surfaced while visually inspecting the pruned `mem-fabric-local` graph in FalkorDB Browser — a real gap in `build_wiki_entities.py`'s heading+lede decomposition, not fixable by editing the graph directly:

- [ ] **Topic-level entities never form when no section heading names the topic.** `WIKI/art-projects/Cityscapes/Cityscape-View the shadows.md` — all 13 section headings are camera-technique-specific (`TS-E Mechanical Setup`, `Shift-Stitch Configuration`, `Post-Processing Pipeline (Photoshop / ACR)`, ...); none contains the word "Cityscape," so the note never links to the `Cityscapes`/`cityscape` entities that exist from other notes in the same folder. Root cause: Phase 2's original finding that note titles match only 2% of entities (documented in [MS7b](plan-history.md#ms7b--wiki-derived-entity-layer--enriched-episode-bodies-experiment-2026-09-13)) meant titles were deliberately excluded as a decomposition source — correct call in aggregate, but it leaves notes like this one topic-orphaned. Compounding: `cityscape` (domain) and `Cityscapes` (project) are themselves near-duplicates Phase 4's merge pass never caught, since `_norm()` doesn't handle singular/plural.
- [ ] **Per-section decomposition can attribute a concept to the wrong section, fragmenting it.** Same note: `Tilt` and `Shift` were both extracted from the section titled **"Zero/Static Configuration"** — specifically the section about using *neither* — while the actual `Tilt Configuration` section produced `Scheimpflug` instead, and a third section produced `Tilt-Shift` as a separate tool entity. Three sections, three fragments of one lens concept, none cross-linked.
- **Options, not yet chosen between:** (a) re-run `build_wiki_entities.py` with the note title/folder path added to each section's decomposition context; (b) extend Phase 4's merge pass beyond literal near-duplicates to catch singular/plural and compound-vs-parts cases; (c) accept it as a known cost of this approach and fix only manually, case by case. Whichever is chosen, `mem-fabric-local`'s current ~1,219 entities mentioned by a note but by no episode/fact/project were deliberately left unpruned specifically so this evidence isn't destroyed before a fix is decided.

#### Entity/edge cleanup pass for the CMF graph (found 2026-09-20, reviewing conversation b23f6f7d)

**Status reconciliation (2026-10-09):** The findings below are the original September record. The claim that no candidate list exists is superseded by [MS9 tasks 1, 1b, 2 and 2b](plan-history.md#ms9--graph-quality-for-retrieval-2026-09-30--10-07): candidate reports, 125 duplicate merges, and the legacy-episode cleanup were completed, with the subsequent damage repaired. Do not repeat that completed pass. Establish which extraction-quality findings remain before scheduling another cleanup; the original checkboxes below are retained as historical findings, not a fresh execution checklist.

Surfaced visually inspecting `mem-fabric-local` in FalkorDB Browser after promoting the "review by conversation" batch's approved episodes — the same graph-quality question the item above raises for wiki-derived entities, but from the episode-extraction side and scoped as an actual cleanup pass rather than a root-cause fix:

- [ ] **Near-duplicate entities from qwen-122b extraction** (e.g. "MemPalace-like memory substrate layer" vs. "memory palace layer", "Obsidian URI schemes" vs. "Obsidian") need merging, not just cosmetic renaming. Mechanically: redirect every `MENTIONS`/`RELATES_TO`/`IN_PROJECT` edge from the duplicate onto the canonical node (`MATCH (dup)<-[r:MENTIONS]-(ep) MERGE (ep)-[:MENTIONS]->(canonical) DELETE r`, same pattern each direction/edge type), then delete the duplicate. **Not a bare `MERGE`-and-delete**: graphiti's edges carry real content (`fact` text, embeddings, `valid_at`/`created_at`) that a fresh bare edge won't have — properties must be copied onto the new edge first, or the redirect silently drops the fact each edge represents. No candidate list built yet.
- [ ] **Confirmed unsafe to judge by eyeballing the graph.** Two entities that looked orphaned by visual inspection (`100MB limit`, `120-second OpenClaw timeouts`) both turned out to be real, correctly-connected data on closer query — `100MB limit` is a genuine fact from an unrelated Obsidian-project episode (`gemini-obsidian-009`) that surfaces in CMF's view only because it shares the `GitHub` entity (projects are many-to-many by `tag_projects.py`'s own design); `120-second OpenClaw timeouts` had two mentioning episodes, one from this very CMF batch, just not visible in a dense 127-node force layout. Any cleanup pass needs to query connectivity directly, not read the rendered graph.
- [ ] **The real candidate set is graph-wide zero-`MENTIONS` entities — measured at 1,219**, which is very likely the same population the "Wiki entity-extraction quality" item above already names (`~1,219 entities mentioned by a note but by no episode/fact/project`) — worth confirming they're the same set before treating this as two separate backlog items. Even a zero-`MENTIONS` entity can carry real `RELATES_TO` fact text (as `100MB limit` shows) — "safe to delete" here means "won't break graph structure or dangling references," not "zero information loss." Needs a categorization pass (genuine standalone facts vs. junk — config values, restated instructions, extraction artifacts) before deleting anything, not a bulk sweep.
- **Planned in [MS9 — Graph quality for retrieval](plan-history.md#ms9--graph-quality-for-retrieval-2026-09-30--10-07)** (2026-09-29): the merge tool, the candidate report and the wiki-only entity categorization live there.
- **Forward fix:** [MS4e](plan-history.md#ms4e--entity-extraction-quality-forward-only-2026-09-28) stops new noise at extraction time. Its optional Phase 5 report, this item's candidate list, moved to MS9 task 1b.
- **Relates to:** [Wiki entity-extraction quality](#wiki-entity-extraction-quality-found-2026-09-17-closing-ms7b) (root-cause side of the same noise) and [Project nodes vs. entity property](plan-history.md#project-nodes-vs-entity-property-open-since-before-ms7b--decided-and-applied-2026-10-13) (paused partly *because of* this same noise; decided 2026-10-13).

### B11 — Context retrieval and assembly

Keep all assembly refinements together: semantic wiki retrieval, conflict/staleness signals, omissions, intent and time modes, templates, token budgets, explanations, reranking, entity summaries/history if adopted, expansion snippets, and residual eval cases. B03 supplies note dates; [B16](plan-history.md#b16--project-aware-retrieval-closed-2026-10-15) delivered the `recall_mem` project boost, and its wiki-side result and expansion snippets now live here. Deferred/conditional options keep their existing status. This is a broad group with corpus embedding and evaluation costs, not a small zero-Spark task.

#### Assembly refinements (deferred out of MS7)

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

#### Note-to-note links in retrieval (User, 2026-10-10)

The graph holds every wiki `[[link]]` as a `(:Note)-[:REFERENCES]->(:Note)` edge (1,144 between real notes, plus 393 into the 233 stub notes that stand for linked `RAW/` sources), but no retrieval code reads them: cheap-linking expansion follows only `MENTIONS` through entities. `search_wiki` does read `RAW/` files directly, so the sources aren't invisible, only unconnected.

- [ ] **Investigate whether link expansion improves retrieval.** For a retrieved note, also offer the notes it links to (sources it cites) and the notes linking to it, ranked and capped like the entity expansion in `server/retrieval_expansion.py`. Measure with the MS7 eval harness (and MS8 replay) against the current baseline before keeping it; watch context size, since hub notes (index, timelines) link widely.
- **Prerequisite done 2026-10-10:** 27 stub paths left stale by the `RAW/RESEARCH/Household` → `RAW/Household` move were repaired (`scripts/sweep_wiki_graph.py --repath-notes`, mapping in `imports/state/note_repath_20261011T034013Z.json`). Run the sweep with `--repath-notes` after future folder moves. One orphan Note (`TO-RESEARCH/…ChatGPT Work.md`, file deleted 2026-09-13, no edges) remains.

#### Project-aware wiki ranking (from B16, dropped 2026-10-15)

B16 tested moving wiki results from the inferred project to the front, the wiki-side twin of the `recall_mem` boost. **Dropped (User, 2026-10-15); recorded here so it isn't re-run blind.**

- **Result** (`scripts/experiment_wiki_project_boost.py`, 40 cases with gold wiki notes, A-then-E guesses, 2026-10-14, `imports/replay/b16-wiki-boost-20261014.json`): Wiki@8 unchanged at 0.875, wiki MRR 0.690 → 0.708 (oracle 0.750); improved C2, C8, C17, C20, regressed B5. The gain is small because lexical `search_wiki` already ranks most gold notes high.
- **Would misfire on `ai-research` queries:** E guesses `interlock` or `openclaw` for the six `ai-research` B cases (one episode is too thin a centroid).
- Revisit only if `search_wiki` semantic retrieval (above) widens the candidate set enough for ordering to matter. Notes now carry a `project:` frontmatter line and `Note.project`, so the input is ready.

#### Expansion snippets (from B16)

- [ ] **Pull the matching section, not the opening sentence.** `get_context`'s episode→note expansion attaches only a note's opening sentence; pulling the section that best matches the query would make expanded notes more useful.

### B13 — Historical graph rebuild

Optional and paused. Preserve the existing checkpoint and original scope; do not resume merely because the backlog is regrouped. Many Spark hours, so excluded from the current low-compute shortlist.

#### Phase 6: full ledger re-ingest into a fresh graph (paused, carried out of MS9, 2026-10-07)

Optional post-MS9: rebuild from transcripts using the current extraction policies (`typed-recall` + debris filter) across all historical episodes. **Paused at 432 of 1,071 episodes (40.3%).** It was paused on 2026-10-05 10:55 CDT to free Spark for Public Preview Phase 2 grading. The isolated scratch graph `mem-fabric-rebuild-scratch` holds 432 committed episodes, 1,017 nodes, 2,756 edges and 721 facts, and can resume immediately with `scripts/rebuild_graph_from_ledger.py` (replay into a new graph name, or everything is skipped). Needs Spark for many hours and takes `spark_job.lock`.

### B14 — OAuth audience preservation

Deliberately deferred by the user. Retain the completed hardening context and the remaining refresh-token resource issue together. No Spark needed, but low compute does not override the explicit deferral or make this a current high-priority selection.

#### Auth hardening (deferred out of MS6c, 2026-09-16)

Raised in review on [PR #6](https://github.com/username/context-memory-fabric/pull/6) and consciously merged without fixing (User, 2026-09-16) — the OAuth layer works and these are hardening, not blockers, on a single-user personal server. The design itself reviewed clean: PKCE correct, codes single-use, refresh tokens rotate on exchange, expiry enforced on both token types, consent password compared with `secrets.compare_digest`. Four of the five items were closed 2026-09-28 ([plan-history.md](plan-history.md#auth-hardening-deferred-out-of-ms6c-2026-09-16-closed-2026-09-28)); the one left is deliberately deferred.

- [ ] **`exchange_refresh_token` drops `resource`.** `exchange_authorization_code` persists `authorization_code.resource` (`server/core/oauth_provider.py:161`); the refresh path hardcodes `None` (`:202`), so a token's audience binding silently disappears the first time it refreshes. Not exploitable today — the SDK's `ProviderTokenVerifier` only calls `load_access_token` and never checks `resource` — but it becomes a real bug the moment RFC 8707 audience validation is enabled, and it would surface ~30 days after a client first connects. `RefreshToken` needs to carry the resource forward for this to be fixable at all. **Deferred (User, 2026-09-28):** `resource` names which server a token was issued for, not which client or IP holds it, so this only bites with more than one server behind the same auth *and* audience validation turned on. Neither applies to this single-server deployment.


### B18 — Poller consolidation overflows the local model's context

Found 2026-10-25 in the stack's logs while building v0.1.1 (CMF management's errors-only filter now shows these lines). Every poller cycle, `cmf-poller` logs `Consolidation window failed for …` with `Error code: 400 … n_keep: ~12–16k >= n_ctx: 8192` for the same 8 Codex conversations. The consolidation prompt built in `server/adapters/codex/worker.py` (around line 222) is larger than the 8,192-token context the Spark model is loaded with, so those conversations never consolidate, and the same calls fail again each cycle.

- [ ] **Cap the window by tokens.** Measure the consolidation prompt before sending it and split or trim the window to fit the model's context, with room left for the reply. Read the limit from settings (or from the server's model info where it reports it), not a constant, since another user's local model may be smaller still.
- [ ] **Stop retrying the same failure every cycle.** A window that overflows after splitting should be recorded once and skipped, not resent each poll.
- [ ] **Check the other adapters** (Claude Code, Cowork, Antigravity) for the same unbounded window.
- [ ] **Optional, operational:** reload the Spark model with a larger context (the 2026-09-21 merge overflow ran it at 32,768). It helps this machine only, so it doesn't replace the cap.
- [ ] **Verify:** the 8 conversations consolidate on the next cycle, and `cmf logs cmf-poller --errors` shows no `n_ctx` lines.

---

## Two standing notes

- **MS6 is the differentiation milestone.** *"Why does the system believe this?"* is the capability competitors do not offer. It should not slip indefinitely behind adapter work.
- **MS7 is done** (2026-09-10). The answer-quality eval (`tests/fixtures/ms7_eval/`, Artifact `ms7-answer-grader`) is the reusable instrument — re-run `capture.py` + `answer_eval.py` after any retrieval or assembly change.
