# Installation, Configuration & Setup Guide

This guide covers setting up, configuring, running, and testing the **Context Memory Fabric (CMF)** MCP server.

Context Memory Fabric is released as an experimental, single-user, self-hosted developer preview under the **PolyForm Perimeter License 1.0.1**.

> [!IMPORTANT]
> **Pick ONE way to run CMF, and install only that one.**
>
> - **Docker preview (this page, recommended):** everything runs in Docker. Do **not** also run `uv sync`, `deploy/pollers/macos/install.sh` (with or without `--mcp-server`) or `python -m server.mcp` on your computer.
> - **Developer mode (from source):** [DEVELOPER-SETUP.md](DEVELOPER-SETUP.md). For working on CMF itself.
>
> The two read different config, use different FalkorDB ports (6380 vs 6379) and graph names, and both claim port 8000. Mixing them gives "connection refused" errors and two servers fighting over one port. Background capture (the transcript pollers and capture hooks) currently needs Developer mode; a Docker version is planned.

---

## Architecture Overview

CMF keeps four architectural layers distinct:

1. **Source Evidence:** Append-only local SQLite journal recording raw tool events and session evidence.
2. **Derived Episodic Memory:** Temporal knowledge graph in FalkorDB (via Graphiti) capturing past decisions, state evolutions, preferences, and events with `valid_at` / `invalid_at` temporal tracking.
3. **Durable Knowledge:** Configured knowledge sources, including a local Markdown corpus (such as an Obsidian vault or `starter-wiki/`) and live Notion notes, with separate source attribution.
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
Leave a key empty if you don't use that provider; don't put placeholder text in it. Everything else in `.env.example` is optional, and the **Host install only** block doesn't apply to the preview.

