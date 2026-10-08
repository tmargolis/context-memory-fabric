# Installation, Configuration & Setup Guide

This guide covers setting up, configuring, running, and testing the **Context Memory Fabric (CMF)** MCP server.

Context Memory Fabric is released as an experimental, single-user, self-hosted developer preview under the **PolyForm Perimeter License 1.0.1**.

---

## Architecture Overview

CMF keeps four architectural layers distinct:

1. **Source Evidence:** Append-only local SQLite journal recording raw tool events and session evidence.
2. **Derived Episodic Memory:** Temporal knowledge graph in FalkorDB (via Graphiti) capturing past decisions, state evolutions, preferences, and events with `valid_at` / `invalid_at` temporal tracking.
3. **Durable Knowledge:** Local Markdown corpus (such as an Obsidian vault or the included `starter-wiki/`) containing curated technical notes, project specs, and systems documentation.
4. **Assembled Runtime Context:** Unified retrieval via `get_context`, synthesizing episodic memory and durable knowledge with source attribution and conflict guidance.

---

## Quickstart: Docker Compose Preview (Recommended)

The fastest and most isolated way to run CMF is using the standalone preview Compose stack. It packages the CMF MCP server, FalkorDB, and the starter knowledge vault without requiring local Python installation or machine-specific configurations.

### 1. Clone & Configure

```bash
git clone https://github.com/tmargolis/context-memory-fabric.git
cd context-memory-fabric

# Copy the environment template
cp .env.example .env
```

Edit `.env` to supply your **Google Gemini API Key**:
```bash
GEMINI_API_KEY=AIzaSy...
```
*(By default, this points `LLM_WIKI_PATH` to the included fictional `starter-wiki/` and uses graph `CMF-local` on non-conflicting host ports.)*

### 2. Launch the Stack

```bash
docker compose -f docker-compose.preview.yml up -d
```

This starts:
- **FalkorDB:** on `localhost:6380` (Browser UI at `http://localhost:3002`)
- **CMF MCP Server:** Streamable HTTP endpoint at `http://localhost:8000/mcp`
- **Mounted Knowledge:** `./starter-wiki` mounted at `/wiki` inside the container

### 3. Connect Your AI Client

