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
git clone https://github.com/<your-username>/context-memory-fabric.git
cd context-memory-fabric

# Install virtualenv and dependencies with uv
uv sync
```

### 2. Start FalkorDB Container

Run the lightweight FalkorDB Docker container:

```bash
docker run -d \
  --name context-memory-fabric-falkordb \
  -p 6379:6379 \
  -p 3001:3000 \
  falkordb/falkordb:latest
```

Alternatively, use the included `docker-compose.yml`:

```bash
docker compose up -d
```

- **FalkorDB Database Port:** `localhost:6379`
- **FalkorDB Web Browser UI:** `http://localhost:3001` (connect to host `localhost:6379`, graph name `default_db`)

---

## Configuration Options

Configuration can be set via a project-root `.env` file, environment variables, or CLI arguments:

### Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `LLM_WIKI_PATH` | **Yes** | — | Absolute path to the local durable knowledge corpus directory (e.g. `/Users/username/LLM_Wiki`). |
| `GEMINI_API_KEY` | **Yes** | — | Google Gemini API key used by Graphiti for entity extraction and embeddings. |
| `FALKORDB_HOST` | No | `localhost` | FalkorDB server host. |
| `FALKORDB_PORT` | No | `6379` | FalkorDB server port. |
| `FALKORDB_PASSWORD` | No | `None` | FalkorDB password if authentication is enabled. |
| `CMF_STATE_DIR` | No | `./wiki-proposals` | Custom directory for storing pending wiki update proposals. |

### Example `.env`

Create a `.env` file in the project root:

```bash
# Path to your local durable knowledge folder
LLM_WIKI_PATH=/path/to/your/LLM_Wiki

# Gemini API Key for Graphiti
GEMINI_API_KEY=AIzaSy...

# FalkorDB connection settings
FALKORDB_HOST=localhost
FALKORDB_PORT=6379
```

---

## Running the MCP Server

The server supports three transport protocols via CLI flags:

### 1. Standard I/O (Default, recommended for desktop clients)

```bash
uv run python -m server.mcp
```

### 2. Server-Sent Events (SSE)

```bash
uv run python -m server.mcp --transport sse --host 127.0.0.1 --port 8000
```
- SSE endpoint: `http://localhost:8000/sse`

### 3. Streamable HTTP

```bash
uv run python -m server.mcp --transport streamable-http --host 127.0.0.1 --port 8000
```
- Endpoint: `http://localhost:8000/mcp`

### CLI Arguments

```text
--transport     Transport protocol: 'stdio', 'sse', or 'streamable-http' (default: stdio)
--host          Host address for network transports (default: 127.0.0.1)
--port          Port number for network transports (default: 8000)
--wiki-path     Path to local LLM_Wiki corpus root directory (overrides LLM_WIKI_PATH in .env)
```

---

## Running Tests

Run the full automated unit and integration test suite with `pytest`:

```bash
uv run pytest
```
