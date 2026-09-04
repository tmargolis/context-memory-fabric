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

3. Restart Claude Desktop (`Cmd + Q` and reopen). All 7 Context Memory Fabric tools (`get_context`, `search_wiki`, `recall`, `remember`, `edit_memory`, `propose_wiki_update`, `import_memories`) will appear in the connectors/tool list.

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

## Available MCP Tools Summary

All connected clients receive access to the full suite of 9 Context Memory Fabric tools:

1. **`get_context(topic)`** *(Read-Only)* — Default unified context retrieval tool combining durable Wiki notes and recent episodic memory.
2. **`search_wiki(query)`** *(Read-Only)* — Lexical search across the local curated `LLM_Wiki` corpus (`WIKI/`, `REPORTS/`, `RAW/`, etc.).
3. **`recall(query)`** *(Read-Only)* — Semantic search across episodic memory facts and temporal history in FalkorDB / Graphiti.
4. **`remember(content, name, source_description)`** *(State Write)* — Ingests a substantive decision, milestone, preference change, or event into episodic memory.
5. **`edit_memory(target_query, new_reference_time, new_content, new_summary, new_name, dry_run)`** *(Memory Mutation)* — Edits, corrects, or re-dates existing episodic episodes, entity nodes, and graph edges, synchronizing local import state.
6. **`reconcile_memories(records, dry_run)`** *(Reconciliation)* — Consolidates, updates, and upserts episodic memories with real upsert/reject semantics in FalkorDB and synchronizes local import registry state.
7. **`propose_wiki_update(target_path, proposed_content, rationale)`** *(Proposal Write)* — Creates a reviewable staging proposal in `wiki-proposals/` without mutating the canonical Wiki.
8. **`import_memories(content, source, source_description, dry_run)`** *(Admin Ingest)* — Administrative bulk import tool for importing AI memory summaries (ChatGPT, Claude, Gemini) into episodic memory.
9. **`import_chatgpt_exports(paths, dry_run, graph_name, review_overrides, review_overrides_path)`** *(Admin Ingest)* — Parses native ChatGPT `conversations-*.json` export files by explicit file path and classifies candidates into episodic, durable, ambiguous, and non-memory buckets. Requires an explicit non-default `graph_name` when committing.
