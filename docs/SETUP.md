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

Edit `.env` to choose your [model providers](#model-providers) and supply their keys. The template's default is Gemini for both jobs, which works on Gemini's free tier:
```bash
GEMINI_API_KEY=AIzaSy...
```
Or, with Anthropic and OpenAI keys:
```bash
CMF_LLM_PROVIDER=anthropic
CMF_EMBED_PROVIDER=openai
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
```
*(By default, this points `LLM_WIKI_PATH` to the included fictional `starter-wiki/` and uses graph `CMF-local` on non-conflicting host ports. The preview always mounts a wiki, so it can't run without one; for a wiki-less setup use the local source installation below.)*

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

## Model Providers

CMF uses a model for two separate jobs, and each can come from a different provider:

- **LLM (extraction):** turns conversations into episodes, and episodes into graph entities and facts. Set by `CMF_LLM_PROVIDER`, and for the background transcript pollers by `CMF_CAPTURE_LLM_PROVIDER`.
- **Embeddings:** turn text into vectors for search. Set by `CMF_EMBED_PROVIDER`.

| Provider | LLM | Embeddings | Needs |
|---|---|---|---|
| `anthropic` | ✅ | ❌ (Anthropic has no embeddings API) | `ANTHROPIC_API_KEY` |
| `openai` | ✅ | ✅ | `OPENAI_API_KEY` |
| `gemini` | ✅ | ✅ | `GEMINI_API_KEY` (the free tier works) |
| `local` | ✅ | ✅ | An OpenAI-compatible server: LM Studio, Ollama or vLLM (`CMF_LOCAL_LLM_BASE_URL` / `CMF_LOCAL_EMBED_BASE_URL`) |

Common setups:

| LLM | Embeddings | Notes |
|---|---|---|
| `anthropic` | `openai` | Claude for extraction (default model `claude-sonnet-5-5`), OpenAI `text-embedding-3-small` vectors |
| `openai` | `openai` | One key for both (default model `gpt-5.5`) |
| `gemini` | `gemini` | No cost on the free tier; CMF paces itself to the free-tier limits |
| any | `local` | A small embedder on your own machine; see [Run a Small Local Embedder](#optional-run-a-small-local-embedder-ollama) |

API keys are separate from a ChatGPT or Claude subscription: create them at [platform.claude.com](https://platform.claude.com) (Settings → API Keys) or [platform.openai.com](https://platform.openai.com) (API keys) and add prepaid credit. An unfunded key fails with the provider's own "add credits" message. Model and cost settings (`CMF_ANTHROPIC_MODEL`, `CMF_ANTHROPIC_EFFORT`, `CMF_OPENAI_MODEL`) are in the [configuration reference](#configuration-reference).

**Pick the embedder before your graph has data.** A graph's vectors are built at one width (`EMBEDDING_DIM`: 1024 for Gemini or OpenAI, 768 for nomic). Changing embedders later means a new `FALKORDB_DATABASE`.

---

## Setting Up on Your Own Machine (checklist)

The path from a fresh clone to captured, reviewed and recalled memory, with your own keys and no code changes:

1. **Install and start FalkorDB** (local source installation, steps 1–3 below).
2. **Configure `.env`** (step 4): providers and keys from [Model Providers](#model-providers); `FALKORDB_DATABASE`; `LLM_WIKI_PATH` pointing at your notes, or empty to run without a wiki; on Linux or Windows, `CMF_PROJECT_ROOTS` naming the folders your projects live in.
3. **Run the server and connect your clients** (step 5 and [Connecting Your MCP Client](#connecting-your-mcp-client)).
4. **Turn on capture** of your coding sessions ([Background Capture](#background-capture)): capture checkpoints (the agent saves what was decided; nothing reads your transcripts), transcript pollers, or both.
5. **Tell your AI assistants how to use CMF:** paste the block from [INSTRUCTIONS-FOR-AGENTS.md](INSTRUCTIONS-FOR-AGENTS.md) into each app's custom instructions.
6. **Review what was captured** ([Reviewing What Was Captured](#reviewing-what-was-captured)), by hand or with a [nightly review](NIGHTLY-REVIEW.md) that recommends verdicts for you to confirm. Nothing reaches memory or your wiki until you approve it.

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
# Model providers and their keys (see Model Providers above)
CMF_LLM_PROVIDER=anthropic
CMF_EMBED_PROVIDER=openai
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...

# Target FalkorDB graph (always name this explicitly!)
FALKORDB_DATABASE=memory-fabric

# Optional: durable knowledge corpus. Leave it empty to run without a wiki:
# the wiki tools are then not offered and extraction proposes episodes only.
LLM_WIKI_PATH=/absolute/path/to/your/notes
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

## Background Capture

There are two ways to get your coding sessions into CMF. They can run together.

### Capture checkpoints (no transcript reading)

A Stop hook in Claude Code, Codex or Antigravity tells the agent, every few turns, to call `capture_session` with whatever was decided or concluded since the last checkpoint. The agent chooses what to keep, and nothing reads your transcripts. Instructions alone ([INSTRUCTIONS-FOR-AGENTS.md](INSTRUCTIONS-FOR-AGENTS.md)) rarely get an agent to save anything on its own; the hook supplies the trigger.

```bash
.venv/bin/python3 -m server.adapters.capture_nudge install --harness all   # or claude_code / codex / antigravity
.venv/bin/python3 -m server.adapters.capture_nudge status
.venv/bin/python3 -m server.adapters.capture_nudge uninstall --harness all
```

- The nudge fires after `CMF_NUDGE_MIN_TURNS` agent replies (default 6), or after at least 2 replies and `CMF_NUDGE_MIN_MINUTES` (default 30). The reply that answers a nudge never triggers another.
- **Codex** runs a new hook only after you trust it: run `/hooks` in the Codex CLI once after installing.
- **Each nudge costs one extra agent step**, and the session must have CMF connected. Without it the agent just finishes.
- Antigravity has no pre-compaction hook, and Claude Code's and Codex's can't prompt the agent, so a very long session is covered only by the periodic nudges.
- Chat apps without hooks (Claude Desktop chat, Cowork, ChatGPT) rely on the instructions block, plus a nightly review.

### Transcript pollers

CMF can also capture your coding sessions by polling their transcripts every 15 minutes: Claude Code (the CLI and the desktop app's Code tab, including subagents), Codex CLI, local Cowork sessions and Antigravity. [`deploy/pollers/README.md`](../deploy/pollers/README.md) installs the pollers as background jobs on macOS (launchd), Linux (systemd) or Windows (Task Scheduler).

Before turning them on:
- **Set `CMF_CAPTURE_LLM_PROVIDER`.** The pollers extract with it, not `CMF_LLM_PROVIDER`, and it defaults to `local`. Without a local model server, set it to the provider whose key you have. Unattended capture spends that provider's credit: one long session is dozens of extraction calls.
- **On Linux and Windows, set `CMF_PROJECT_ROOTS`** (e.g. `~/code,~/work`), so sessions are tagged with their project rather than `other`.

Cloud Cowork sessions leave no transcript on your computer; only their calls to CMF's tools are captured.

## Reviewing What Was Captured

Captured sessions become **episodes** (things decided, planned or found) and, when a wiki is configured, **doc proposals** (reference knowledge for a wiki page). Both wait for review. From any connected client:

1. `list_review_conversations` shows which conversations have items waiting.
2. `list_episode_proposals` / `list_doc_proposals` (filter by `conversation_id` or `project`) show the items; `get_doc_proposal` shows a doc's diff.
3. `bulk_review_episodes` / `review_doc_proposal` record approve or reject, with a reason.
4. `promote_approved_episodes` writes an approved episode into memory (one per call); `apply_doc_proposal` writes an approved doc into the wiki (dry run first; each apply is committed in the wiki's git repo when it has one).

From a terminal, `uv run python -m server.review.cli queue --tier 1` lists the queue and `uv run python -m server.review.cli promote --apply` promotes every approved episode.

**Nightly review.** A scheduled task in your AI app can do the first pass every night at 11:11 PM: it recommends approve, reject or flag for each new item and leaves you a summary to confirm in the morning. Set it up by pasting one block into the app: see [NIGHTLY-REVIEW.md](NIGHTLY-REVIEW.md).

---

## Configuration Reference

Configuration can be supplied via `.env`, environment variables, or CLI flags.

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | Conditional | — | Required when any provider setting is `gemini` (the template's default). |
| `ANTHROPIC_API_KEY` | Conditional | — | Required when any provider setting is `anthropic`. |
| `OPENAI_API_KEY` | Conditional | — | Required when any provider setting is `openai`. |
| `FALKORDB_DATABASE` | Recommended | `default_db` | Target FalkorDB graph name. Always specify an explicit graph name (e.g. `CMF-local` or `memory-fabric`) to avoid silent collisions. |
| `LLM_WIKI_PATH` | Optional | — | Path to your Markdown wiki or Obsidian vault. When unset or empty, the wiki tools (`search_wiki`, `propose_doc_update`, ...) are not offered and extraction proposes episodes only. |
| `FALKORDB_HOST` | No | `localhost` | FalkorDB host. |
| `FALKORDB_PORT` | No | `6379` | FalkorDB port. |
| `CMF_STATE_DIR` | No | repo root | Where `doc-proposals/` and `episode-proposals/` are kept. |
| `CMF_LLM_PROVIDER` | No | `gemini` | LLM provider: `anthropic`, `openai`, `gemini` or `local` ([which provider does what](#model-providers)). |
| `CMF_EMBED_PROVIDER` | No | `gemini` | Embeddings provider: `openai`, `gemini` or `local`. Not `anthropic`: it has no embeddings API. |
| `CMF_CAPTURE_LLM_PROVIDER` | No | `local` | LLM the background transcript pollers extract with. Same values as `CMF_LLM_PROVIDER`. |
| `CMF_NUDGE_MIN_TURNS` | No | `6` | Agent replies between capture-checkpoint nudges ([Capture checkpoints](#capture-checkpoints-no-transcript-reading)). |
| `CMF_NUDGE_MIN_MINUTES` | No | `30` | Or nudge after this many minutes, once there are at least 2 replies. |
| `CMF_ANTHROPIC_MODEL` | No | `claude-sonnet-5-5` | Claude model for extraction. `claude-opus-5-5` is the step up, `claude-haiku-5-5` costs less. |
| `CMF_ANTHROPIC_EFFORT` | No | model default | Claude effort: `low`, `medium`, `high`, `xhigh` or `max`. Lower is cheaper. |
| `CMF_OPENAI_MODEL` | No | `gpt-5.5` | OpenAI model for extraction. |
| `CMF_OPENAI_EMBED_MODEL` | No | `text-embedding-3-small` | OpenAI embedding model, shortened to `EMBEDDING_DIM`. |
| `CMF_LOCAL_BASE_URL` | No | `http://127.0.0.1:12345/v1` | OpenAI-compatible endpoint for local inference (vLLM, Ollama, LM Studio). Ollama: `http://127.0.0.1:11434/v1`. |
| `CMF_LOCAL_LLM_BASE_URL` / `CMF_LOCAL_EMBED_BASE_URL` | No | `CMF_LOCAL_BASE_URL` | Separate endpoints for a local LLM and a local embedder, each with its own `_API_KEY`. |
| `CMF_LOCAL_EMBED_MODEL` | No | `text-embedding-nomic-embed-text-v1.5` | Local embedding model id as your server names it (Ollama: `nomic-embed-text`). |
| `EMBEDDING_DIM` | No | `1024` | Vector embedding dimension (1024 for Gemini or OpenAI; 768 for nomic local embedder). Fixed per graph. |
| `CMF_EXTRACTION_PROFILE` | No | `typed-recall` | Entity extraction prompt. `legacy` (the pre-2026-09-28 prompt) is opt-in, for reproducing old extraction. |
| `CMF_PROJECT_ROOTS` | No | macOS `~/Dev`, `~/Documents` | Folders whose subfolders are projects (comma-separated). Set it on Linux and Windows. |
| `CMF_PROJECT_FOLDER_MAP` / `CMF_PROJECT_ALIASES` | No | — | Map a folder to a project, or fold one project name into another (`path=project,...` / `old=new,...`). |
| `CMF_REVIEWER` | No | OS login name | Name recorded on review verdicts. |
| `CMF_REVIEW_RULES_PATH` | No | — | A Markdown file of your own review rules, appended to the generic ones the [nightly review](NIGHTLY-REVIEW.md) follows. |
| `CMF_EXTRACTION_WORKSTREAMS` / `_HARDWARE` / `_TOPICS` / `_PERSON` / `_DEBRIS_FILES` | No | generic examples | Names from your own work shown to the extraction model as examples (comma-separated). |
| `CMF_MCP_AUTH_TOKEN`| No | — | Optional shared-secret bearer token for network HTTP/SSE transports. |

---

## Start Building Context Now

### 1. Day-to-Day Use
Once connected, and instructed with [INSTRUCTIONS-FOR-AGENTS.md](INSTRUCTIONS-FOR-AGENTS.md), your AI assistant uses CMF's tools:
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