**Your notes.** `LLM_WIKI_PATH` defaults to `./starter-wiki`, a bundled **fictional** demo wiki (that's where "Project Aether" comes from). To use your own notes, such as an Obsidian or Quartz vault, set it to that folder's absolute path. The preview mounts it read-only and always mounts some folder; to run without notes, point it at an empty folder. After changing it, recreate the server so it picks the new folder up:
```bash
docker compose -f docker-compose.preview.yml up -d
```

### 2. Launch the Stack

```bash
docker compose -f docker-compose.preview.yml up -d
```

This starts:
- **FalkorDB:** on `localhost:6380` (Browser UI at `http://localhost:3002`)
- **CMF MCP Server:** Streamable HTTP endpoint at `http://localhost:8000/mcp`
- **Mounted Knowledge:** your `LLM_WIKI_PATH` folder (default `./starter-wiki`) mounted read-only at `/wiki` inside the container

### 3. Connect Your AI Client

See [Connecting Your MCP Client](#connecting-your-mcp-client) below to wire Claude Desktop, Cursor, or other tools to `http://localhost:8000/mcp`.

### 4. Check That It Works

1. In a **new** Claude session, ask it to remember something: *"Remember that I set up CMF today."* It calls `remember`, which replies that the memory was *queued for background ingestion (not yet confirmed)*. That means accepted, **not yet saved**: the write to the graph runs in the background and takes up to a minute.
2. Wait about 30 seconds, open the FalkorDB Browser at `http://localhost:3002`, and pick the graph **`CMF-local`** (the preview's graph; `memory-fabric` belongs to the developer setup). Run `MATCH (n) RETURN n LIMIT 25`; you should see the new memory and its entities.
3. Ask *"What do you remember about CMF?"* It should call `recall_mem` and find it.

Faster checks: ask Claude to run **`memory_status`**, which says whether the memory was saved and, if not, why. Or open `http://127.0.0.1:8000/health` in a browser: it shows which graph and FalkorDB this server uses, whether FalkorDB answers, the providers, the latest background writes and which AI apps have called CMF.

**Before the first successful write, the graph doesn't exist**, so an empty graph list in the Browser just means nothing has been saved yet. For the full traceback of a failure, read the server log:
```bash
docker logs cmf-preview-server 2>&1 | grep -iE "error|traceback" | tail -20
```
The same log explains a bare "Error executing tool" in Claude.

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

### Optional: Run a Small Local Embedder (Ollama)

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
4. **Point CMF at it** in `.env`. In the Docker preview the server runs in a container, so it reaches Ollama on your computer as `host.docker.internal`; in [Developer mode](DEVELOPER-SETUP.md) use `http://127.0.0.1:11434/v1` instead.
   ```bash
   CMF_EMBED_PROVIDER=local
   CMF_LOCAL_EMBED_BASE_URL=http://host.docker.internal:11434/v1
   CMF_LOCAL_EMBED_API_KEY=ollama      # Ollama ignores it; the client just needs a non-empty value
   CMF_LOCAL_EMBED_MODEL=nomic-embed-text
   EMBEDDING_DIM=768                   # nomic's fixed output width
   ```

Things to know:
- **Pick the embedder before your graph has data.** A graph's vectors and index are built at one width. Switching embedders later (e.g. to 1024-wide Gemini or OpenAI vectors) means a new `FALKORDB_DATABASE` and re-importing your history into it, not an in-place change.
- **Ollama must be running** whenever CMF writes or searches memory. If it's down, those calls fail rather than falling back.
- **Any LLM can pair with it:** `anthropic`, `openai`, `gemini`, or a local LLM on a different server. The embedder reads `CMF_LOCAL_EMBED_BASE_URL` and a local LLM reads `CMF_LOCAL_LLM_BASE_URL`; each falls back to `CMF_LOCAL_BASE_URL` when unset.
- **Background transcript capture** (the Claude Code / Codex pollers, Developer mode only for now) extracts with `CMF_CAPTURE_LLM_PROVIDER`, not `CMF_LLM_PROVIDER`. It defaults to `local`, so with an embedder-only Ollama setup, set it to your LLM provider (for example `anthropic`) before turning the pollers on.

---

## Setting Up on Your Own Machine (checklist)

The path from a fresh clone to reviewed and recalled memory, with your own keys and no code changes:

1. **Start the Docker preview** ([Quickstart](#quickstart-docker-compose-preview-recommended)) with your providers, keys and notes folder in `.env`.
2. **Connect your AI apps** ([Connecting Your MCP Client](#connecting-your-mcp-client)) and [check that it works](#4-check-that-it-works).
3. **Use it.** The server's own instructions tell connected apps when to use CMF, so you don't have to say "CMF" in your prompts. For apps that ignore them, paste the block from [INSTRUCTIONS-FOR-AGENTS.md](INSTRUCTIONS-FOR-AGENTS.md) into the app's custom instructions.
4. **Review what was captured** ([Reviewing What Was Captured](#reviewing-what-was-captured)), by hand or with a [nightly review](NIGHTLY-REVIEW.md) that recommends verdicts for you to confirm. Nothing reaches memory or your wiki until you approve it.

Automatic capture of your coding sessions ([Background Capture](#background-capture)) currently needs Developer mode.

---

## Connecting Your MCP Client

### Claude Desktop (Chat tab)

Claude Desktop reaches the preview through a small bridge, [`mcp-remote`](https://www.npmjs.com/package/mcp-remote), which needs **Node.js** (`node --version` should print v18 or later; install from [nodejs.org](https://nodejs.org) if not).

1. Open the config file: Claude Desktop → **Settings → Developer → Edit Config**, or directly:
   - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
2. Add **one** `context-memory-fabric` entry under `"mcpServers"` (keep any other servers that are already there, and remove any older CMF entry):
   ```json
   {
     "mcpServers": {
       "context-memory-fabric": {
         "command": "npx",
         "args": ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"]
       }
     }
   }
   ```
3. **Quit Claude Desktop completely** (macOS: Cmd+Q; Windows: right-click the tray icon → Quit) and reopen it.
4. **Start a new session.** A resumed or already-open conversation never loads newly added MCP servers. This is the most common reason CMF "isn't there".

Why not **Settings → Connectors → Add custom connector**? Connectors run from Anthropic's cloud and reject `localhost` ("enter a public URL"). They work only with a public HTTPS address and OAuth; see [CLIENTS.md §4](CLIENTS.md#4-multi-agent--remote-harnesses-chatgpt-gemini-spark-etc).

### Claude Desktop (Code tab)

On the first tester's machine, the Code tab used the same `claude_desktop_config.json` entry above after a restart and a new session. This is still being confirmed. To check, ask a new Code-tab session to list its CMF tools.

### Cursor / Generic MCP Clients

Clients that accept a URL (Cursor: Settings → MCP → Add) take `http://127.0.0.1:8000/mcp` directly. Clients that only start a command can use the same `npx -y mcp-remote http://127.0.0.1:8000/mcp` as Claude Desktop.

More clients (Antigravity, Codex, ChatGPT, Gemini): [CLIENTS.md](CLIENTS.md).

---

## Background Capture

CMF can capture your coding sessions automatically, with Stop-hook **capture checkpoints** (the agent saves what was decided) and **transcript pollers** (CMF reads Claude Code, Codex, Cowork and Antigravity transcripts). Both currently install on your computer with Python, so they need [Developer mode](DEVELOPER-SETUP.md#background-capture). Running them in the Docker preview is planned.

With the preview alone, memories come from what your AI apps save through CMF's tools (`remember`, `capture_session`) as you work.

## Reviewing What Was Captured

Captured sessions become **episodes** (things decided, planned or found) and, when a wiki is configured, **doc proposals** (reference knowledge for a wiki page). Both wait for review. From any connected client:

1. `list_review_conversations` shows which conversations have items waiting.
2. `list_episode_proposals` / `list_doc_proposals` (filter by `conversation_id` or `project`) show the items; `get_doc_proposal` shows a doc's diff.
3. `bulk_review_episodes` / `review_doc_proposal` record approve or reject, with a reason.
4. `promote_approved_episodes` writes an approved episode into memory (one per call); `apply_doc_proposal` writes an approved doc into the wiki (dry run first; each apply is committed in the wiki's git repo when it has one).

In [Developer mode](DEVELOPER-SETUP.md), from a terminal, `uv run python -m server.review.cli queue --tier 1` lists the queue and `uv run python -m server.review.cli promote --apply` promotes every approved episode.

**Nightly review.** A scheduled task in your AI app can do the first pass every night at 11:11 PM: it recommends approve, reject or flag for each new item and leaves you a summary to confirm in the morning. Set it up by pasting one block into the app: see [NIGHTLY-REVIEW.md](NIGHTLY-REVIEW.md).

---

## Notion knowledge (optional)

**Developer mode only for now:** the Docker preview doesn't pass the Notion settings into its container yet.

In your gitignored `.env`, set `CMF_KNOWLEDGE_PROVIDERS=notion` for Notion alone,
or `wiki,notion` to query both. Built-in names are `wiki`, `gmail`, and `notion`;
existing `module:Class` paths still work for external providers. For a local
source installation without a Markdown vault, leave `LLM_WIKI_PATH` empty.

Create a dedicated Notion internal integration with **Read content only**, share
the intended knowledge root with it, and set `CMF_NOTION_TOKEN` plus
`CMF_NOTION_WORKSPACE` (a stable workspace UUID/label used in result provenance).
Keep real tokens and deployment settings out of Git; `.env.example` contains
generic examples only. This is the first knowledge provider whose third-party
credential CMF holds directly. Enabling it exposes retrieved note excerpts to
**every connected client's `get_context`**; the scope label is not access control.

Notion is queried live with a default 10-second total deadline and five candidate
pages. `CMF_NOTION_PLAN` selects pacing: `free`/`plus` use 3 requests/s;
`business`/`enterprise` use 10. Server rate-limit cooldowns always take precedence.
`CMF_NOTION_CANDIDATE_LIMIT` accepts 1–20; `CMF_NOTION_TIMEOUT_SECONDS` accepts 1–30.
Bodies are cached only in process memory, keyed by page UUID and its freshly
retrieved edit timestamp. Restarting loses the cache; no sync job is needed.

Workspace REST search matches titles. To also discover matches in database
title/rich-text properties, set `CMF_NOTION_DATA_SOURCE_IDS` to up to two
data-source UUIDs (not database container IDs). Optional
`CMF_NOTION_DATA_SOURCE_FILTERS` is a JSON map from those UUIDs to native property
filters, for example a status restriction; these restrict only that discovery
branch. Property text and fetched page blocks are ranked locally with BM25.
This does not search arbitrary bodies outside the bounded discovered candidates.
Hosted Notion MCP offers broader content search but uses a different credential
and API contract and is outside this provider's REST implementation.

After sharing, verify a known note with `search_knowledge` restricted to
`providers=["notion"]`. Missing notes may be unshared or not yet indexed; an
empty response does not certify workspace coverage. Set
`CMF_NOTION_ROOT_PAGE_ID` to an expected shared page to make loss of root access
an explicit provider error. Authentication, network, or deadline failures appear
as Notion source errors while other knowledge sources still answer. Depth,
request, and text caps are marked as truncation in excerpts. Paragraph proposal
staging and apply dry runs use the same read token; real apply has separate write gates.

Tests are offline by default. Opt-in live tests require separate
`CMF_NOTION_TEST_TOKEN`, `_WORKSPACE`, `_QUERY`, and `_PAGE_ID` settings; see
`tests/test_notion_provider.py` for manual-edit/pagination fixture settings.
Never run them implicitly with operational credentials. The
[design](notion-provider-design.md) records bounds, API references, and remaining
live freshness assumptions.

## Configuration Reference

Configuration can be supplied via `.env`, environment variables, or CLI flags. The Docker preview passes only some settings into its container; the comments in `.env.example` say which.

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | Conditional | — | Required when any provider setting is `gemini` (the template's default). |
| `ANTHROPIC_API_KEY` | Conditional | — | Required when any provider setting is `anthropic`. |
| `OPENAI_API_KEY` | Conditional | — | Required when any provider setting is `openai`. |
| `FALKORDB_DATABASE` | Recommended | `default_db` | Target FalkorDB graph name. Always specify an explicit graph name (e.g. `CMF-local` or `memory-fabric`) to avoid silent collisions. |
| `LLM_WIKI_PATH` | Optional | — | Path to your Markdown wiki or Obsidian vault. When unset or empty, the wiki tools (`search_wiki`, `propose_doc_update`, ...) are not offered and extraction proposes episodes only. |
| `CMF_KNOWLEDGE_PROVIDERS` | No | `wiki` | Comma-separated `wiki`, `gmail`, `notion`, or external `module:Class` paths. |
| `FALKORDB_HOST` | No | `localhost` | FalkorDB host. Host install only; the preview sets or ignores it. |
| `FALKORDB_USER` | No | — | FalkorDB ACL user every CMF client logs in as (alias `FALKORDB_USERNAME`). Unset: the built-in `default` user. |
| `FALKORDB_PASSWORD` | No | — | Password for that user. |
| `FALKORDB_PORT` | No | `6379` | FalkorDB port. Host install only; the preview sets or ignores it. |
| `CMF_STATE_DIR` | No | repo root | Where `proposals-docs/` and `proposals-episode/` are kept. Host install only; the preview sets or ignores it. |
| `CMF_LLM_PROVIDER` | No | `gemini` | LLM provider: `anthropic`, `openai`, `gemini` or `local` ([which provider does what](#model-providers)). |
| `CMF_EMBED_PROVIDER` | No | `gemini` | Embeddings provider: `openai`, `gemini` or `local`. Not `anthropic`: it has no embeddings API. |
| `CMF_CAPTURE_LLM_PROVIDER` | No | `local` | LLM the background transcript pollers extract with. Same values as `CMF_LLM_PROVIDER`. Host install only; the preview sets or ignores it. |
| `CMF_NUDGE_MIN_TURNS` | No | `5` | Agent replies between capture-checkpoint nudges ([Capture checkpoints](DEVELOPER-SETUP.md#capture-checkpoints-no-transcript-reading)). |
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
- For sensitive security disclosures, see [SECURITY.md](../SECURITY.md).

### Notion paragraph proposals

The existing read token also supports `inspect_knowledge_document("notion", page_uuid)`
and proposal staging. Choose an editable paragraph's returned `target_path`, then
call `propose_knowledge_change` with its complete replacement text (1–2,000
characters). Formatted, linked, synced and non-paragraph targets are refused.
When `CMF_NOTION_ROOT_PAGE_ID` is set, staging must also prove the page lies
beneath that root within the shared four-ancestor allowance.
Inspection omissions are explicit; staging always reads fresh block evidence.

`get_doc_proposal` shows the exact block payload and `review_digest`. Supply that
digest as `expected_review_digest` when approving or rejecting. Wiki callers do
not need this extra parameter. These records live in the existing local doc
proposal store; protect it as private workspace content. Staging uses no new
configuration or credential permissions.

`apply_doc_proposal` defaults to a fresh, read-only dry run. Supply both
`expected_sha256` and `expected_review_digest` from `get_doc_proposal`; the
proposal must already be approved. The result shows the exact single PATCH
payload and refuses changed content, timestamps, scope or ancestry.

Real application is **off by default**. After separate operator authorization,
configure `CMF_NOTION_ENABLE_WRITES=true` and `CMF_NOTION_WRITE_TOKEN` in your
secret injection or gitignored `.env`. The write token must differ from
`CMF_NOTION_TOKEN`, have Read + Update content capability, and be shared only
with intended pages. Keep the read integration read-only. No Insert capability
is needed. These instructions do not authorize token changes, a service restart,
or a live fixture edit; the implementation has only been tested offline.

A real apply verifies access with the write token, saves an attempt before one
PATCH, then re-reads the paragraph and preserved fields. It uses no wiki path
or Git commit. Repeated application of an applied proposal does nothing.
Unknown/interrupted outcomes block another PATCH. Explicitly call
`apply_doc_proposal(..., reconcile=True)` with the same two digests to perform
fresh reads and record reconciliation (no PATCH even with `dry_run=False`).
Desired content marks applied with uncertain attribution; the unchanged exact
base marks `retry_ready` for a later explicitly requested apply; other changes
mark conflict and require a fresh proposal. Review digest and base never rebase.
Check application state in list/get responses. Local locks serialize review and
apply across CMF processes; there is no server-side conditional-write guarantee,
so external edits during GET/check/PATCH can still be overwritten. Apply only
while the target is not being edited. Automatic review/apply and consolidation
continue excluding Notion proposals. See [the design](notion-proposals-design.md).
