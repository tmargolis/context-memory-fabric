# Context Memory Fabric

**Status:** Phase 1 / Active

Context Memory Fabric is a portable personal context and memory layer shared across AI clients (Claude Desktop, IDE agents, chat interfaces, and CLI tools). It unites **curated durable knowledge** with **temporal episodic memory** and exposes them through standard **Model Context Protocol (MCP)** tools.

---

## Architectural Separation

```text
                                         Context Memory Fabric MCP (mcp.py)
                                                         │
   ┌──────────────┬──────────────┬──────────────┬────────┴───────┬──────────────┬──────────────────────┬──────────────────┐
   │              │              │              │                │              │                      │                  │
remember()    recall()     edit_memory()  search_wiki()    get_context()   propose_wiki_update()   import_memories()
   │              │              │              │                │              │                      │
 direct        episodic      episodic       canonical         unified       reviewable             administrative
episodic       factual      mutation /       durable          context        proposal                historical
 write           read        re-date          read          assembly          write                 bulk import
   │              │              │              │                │              │                      │
   ▼              ▼              ▼              ▼                ▼              ▼                      ▼
        Graphiti / FalkorDB                  LLM_Wiki       Both Stores   wiki-proposals/     Graphiti / FalkorDB
                                                                          (Wiki UNTOUCHED)    (imports/ registry)
```

1. **Durable Knowledge (`LLM_Wiki`):**
   - Curated Markdown notes, research reports, PDFs, specifications, and structured documents stored in a local folder/repository.
   - Accessed read-only via lexical search with match snippets and provenance.
   - Proposed updates never overwrite the corpus directly; they are generated as reviewable staging files in `wiki-proposals/`.

2. **Episodic Memory (Graphiti + FalkorDB):**
   - Dynamic temporal knowledge graph storing past decisions, evolving state, preferences, milestones, and entity relationships.
   - Tracks temporal validity (`valid_at`, `invalid_at`) so newer decisions supersede older ones without silently deleting history.
   - Can be directly updated, re-dated, and corrected using `edit_memory()` while keeping provenance and import state in sync.

3. **Unified Context Assembly (`get_context`):**
   - Assembles both durable knowledge and episodic memory into clean Markdown with clear provenance tags and conflict interpretation guidance.

