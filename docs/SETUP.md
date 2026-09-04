# Installation, Configuration & Setup Guide

This guide covers setting up, configuring, running, and testing the **Context Memory Fabric** MCP server and its underlying services.

---

## Prerequisites & Dependencies

- **Python:** `>= 3.12`
- **Package Manager:** [`uv`](https://github.com/astral-sh/uv) (recommended) or `pip`
- **Docker:** To run the local FalkorDB graph instance
- **API Keys:** Google Gemini API key (for Graphiti LLM extraction and embeddings)

### Core Dependencies

- `mcp>=2.1.1` — Model Context Protocol SDK (supports stdio, SSE, and streamable-http)
- `graphiti-core[falkordb,google-genai]>=0.29.3` — Temporal episodic knowledge graph engine
- `pypdf>=6.16.2` — Pure-Python selectable text extraction from PDF documents
- `python-dotenv>=1.2.3` — Local `.env` configuration loader

---

## Installation & Setup

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/tmargolis/context-memory-fabric.git
cd context-memory-fabric
uv sync
```

### 2. Start FalkorDB Container

```bash
docker run -d \
  --name context-memory-fabric-falkordb \
  -p 6379:6379 \
  -p 3001:3000 \
  falkordb/falkordb:latest
```

Or use the included Compose file:

```bash
docker compose up -d
```

- **FalkorDB:** `localhost:6379`
- **Browser:** `http://localhost:3001` (graph `default_db`)

---

## Configuration Options

Configuration can be set via project-root `.env`, environment variables, or CLI arguments.

| Variable | Required | Default | Description |
|---|---|---|---|
| `LLM_WIKI_PATH` | **Yes** | — | Absolute path to the local durable knowledge corpus. |
| `GEMINI_API_KEY` | **Yes** | — | Gemini key used by Graphiti extraction/embeddings. |
| `FALKORDB_HOST` | No | `localhost` | FalkorDB host. |
| `FALKORDB_PORT` | No | `6379` | FalkorDB port. |
| `FALKORDB_PASSWORD` | No | `None` | FalkorDB password if enabled. |
| `FALKORDB_DATABASE` | **Yes** | — | Target FalkorDB graph database name. No default is assumed — an unset value raises a startup error rather than silently selecting a graph. |
| `CMF_STATE_DIR` | No | `./wiki-proposals` | Pending Wiki proposal storage. |

Example:

```bash
LLM_WIKI_PATH=/path/to/your/LLM_Wiki
GEMINI_API_KEY=...
FALKORDB_HOST=localhost
FALKORDB_PORT=6379
FALKORDB_DATABASE=memory-fabric
```

> [!IMPORTANT]
> `FALKORDB_DATABASE` has no implicit default. Earlier versions of this project silently fell back to a graph named `default_db`, which caused MCP reads/writes to diverge from the graph an operator believed was configured. Always set this explicitly.

---

## Running the MCP Server

### Standard I/O (default; desktop clients)

```bash
uv run python -m server.mcp
```

### SSE

```bash
uv run python -m server.mcp --transport sse --host 127.0.0.1 --port 8000
```

### Streamable HTTP

```bash
uv run python -m server.mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

CLI options: `--transport`, `--host`, `--port`, `--wiki-path`.

---

## Reset Episodic Memory

For a clean import/test cycle, clear all nodes and relationships from the Graphiti graph while leaving the FalkorDB container/volume intact:

```bash
docker exec context-memory-fabric-falkordb redis-cli GRAPH.QUERY default_db "MATCH (n) DETACH DELETE n"
```

Confirm the graph name first if needed:

```bash
docker exec context-memory-fabric-falkordb redis-cli GRAPH.LIST
```

This is destructive to episodic memory. It does **not** modify `LLM_Wiki` or pending Wiki proposals. Avoid `docker compose down -v` unless you intend to delete the entire FalkorDB volume. For a complete list of safe inspection and validation queries, see [FalkorDB / Cypher Query Reference](FALKORDB-QUERIES.md).

---

## Historical Memory Imports

Historical memory content is supplied directly by the calling AI client/agent to the import MCP tool. No source file needs to be copied into the project.

The importer should classify atomic items as:

- **episodic** — dated events, decisions, changes, milestones → ingest into Graphiti
- **durable candidate** — stable facts/reference knowledge/preferences → retain for review, not episodic ingestion
- **ambiguous/undated** — retain for review rather than inventing a date

Project-root `imports/` is reserved for generated import state/reports (for idempotency and review), not source uploads. Imports should preserve source and original event/reference time, prevent duplicates on reruns, support dry-run, and never write directly to `LLM_Wiki`.

**Privacy:** episodic items ingested through Graphiti are processed by the configured LLM/embedding provider.

---

## Running Tests

```bash
uv run pytest
```
