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
        "GEMINI_API_KEY": "your-gemini-api-key"
      }
    }
  }
}
```

> [!TIP]
> You can also pass `--wiki-path /path/to/your/LLM_Wiki` directly inside the `"args"` array instead of using the `"env"` block.

3. Restart Claude Desktop (`Cmd + Q` and reopen). The 5 Context Memory Fabric tools (`get_context`, `search_wiki`, `recall`, `remember`, `propose_wiki_update`) will appear in the connectors/tool list.

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

---

## 4. Multi-Agent & Remote Harnesses (ChatGPT, Gemini Spark, etc.)

For remote web clients that require an HTTPS endpoint:

1. Run the MCP server with the `streamable-http` or `sse` transport:
   ```bash
   uv run python -m server.mcp --transport streamable-http --port 8000
   ```
2. Expose the port securely through your chosen reverse proxy, Cloudflare Tunnel, or HTTPS gateway.
3. Configure the remote MCP connector URL in the client's developer settings.
