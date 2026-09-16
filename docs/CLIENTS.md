# Client Integration & Harness Guide

This guide describes how to connect **Context Memory Fabric** to AI clients, desktop harnesses, and developer tools via the Model Context Protocol (MCP).

---

## 0. Running the Server Standalone (Network Transport)

Instead of letting each client spawn its own `stdio` subprocess (§1–3), you can run one shared server process and point every client at it over HTTP. Use this when comparing multiple harnesses (Claude Desktop, Gemini, ChatGPT, etc.) against the exact same server and graph — a separate `stdio` subprocess per client means each one resolves its own environment independently, so a stray `.env`/config difference between them can silently point one client at a different graph than the others.

1. Start the server with `streamable-http` (the current MCP transport; `sse` below is kept for older clients):

   ```bash
   uv run --directory /path/to/context-memory-fabric python -m server.mcp \
     --transport streamable-http --host 127.0.0.1 --port 8000 \
     > cmf-mcp.log 2>&1 &
   ```

   The server only logs to stderr (`logging.basicConfig(..., stream=sys.stderr)` in `server/mcp.py`, no file handler), which is why the redirect is needed to capture anything.

2. Tail the logs:

   ```bash
   tail -f /path/to/context-memory-fabric/cmf-mcp.log
   ```

3. Override an `.env` value for a single run by prefixing the command — most commonly `FALKORDB_DATABASE`, to point this run at a specific graph without editing `.env`:

   ```bash
   FALKORDB_DATABASE=mem-fabric-local-ep uv run --directory /path/to/context-memory-fabric python -m server.mcp \
     --transport streamable-http --host 127.0.0.1 --port 8000 \
     > cmf-mcp.log 2>&1 &
   ```

   If `FALKORDB_DATABASE` isn't set anywhere (`.env` or inline), `resolve_target_database()` (`server/providers/memory_graphiti.py`) falls back to `default_db` — a deliberate non-production sandbox, not one of the real graphs (e.g. `mem-fabric-local-ep`, `mem-fabric-local-wiki`), so a client with no explicit config can't silently read or write production data.

4. Point each client at `http://localhost:8000/mcp` (or a tunneled/Tailscale URL — see §4) instead of giving it a `command`/`args` subprocess to spawn.

5. Stop it: `pgrep -fl "server.mcp"` (filter out unrelated processes that merely mention the project path in an argument, e.g. shell/editor background tasks) then `kill <pid>`.

### Auth: OAuth 2.1 (recommended) or a static bearer token

