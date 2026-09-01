# Context Memory Fabric

**Status:** Phase 1 / Active

Context Memory Fabric is a portable personal context and memory layer shared across AI clients (Claude Desktop, IDE agents, chat interfaces, and CLI tools). It unites **curated durable knowledge** with **temporal episodic memory** and exposes them through standard **Model Context Protocol (MCP)** tools.

---

## Architectural Separation

```text
                  Context Memory Fabric MCP (mcp.py)
                                  │
    ┌────────────────┬────────────┼────────────┬────────────────┐
    │                │            │            │                │
remember()        recall()   search_wiki()  get_context()  propose_wiki_update()
    │                │            │            │                │
 direct           episodic    canonical    unified context   reviewable
episodic          factual      durable     (provenance &      proposal
 write              read         read       guidance)          write
    │                │            │            │                │
    ▼                ▼            ▼            ▼                ▼
Graphiti / FalkorDB          LLM_Wiki    Both Stores       wiki-proposals/
                                                          (Wiki UNTOUCHED)
```

1. **Durable Knowledge (`LLM_Wiki`):**
   - Curated Markdown notes, research reports, PDFs, specifications, and structured documents stored in a local folder/repository.
   - Accessed read-only via lexical search with match snippets and provenance.
   - Proposed updates never overwrite the corpus directly; they are generated as reviewable staging files in `wiki-proposals/`.

2. **Episodic Memory (Graphiti + FalkorDB):**
   - Dynamic temporal knowledge graph storing past decisions, evolving state, preferences, milestones, and entity relationships.
   - Tracks temporal validity (`valid_at`, `invalid_at`) so newer decisions supersede older ones without silently deleting history.

3. **Unified Context Assembly (`get_context`):**
   - Assembles both durable knowledge and episodic memory into clean Markdown with clear provenance tags and conflict interpretation guidance.

---

## Documentation & Guides

- 🛠️ **[Installation, Configuration & Setup Guide](SETUP.md):** Complete prerequisites, Docker configuration, `.env` options, and testing instructions.
- 🔌 **[Client Integration & Harness Guide](CLIENTS.md):** Step-by-step setup for Claude Desktop, Antigravity IDE, Cursor, and other MCP harnesses.

---

## Available MCP Tools Reference

| Tool | Mode | Description |
|---|---|---|
| **`get_context(topic)`** | Read-Only | **DEFAULT** personal context retrieval tool. Concurrently searches durable Wiki files and episodic memories, returning formatted Markdown with temporal conflict guidance. |
| **`search_wiki(query)`** | Read-Only | Lexical search over the local durable knowledge corpus (`WIKI/`, `REPORTS/`, `RAW/`, `TO-RESEARCH/`, etc.) with extracted snippet previews and media metadata. |
| **`recall(query)`** | Read-Only | Queries the Graphiti episodic knowledge graph in FalkorDB for temporal facts, past decisions, milestones, and preference changes. |
| **`remember(content, name, source_description)`** | State Write | Ingests a substantive decision, event, preference change, or milestone into the episodic knowledge graph in FalkorDB. |
| **`propose_wiki_update(target_path, proposed_content, rationale)`** | Proposal Write | Creates a persistent staging proposal under `wiki-proposals/` with SHA-256 hashes and a unified diff. **Never modifies the Wiki directly.** |

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