4. **MCP-Boundary Capture (Milestone 4a):**
   - Every tool call from any connected MCP client is journaled as evidence in the background — one implementation covering Claude Desktop, Claude Code, Cursor, and any other MCP client, with harness identity, redacted arguments, and a bounded, drop-counted queue so capture never blocks a tool call. See [docs/CLIENTS.md](docs/CLIENTS.md#mcp-boundary-capture-milestone-4a) for the full capture model and its per-client limits.

---

## Documentation & Guides

All project documentation beyond this README lives under [`docs/`](docs/).

- 🛠️ **[Installation, Configuration & Setup Guide](docs/SETUP.md):** Prerequisites, Docker, `.env`, testing, graph reset, and historical-memory import.
- 🔌 **[Client Integration & Harness Guide](docs/CLIENTS.md):** Claude Desktop, Antigravity IDE, Cursor, and other MCP clients.
- 📊 **[FalkorDB / Cypher Query Reference](docs/FALKORDB-QUERIES.md):** Operator and developer Cypher query reference for inspecting, auditing, and validating the Graphiti graph.
- 📓 **[Journal Query Guide](docs/JOURNAL-QUERIES.md):** How to inspect the evidence layer (the append-only source-event journal) directly — CLI and raw SQL.
- 🗺️ **[Roadmap](docs/ROADMAP.md):** Architectural principles and the target logical architecture — the *what and why*.
- ✅ **[Implementation Plan](docs/IMPLEMENTATION-PLAN.md):** Milestone-by-milestone execution log, exit gates, and acceptance tests — the *how, in what order, and how we know it worked*.
- 📐 **[Architecture Decision Records](docs/adr/):** ADR 0001 (four-layer model), 0002 (provider boundaries), 0003 (graph/state topology), 0004 (message classifier v2 and work journal — proposed, not yet implemented).

---

## Available MCP Tools Reference

| Tool | Mode | Description |
|---|---|---|
| **`get_context(topic)`** | Read-Only | **DEFAULT** personal context retrieval tool. Concurrently searches durable Wiki files and episodic memories, returning formatted Markdown with temporal conflict guidance. |
| **`search_wiki(query)`** | Read-Only | Lexical search over the local durable knowledge corpus (`WIKI/`, `REPORTS/`, `RAW/`, `TO-RESEARCH/`, etc.) with extracted snippet previews and media metadata. |
| **`recall(query)`** | Read-Only | Queries the Graphiti episodic knowledge graph in FalkorDB for temporal facts, past decisions, milestones, and preference changes. |
| **`remember(content, name, source_description)`** | State Write | Ingests a substantive decision, event, preference change, or milestone into the episodic knowledge graph in FalkorDB. |
| **`edit_memory(target_query, new_reference_time, new_content, new_summary, new_name, dry_run)`** | Memory Mutation | Modifies, corrects, or re-dates existing episodic episodes, entity nodes, and graph relationships in FalkorDB, synchronizing local import state. |
| **`reconcile_memories(records, dry_run)`** | Reconciliation | Consolidates, updates, and upserts episodic memories with real upsert/reject semantics in FalkorDB and synchronizes local import registry state. |
| **`propose_wiki_update(target_path, proposed_content, rationale)`** | Proposal Write | Creates a persistent staging proposal under `wiki-proposals/` with SHA-256 hashes and a unified diff. **Never modifies the Wiki directly.** |
| **`import_memories(content, source, source_description, dry_run)`** | Admin / Bulk Ingest | Parses historical memory exports passed directly by AI clients, conservatively classifies them, and ingests dated episodic entries into Graphiti. |
| **`import_chatgpt_exports(paths, dry_run, graph_name, review_overrides, review_overrides_path)`** | Admin / Bulk Ingest | Parses native ChatGPT `conversations-*.json` export files by explicit file path, reconstructs active conversation branches, and classifies candidates into episodic, durable, ambiguous, and non-memory buckets. Refuses to write into the protected `default_db` graph. |
| **`capture_note(content, kind)`** | Evidence Write | Milestone 4a: explicit checkpoint captured to the append-only evidence journal — not episodic memory (`remember` is for that). Fire-and-forget. |
| **`capture_health()`** | Read-Only | Milestone 4a: in-process MCP-boundary capture status — events captured, dropped, redacted, current queue depth. |
| **`promote_auto_accepted_memories(dry_run, limit)`** | Memory Write | Promotes consolidation candidates already classified `auto_accepted` (Milestone 3) into episodic memory in FalkorDB. Idempotent — a candidate promoted once is never promoted twice. |

### Historical memory imports

Historical memory content should be passed directly by an AI client/agent to the import MCP tool; source files do **not** need to be copied into the project. The importer should split the supplied content into atomic candidates and classify them conservatively:

- **episodic:** dated decisions, events, changes, milestones → Graphiti/FalkorDB
- **durable candidate:** stable facts, reference material, long-lived preferences → review only
- **ambiguous/undated:** review rather than inventing an event date

Project-root `imports/` is reserved for local import state/reports, not source uploads. Imports must preserve source/date provenance, be idempotent, and never mutate `LLM_Wiki`.

---

## Quick Start Summary

```bash
# 1. Start FalkorDB
docker run -d --name context-memory-fabric-falkordb -p 6379:6379 -p 3001:3000 falkordb/falkordb:latest

# 2. Install dependencies & configure environment
uv sync
cp .env.example .env  # configure LLM_WIKI_PATH and GEMINI_API_KEY

# 3. Run the MCP server
uv run python -m server.mcp
```