`stdio` (§1–3's default configs) is trusted by process ownership alone — only something your OS already let spawn the subprocess can talk to it. `streamable-http`/`sse` have no such boundary: anyone who can reach the port can call every tool, full read/write, unless one of the two mechanisms below is configured. They're **mutually exclusive** — if both are set, OAuth wins and the static token is ignored (with a startup warning).

#### OAuth 2.1 (`CMF_MCP_ISSUER_URL` + `CMF_MCP_OAUTH_PASSWORD`) — use this for GUI clients

Claude Desktop's, ChatGPT's, and Gemini's own "add a custom connector" flows are built around OAuth (Dynamic Client Registration + authorization-code + PKCE) — none of their normal UIs have a field for pasting a static header, and OpenAI's Secure MCP Tunnel hard-requires valid OAuth metadata discovery even when a static header is also supplied. This server implements a minimal, single-user OAuth 2.1 authorization server (`server/core/oauth_provider.py`) specifically so all three can connect the way they're actually designed to.

Set **both** vars together, or leave both unset — the server refuses to start with only one:

```bash
CMF_MCP_ISSUER_URL=https://todds-macbook-air.tail54bb78.ts.net \
CMF_MCP_OAUTH_PASSWORD=$(openssl rand -hex 16) \
  uv run --directory /path/to/context-memory-fabric python -m server.mcp \
  --transport streamable-http --host 127.0.0.1 --port 8000 \
  > cmf-mcp.log 2>&1 &
```

- **`CMF_MCP_ISSUER_URL` must exactly match wherever the server is publicly reachable** (your Tailscale Funnel hostname, a tunnel URL, etc. — no trailing slash). Every client validates this against the server's own metadata; a mismatch breaks the flow, not just a formality. If you change how the server is exposed, this has to change too.
- **`CMF_MCP_OAUTH_PASSWORD` is the actual security boundary.** Both `/register` and `/authorize` are unauthenticated by spec (any client, including someone else's, can reach as far as the consent page) — this password is what a human has to know to actually approve a new client and get it a working access token. Save it somewhere durable; you'll type it once per client.

**What happens when a client connects**, for all three:
1. You paste the server URL (`https://<issuer-host>/mcp`) into the client's connector setup.
2. The client auto-discovers the server's OAuth metadata and self-registers via Dynamic Client Registration — no manual Client ID/Secret entry needed.
3. It opens a browser to `<issuer-url>/oauth/consent?request_id=...` — this server's own minimal consent page, showing the client's name and asking for `CMF_MCP_OAUTH_PASSWORD`.
4. Enter the password, click **Approve**. The page redirects back to the client with an authorization code; the client exchanges it for an access token (30-day expiry, refresh token good for 180 days) behind the scenes.
5. Tool calls proceed as normal, authenticated by that access token.

Per-client notes:
- **Claude Desktop:** Settings → Connectors → Add custom connector → paste the server URL. This is the path its UI is actually built for (unlike a static header, which its custom-connector flow has no field for at all).
- **ChatGPT:** Settings → Apps (or Plugins, naming varies by account) → Developer mode → Connectors → Create → **Server URL** tab → paste the URL, leave Authentication on **OAuth** (Dynamic Client Registration, the default) rather than "Mixed" or "None" — DCR means you don't need to touch the "Advanced OAuth settings" panel's Client ID/Secret/endpoint fields at all.
- **Gemini (Spark web/mobile):** Connected Apps / "Custom apps for Spark" → paste the server URL → follow its consent flow the same way.
- **ChatGPT via Secure MCP Tunnel:** also works now — `tunnel-client doctor`'s `oauth_metadata` check was failing before because this server had no valid Protected Resource/Authorization Server metadata to discover; it does now. Point `tunnel-client` at the server and select Authentication **OAuth** (not "None") in ChatGPT's tunnel connector dialog.

Access tokens and refresh tokens persist in `imports/journal/journal.db` (same sqlite file the journal/consolidation state already uses) — they survive a server restart, so clients don't need to re-approve every time you restart the server. Authorization codes and pending consent requests are intentionally in-memory only and don't survive a restart (they're short-lived by design; if a flow is mid-way through when you restart, the client just retries).

#### Static bearer token (`CMF_MCP_AUTH_TOKEN`) — direct/manual/scripted access only

For a raw HTTP client, a script, or `curl` — not for any of the GUI clients above, whose connector UIs don't have a field for it:

```bash
CMF_MCP_AUTH_TOKEN=$(openssl rand -hex 32) uv run --directory /path/to/context-memory-fabric python -m server.mcp \
  --transport streamable-http --host 127.0.0.1 --port 8000 \
  > cmf-mcp.log 2>&1 &
```

Send it as a standard bearer header: `Authorization: Bearer <token>`.

If neither `CMF_MCP_ISSUER_URL` nor `CMF_MCP_AUTH_TOKEN` is set, the server logs a warning on startup and runs unauthenticated — fine for a `127.0.0.1`-only run during local development, not for anything exposed further (see the Tailscale warning in §4).

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
        "FALKORDB_DATABASE": "mem-fabric-local-ep"
      }
    }
  }
}
```

> [!TIP]
> You can also pass `--wiki-path /path/to/your/LLM_Wiki` directly inside the `"args"` array instead of using the `"env"` block.

> [!NOTE]
> `FALKORDB_DATABASE` is optional. If omitted, the server falls back to `default_db`, a non-production sandbox — see §0 for why.

3. Restart Claude Desktop (`Cmd + Q` and reopen). All Context Memory Fabric tools will appear in the connectors/tool list — see [Available MCP Tools Summary](#available-mcp-tools-summary) below for the full, current list (count varies with whether `LLM_WIKI_PATH` is configured).

---

### Alternative: Network Connector (streamable-http or SSE)

If you'd rather connect to an already-running server instance (see §0) instead of having Claude Desktop spawn its own subprocess — e.g. so it shares one server/graph with other clients during a harness comparison:

1. Start the server per §0, with `--transport streamable-http` (preferred) or `--transport sse` (legacy).
2. In Claude Desktop, go to **Settings > Connectors > Add custom connector**.
3. Set the remote MCP server URL to `http://localhost:8000/mcp` (streamable-http) or `http://localhost:8000/sse` (SSE) — or a tunneled/Tailscale HTTPS URL if the server isn't running on the same machine (see §4).

Since Claude Desktop's connector flow is OAuth-only with no field for a static header, use §0's OAuth setup (`CMF_MCP_ISSUER_URL` + `CMF_MCP_OAUTH_PASSWORD`) if you want this connection authenticated — it'll self-register via DCR and walk you through the consent page automatically.

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
  - `FALKORDB_DATABASE`: `mem-fabric-local-ep` (optional — omit to use the `default_db` sandbox; see §0)

---

## 4. Multi-Agent & Remote Harnesses (ChatGPT, Gemini Spark, etc.)

Cloud-hosted clients (ChatGPT's connector, Gemini's web app, etc.) run outside your network and need an HTTPS URL they can actually reach — `localhost` isn't enough.

