# Client Integration & Harness Guide

This guide describes how to connect **Context Memory Fabric** to AI clients, desktop harnesses, and developer tools via the Model Context Protocol (MCP).

---

## 1. Claude Desktop

### Recommended: Local Desktop Configuration (`stdio`)

Claude Desktop can launch and manage Context Memory Fabric automatically in the background using standard I/O (`stdio`).

1. Open your Claude Desktop configuration file:
   - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

2. Add `context-memory-fabric` under the `"mcpServers"` object:

```json
{
  "mcpServers": {
    "context-memory-fabric": {
      "command": "/opt/homebrew/bin/uv",
      "args": [
        "run",
        "--directory",
        "/path/to/context-memory-fabric",
        "python",
        "-m",
        "server.mcp"
      ],
      "env": {
        "LLM_WIKI_PATH": "/path/to/your/LLM_Wiki",
        "GEMINI_API_KEY": "your-gemini-api-key",
        "FALKORDB_DATABASE": "memory-fabric"
      }
    }
  }
}
```

> [!TIP]
> You can also pass `--wiki-path /path/to/your/LLM_Wiki` directly inside the `"args"` array instead of using the `"env"` block.

3. Restart Claude Desktop (`Cmd + Q` and reopen). All Context Memory Fabric tools will appear in the connectors/tool list — see [Available MCP Tools Summary](#available-mcp-tools-summary) below for the full, current list (count varies with whether `LLM_WIKI_PATH` is configured).

---

### Alternative: Network SSE Connector

If you prefer connecting to a running server instance over HTTP/SSE:

1. Start the server in SSE mode:
   ```bash
   uv run python -m server.mcp --transport sse --host 127.0.0.1 --port 8000
   ```
2. In Claude Desktop, go to **Settings > Connectors > Add custom connector**.
3. Set the remote MCP server URL to `http://localhost:8000/sse` (or an HTTPS tunnel URL if using remote mode).

---

## 2. Antigravity IDE & VS Code

You can configure Context Memory Fabric as an MCP extension in your workspace settings:

1. In `.vscode/mcp.json` or Antigravity IDE configuration:

```json
{
  "mcpServers": {
    "context-memory-fabric": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "${workspaceFolder}",
        "python",
        "-m",
        "server.mcp"
      ]
    }
  }
}
```

---

## 3. Cursor / Other MCP-Enabled Clients

Add the MCP server command in your client settings:

- **Type:** `command` (or `stdio`)
- **Command:** `uv`
- **Args:** `run --directory /path/to/context-memory-fabric python -m server.mcp`
- **Environment Variables:**
  - `LLM_WIKI_PATH`: `/path/to/your/LLM_Wiki`
  - `GEMINI_API_KEY`: `AIzaSy...`
  - `FALKORDB_DATABASE`: `memory-fabric`

---

## 4. Multi-Agent & Remote Harnesses (ChatGPT, Gemini Spark, etc.)

For remote web clients that require an HTTPS endpoint:

1. Run the MCP server with the `streamable-http` or `sse` transport:
   ```bash
   uv run python -m server.mcp --transport streamable-http --port 8000
   ```
2. Expose the port securely through your chosen reverse proxy, Cloudflare Tunnel, or HTTPS gateway.
3. Configure the remote MCP connector URL in the client's developer settings.

---

## MCP-Boundary Capture (Milestone 4a)

Every tool call any connected client makes is automatically journaled as evidence in the background (`server/capture/middleware.py`), regardless of which client is connected — this is one implementation covering Claude Desktop, Claude Code, Cursor, ChatGPT-via-MCP, and any other MCP client, rather than a per-client adapter. It writes to the same append-only journal Milestone 2's importers write to (`imports/journal/journal.db`), tagged with a normalized harness identity and a session identity (see below).

**What this captures:** every tool call routed through this server, with harness provenance, redacted arguments, and a truncated result summary.

**What it does not capture — a real limitation, not a technicality:** turns where the client never calls a Context Memory Fabric tool. Capture is *interaction-triggered*, not a continuous transcript. There is no architecture here that captures a Claude Desktop conversation CMF's tools were never invoked during. Mitigations:

- Call `capture_note` at natural checkpoints (a decision reached, a milestone hit, a session wrapping up) to leave an explicit marker beyond what tool-call capture alone records.
- `get_context` at session start pulls in durable/recent state proactively.
- Periodic Claude/ChatGPT/Gemini export ingestion (Milestone 2's importers) backfills the gaps between what capture caught live and what actually happened.

**Session identity:** MCP has no native "conversation" concept. A transport-level `session_id` exists for streamable-http/SSE connections but is `None` on stdio — which is exactly the transport Claude Desktop uses. Context Memory Fabric therefore synthesizes its own session identity (a UUID minted once per connection, cached for that connection's lifetime) when no transport session id is available. This is CMF-local identity, not a protocol-level session id, and does not persist across a client restart (a new stdio connection gets a new synthesized session id).

**Secret filtering:** arguments are scanned for credential-shaped values (API keys, OAuth tokens, JWTs) and credential-named fields (`api_key`, `token`, `secret`, `password`, etc.) before anything is hashed or written to the journal — see `server/capture/filters.py`.

**Excluded by default:** `import_memories` and `import_chatgpt_exports` calls are not captured through this generic path — they already have dedicated Milestone 2 importers producing higher-fidelity source events (structured branch reconstruction, per-message provenance) than generic call capture could. Configurable via `CMF_CAPTURE_DENY_TOOLS`/`CMF_CAPTURE_DENY_CLIENTS` in `.env`.

**Never blocks a tool call:** capture is fire-and-forget onto a bounded in-process queue (default 500 events); a full queue drops the newest event and counts it rather than blocking. Check current status with the `capture_health` tool.

**Per-client notes:**

| Client | Transport (typical) | Session identity | Notes |
|---|---|---|---|
| Claude Desktop | stdio | Synthesized (no native session id) | Primary MS4a target. No local transcript exists for CMF to backfill from beyond this capture and periodic Claude exports. |
| Claude Code | stdio (via this server, if configured as an MCP tool) | Synthesized | Claude Code also has its own much higher-fidelity local transcript/hook capture path — Milestone 4b, separate from this generic MCP-boundary capture. |
| Cursor / other stdio clients | stdio | Synthesized | Same limitation as Claude Desktop. |
| Remote/HTTP clients (streamable-http, SSE) | HTTP | Native transport session id, prefixed `native:` | More stable across reconnects than a synthesized id. |

---

## Available MCP Tools Summary

All connected clients receive access to the full suite of 11 Context Memory Fabric tools (9 when `LLM_WIKI_PATH` is unset — `search_wiki`/`propose_wiki_update` are only registered when a knowledge provider is configured):

1. **`get_context(topic)`** *(Read-Only)* — Default unified context retrieval tool combining durable Wiki notes and recent episodic memory.
2. **`search_wiki(query)`** *(Read-Only)* — Lexical search across the local curated `LLM_Wiki` corpus (`WIKI/`, `REPORTS/`, `RAW/`, etc.).
3. **`recall(query)`** *(Read-Only)* — Semantic search across episodic memory facts and temporal history in FalkorDB / Graphiti.
4. **`remember(content, name, source_description)`** *(State Write)* — Ingests a substantive decision, milestone, preference change, or event into episodic memory.
5. **`edit_memory(target_query, new_reference_time, new_content, new_summary, new_name, dry_run)`** *(Memory Mutation)* — Edits, corrects, or re-dates existing episodic episodes, entity nodes, and graph edges, synchronizing local import state.
6. **`reconcile_memories(records, dry_run)`** *(Reconciliation)* — Consolidates, updates, and upserts episodic memories with real upsert/reject semantics in FalkorDB and synchronizes local import registry state.
7. **`propose_wiki_update(target_path, proposed_content, rationale)`** *(Proposal Write)* — Creates a reviewable staging proposal in `wiki-proposals/` without mutating the canonical Wiki.
8. **`import_memories(content, source, source_description, dry_run)`** *(Admin Ingest)* — Administrative bulk import tool for importing AI memory summaries (ChatGPT, Claude, Gemini) into episodic memory.
9. **`import_chatgpt_exports(paths, dry_run, graph_name, review_overrides, review_overrides_path)`** *(Admin Ingest)* — Parses native ChatGPT `conversations-*.json` export files by explicit file path and classifies candidates into episodic, durable, ambiguous, and non-memory buckets. Requires an explicit non-default `graph_name` when committing.
10. **`capture_note(content, kind)`** *(Evidence Write)* — Milestone 4a: explicit checkpoint captured to the evidence journal (not episodic memory — see `remember` for that). Fire-and-forget.
11. **`capture_health()`** *(Read-Only)* — Milestone 4a: in-process capture status — events captured, dropped, redacted, current queue depth.
