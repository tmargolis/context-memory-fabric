# Developer setup (from source)

This is **Developer mode**: CMF's server runs on your computer with Python from a checkout of this repository, and FalkorDB runs in Docker. It is the author's own setup and the way to work on CMF itself.

> [!IMPORTANT]
> **New users: use the [Docker preview](SETUP.md#quickstart-docker-compose-preview-recommended) instead.** Don't install half of each. The two paths read different config, use different FalkorDB ports (6379 here, 6380 in the preview) and different graph names, and both want port 8000. If the preview is running, stop it first (`docker compose -f docker-compose.preview.yml down`; this keeps its data) and don't install the launchd MCP server alongside it.

---

## Consolidated install (`~/.cmf`, in progress)

The setup plan ([setup-simplification-plan.md](setup-simplification-plan.md) §3.8) moves both modes to one folder, `~/.cmf`: one `.env`, one `state/` folder, one Docker stack (`deploy/docker/compose.yml`). Developer mode adds `deploy/docker/compose.dev.yml` as `~/.cmf/compose.override.yml`, which builds the server and poller from this checkout.

- **Config:** every CMF process reads `$CMF_HOME/.env` (default `~/.cmf/.env`) when it exists, otherwise this repo's `.env` (`server/core/env.py`). With `~/.cmf/.env`, state (journal, proposals, imports) defaults to `~/.cmf/state`.
- **Moving an existing install:** `scripts/consolidate_install.py plan` prints what would happen and changes nothing. `apply` backs up `.env`, state, a FalkorDB dump and the launchd plists to `~/.cmf/backups/<stamp>/`. It then copies config and state into `~/.cmf`, attaches the existing FalkorDB volume, disables (never removes) `com.cmf.mcp-server` and `com.cmf.capture-poller`, starts the stack and checks the graph's node count. If anything fails, it restores. `restore <stamp>` undoes it.
- **Until then**, the steps below still apply unchanged.

---

## Install

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

FalkorDB starts with no password; both ports bind to `127.0.0.1` only. Users you add with `ACL SETUSER` (e.g. a login for the Browser UI) persist across restarts and container recreates: the server keeps them in `/data/users.acl` on the data volume, so run `ACL SAVE` after changing one. To require a login, create your own user (`ACL SETUSER <name> on ><password> ~* &* +@all`), put it in `.env` as `FALKORDB_USER` / `FALKORDB_PASSWORD`, then turn the built-in user off (`ACL SETUSER default off`) and `ACL SAVE`. Every CMF client (server, pollers, scripts, replay, tests) reads those two variables; with `FALKORDB_USER` unset they connect as `default`. To reach the Browser from your other devices, share it over your VPN only (Tailscale: `tailscale serve --bg --https=8443 http://127.0.0.1:3001`), never through a public Funnel: while `default` is on, anyone who reaches the Browser can log in as it.

### 4. Configure Environment

Create `.env` in the repository root:

```bash
cp .env.example .env
```

Set the required variables:
```bash
# Model providers and their keys (see Model Providers in SETUP.md)
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

For a local embedder (Ollama), see [Run a Small Local Embedder](SETUP.md#optional-run-a-small-local-embedder-ollama); on the host, its URL is `http://127.0.0.1:11434/v1`. For every other setting, see the comments in `.env.example`: the **Host install only** block is for this mode.

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

## Connecting clients to a host server

The server can run per client over stdio (each client starts its own copy) or once over HTTP (step 5, `--transport streamable-http`), shared by every client. For one server shared by several apps, use HTTP and the `mcp-remote` entry in [SETUP.md](SETUP.md#connecting-your-mcp-client); it works the same against a host server.

**Claude Desktop, stdio.** In `claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/`, Windows: `%APPDATA%\Claude\`):

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
      ]
    }
  }
}
```

The server reads the repo's `.env`, so keys and the graph name don't need repeating in an `"env"` block. Claude Desktop may not find `uv` on its own `PATH`; if so, use its full path (`which uv`, e.g. `/opt/homebrew/bin/uv`). Quit and reopen Claude Desktop, then start a **new** session: a resumed one doesn't load new MCP servers.

**Cursor and other stdio clients:** command `uv`, args `run --directory /absolute/path/to/context-memory-fabric python -m server.mcp`.

More clients (Antigravity, Codex, remote connectors): [CLIENTS.md](CLIENTS.md).

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

- The nudge fires after `CMF_NUDGE_MIN_TURNS` agent replies (default 5), or after at least 2 replies and `CMF_NUDGE_MIN_MINUTES` (default 30). The reply that answers a nudge never triggers another.
- **Codex** runs a new hook only after you trust it: run `/hooks` in the Codex CLI once after installing. Its `hooks.json` uses `{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "..."}]}]}}`. If `/hooks` reports an unknown top-level `SessionStart` or `Stop`, rerun the Codex hook installer (`.venv/bin/python3 -m server.adapters.codex.cli hooks install`); it backs up and migrates the old flat format while preserving existing handlers. Close and reopen `/hooks` to review the corrected definitions. Installation/status does not prove that hooks are trusted or firing.
- **Each nudge costs one extra agent step**, and the session must have CMF connected. Without it the agent just finishes.
- Antigravity has no pre-compaction hook, and Claude Code's and Codex's can't prompt the agent, so a very long session is covered only by the periodic nudges.
- Chat apps without hooks (Claude Desktop chat, Cowork, ChatGPT) rely on the instructions block, plus a nightly review.

### Transcript pollers

CMF can also capture your coding sessions by polling their transcripts every 15 minutes: Claude Code (the CLI and the desktop app's Code tab, including subagents), Codex CLI, local Cowork sessions and Antigravity. [`deploy/pollers/README.md`](../deploy/pollers/README.md) installs the pollers as background jobs on macOS (launchd), Linux (systemd) or Windows (Task Scheduler).

Before turning them on:
- **Set `CMF_CAPTURE_LLM_PROVIDER`.** The pollers extract with it, not `CMF_LLM_PROVIDER`, and it defaults to `local`. Without a local model server, set it to the provider whose key you have. Unattended capture spends that provider's credit: one long session is dozens of extraction calls.
- **On Linux and Windows, set `CMF_PROJECT_ROOTS`** (e.g. `~/code,~/work`), so sessions are tagged with their project rather than `other`.

Cloud Cowork sessions leave no transcript on your computer; only their calls to CMF's tools are captured.
