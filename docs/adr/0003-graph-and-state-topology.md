# ADR 0003: FalkorDB graph topology and configuration

**Status:** Accepted and implemented (2026-09-03)
**Date:** 2026-09-03

## Context

By 2026-09-03, three FalkorDB graphs existed under one FalkorDB instance:

| Graph | Episodic count | Origin |
|---|---:|---|
| `default_db` | 86 | Markdown-summary importer (`import_memories`), plus accumulated test-suite pollution |
| `memory-fabric` | 57 | Verified production import of native ChatGPT export JSON (`import_chatgpt_exports`), idempotency-checked, 1-to-1 registry/graph mapping |
| `cmf_chatgpt_000` | 20 | An earlier validation run of the same native importer; a strict subset of `memory-fabric`'s 57 |

`server/memory.py` resolved the target graph as `os.getenv("FALKORDB_DATABASE", "default_db")` — a silent fallback. `FALKORDB_DATABASE` was set in no `.env`, `.env.example`, `SETUP.md`, or `CLIENTS.md` at any point before this ADR. The consequence: every MCP client (Claude Desktop included) read and wrote `default_db`, while the verified, idempotency-checked production import lived in `memory-fabric` — invisible to the running server.

Separately, `default_db` had accumulated duplicate test fixtures (`phase1_step6_atlas_test_memory` and `step6b_orion_sqlite_decision`, each written three times) from `tests/test_step6_mcp_tools.py::test_remember_and_recall_tools` and `tests/test_step6b_proposals.py`, which call the real `remember`/`propose_wiki_update` MCP tools against whatever graph the environment resolved — there was no test/production graph separation.

## Decision

1. **`memory-fabric` is the production graph**, pinned explicitly via `FALKORDB_DATABASE=memory-fabric` in `.env` and documented as a required variable with no default (`server/memory.py`'s `resolve_target_database()` now raises `MissingGraphConfigurationError` rather than falling back to any graph name).
2. **`default_db` and `cmf_chatgpt_000` were snapshotted and deleted**, not merely frozen. Recoverability was verified before deletion: `imports/results/*_committed.json` retain every parsed candidate's full text, category, `reference_time`, `date_precision`, `section_heading`, and fingerprint — nothing is lost, since Milestone 2 re-derives `default_db`'s content through the journal regardless. Snapshots were still written to `imports/state/graph_snapshot_{graph}_{timestamp}.json` before deletion as a zero-cost safety margin.
3. **Tests target a dedicated `cmf_test` graph**, enforced by `tests/conftest.py`, which sets `FALKORDB_DATABASE=cmf_test` in `os.environ` before any `server.*` module is imported (relying on `python-dotenv`'s `load_dotenv(override=False)` default not clobbering an already-set variable) and refuses to run at all if `.env` itself declares `FALKORDB_DATABASE=cmf_test` for production. A session-scoped autouse fixture additionally snapshots the production graph's node/edge counts before the suite and asserts they are unchanged after — best-effort, skipping silently if FalkorDB is unreachable.
4. One test (`tests/test_step8_edit_memory.py::test_edit_memory_dry_run`) was found to depend on content ("C7 right transverse process fracture") that existed only because it happened to be present in whatever graph the suite was pointed at — real personal medical history serving as an unintentional test fixture. Fixed by seeding a synthetic, clearly-labeled fixture episode in the test itself, matching the existing self-contained pattern used by `test_remember_and_recall_tools`.

## A discovered non-issue: `graphiti-core` recreates an empty `default_db`

During verification, `default_db` reappeared in `GRAPH.LIST` after a test run, with 0 nodes and 0 edges. This is not a regression of the fix above. `graphiti_core`'s `FalkorDriver.clone()` (used internally for group-scoped operations, `default_group_id = '_'`) constructs a fresh `FalkorDriver(falkor_db=self.client)` with no explicit `database=` argument, falling back to that class's own hardcoded constructor default of `'default_db'` (`graphiti_core/driver/falkordb_driver.py:140`). That new driver's `__init__` schedules `build_indices_and_constraints()`, which creates an empty graph shell purely to hold index/constraint metadata — no episode data. This is an internal implementation detail of the installed `graphiti-core` version, external to CMF's own configuration, and cannot be suppressed by `resolve_target_database()`. It is also precisely what the existing "refuse to write into protected graph `default_db`" check in `server/chatgpt_export_parser.py:1494` anticipates. Treat an empty `default_db` reappearing as expected and harmless; treat a `default_db` with episode data as a real bug (something wrote to it directly, bypassing configuration).

## Consequences

- Claude Desktop (and every other MCP client) must be restarted after this change to pick up the new `.env` value, since `uv run --directory <project>` loads `.env` from the project root at process start.
- Milestone 2's backfill task gained a concrete, non-speculative input: the 86 `default_db` episodes are re-derived from `imports/results/*_committed.json`, not from the graph (which no longer exists).
- Any future graph-name fallback default reintroduced in `server/memory.py` should be treated as a regression of this ADR, not a convenience.

## Alternatives considered

- **Merge `default_db`'s content into `memory-fabric` immediately** (plan option B). Rejected: merges pre-journal data carrying a known parser defect (see the validity/expiration-date regression in `tests/test_regressions_baseline.py`) directly into production, and `cmf_chatgpt_000`'s 20 episodes overlap `memory-fabric`'s 57, creating dedup risk with no journal-level dedup mechanism yet to resolve it safely.
- **Keep `default_db` as production, re-import the 57 into it** (plan option C). Rejected: discards the already-verified idempotent production import and re-spends Gemini extraction cost for no benefit.