1. Start the server per §0 with `--transport streamable-http` (or `sse`).
2. Expose the port via one of:
   - **Tailscale Funnel** (see below, if your tailnet is already set up) — quickest if you're already on Tailscale.
   - A reverse proxy, Cloudflare Tunnel, or other HTTPS gateway.
3. Configure the remote MCP connector URL in the client's developer settings — `https://<your-tunnel-host>/mcp` (streamable-http) or `.../sse` (SSE).

### Exposing via Tailscale

Two different Tailscale commands do very different things here — don't confuse them:

- **`tailscale serve --bg 8000`** — reachable only by devices already on *your* tailnet (e.g. another one of your own machines, or a local Gemini CLI). **Not reachable by ChatGPT's or Gemini's cloud-hosted connector.** This is the safer default when it's sufficient.
- **`tailscale funnel --bg 8000`** — reachable by **anyone on the public internet** who has the URL. This is what a cloud-hosted client like ChatGPT actually needs.

> [!WARNING]
> The MCP HTTP transport (`streamable-http`/`sse`) has no auth of its own — every request that reaches the port can call every tool, including write tools with full access to your personal memory graph: `remember`, `edit_memory`, `reconcile_memories`, `import_chatgpt_exports`, `promote_auto_accepted_memories`. Configure OAuth (`CMF_MCP_ISSUER_URL` + `CMF_MCP_OAUTH_PASSWORD`, see §0 — this is what actually makes ChatGPT's and Gemini's connector flows work anyway) or, for scripted/direct access only, `CMF_MCP_AUTH_TOKEN`, **before** running `tailscale funnel`. A Funnel URL is not secret — it can surface in proxy logs, shared links, or synced browser history — and unlike `tailscale serve` it's reachable by literally anyone on the internet, not just your own tailnet devices. Confirm the server logged neither the OAuth-disabled nor the `CMF_MCP_AUTH_TOKEN is not set` warning on startup before flipping Funnel on.

Turn Funnel off when you're done testing: `tailscale funnel --bg off` (or `tailscale funnel reset` to clear all Funnel config).

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

All connected clients receive access to the full suite of 12 Context Memory Fabric tools (10 when `LLM_WIKI_PATH` is unset — `search_wiki`/`propose_wiki_update` are only registered when a knowledge provider is configured):

1. **`get_context(topic)`** *(Read-Only)* — Default unified context retrieval tool combining durable Wiki notes and recent episodic memory.
2. **`search_wiki(query)`** *(Read-Only)* — Lexical search across the local curated `LLM_Wiki` corpus (`WIKI/`, `REPORTS/`, `RAW/`, etc.).
3. **`recall_mem(query)`** *(Read-Only)* — Semantic search across episodic memory facts and temporal history in FalkorDB / Graphiti.
4. **`remember(content, name, source_description)`** *(State Write)* — Ingests a substantive decision, milestone, preference change, or event into episodic memory.
5. **`edit_memory(target_query, new_reference_time, new_content, new_summary, new_name, dry_run)`** *(Memory Mutation)* — Edits, corrects, or re-dates existing episodic episodes, entity nodes, and graph edges, synchronizing local import state.
6. **`reconcile_memories(records, dry_run)`** *(Reconciliation)* — Consolidates, updates, and upserts episodic memories with real upsert/reject semantics in FalkorDB and synchronizes local import registry state.
7. **`propose_wiki_update(target_path, proposed_content, rationale)`** *(Proposal Write)* — Creates a reviewable staging proposal in `wiki-proposals/` without mutating the canonical Wiki.
8. **`import_memories(content, source, source_description, dry_run)`** *(Admin Ingest)* — Administrative bulk import tool for importing AI memory summaries (ChatGPT, Claude, Gemini) into episodic memory.
9. **`import_chatgpt_exports(paths, dry_run, graph_name, review_overrides, review_overrides_path)`** *(Admin Ingest)* — Parses native ChatGPT `conversations-*.json` export files by explicit file path and classifies candidates into episodic, durable, ambiguous, and non-memory buckets. Requires an explicit non-default `graph_name` when committing.
10. **`capture_note(content, kind)`** *(Evidence Write)* — Milestone 4a: explicit checkpoint captured to the evidence journal (not episodic memory — see `remember` for that). Fire-and-forget.
11. **`capture_health()`** *(Read-Only)* — Milestone 4a: in-process capture status — events captured, dropped, redacted, current queue depth.
12. **`promote_auto_accepted_memories(dry_run, limit)`** *(Memory Write)* — Promotes consolidation candidates already classified `auto_accepted` into episodic memory (Graphiti/FalkorDB). Idempotent; only `auto_accepted` candidates are eligible — everything else needs Milestone 6 review tooling.
