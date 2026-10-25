# Setup simplification plan

**Status:** Draft for Todd's review. v1 2026-10-21; v2 2026-10-22 adds the **CMF Setup app** (a platform-neutral GUI for setup, adding features and repair); v3 2026-10-23 records Todd's answers to the open questions (§7) and adds **install modes** (§3.8); v4 2026-10-23 consolidates both modes into `~/.cmf/`, adds bi-directional capture (§3.9) and communication Sources. No code changes yet.
**Evidence:** the 75-minute setup call with Noel on 2026-10-21 (transcript kept locally, not in the repo), the screenshots and messages from that session, and a read of `docs/SETUP.md`, `docs/CLIENTS.md`, `.env.example`, both compose files, `deploy/pollers/` and the `remember` / `recall_mem` error paths.
**Related:** [PUBLIC-PREVIEW-ONBOARDING.md](PUBLIC-PREVIEW-ONBOARDING.md) already names the goals (Docker-only, `cmf doctor`, optional wiki). This plan is the concrete version, ordered by what actually broke.

---

## 1. What went wrong on the call

Noel is technical, followed SETUP.md, and had Docker, three API keys and Claude Desktop ready. After 75 minutes with the author helping live, CMF still had **no memory written**. Every problem below happened on the call.

| # | Symptom | Root cause | Time lost |
|---|---|---|---|
| P1 | `recall_mem` / `get_context`: "connection refused, localhost:6379" | Noel ran the **Docker preview** (FalkorDB on host port **6380**), then installed host-side pieces (pollers, launchd MCP server) that read `.env`, where the template sets `FALKORDB_PORT=6379`. The two install paths have different ports, and the template's defaults suit the path the quickstart doesn't use. | ~15 min |
| P2 | Kill a server, it comes back; "which server am I talking to?" | **Two CMF servers fighting over 127.0.0.1:8000:** the preview container (`cmf-preview-server`) and the launchd agent from `install.sh --mcp-server` (KeepAlive, same port). Whichever binds first wins and the other crash-loops. Claude's calls went to whichever won; the Docker log showed no requests. Recovering needed `lsof`, `ps`, `launchctl` and a third-party app (LaunchControl). | ~20 min |
| P3 | `CMF_EMBED_PROVIDER=anthropic` fails | **Provider setup is three variables plus a key per provider, with rules the user can't see.** Noel had three keys but never chose providers (defaults: gemini). Changing to "Anthropic for both" failed late because Anthropic has no embeddings API. `CMF_CAPTURE_LLM_PROVIDER` defaults to `local` and was commented out. Unused `CMF_LOCAL_*` lines confused him. Placeholder keys (`your_gemini_api_key_here`) are non-empty, so nothing fails until the first model call. | ~12 min |
| P4 | Poller install: "No interpreter" | The Docker path never creates `.venv`, but the poller installer needs `.venv/bin/python3`, so a Docker user has to also do the host-Python setup (`uv sync`). Commands also fail unless run from the repo folder. | ~8 min |
| P5 | Claude can't reach CMF | **Client wiring is undocumented or stale.** The docs say developer config with `mcp-proxy`; Todd now uses Connectors; Connectors reject `localhost` ("enter a public URL"), so it went back to developer config, with `mcp-remote` this time. Then a Code-tab session, a Chat-tab config, `claude mcp list` (no CLI on PATH), config edits and restored backups. New MCP entries only load in a **new** session, never a resumed one, and nothing said so. | ~25 min |
| P6 | "Error executing tool", nothing else | The MCP SDK hides any unexpected exception from the client and logs the traceback on the server only (`mcp/server/mcpserver/exceptions.py`, `UnexpectedToolError`). On Noel's machine that log was in a container or a launchd log file he didn't know about. | throughout |
| P7 | "Remember worked", then the graph was still empty | `remember` returns `queued — not yet confirmed` immediately (`remember_queued`); the real write runs in the background, and a failure there only reaches the server log. Both Noel and his Claude read the ack as success. The call ended on an empty graph with the cause unknown. | end of call |
| P8 | FalkorDB Browser: "Graphs: 0", no `memory-fabric` | The preview graph is `CMF-local`; the author expected `memory-fabric`. Nothing creates the graph until the first successful write, so an empty dropdown looks like a broken install. | ~5 min |
| P9 | "How does it know my workspace?" / "Why did it say Project Aether?" | `LLM_WIKI_PATH` still pointed at the fictional `starter-wiki`. Noel has an Obsidian/Quartz wiki and a `Work` folder and didn't know how to point CMF at them, or that the preview needs the container recreated after changing it. Also unclear: whether he has to say "CMF" for it to be used (the INSTRUCTIONS-FOR-AGENTS step). | ~5 min |
| P10 | Debugging over Meet | Tiny terminal text, commands pasted through Meet chat, no single "what's wrong" command. Every diagnosis took a round of `docker ps`, `lsof`, `grep .env` and screenshots. | throughout |

**The underlying problem:** CMF has **two install paths that look like one** (Docker preview and bare-metal host), and the docs let a user build half of each. Each half reads its own config (compose environment variables vs. `.env`), uses its own ports (6380 vs. 6379) and its own graph name (`CMF-local` vs. `memory-fabric`), and the two can both claim port 8000.

---

## 2. Design principles

1. **One supported path for new users:** Docker only. Everything runs in the compose stack, including the capture poller. No host Python, `uv`, `.venv` or launchd. The from-source path stays as **Developer mode** (§3.8), documented separately and kept out of the Quickstart. LaunchControl is dropped from all guidance.
2. **One app for setup, adding features and repair: CMF Setup.** A double-clickable, platform-neutral GUI. The same engine also runs as a command line (`cmf doctor`, `cmf apply`) for scripting and remote help. A user should never need `lsof`, `ps`, `launchctl` or `grep .env`.
3. **Re-runnable and idempotent.** Opening the app again always starts from the machine's *actual* state (files, containers, client configs), never from a remembered wizard. It shows what's healthy, what's broken (with a Fix button) and what can be added.
4. **Preview, back up, then apply.** Every change shows a plain-language list of what will be written or restarted, backs up each file it touches, and can be undone.
5. **Fail at startup, not on the first tool call.** Bad provider combinations, placeholder keys, unreachable FalkorDB and wrong embedding widths are caught before the server accepts connections, and the app shows the same checks.
6. **Every tool error says what to do.** No bare "Error executing tool" for anything we can foresee.
7. **Confirmed writes.** A user can see whether a memory actually landed without reading logs.
8. **One CMF per machine,** and both the app and the AI client can tell which one is running.