See [Connecting Your MCP Client](#connecting-your-mcp-client) below to wire Claude Desktop, Cursor, or other tools to `http://localhost:8000/mcp`.

---

## Alternative: Local Source Installation

If you prefer running directly on your host with Python:

### 1. Prerequisites
- **Python:** `>= 3.12`
- **Package Manager:** [`uv`](https://github.com/astral-sh/uv) (recommended)
- **Docker:** Required for running FalkorDB

### 2. Install Dependencies

```bash
uv sync
```

### 3. Start FalkorDB Container

Run the standard operational FalkorDB instance:

```bash
docker compose up -d falkordb
```

- FalkorDB: `localhost:6379`
- FalkorDB Browser UI: `http://localhost:3001`

### 4. Configure Environment

Create `.env` in the repository root:

```bash
cp .env.example .env
```

Set the required variables:
```bash
# Model provider key
GEMINI_API_KEY=AIzaSy...

# Target FalkorDB graph (always name this explicitly!)
FALKORDB_DATABASE=memory-fabric

# Optional: Durable knowledge corpus (if omitted, wiki tools are disabled gracefully)
LLM_WIKI_PATH=./starter-wiki
```

#### Optional: Run a Small Local Embedder (Ollama)

Embeddings can run on your own machine instead of a paid or rate-limited API. `nomic-embed-text` is small (~270 MB) and runs fine on a laptop CPU, with no GPU needed. Setup takes about 15 minutes on macOS, Linux or Windows.

1. **Install Ollama** from [ollama.com/download](https://ollama.com/download). It installs as a background service listening on `http://127.0.0.1:11434`.
2. **Pull the embedding model:**
   ```bash
   ollama pull nomic-embed-text
   ```
3. **Check that it answers** on Ollama's OpenAI-compatible endpoint (the reply should contain a 768-number vector):
   ```bash
   curl http://127.0.0.1:11434/v1/embeddings -H "Content-Type: application/json" -d '{"model": "nomic-embed-text", "input": "hello"}'
   ```
4. **Point CMF at it** in `.env`:
   ```bash
   CMF_EMBED_PROVIDER=local
   CMF_LOCAL_EMBED_BASE_URL=http://127.0.0.1:11434/v1
   CMF_LOCAL_EMBED_API_KEY=ollama      # Ollama ignores it; the client just needs a non-empty value
   CMF_LOCAL_EMBED_MODEL=nomic-embed-text
   EMBEDDING_DIM=768                   # nomic's fixed output width
   ```

Things to know:
- **Pick the embedder before your graph has data.** A graph's vectors and index are built at one width. Switching embedders later (e.g. to 1024-wide Gemini or OpenAI vectors) means a new `FALKORDB_DATABASE` and re-importing your history into it, not an in-place change.
- **Ollama must be running** whenever CMF writes or searches memory. If it's down, those calls fail rather than falling back.
- **Any LLM can pair with it:** `anthropic`, `openai`, `gemini`, or a local LLM on a different server. The embedder reads `CMF_LOCAL_EMBED_BASE_URL` and a local LLM reads `CMF_LOCAL_LLM_BASE_URL`; each falls back to `CMF_LOCAL_BASE_URL` when unset.
- **Background transcript capture** (the Claude Code / Codex pollers) extracts with `CMF_CAPTURE_LLM_PROVIDER`, not `CMF_LLM_PROVIDER`. It defaults to `local`, so with an embedder-only Ollama setup, set it to your LLM provider (for example `anthropic`) before turning the pollers on.

### 5. Run the Server

**Standard I/O (default for desktop clients):**
```bash
uv run python -m server.mcp
```

**Streamable HTTP (for network/remote clients):**
```bash
uv run python -m server.mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

---

## Connecting Your MCP Client

### Claude Desktop

Edit your Claude Desktop configuration file:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

#### Option A: Docker Compose Preview (Streamable HTTP)

```json
{
  "mcpServers": {
    "context-memory-fabric": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-proxy",
        "http://127.0.0.1:8000/mcp"
      ]
    }
  }
}
```

#### Option B: Local Source (stdio)

```json
{
  "mcpServers": {
    "context-memory-fabric": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/context-memory-fabric",
        "python",
        "-m",
        "server.mcp"
      ],
      "env": {
        "GEMINI_API_KEY": "AIzaSy...",
        "FALKORDB_DATABASE": "memory-fabric",
        "LLM_WIKI_PATH": "/absolute/path/to/starter-wiki"
      }
    }
  }
}
```

### Cursor / Generic MCP Clients

In Cursor Settings → Features → MCP:
- **Type:** `command`
- **Command:** `uv run --directory /path/to/context-memory-fabric python -m server.mcp`

---

## Configuration Reference

Configuration can be supplied via `.env`, environment variables, or CLI flags.

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | Conditional | — | Required when using Gemini extraction/embeddings (`CMF_LLM_PROVIDER=gemini`). |
| `ANTHROPIC_API_KEY` | Conditional | — | Required when any provider setting is `anthropic`. |
| `OPENAI_API_KEY` | Conditional | — | Required when any provider setting is `openai`. |
| `FALKORDB_DATABASE` | Recommended | `default_db` | Target FalkorDB graph name. Always specify an explicit graph name (e.g. `CMF-local` or `memory-fabric`) to avoid silent collisions. |
| `LLM_WIKI_PATH` | Optional | — | Path to your Markdown wiki or Obsidian vault. When unset, knowledge tools (`search_wiki`, `propose_doc_update`) are gracefully omitted. |
| `FALKORDB_HOST` | No | `localhost` | FalkorDB host. |
| `FALKORDB_PORT` | No | `6379` | FalkorDB port. |
| `CMF_STATE_DIR` | No | `./wiki-proposals` | Staging directory for generated document update proposals and local state. |
| `CMF_LLM_PROVIDER` | No | `gemini` | Extractor LLM provider: `anthropic`, `openai`, `gemini` or `local`. |
| `CMF_EMBED_PROVIDER` | No | `gemini` | Embedder provider: `openai`, `gemini` or `local` (Anthropic has no embeddings API). |
| `CMF_CAPTURE_LLM_PROVIDER` | No | `local` | LLM the background transcript pollers extract with. Same values as `CMF_LLM_PROVIDER`. |
| `CMF_ANTHROPIC_MODEL` | No | `claude-opus-5-5` | Claude model for extraction. A Sonnet or Haiku costs less. |
| `CMF_ANTHROPIC_EFFORT` | No | model default | Claude effort: `low`, `medium`, `high`, `xhigh` or `max`. Lower is cheaper. |
| `CMF_OPENAI_MODEL` | No | `gpt-5.5` | OpenAI model for extraction. |
| `CMF_OPENAI_EMBED_MODEL` | No | `text-embedding-3-small` | OpenAI embedding model, shortened to `EMBEDDING_DIM`. |
| `CMF_LOCAL_BASE_URL` | No | `http://127.0.0.1:12345/v1` | OpenAI-compatible endpoint for local inference (vLLM, Ollama, LM Studio). Ollama: `http://127.0.0.1:11434/v1`. |
| `CMF_LOCAL_LLM_BASE_URL` / `CMF_LOCAL_EMBED_BASE_URL` | No | `CMF_LOCAL_BASE_URL` | Separate endpoints for a local LLM and a local embedder, each with its own `_API_KEY`. |
| `CMF_LOCAL_EMBED_MODEL` | No | `text-embedding-nomic-embed-text-v1.5` | Local embedding model id as your server names it (Ollama: `nomic-embed-text`). |
| `EMBEDDING_DIM` | No | `1024` | Vector embedding dimension (1024 for Gemini or OpenAI; 768 for nomic local embedder). Fixed per graph. |
| `CMF_EXTRACTION_PROFILE` | No | `typed-recall` | Entity extraction prompt. `legacy` (the pre-2026-09-28 prompt) is opt-in, for reproducing old extraction. |
| `CMF_MCP_AUTH_TOKEN`| No | — | Optional shared-secret bearer token for network HTTP/SSE transports. |