---

## 3. CMF Setup: the app

### 3.1 Is a platform-neutral GUI feasible? Yes.

The app needs to do things a web page or a container can't do on its own: run `docker compose`, read and write `claude_desktop_config.json` / `~/.claude.json` / Codex and Cursor configs, find leftover launchd agents, open a folder picker, and store keys. So it has to be a small program on the host. The options:

| Option | How it works | Runtime the user needs | Verdict |
|---|---|---|---|
| **A. Go binary + local web UI (recommended)** | One self-contained executable per OS. Double-clicking it starts a server on `127.0.0.1` (random port, one-time token in the URL) and opens the default browser to it. The UI is plain HTML/JS embedded in the binary. Run with arguments, it's the CLI. | None | Smallest and simplest; one codebase for macOS, Windows and Linux; CLI and GUI share the engine, so they can't drift. Looks like a web page rather than a native app. |
| B. Tauri (Rust + web UI in a native window) | Same UI, shown in a native window using the OS web view. | None | Native feel, ~10 MB. More build tooling (Rust, per-OS web view quirks). A reasonable upgrade later; the UI carries over from A. |
| C. Python + pywebview / tkinter | Python app with a GUI toolkit. | Python 3.12 on the host | Brings back the host-Python requirement we're removing (P4). No. |
| D. Electron | Full Chromium bundled. | None | ~150 MB per platform, heavy to maintain. No. |
| E. Web UI inside the CMF container | The server serves a `/setup` page. | Docker already running | Chicken-and-egg: it can't help until Docker and the stack are up, and it can't edit host client configs or find launchd agents without broad host mounts. Useful only as a status page (see 3.6). |

**Recommendation: option A,** written so the UI can later move into a Tauri window (option B) without rewriting it.

### 3.2 No git clone

Today a user clones the repo and runs compose from it. With the app:

- the CMF server image is published to GitHub Container Registry (`ghcr.io/tmargolis/context-memory-fabric:<version>`);
- the app carries its own compose file and writes it, the `.env` and CMF's state to one folder: **`~/.cmf/`** (Windows: `%USERPROFILE%\.cmf`);
- updating CMF = "Update" in the app (pull the new image, recreate, run the checks).

The repo checkout stays the developer path. This also removes the "you have to be in the repo folder" problem (P4).

### 3.3 Screens

**Status (opens first, every time).** The doctor checklist, each row green / yellow / red with a one-line explanation and a **Fix** button when the app can fix it:

- Docker installed and running
- CMF server and FalkorDB containers healthy
- Exactly one CMF on port 8000, and it's ours (finds leftover launchd agents, host `server.mcp` processes, other containers)
- FalkorDB reachable; graph exists, with N memories
- Model providers answer (a 1-token LLM call, a 1-string embedding)
- Embedding width matches the graph
- Knowledge sources reachable (wiki folder: N files; Notion: root page readable)
- Capture running; last pass time; last 5 background memory writes and any errors
- Each enabled AI app has exactly one CMF entry, pointing at this server
- **AI apps seen:** which clients actually connected in the last 24 h (Claude Chat, Code tab, Cowork, Codex, ...), from `/health` (item 1.5). An app that is configured but never seen is yellow, with "quit and reopen it, then start a new session".
- **Self-test:** write a memory to a scratch graph, recall it, clean up (Phase 1, item 1.7)

Buttons along the bottom: **Copy diagnostics** (a redacted report: versions, checks, last 50 ERROR log lines, no keys or memory content, to send to Todd; fixes P10), **View logs**, **Open FalkorDB Browser** (preselects the right graph; fixes P8).

**Features.** A toggle per feature showing its current state read from the machine, with the settings for it. Changing anything enables **Review changes**.

**Review changes.** A plain-language list ("Write `~/.cmf/.env` (backup kept)", "Recreate the CMF server container", "Add CMF to Claude Desktop (backup kept); quit and reopen Claude, then start a new session"), plus cost notes where relevant. **Apply** runs the steps with live progress, then re-runs Status.

**First run** is the same three screens, with Features prefilled with a sensible minimum (one model provider, memory-only or a notes folder, Claude Desktop if found).

### 3.4 Features the app manages

Defined in one **feature manifest** (a JSON/YAML file shipped with the app) so adding a feature is mostly data, not UI code. For each feature: label, description, inputs, dependencies and conflicts, what it writes (`.env` keys, compose services or profiles, client config entries), its Status checks, and a cost note.

| Group | Feature | Inputs | Notes and rules the app enforces |
|---|---|---|---|
| **Core** (always on) | CMF server + FalkorDB | none (ports auto-chosen if 8000 is busy) | Graph name fixed per install (`cmf`), never asked. |
| **Models** | LLM for extraction | providers, **ranked by the user**: Anthropic / OpenAI / Gemini / Local (Ollama, LM Studio, or any OpenAI-compatible URL such as a Spark box); key or URL; model | Every provider with a key is listed; the user drags them into order. The top one is used; the rest are fallbacks on rate-limit or auth errors (item 1.8). "Test" button makes one call per provider. Masked key fields; blank, not placeholder text. |
| | Embeddings | **exactly one** provider: OpenAI / Gemini / Local; key or URL | Single choice, no ranking (vectors from different models can't be mixed). Anthropic not offered (P3). **Locked once the graph has data**; changing it offers "start a new graph" with a warning. |
| | Background capture LLM | "Same as extraction" (default) or another | Used by the poller and promotion. Removes the hidden `local` default (P3). |
| **Knowledge** | First-run choice | **A. No knowledge base** (memory only) / **B. Demo wiki** (fictional) / **C. Bring your own** | One of three, asked on first run and changeable later. C opens the source rows below. |
| | LLM Wiki / notes folder | folder picker (Obsidian, Quartz, any Markdown) | Mounted read-only; Status shows the file count. Proposals still go to review. |
| | Notion | integration token, workspace label, optional root page | "Test" reads the root page. Shows the existing privacy note (excerpts reach every connected client). Uses the settings of the live Notion provider. |
| | Demo notes (starter wiki) | none | Option B only; labeled "fictional demo data" (P9). |
| | Google Drive, Microsoft 365 / SharePoint | — | Shown under C as **"coming soon"** (disabled rows). Each needs a new knowledge provider in the server (not in this plan). |
| **Sources** (what CMF listens to) | AI assistants | per app, auto-detected: Claude Code / Code tab, Cowork, Codex, Antigravity, ChatGPT, Gemini | **On by default** (bi-directional, §3.9). One live capture method per app: hook where the app has hooks, otherwise the poller, otherwise server instructions. Shows an estimated cost per day before Apply. |
| | Communication: Gmail, Slack, Teams | — | Shown as **"coming soon"**. These are listening sources, like the AI assistants: new messages become reviewable episodes or doc proposals rather than a searchable knowledge base. The current Gmail sent-mail snapshot provider is not shown in the app; it stays an env-only developer setting until it is redesigned as a listener (out of scope). |
| | Backfill past sessions | last N days, per AI app | **Optional**, offered once after setup and available later. Estimate before it starts; progress bar (item 4.1). |
| **AI apps** (where CMF is reachable) | Claude Desktop (Chat) | none (auto-found) | Writes one entry that runs the app's own bridge (`cmf bridge`, item 3.8; `mcp-remote` until then, which needs Node, so the app checks for it). Removes duplicate CMF entries, backup kept. Says to quit, reopen and start a new session (P5). |
| | Claude Code / Code tab | none | Which file the desktop Code tab reads is unconfirmed (Q3). The app writes the Desktop entry and the "AI apps seen" check (3.3) confirms whether the Code tab connected; confirmed on the first tester's setup. No `claude` CLI needed. |
| | Codex, Cursor, Antigravity / VS Code | none | Each tool's own config file. |
| | Capture hook | per app with hooks | Installs the `cmf-capture` Stop hook (Claude Code / Code tab, Cowork plugin, Codex, Antigravity). On whenever that app is a Source. |
| | Agent instructions | per app | **Only for apps that get neither server instructions nor a hook** (§3.9): a short, marker-fenced block in CLAUDE.md / AGENTS.md, or text to paste into a settings box. Never duplicates the server instructions. Answers "do I have to say CMF?" (P9): no. |
| **Remote access** (advanced) | ChatGPT, Gemini, Claude Connectors | public URL; OAuth password generated by the app | Off by default. Explains these need a public HTTPS URL (Connectors reject localhost). **OAuth is a required dependency**: turning on any connector turns on OAuth, and the app sets `CMF_MCP_ISSUER_URL` to the public URL. **Tunnel detection:** the app looks for Tailscale (installed, signed in, Funnel allowed), cloudflared and ngrok, and offers to set up only what CMF needs, e.g. `tailscale funnel --bg 8000`, after OAuth is on. It never installs, signs in to or reconfigures the VPN/tunnel beyond CMF's port; if none is found it links to install one, or the user pastes their own URL. Undo removes only what the app added. |
| **Review** | Nightly review | schedule | Shows the NIGHTLY-REVIEW.md block to paste into the AI app; the app can't create the scheduled task itself. |

### 3.5 Repair and re-run behavior

- **Source of truth is the machine.** On open, the app reads `~/.cmf/.env`, `docker compose ps`, each client config and launchd/systemd/Task Scheduler entries. It keeps no hidden wizard state that can disagree with reality.
- **Each Status check maps to a Fix:** start Docker Desktop; recreate unhealthy containers; **disable** a conflicting launchd agent / systemd unit / scheduled task (stop it and mark it disabled; the plist or unit file stays where it is, never deleted or uninstalled); rewrite a client entry; reset an embedding mismatch to a new graph (with explicit confirmation); restart capture. Fixes work the same in both install modes (§3.8).
- **Migration from today's installs (P1, P2, P4):** the first run on a machine like Noel's finds the repo-based preview stack, the launchd MCP server and pollers, and a repo `.env`. It runs the consolidation in §3.8 (back up, copy into `~/.cmf/`, attach the existing graph, **disable** the replaced services, verify), in either mode. It never deletes a FalkorDB volume without a separate, explicit confirmation.
- **Undo:** every Apply writes a dated backup set; **Restore previous setup** puts the files back and recreates the containers.

### 3.6 Security

- The local UI binds `127.0.0.1` only, on a random port, and requires the one-time token from the URL the app opened (so another web page or local process can't drive it). It stops when the window or tab closes, or after 30 idle minutes.
- Keys live in `~/.cmf/.env` with owner-only permissions (`chmod 600`; ACL on Windows). The UI never shows a saved key in full.
- Every Apply step that writes outside `~/.cmf` (client configs, hooks) is listed in Review before it runs.
- Copy diagnostics redacts keys, tokens and memory content, and shows the report before copying.
- Later option: keys in the OS keychain, with the app writing a temporary env file at container start.

### 3.7 Distribution

- GitHub Releases: macOS universal binary (Apple Silicon + Intel), Windows `.exe`, Linux AppImage or plain binary. Versioned together with the server image.
- **Unsigned for the preview (decided, Q9).** macOS Gatekeeper blocks a double-clicked unsigned download, so the Quickstart and the release page give explicit steps with screenshots: right-click (or Control-click) the app → **Open** → **Open** again; on newer macOS, if there's no Open button, System Settings → Privacy & Security → **Open Anyway**. Windows SmartScreen: **More info** → **Run anyway**. Notarization (Apple Developer account, $99/yr) is revisited before a wider release.
- Optional later: `brew install --cask cmf-setup`, `winget`.

### 3.8 Install modes: one home, two ways to run the server

Both modes share **one home, `~/.cmf/`**, and one set of everything. The only difference is where the CMF server (and poller) code comes from. The mode is recorded in `~/.cmf/mode.json` (`{"mode": "standard"}` or `{"mode": "developer", "repo": "/path/to/checkout"}`) and shown at the top of Status. **Everything is writable in both modes**; every change goes through Review with a backup, as in §3.5.

| Component | Standard (new users) | Developer (from source; Todd) |
|---|---|---|
| Config | `~/.cmf/.env` | the same file. The server finds it via `CMF_HOME` (default `~/.cmf`) instead of searching from the working folder (item 2.4). |
| State (journal, proposals, imports) | `~/.cmf/state/` via `CMF_STATE_DIR` | the same folder |
| FalkorDB | one container + one volume in the `~/.cmf` stack | the same container and volume |
| CMF server | `cmf-server` service from the published image | the same service, **built from the checkout** (`build:` instead of `image:`, source bind-mounted). Running `server.mcp` on the host for debugging is allowed, but the port guard (1.6) stops two servers running at once. |
| Capture poller | `cmf-poller` service from the published image | the same service, built from the checkout |
| Dev-only extras | — | `docker-compose.dev.yml` as an **overlay** on the same stack (Spark proxy, ACL entrypoint), not a second stack |
| Client entries, hooks, agent instructions | written by the app | written by the app |

Because there is one of each, duplicates (two servers on the port, two pollers capturing every transcript twice, two CMF entries in one AI app, two graphs) can't happen by design. Status still checks for leftovers from older installs.

**Consolidating an existing install** (Todd's Mac first, then Noel's) is item 2.5. Every step backs up before it deprecates anything, and **Restore previous setup** undoes it:

1. **Back up** into `~/.cmf/backups/<date>/`: the repo `.env`; the state folders (`proposals-*`, `episode-proposals`, `doc-proposals`, `imports`, the journal); a FalkorDB dump (`BGSAVE`, then copy the RDB file); and every CMF launchd plist.
2. **Copy** the `.env` and state into `~/.cmf/`. The originals stay in the repo; `.env` is renamed `.env.migrated-<date>` so the server can't load two configs.
3. **FalkorDB:** attach the existing volume to the new stack (no data copy). Journal replay into a new graph is the fallback.
4. **Disable**, never uninstall, the launchd jobs the stack now replaces (`com.cmf.mcp-server`, `com.cmf.capture-poller`). Other jobs are shown, each with its purpose, and left alone unless Todd chooses otherwise. That includes `com.cmf.endpoint-probe` and `com.cmf.spark-tunnel`; the Spark tunnel is user-managed infrastructure (Q11).
5. **Verify:** self-test (1.7), recall a known memory from the live graph, and check that the AI apps have connected. If anything fails, restore automatically.

**Switching modes** later just changes where the server image comes from and recreates the containers. Data and config don't move.

### 3.9 Bi-directional by default: capture, recall and prompt context

CMF is **on in both directions by default**: AI apps read from it (recall, `get_context`, knowledge search) and write to it (capture). The first-run screen states this plainly, with the cost estimate, and a single switch turns capture off.

**How each source is captured: hooks first.**

| Method | Used for | Cost lands on |
|---|---|---|
| **Hook** (Stop hook asks the assistant to call `capture_session` at checkpoints; already throttled to every 5 turns / 30 min) | Claude Code and Code tab, Cowork, Codex, Antigravity: every app with a hook system | the user's AI app (a few hundred tokens per checkpoint), plus CMF's LLM/embedding calls when items are promoted to the graph |
| **Transcript poller** (`cmf-poller` extracts from transcripts on disk) | apps that write transcripts but have no hooks; backfill | CMF's LLM provider, for every transcript |
| **Server instructions only** (the MCP server's own instructions ask for checkpoints) | cloud apps with neither: ChatGPT, Gemini, Claude Chat via connector | the user's AI app |

Each source uses **one live method**, so the same conversation isn't captured twice: a hook where the app has hooks, otherwise the poller. A "full transcript" option (the poller as well as, or instead of, the hook) sits under Advanced for people who want everything extracted.

**Estimates, wherever possible.** Live capture: estimated cost per day from the last 7 days of local transcript volume × the ranked provider's price, or a per-session range on a fresh machine with no history. Backfill: an estimate for the chosen date range before it starts. A local model shows $0 with an estimated time instead. Status shows actual spend so far against the estimate.

**Backfill is optional.** It's offered once after setup ("Import your last N days of conversations from before CMF was installed?"), with its estimate; it can also be run later from Features.

**Prompt context stays minimal.** Every token CMF adds to every session costs the user, so:

- The **MCP server instructions are the single source** of "when to use CMF". Hooks and agent instructions never repeat them.
- **Agent-instruction blocks** (CLAUDE.md, AGENTS.md, settings boxes) are installed only for apps that receive neither server instructions nor hooks. When installed, the block is short and fenced with `<!-- cmf:begin -->` / `<!-- cmf:end -->` markers so it's replaced in place, never appended twice. The app flags any other CMF text it finds in those files as a duplicate.
- Status shows a **prompt-context budget** per AI app: the approximate tokens CMF adds per session (server instructions + tool descriptions + hook text + any agent block).


---

## 4. Implementation plan

### Phase 0: Stop the bleeding (docs/config only, no server code)

Nothing in Phase 0 or later waits on Noel. His follow-ups are listed separately below.

| Item | What | Status |
|---|---|---|
| 0.1 | SETUP.md: put a bold "pick ONE path" box at the top. Docker path = do not run `install.sh --mcp-server`, do not `uv sync`. Mark the poller section "host install only" until Phase 2 lands. | ✅ Done (b7e9a10) |
| 0.2 | SETUP.md / CLIENTS.md: replace the Claude Desktop instructions with what works today: developer config with `mcp-remote` to `http://127.0.0.1:8000/mcp` (needs Node; Connectors can't reach localhost); start a **new** session after any MCP change. For the Code tab, say the Desktop entry is what worked on Noel's machine and is still being confirmed (Q3). Remove LaunchControl from all guidance (Q7). Move the from-source steps to a separate `docs/DEVELOPER-SETUP.md` (Q1). | ✅ Done (b7e9a10) |
| 0.3 | `.env.example`: split into a short "Docker preview" block on top (providers, keys, wiki path) and a clearly fenced "host install only" block (`FALKORDB_HOST/PORT`, `CMF_LOCAL_*`, poller settings). Blank the placeholder keys instead of `your_..._here`. | ✅ Done (b7e9a10) |
| 0.4 | Document what "working" looks like: run `remember`, wait ~30 s, refresh the FalkorDB Browser, select `CMF-local`, see nodes. Fix the `memory-fabric` / `CMF-local` naming in the docs. | ✅ Done (b7e9a10) |

### Noel follow-ups (non-blocking)

These help confirm P7's exact cause and tidy Noel's machine. They run whenever he has time; nothing above or below depends on them. Phase 1 (1.2, 1.4, 1.7) addresses the likely causes either way, and his migration test (§5, step 3) would surface the failure again with better errors.

| Item | When |
|---|---|
| N1 Ask Noel to **save** `docker logs cmf-preview-server` to a file now (it's lost if the container is recreated) and send it with `~/Library/Logs/cmf-mcp-server*.log` from the time of the empty-graph `remember`. The traceback is there at ERROR level (P6). | ask now, collect any time before his migration test |
| N2 Ask Noel to **disable** the launchd MCP server (`launchctl bootout gui/$(id -u)/com.cmf.mcp-server`, then `launchctl disable gui/$(id -u)/com.cmf.mcp-server`; the plist stays in place) and keep only the Docker server. | after N1's logs are saved |

### Phase 1: Make failures visible and self-explaining (server code)

The app's Status screen is only as good as what the server reports, so this comes before the app.

| Item | What | Status |
|---|---|---|
| 1.1 Startup config check | Before serving: validate the provider combination (no `anthropic` embeddings), the needed keys are present and not placeholders, FalkorDB is reachable, and `EMBEDDING_DIM` matches an existing graph's index. Exit with one plain sentence per problem. | ✅ Done (step E): `server/core/startup_check.py`; `CMF_STARTUP_CHECK=warn` serves anyway; result and the graph's vector width on `/health`, live provider tests on `/health?check=providers` |
| 1.2 Anticipated errors become `ToolError` | Wrap the known failure classes in `recall_mem`, `get_context`, `remember` and the background writer: FalkorDB unreachable, provider auth/quota, embedding width mismatch, model not found. The model then gets an actionable sentence instead of "Error executing tool". | ✅ Done (cf94d98) |
| 1.3 Empty graph is not an error | `recall_mem` / `get_context` on a graph that doesn't exist yet return "No memories yet." | ✅ Done (cf94d98) |
| 1.4 Background-write status | Record each queued `remember`'s outcome (committed / failed + reason) in the journal; add a `memory_status` tool (or extend `capture_health`). The `remember` ack says when to check. | ✅ Done (cf94d98): `memory_status` tool |
| 1.5 Health endpoint | `GET /health` (no tool call needed, for the app) and a `capture_health` field: version, docker / host, graph, FalkorDB host:port, providers, knowledge sources, start time, last background-write results, and **clients seen** (MCP client name + harness + last-seen time, last 24 h; no content). | ✅ Done (cf94d98) |
| 1.6 Port guard | If port 8000 is already taken, exit with "another process is already on 8000; open CMF Setup" instead of KeepAlive crash-looping. | ✅ Done (step F): `port_problem()` in `server/core/startup_check.py`, exit code 3 |
| 1.7 Self-test entry point | `python -m server.selftest` (run by the app via `docker exec`) and a `cmf_selftest` MCP tool: write a memory to a scratch graph, wait for commit, recall it, delete the scratch graph, report each stage. | ✅ Done (step G): `server/selftest.py`, `cmf_selftest` tool, `cmf selftest`; latest result in `selftest.json`, on `/health` |
| 1.8 Ranked LLM providers | `CMF_LLM_PROVIDERS=anthropic,openai,gemini` (ordered). Use the first; on rate-limit, quota or auth errors, retry the call on the next and log which one served it. `CMF_LLM_PROVIDER` stays as a one-item alias. Embeddings stay single (`CMF_EMBED_PROVIDER`). | ✅ Done (step H): `server/providers/failover_llm.py`; a Gemini primary out of free-tier headroom is skipped too |

### Phase 2: Docker stack that needs no checkout

| Item | What | Status |
|---|---|---|
| 2.1 Publish the server image | CI builds and pushes `ghcr.io/tmargolis/context-memory-fabric:<version>` (multi-arch: arm64 + amd64) on each release tag. | 🔶 Workflow written (`.github/workflows/release-image.yml`, c4b9b49); first run needs the branch pushed and a release tag (step L) |
| 2.2 One user compose file | A user-facing compose file (CMF server, FalkorDB, Browser, capture poller) using the published image, with profiles per optional feature. Todd's extras (Spark proxy, ACL entrypoint) move to `docker-compose.dev.yml`, an **overlay** on the same stack, not a second stack. One graph name, one set of ports, in both modes. | ✅ Done (c4b9b49): `deploy/docker/compose.yml` + `compose.dev.yml` overlay |
| 2.3 Capture poller as a container | `cmf-poller` service runs `capture_runner tail` on an interval, with `~/.claude`, `~/.codex`, `~/.gemini` and the Cowork folder mounted read-only. Captures only the sources assigned to the poller (§3.9: no source is captured by both hook and poller). Retires host `.venv` and launchd for capture. | ✅ Done (c4b9b49); journal in the `cmf-journal` Docker volume (7fb0b98). Per-source hook/poller assignment comes with 3.11 |
| 2.4 Config in `~/.cmf/` for both modes | One env loader (`server/core/env.py`) replaces the ~8 scattered `load_dotenv()` calls: it reads `$CMF_HOME/.env` (default `~/.cmf/.env`), falls back to a repo `.env` with a deprecation warning, and warns if both exist. `CMF_STATE_DIR` (already supported) defaults to `~/.cmf/state`. The dev stack builds the server and poller from the checkout (§3.8). | ✅ Done (c4b9b49); review rules default to the state folder (c42d958) |
| 2.5 Consolidate existing installs | The §3.8 back up → copy → attach graph → disable → verify sequence, as a script the app later runs (3.5). **Run first on Todd's Mac**, with a milestone review before and after; rollback restores the `.env`, re-enables the launchd jobs and reattaches the old stack. | ✅ Done and run on Todd's Mac 2026-10-23 (backup `20261023-164606`, graph `mem-fabric-local` 6769 nodes); journal moved into the volume with `journal-to-volume` (7fb0b98) |

### Phase 3: The CMF Setup app

| Item | What | Status |
|---|---|---|
| 3.1 Engine + CLI | Go module with the operations both front-ends use: read current state, run checks, plan changes, apply with backups, restore. CLI: `cmf status`, `cmf doctor [--json]`, `cmf apply <feature-file>`, `cmf restore`, `cmf logs`, `cmf diagnostics`. | ✅ Done: `status`, `doctor` (d90ea98), `features`, `apply`, `restore` (e3e6db0), `logs`, `diagnostics` (step D) |
| 3.2 Feature manifest | The feature list from 3.4 as data: inputs, rules (e.g. Anthropic LLM requires a non-Anthropic embedder), what each writes, its checks. Unit-tested rule validation. | ✅ Done (e3e6db0): `internal/features/manifest.json`; Google Drive, M365 and client-app rows come with later steps |
| 3.3 Status checks + Fixes | Everything in 3.3's Status list, each with its Fix action, using `/health` (1.5) and the self-test (1.7). | ✅ Done: every row in 3.3's list (d90ea98, steps E, G, I); green on Todd's Mac except the rows that wait on Docker |
| 3.4 Client config writers | Claude Desktop, Claude Code / Code tab, Codex, Cursor, Antigravity / VS Code: merge one CMF entry, remove duplicates, back up, and verify by reading the file back. Hook + agent-instructions installers. | 🔶 Entries done (step I): `internal/clients`, `cmf clients`, `"clients"` in feature files; the hook and agent-instructions installers moved to step O with the rest of the capture wiring |
| 3.5 Migration | Runs the 2.5 consolidation from the app: detects repo-based stacks, launchd/systemd/Task Scheduler CMF jobs and stray host servers; backs up, imports keys and paths, **disables** (never uninstalls) the replaced services. | ✅ Done (step J): `internal/migrate`, `cmf migrate [plan|apply|restore]`; Developer mode until the image is published (step L) |
| 3.6 GUI | Embedded web UI over the engine: Status, Features, Review changes, progress, logs, diagnostics. Local-only server with token auth (§3.6). | Not started (step K) |
| 3.7 Packaging | Cross-compiled release binaries (unsigned for the preview, with right-click → Open instructions); checksums; "Update" pulls the new image and app version. | Not started (step L) |
| 3.8 `cmf bridge` | A subcommand that relays Claude Desktop's stdio MCP to the server's HTTP endpoint, so the Desktop entry runs the app binary instead of `npx mcp-remote`. Removes the Node requirement. | Not started (step M) |
| 3.9 Install modes | `mode.json`, mode detection, image-vs-checkout switching (§3.8). Small, since both modes share everything else. | ✅ Done (a49d4c8): `mode.json`, detection, `cmf mode set`; recreating the stack after a switch is still a manual `docker compose up -d --build` |
| 3.10 Remote access helper | Detect Tailscale / cloudflared / ngrok; enable OAuth first; run only the CMF-specific expose command; set `CMF_MCP_ISSUER_URL`; undo. | Not started (step N) |
| 3.11 Capture wiring + estimates | Per-source capture method assignment (hook / poller / server instructions, §3.9), hook installers, marker-fenced agent blocks with duplicate detection, the prompt-context budget, and cost estimates for live capture and backfill (with actual-vs-estimate in Status). | Not started (step O) |

### Phase 4: First-run value and docs

| Item | What | Status |
|---|---|---|
| 4.1 Backfill | Optional, offered once after setup: one-time import of the last N days of transcripts from before CMF was installed, through the poller container, with a cost estimate first and a progress bar. The first question in Claude gets a real answer. | Not started (step P) |
| 4.2 Quickstart | `docs/QUICKSTART.md`: install Docker Desktop → download CMF Setup → pick features → Apply → in Claude: "Run the CMF self-test", then "What did I work on last week?". SETUP.md becomes the reference; README links Quickstart first. | Not started (step Q) |
| 4.3 Retire old instructions | SETUP.md / CLIENTS.md client sections point to the app; manual steps move under "Advanced / no app". | Not started (step R) |

### Build steps

The order of §6, one letter per step. Commits go on `setup-simplification`; since 2026-10-24 the steps run one after another, with a commit after each, stopping only for decisions that are Todd's; nothing is pushed until he asks. Steps E onward are planned, not yet detailed.

The engine and CLI are a Go module at the repo root (`cmd/cmf`, `internal/…`), with the compose files embedded from `deploy/docker`; every external command goes through a fake-able runner with a timeout.

- [x] **A. Skeleton, `~/.cmf` home, install modes (3.9):** `.env` edits line by line (owner-only), dated backups, `mode.json`, `cmf mode [set]`, which keeps the install's own volume block in `compose.override.yml`. (a49d4c8)
- [x] **B. Probe, `cmf status`, `cmf doctor [--json] [--fix[=<check>]]` (3.1, 3.3):** Docker (including a hung daemon), stack containers, port owner, leftover launchd jobs and containers, host `server.mcp`, `/health`, graph node count. Fixes start or restart Docker Desktop, `compose up` / force-recreate, disable replaced launchd jobs (plists stay), stop old containers (not removed), restart `cmf-server`. (d90ea98) Tried on Todd's Mac 2026-10-24: `--fix=docker` restarted a hung Docker and the stack came back with 6769 nodes; the fix now waits for Docker and `/health` before checking again.
- [x] **C. Feature manifest (3.2), `cmf features`, `cmf apply <feature-file>`, `cmf restore [<stamp>]` (3.1):** embedded JSON manifest (models, knowledge, capture); rules forbid_value, exactly_one, conflicts, requires_any, needs_key, locked_when_graph_has_data, all reported at once; apply reviews, backs up, writes `.env` and `COMPOSE_PROFILES`, stops what it turned off, `compose up -d`, waits for `/health`, and restores on failure; restore is itself undoable. Client-config steps wait for step I. (e3e6db0)
- [x] **D. `cmf logs`, `cmf diagnostics` (3.1):** `cmf logs` shows the stack's logs as they are (`--tail`, `-f`, `--errors`); `cmf diagnostics` builds a redacted report (versions, checks, `/health`, which settings are set, last 50 error lines) with keys, tokens, non-allowlisted setting values, emails, the home path and long quoted text removed; shown first, then `--copy` (asks) or `--out`. Checked on Todd's Mac: none of 26 hidden `.env` values appear in the report. `cmf logs` not yet run live (Docker was down).
- [x] **E. Startup config check (1.1):** plus the provider-test and embedding-width Status rows (3.3). The server checks providers, keys (placeholders count as missing), FalkorDB (30 s of retries; a rejected login fails at once) and the graph's stored vector width before serving, and exits with one ERROR line per problem; a missing capture key only warns. `/health` carries the result and the graph's width; `?check=providers` asks each provider's free model list (key accepted, model listed), in parallel. `cmf status` turns these into real rows, and points at `cmf logs cmf-server --errors` when the server keeps stopping. Also fixed: the capture row and diagnostics read background-write fields `/health` never sent. Not yet run live (Docker was down); the 42 Python failures are the same on the previous commit (they need a live FalkorDB).
- [x] **F. Port guard (1.6):** before uvicorn starts on a network transport, the server tries to connect to its port (anything answering means taken; on macOS a bind can succeed next to a listener on 0.0.0.0) and then to bind it; if taken it logs one sentence pointing at `cmf status` and exits with code 3, before the config check. Tried with the real server on a held port (exit 3) and, for step E, on a free port with FalkorDB down (exit 2 after the 30 s wait, one ERROR line).
- [x] **G. Self-test (1.7):** plus the self-test Status row (3.3). Stages config → embed (width checked) → write (`add_episode`, production extraction profile) → recall (search must return the fact carrying a unique marker) → cleanup (`GRAPH.DELETE`, only `cmf_selftest_*` names, always runs); 5-minute limit. Uses its own uncached Graphiti on the scratch graph rather than changing `FALKORDB_DATABASE`, which concurrent tool calls read. `cmf selftest` streams it from `cmf-server`; the Status row shows the latest result's age, or the failed stage with a rerun fix. Not yet run live (Docker was down).
- [x] **H. Ranked LLM providers (1.8):** `CMF_LLM_PROVIDERS` (ordered, validated, de-duplicated) wins over `CMF_LLM_PROVIDER`, which stays the one-item alias. `FailoverLLMClient` retries the same call on the next provider after rate-limit / quota / billing / overload / rejected-key errors (HTTP 429/503/529 or the Phase 1 classifier) and logs which provider served it; an unreachable local server or unknown model is raised instead, so an offline Spark never becomes paid calls. Fallbacks use their own model setting; one without a key is skipped (a startup warning); a Gemini primary with no free-tier headroom is skipped to the next provider, reranker included. `/health` lists the chain; provider tests cover every entry. A provider's own SDK retries run first, so a rate-limited call can take a minute or two before it moves on. Not yet run against live providers.
- [x] **I. Client config writers (3.4):** plus the client-entries Status row (3.3) and the client steps of `cmf apply`. One definition per app (paths per OS; Desktop and Antigravity via `npx mcp-remote`, Claude Code and VS Code `type: http`, Cursor `url`, Codex TOML `url`). A CMF entry is found by name or by pointing at this install, and is *correct* when it reaches `127.0.0.1:<port>/mcp` or the public URL; a correct one is kept, duplicates and stale ones (old stdio servers, other ports) are removed. JSON keeps its key order; Codex tables are rewritten with their subtables. Writes are refused if the file changed since it was read, backed up into the same set as `.env`, and read back. `cmf clients fix` never adds an entry. `cmf restore` of a clients-only backup leaves the stack alone. On Todd's Mac (read-only): Codex's Funnel entry is "connected (public URL)", nothing to fix, files unchanged. Hook and agent-instructions installers moved to step O.
- [x] **J. Migration from the app (3.5):** `cmf migrate` is the Go form of `scripts/consolidate_install.py`, widened to the preview stack (`cmf-preview-falkordb` / `cmf-preview-server`, state and journal in a named volume) besides Todd's host install. Names come from the machine (repo from the compose `working_dir` label or the launchd plist, volumes from `docker inspect` mounts, FalkorDB's data folder from `CONFIG GET dir`); it refuses when FalkorDB writes outside its volume. Node count, BGSAVE and the journal check run through `docker exec` / the cmf-server image, so no host Python. Same sequence and step-by-step self-restore as the script; `migrate.json` in the backup drives `cmf migrate restore`, and `cmf restore` points there. Developer mode only until the image is published (step L). On Todd's Mac (read-only): "Nothing to migrate", already consolidated. Not yet run against a real old install (next: Noel's machine).
- [ ] **K. GUI (3.6).**
- [ ] **L. Packaging (3.7):** with the first release tag, which also runs the image workflow for the first time (2.1).
- [ ] **M. `cmf bridge` (3.8).**
- [ ] **N. Remote access helper (3.10).**
- [ ] **O. Capture wiring + estimates (3.11):** includes the hook and agent-instructions installers from 3.4.
- [ ] **P. Backfill (4.1).**
- [ ] **Q. Quickstart (4.2).**
- [ ] **R. Retire old instructions (4.3).**

### Not in scope

- Making Claude **Connectors** work with localhost (Anthropic's product rule). The app explains and offers the Remote access feature instead.
- Multi-user hosting (see PUBLIC-PREVIEW-ONBOARDING.md, Phase 4).
- **Communication listeners** (Gmail redesign, Slack, Teams): a separate design. The app shows them as "coming soon" Sources.
- New knowledge providers for Google Drive and Microsoft 365 / SharePoint ("coming soon" under option C).
- Installing Docker for the user. The app detects it's missing and links to Docker Desktop (or OrbStack / Colima on macOS).

---

## 5. Definition of done

A new tester on a clean Mac or Windows PC, with Docker Desktop installed and one API key, without help from the author:

1. downloads CMF Setup, opens it, picks features and clicks Apply; about 10 minutes total;
2. sees every Status row green, including the self-test;
3. works with Claude for a while, then asks "what did I work on recently?" and gets an answer drawn from captured sessions (backfill optional) or their own notes;
4. later adds a feature (e.g. Notion) by opening the app again, without reading docs;
5. if anything breaks, reopens the app, which names the problem and fixes it in one click, or produces a diagnostics report that lets Todd fix it without a screen-share.

Test in this order:

1. **Todd's Mac (Developer mode):** consolidation (2.5) with backups, then a self-test and recall of a known memory from the live graph; rollback rehearsed once.
2. **Todd's Windows PC (Standard mode, clean):** the first full run of steps 1–5 above, with no repo and no prior CMF.
3. **Noel (migration):** redo his setup with the app, which should migrate his half-and-half install to Standard. This run also answers Q3: the "AI apps seen" check shows whether his Code tab connected through the Desktop entry.
4. **One more tester who has never seen CMF**, ideally on a Mac.

---

## 6. Sequencing and conflicts

- The Notion provider work (Codex) is committed, so Phase 0 and Phase 1 can start now. The app's Notion feature (3.2) uses its settings.
- Phases 2 and 3 are mostly new files (CI, compose, a new Go module), so they can start in parallel with Phase 1.
- Suggested order: **0 → 1.2–1.5 → 2.1–2.5 → 3.1–3.3 + 3.9 (engine, CLI and install modes, usable without the GUI) → 1.1/1.6/1.7/1.8 → 3.4–3.6 → 3.7/3.8 → 3.10/3.11 → 4.x.** The CLI engine ships first so Status checks can be tested before the GUI exists.
- Each phase ends with Todd's milestone review before the next one starts.

---

## 7. Decisions (Todd, 2026-10-23)

1. **Bare-metal host path:** kept as **Developer mode** (§3.8), documented in a separate `docs/DEVELOPER-SETUP.md` and left out of the Quickstart. It is Todd's own setup, so it is not "contributors only". Revised the same day: Developer mode now shares `~/.cmf/`, FalkorDB and the stack with Standard, and differs only in building the server from the checkout.
2. **Providers:** the user ranks the LLM providers they have keys for; the first is used and the rest are fallbacks (item 1.8). Embeddings take exactly one provider.
3. **Code tab config:** no test account. Confirmed during the first tester's setup (Noel or another friend) through the "AI apps seen" Status check (items 1.5, 3.3). Phase 0 docs describe the Desktop entry as what worked on Noel's machine.
4. **Bridge:** `mcp-remote` now (the app checks for Node); `cmf bridge` in the app binary later removes Node (item 3.8). Todd's own setup uses neither: it is a claude.ai custom connector over Tailscale Funnel with OAuth, which reaches every Claude tab but needs a public URL, so it stays under Remote access.
5. **Knowledge base:** three first-run choices: none, demo wiki, or bring your own (folder, Notion; Google Drive and Microsoft 365 / SharePoint shown as "coming soon").
6. **Capture:** revised the same day. CMF runs **bi-directionally by default** (read and write), using hooks where the app has them (§3.9). Backfill of pre-install transcripts is optional. Both live capture and backfill show cost estimates wherever possible. Agent instructions never duplicate the server instructions and add as little prompt context as possible.
7. **LaunchControl:** dropped from all guidance. launchd services that are found are disabled, never deleted or uninstalled.
8. **App technology:** Go binary + local web UI.
9. **Signing:** unsigned for the preview; right-click → Open instructions provided (§3.7).
10. **Config location:** `~/.cmf/` for both modes, consolidated with backups before anything is deprecated (§3.8, item 2.5). Everything is writable in both modes. Testing: Todd's Mac (Developer, consolidation first), Todd's Windows PC (clean Standard), then Noel (§5).
11. **Todd's operational setup:** local models (e.g. Spark) are a provider option and VPNs/tunnels are user-supplied; CMF doesn't install or manage them. The app detects Tailscale, cloudflared or ngrok and offers to set up only what CMF needs. OAuth is turned on automatically as a dependency of any connector (ChatGPT, Gemini, Claude).
12. **Communication sources:** Gmail, Slack and Teams are listening sources (inputs that become episodes or doc proposals), like the AI assistants, not knowledge bases. Shown as "coming soon"; the current Gmail snapshot provider is kept out of the app until redesigned.