---

## Start Building Context Now

### 1. Day-to-Day Use
Once connected, your AI assistant will automatically use CMF tools:
- **`get_context(topic)`**: Synthesizes relevant durable wiki notes and recent episodic decisions.
- **`remember(content)`**: Explicitly stores decisions, state changes, and preferences into the temporal memory graph.
- **`capture_session(items)`**: Checkpoints multiple facts from a conversation into reviewable proposals and episodes.
- **`propose_doc_update(...)`**: Stages changes to durable wiki notes for human review without overwriting files.

### 2. Historical Memory Import (Experimental)
CMF provides `import_memories` and `import_chatgpt_exports` for parsing historical AI exports into episodic candidates. These tools are labeled **experimental** in this preview. You do not need to run historical backfills to begin using CMF; starting fresh with live conversations is the recommended path.

---

## Operational Safeguards & Persistence

1. **Database Persistence:**
   - In Docker Compose, FalkorDB data is stored in the named volume `falkordb_data` (or `cmf_preview_falkordb_data`).
   - Do NOT run `docker compose down -v` unless you explicitly intend to destroy all episodic memory graphs.
2. **Query Timeout Persisted:**
   - Both compose files persist `FALKORDB_ARGS=MAX_QUEUED_QUERIES 25 TIMEOUT 30000 RESULTSET_SIZE 10000` to prevent query timeouts on growing graphs.
3. **Resetting Test Graphs Safely:**
   To wipe only a specific test graph without destroying other graphs or container volumes:
   ```bash
   docker exec cmf-preview-falkordb redis-cli GRAPH.QUERY CMF-local "MATCH (n) DETACH DELETE n"
   ```

---

## Feedback & Issues

Have questions, setup difficulties, or retrieval feedback?
- Please open an issue using the [Setup or Retrieval Problem template](https://github.com/tmargolis/context-memory-fabric/issues).
- For sensitive security disclosures, see [SECURITY.md](SECURITY.md).
