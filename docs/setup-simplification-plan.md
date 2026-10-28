# Setup simplification plan

**Status:** Draft for Todd's review. v1 2026-10-21; v2 2026-10-22 adds the **Context Memory Fabric management app** (a platform-neutral GUI for setup, adding features and repair); v3 2026-10-23 records Todd's answers to the open questions (§7) and adds **install modes** (§3.8); v4 2026-10-23 consolidates both modes into `~/.cmf/`, adds bi-directional capture (§3.9) and communication Sources. No code changes yet.
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
2. **One app for setup, adding features and repair: Context Memory Fabric management.** A double-clickable, platform-neutral GUI. The same engine also runs as a command line (`cmf doctor`, `cmf apply`) for scripting and remote help. A user should never need `lsof`, `ps`, `launchctl` or `grep .env`.
3. **Re-runnable and idempotent.** Opening the app again always starts from the machine's *actual* state (files, containers, client configs), never from a remembered wizard. It shows what's healthy, what's broken (with a Fix button) and what can be added.
4. **Preview, back up, then apply.** Every change shows a plain-language list of what will be written or restarted, backs up each file it touches, and can be undone.
5. **Fail at startup, not on the first tool call.** Bad provider combinations, placeholder keys, unreachable FalkorDB and wrong embedding widths are caught before the server accepts connections, and the app shows the same checks.
6. **Every tool error says what to do.** No bare "Error executing tool" for anything we can foresee.
7. **Confirmed writes.** A user can see whether a memory actually landed without reading logs.
8. **One CMF per machine,** and both the app and the AI client can tell which one is running.

---

## 3. Context Memory Fabric management: the app

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
| | Background capture LLM | "Same as extraction" (default) or another | Used by the poller and promotion. Removes the hidden `local` default (P3). **v0.1.2: merged into the one LLM card** (capture always uses the shared list; an older `CMF_CAPTURE_LLM_PROVIDER` wins until the card is saved). |
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

- The local UI binds `127.0.0.1` only, on a random port, and requires the one-time token from the URL the app opened (so another web page or local process can't drive it). It stops from a Quit button, or 30 minutes after its last window or tab goes away.
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
| 1.6 Port guard | If port 8000 is already taken, exit with "another process is already on 8000; open Context Memory Fabric management" instead of KeepAlive crash-looping. | ✅ Done (step F): `port_problem()` in `server/core/startup_check.py`, exit code 3 |
| 1.7 Self-test entry point | `python -m server.selftest` (run by the app via `docker exec`) and a `cmf_selftest` MCP tool: write a memory to a scratch graph, wait for commit, recall it, delete the scratch graph, report each stage. | ✅ Done (step G): `server/selftest.py`, `cmf_selftest` tool, `cmf selftest`; latest result in `selftest.json`, on `/health` |
| 1.8 Ranked LLM providers | `CMF_LLM_PROVIDERS=anthropic,openai,gemini` (ordered). Use the first; on rate-limit, quota or auth errors, retry the call on the next and log which one served it. `CMF_LLM_PROVIDER` stays as a one-item alias. Embeddings stay single (`CMF_EMBED_PROVIDER`). | ✅ Done (step H): `server/providers/failover_llm.py`; a Gemini primary out of free-tier headroom is skipped too |

### Phase 2: Docker stack that needs no checkout

| Item | What | Status |
|---|---|---|
| 2.1 Publish the server image | CI builds and pushes `ghcr.io/tmargolis/context-memory-fabric:<version>` (multi-arch: arm64 + amd64) on each release tag. | ✅ Done: `v0.1.0` (2026-10-25) published `ghcr.io/tmargolis/context-memory-fabric:0.1.0` for linux/amd64 and linux/arm64; pulls anonymously (public) |
| 2.2 One user compose file | A user-facing compose file (CMF server, FalkorDB, Browser, capture poller) using the published image, with profiles per optional feature. Todd's extras (Spark proxy, ACL entrypoint) move to `docker-compose.dev.yml`, an **overlay** on the same stack, not a second stack. One graph name, one set of ports, in both modes. | ✅ Done (c4b9b49): `deploy/docker/compose.yml` + `compose.dev.yml` overlay |
| 2.3 Capture poller as a container | `cmf-poller` service runs `capture_runner tail` on an interval, with `~/.claude`, `~/.codex`, `~/.gemini` and the Cowork folder mounted read-only. Captures only the sources assigned to the poller (§3.9: no source is captured by both hook and poller). Retires host `.venv` and launchd for capture. | ✅ Done (c4b9b49); journal in the `cmf-journal` Docker volume (7fb0b98). Per-source hook/poller assignment comes with 3.11 |
| 2.4 Config in `~/.cmf/` for both modes | One env loader (`server/core/env.py`) replaces the ~8 scattered `load_dotenv()` calls: it reads `$CMF_HOME/.env` (default `~/.cmf/.env`), falls back to a repo `.env` with a deprecation warning, and warns if both exist. `CMF_STATE_DIR` (already supported) defaults to `~/.cmf/state`. The dev stack builds the server and poller from the checkout (§3.8). | ✅ Done (c4b9b49); review rules default to the state folder (c42d958) |
| 2.5 Consolidate existing installs | The §3.8 back up → copy → attach graph → disable → verify sequence, as a script the app later runs (3.5). **Run first on Todd's Mac**, with a milestone review before and after; rollback restores the `.env`, re-enables the launchd jobs and reattaches the old stack. | ✅ Done and run on Todd's Mac 2026-10-23 (backup `20261023-164606`, graph `mem-fabric-local` 6769 nodes); journal moved into the volume with `journal-to-volume` (7fb0b98) |

### Phase 3: The Context Memory Fabric management app

| Item | What | Status |
|---|---|---|
| 3.1 Engine + CLI | Go module with the operations both front-ends use: read current state, run checks, plan changes, apply with backups, restore. CLI: `cmf status`, `cmf doctor [--json]`, `cmf apply <feature-file>`, `cmf restore`, `cmf logs`, `cmf diagnostics`. | ✅ Done: `status`, `doctor` (d90ea98), `features`, `apply`, `restore` (e3e6db0), `logs`, `diagnostics` (step D) |
| 3.2 Feature manifest | The feature list from 3.4 as data: inputs, rules (e.g. Anthropic LLM requires a non-Anthropic embedder), what each writes, its checks. Unit-tested rule validation. | ✅ Done (e3e6db0): `internal/features/manifest.json`; Google Drive, M365 and client-app rows come with later steps |
| 3.3 Status checks + Fixes | Everything in 3.3's Status list, each with its Fix action, using `/health` (1.5) and the self-test (1.7). | ✅ Done: every row in 3.3's list (d90ea98, steps E, G, I); green on Todd's Mac except the rows that wait on Docker |
| 3.4 Client config writers | Claude Desktop, Claude Code / Code tab, Codex, Cursor, Antigravity / VS Code: merge one CMF entry, remove duplicates, back up, and verify by reading the file back. Hook + agent-instructions installers. | 🔶 Entries done (step I): `internal/clients`, `cmf clients`, `"clients"` in feature files; the hook and agent-instructions installers moved to step O with the rest of the capture wiring |
| 3.5 Migration | Runs the 2.5 consolidation from the app: detects repo-based stacks, launchd/systemd/Task Scheduler CMF jobs and stray host servers; backs up, imports keys and paths, **disables** (never uninstalls) the replaced services. | ✅ Done (step J): `internal/migrate`, `cmf migrate [plan|apply|restore]`; Developer mode until the image is published (step L) |
| 3.6 GUI | Embedded web UI over the engine: Status, Features, Review changes, progress, logs, diagnostics. Local-only server with token auth (§3.6). | ✅ Done (step K): `cmf ui` / `cmf`; page embedded in the binary (`cmd/cmf/web`) |
| 3.7 Packaging | Cross-compiled release binaries (unsigned for the preview, with right-click → Open instructions); checksums; "Update" pulls the new image and app version. | 🔶 Released (step L): `v0.1.0` prerelease (2026-10-25) with macOS universal, Windows and Linux amd64/arm64 binaries + `SHA256SUMS`; checksum and `cmf status` verified on the downloaded macOS binary. "Update" (pull a newer image and cmf) isn't built yet |
| 3.8 `cmf bridge` | A subcommand that relays Claude Desktop's stdio MCP to the server's HTTP endpoint, so the Desktop entry runs the app binary instead of `npx mcp-remote`. Removes the Node requirement. | ✅ Done (step M): `cmf bridge`; Desktop and Antigravity entries use it |
| 3.9 Install modes | `mode.json`, mode detection, image-vs-checkout switching (§3.8). Small, since both modes share everything else. | ✅ Done (a49d4c8): `mode.json`, detection, `cmf mode set`; recreating the stack after a switch is still a manual `docker compose up -d --build` |
| 3.10 Remote access helper | Detect Tailscale / cloudflared / ngrok; enable OAuth first; run only the CMF-specific expose command; set `CMF_MCP_ISSUER_URL`; undo. | ✅ Done (step N): `cmf remote [enable tailscale | use <url> | disable]`; not yet in the UI |
| 3.11 Capture wiring + estimates | Per-source capture method assignment (hook / poller / server instructions, §3.9), hook installers, marker-fenced agent blocks with duplicate detection, the prompt-context budget, and cost estimates for live capture and backfill (with actual-vs-estimate in Status). | 🔶 Mostly done (step O): `cmf capture`, `cmf hook stop`, hook installers, agent block, token estimates, prompt budget. Left: actual-vs-estimate in Status (needs server-side usage metering), dollar estimates (needs a price table), per-source poller switch in the UI |

### Phase 4: First-run value and docs

| Item | What | Status |
|---|---|---|
| 4.1 Backfill | Optional, offered once after setup: one-time import of the last N days of transcripts from before CMF was installed, through the poller container, with a cost estimate first and a progress bar. The first question in Claude gets a real answer. | ✅ Done (step P): `cmf backfill`; the first-run offer itself comes with the Quickstart flow (step Q) |
| 4.2 Quickstart | `docs/QUICKSTART.md`: install Docker Desktop → download Context Memory Fabric management → pick features → Apply → in Claude: "Run the CMF self-test", then "What did I work on last week?". SETUP.md becomes the reference; README links Quickstart first. | ✅ Written (step Q): `docs/QUICKSTART.md`, README links it first; walked through only once a release exists |
| 4.3 Retire old instructions | SETUP.md / CLIENTS.md client sections point to the app; manual steps move under "Advanced / no app". | ✅ Done (step R): SETUP.md and CLIENTS.md lead with Context Memory Fabric management; manual client steps under "Advanced / no app" |

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
- [x] **K. GUI (3.6):** `cmf ui` (and `cmf` with no command, so double-clicking opens it) serves a page embedded in the binary: plain HTML/CSS/JS, no build step, all text set with `textContent`, strict CSP, so it can move into a Tauri window unchanged. Status rows with Fix buttons (streamed progress); Features generated from the manifest (toggles, masked secrets that stay unchanged when blank, the ranked LLM list with move up / down / add as fallback) plus the AI apps; Review changes is the same list as `cmf apply --dry-run` (shared `requestPlan`), Apply streams progress and re-runs Status; diagnostics shown before copying; logs; FalkorDB Browser link; backups with Restore. Security per §3.6: 127.0.0.1 on a random port, one-time token traded for an HttpOnly SameSite=Strict cookie and dropped from the address, Host and Origin checked, one change at a time, stops when the tab closes (beacon + 15 s grace so a reload survives) or after 30 idle minutes. Revised 2026-10-28: tab-close stopping removed because browser tab discarding (Chrome Memory Saver, Auto Tab Discard) and sleep looked like a close; now Quit button, Ctrl-C, or 30 minutes after the last tab goes away, with the page reconnecting or showing a stopped notice, and the exit reason printed. Checked in the browser against Todd's real `~/.cmf`: Status, Features and a Review (Apply correctly disabled with Docker down); nothing applied. First-run prefill (§3.3) waits for the Quickstart (step Q).
- [x] **L. Packaging (3.7):** with the first release tag, which also runs the image workflow for the first time (2.1). *Prepared, waiting on the tag:* `.github/workflows/release-cmf.yml` (tag must equal `pyproject.toml`'s version; vet + tests; static builds for macOS arm64+amd64 merged with `lipo`, Windows amd64, Linux amd64/arm64, version stamped; `SHA256SUMS`; a prerelease with `deploy/RELEASE-NOTES.md`, which carries the unsigned-app steps of §3.7). A released `cmf` switching to Standard mode pins `CMF_VERSION` to its own version, so the stack runs the matching image. All five targets cross-compile locally (about 8 MB each; universal binary checked with `lipo -info`). Left: pushing `setup-simplification`, merging, and the `v0.1.0` tag, which are Todd's call; an "Update" action. **Released 2026-10-25:** tag `v0.1.0` on 4a35c10; both workflows succeeded.
- [x] **M. `cmf bridge` (3.8):** relays newline-delimited JSON-RPC from stdin to `POST /mcp` (streamable HTTP: JSON or SSE replies, `Mcp-Session-Id` and `MCP-Protocol-Version` kept after `initialize`, `DELETE` on exit), reading port and token from `~/.cmf/.env` so the app's config holds no secret. When the server is down or refuses, each request gets a JSON-RPC error naming the cause and `cmf status`. `cmf clients set` writes `{"command": "<cmf>", "args": ["bridge"]}` for Claude Desktop and Antigravity when cmf runs from a lasting place (else `npx mcp-remote`); a bridge entry whose binary moved shows as needing a fix. Tested against the real server (host run, scratch state): initialize (2025-06-18), tools/list (34 tools), a `memory_status` call, session closed.
- [x] **N. Remote access helper (3.10):** `internal/remote` finds Tailscale (connected, DNS name, Funnel allowed, which HTTPS ports already serve what, and whether one already publishes CMF's port), cloudflared and ngrok. `cmf remote enable tailscale` turns OAuth on first, then runs only `tailscale funnel --bg --https=<first free of 443/8443/10000> http://127.0.0.1:<port>`, recreates the server through the same executor as `cmf apply` (closing the Funnel port again if that fails) and checks the public `/health`; `cmf remote use <url>` does the OAuth part for a tunnel the user runs (warns about Cloudflare quick tunnels, whose address changes). `state/remote.json` records what was added; `disable` closes only that port and takes out only the settings it set. On Todd's Mac (read-only): OAuth on, Funnel already publishes CMF on 443, the dry run changes nothing. The UI page for it comes with the Quickstart pass (step Q).
- [x] **O. Capture wiring + estimates (3.11):** includes the hook and agent-instructions installers from 3.4. `internal/capture`: a Go port of the Stop-hook nudge (same throttle, loop guard, per-app output, fail-open; a test keeps its text identical to both Python copies) run as `cmf hook stop --harness <app>`, so hooks need no Python; installers for Claude Code (`~/.claude/settings.json`), Codex (`~/.codex/hooks.json`) and Antigravity (`~/.gemini/config/hooks.json`) using the same `cmf-capture-nudge` marker, so the plugin or a Python nudge counts as installed and is never doubled (key order kept via the new `internal/jsonedit`); `cmf capture` reports each source's method (hook / poller / both = full transcript / server instructions only), legacy worker hooks, 7-day transcript volume, a token estimate ($0 on a local model) and the prompt budget read from the running server through the bridge; `cmf capture block` writes the marker-fenced agent block and flags other CMF text. On Todd's Mac (read-only): all four sources hook + poller (full transcript), worker hooks flagged, Python nudges recognised, dry-run installs change nothing. Left for later: actual-vs-estimate in Status (server-side usage metering), dollar figures (a price table), a per-source poller switch in the UI.
- [x] **P. Backfill (4.1):** `cmf backfill [--days N] [--source ...]` prints the estimate first (transcript text in the window per source, as tokens; $0 on a local model), then runs each adapter's own `backfill --since <ISO>` (with its all-projects / all / all-app-dirs flag) in the running `cmf-poller` container, streaming its output; results go to the review queue. `state/backfill.json` records done / failed / declined, so the first-run offer is made once. The commands match each adapter's `backfill --help`. Not run live (it would call the models and fill the review queue).
- [x] **Q. Quickstart (4.2):** `docs/QUICKSTART.md` (Docker Desktop → download Context Memory Fabric management, move it somewhere lasting, unsigned-app steps → Install CMF here → Features → Review → Apply → restart the app → "Run the CMF self-test", then "What did I work on last week?" with `cmf backfill`), and the README links it first, with the Compose preview kept as the from-a-checkout path. Not built: the UI's first-run prefill and its one-time backfill offer (the CLI covers both). The flow can only be walked end to end once the first release exists (step L). Found while preparing it: nothing could create a fresh install (only `cmf migrate` wrote `compose.yml`). Added `cmf install` (Standard mode: embedded `compose.yml`, `mode.json`, a starting `.env` with graph `cmf`, capture on and the image pinned to the cmf version; refuses an existing install and points an older repo install at `cmf migrate`; starts nothing until Features → Apply), the Status row "CMF isn't installed here yet" with it as the fix, and `mode.Plan` now writes or updates `compose.yml` to the binary's copy.
- [x] **R. Retire old instructions (4.3):** SETUP.md's "pick one way" box leads with Context Memory Fabric management (Quickstart) and says how a Docker-preview install moves over (`cmf migrate`); the client sections open with what the app does (`cmf clients set`, `cmf bridge`, no Node) and keep the manual steps under "Advanced / no app"; Background Capture no longer says it needs Developer mode (poller container, `cmf capture hook install`); a new Remote access section covers `cmf remote`. CLIENTS.md opens with the same pointer. All existing `SETUP.md#` anchors still resolve.
- [x] **Pre-release preview fixes:** the demo wiki works without a checkout: `cmf` carries `starter-wiki/` (`starterwiki.go`, `go:embed`) and, in Standard mode, writes it to `~/.cmf/starter-wiki` on Apply (existing notes kept); Developer mode still uses the checkout's copy. User-facing text no longer names plan steps or items (demo-wiki description, three Status rows).
- [x] **v0.1.1 (released 2026-10-25, `cb71f23`, tag `v0.1.1`):** Status page feedback: the app is named Context Memory Fabric management; each check has a description, a monospace detail and a link to the feature or doc that changes it; the summary shows when it was checked; Back returns to the Status position; log error lines are red and the errors-only filter catches poller summaries; the FalkorDB Browser button uses the Tailscale address when one serves it. The Docker preview (`docker-compose.preview.yml`) is removed: SETUP.md, README, `.env.example` and the error hints now describe only the `~/.cmf` stack; `cmf migrate` still moves an old preview install over.
- [x] **v0.1.2 (released 2026-10-25, tag `v0.1.2`):** one LLM card. "LLM for extraction" and "Background capture LLM" are merged: background capture uses the same ranked `CMF_LLM_PROVIDERS`. Fixes a v0.1.1 bug where a ranked list overrode `CMF_CAPTURE_LLM_PROVIDER` during capture passes (capture could run on paid fallbacks while set to `local`). An older install's `CMF_CAPTURE_LLM_PROVIDER` keeps winning until the LLM card is saved, which removes it with a note in Review.
- [x] **v0.1.3 (released 2026-10-25, tag `v0.1.3`):** the app mirrors the real setup. Status: a claude.ai connector row (active when Claude calls came through it in the last day), one row per AI app on this computer (connection, checkpoint hook, poller folder and its last transcript, last call; Chat and Cowork share the Claude Desktop row; the connector counts for Claude Code too), and a Run self-test button. Features: a claude.ai connector card (recommended for Claude, with the user's address and steps) replaces the Claude Desktop card; the other AI app cards keep only their entry switch and show the folder the poller reads; capture is always on (no switch; any Apply keeps the profile and points the poller at every transcript folder), so its card and the two automatic knowledge cards are gone; Google Drive, Microsoft 365 / SharePoint and Messaging apps show as coming soon. With remote access on, the server also accepts `CMF_MCP_AUTH_TOKEN` for local app entries (generated when an app is turned on). Fixed: turning capture on (and `cmf install`) never pointed the poller at the transcript folders.
- [x] **v0.1.4 (released 2026-10-26):** apps on this computer reach CMF through the local address and token, never a lapsing OAuth sign-in. Once `CMF_MCP_AUTH_TOKEN` exists, a public-URL entry or a local one without the right token counts as wrong (the reason shows in Status and Review) and is replaced; `cmf clients set` / `fix` create the token through the Apply plan instead of asking for a shell command. The server counts refused calls (401/403 on `/mcp` and `/token`) per app from the User-Agent, in memory, on `/health` as `refused_calls`; Status turns an app yellow when its calls were refused after its last successful one, with **Use this computer's address** (`cmf clients set <app> --yes`), and the connector row says to reconnect in claude.ai. Codex's client name `codex-mcp-client` now journals as `codex`. Found on Todd's Mac: Codex's OAuth login predated the journal move, so every call was refused while Status showed it green. Also in 0.1.4, backlog B18–B21 ([plan-active.md](plan-active.md)): extraction and `remember` prompts fit the loaded model's context (read from LM Studio, or `CMF_LLM_CONTEXT_TOKENS`); a poller window that still doesn't fit is split once, then skipped as `oversize` until the context grows; `remember` passes only the earlier episodes that fit and saves a too-long memory in parts; failed saves are kept for **Retry failed saves** / `cmf memory retry`; Status warns when the local model's context is under 16K; the Stop hook passes the git repository as `project` (cmf-capture plugin 0.1.2); `cmf clients set codex` removes Codex's stale stored sign-in for CMF; refusals an app got past right after are not flagged. One version everywhere: a `cmf` built without the release stamp reads `pyproject.toml` (`0.1.4-dev`, which never pins an image) instead of saying `dev`; the management header shows `CMF <version>` when cmf and the server agree and both numbers when they don't, and the Install mode row warns on a mismatch (found when the downloaded 0.1.2 cmf showed `cmf 0.1.2` over a 0.1.4 server).
- [x] **v0.1.5 (released 2026-10-26):** each AI app card on the Features page gets an **Instructions** note (where that app takes custom instructions, a **Copy instructions** button with the block from INSTRUCTIONS-FOR-AGENTS.md embedded in `cmf`, and the doc link) and a **Capture** note (its checkpoint hook, the `cmf` it runs and that cmf's version or the plugin's version, and whether the poller reads it). A hook running a `cmf` whose version differs from the server's shows a warning with **Update** (`cmf capture hook update`, copies the released cmf over it, backup kept), on the card and as the Status row's fix; an older cmf-capture plugin says to update it with `/plugin`. Cowork's hook now comes from Cowork's own plugin list (`local-agent-mode-sessions/*/*/rpm/manifest.json`), not Claude Code's (found on Todd's Mac: Status credited Cowork with a plugin it didn't have). Since Cowork's sessions run in the cloud (34 KB of local transcript in a week on Todd's Mac), its warning says the plugin is how Cowork gets captured and gives the steps (Customize → Plugins → Add → Add marketplace `tmargolis/context-memory-fabric`, then install cmf-capture from Plugins → Discover; Todd found the second step wasn't obvious); CMF can't install it, since the list belongs to the account. SETUP.md step 3 is "Add CMF's instructions to each app", and Background Capture explains hooks versus the poller.
- [x] **v0.1.6 (released 2026-10-26):** Status shows each check's level as an icon that differs by shape and symbol, not only colour (a tick in a circle, ! in a triangle, a cross in a square, a dash in an outline), with a text label for screen readers; the summary banner leads with the same icon. Asked for by Todd, who is colour-blind and couldn't tell the dots apart.
- [x] **v0.1.7 (released 2026-10-26):** Status and the Features cards show whether each app's instructions carry CMF's guidance: Claude Code's `~/.claude/CLAUDE.md` and Codex's `AGENTS.md` are read and must name CMF and the core tools (`get_context`, `recall_mem`, `remember`, `capture_session`), else the row warns with what's missing; Claude's account instructions can't be read, so the Claude Desktop row reports Chat and Cowork captures in the last 7 days instead (`/health` `live_captures`). Each app row also shows its captures count. Fixed on the way: `capture_session` dropped the `project` it was given, so every live capture staged with project NULL and was promoted under `misc` even after B19's hook change; episodes and doc proposals now keep it.
- [x] **v0.1.8 (released 2026-10-26):** Antigravity fixes from Todd's test. Its standalone app keeps settings in `~/.gemini/config/` (`mcp_config.json`, `hooks.json`, global rules in `AGENTS.md`), but CMF still read and wrote `~/.gemini/antigravity/mcp_config.json`, so it never saw the live entry (public address, stale OAuth client → `invalid_client`). CMF now uses `~/.gemini/config/mcp_config.json` when that folder exists, and replaces the OAuth entry with a local `cmf bridge`. A bridge entry from a release-named binary (`cmf-macos-universal`) now reads back as CMF's (it was rolled back as wrong). Antigravity has no editor for global rules, so `cmf instructions add <app>` and an **Add to …** button on the Claude Code, Codex and Antigravity cards write CMF's block into the app's file (between cmf:begin/end markers, backup kept); Status checks Antigravity's `~/.gemini/config/AGENTS.md` and offers the same fix.
- [x] **v0.1.9 (released 2026-10-26):** `cmf capture hook install <app>` replaces the older Python checkpoint hook with the cmf one (in place, same `cmf-capture-nudge` marker; a legacy worker hook beside it stays) instead of stopping at "already has the Python checkpoint hook", which left Status's own advice impossible to follow (found on Todd's Antigravity). The Python-hook warning gets a **Replace** button on the card and as the Status row's fix. Status shows every time as the local date and time (`10/26/26 03:42:38 PM`: checked, last call, seen, last transcript, latest capture, self-test, refused calls) instead of "20 min ago"; `/health` names the model each provider runs and the LLM and Embeddings cards show it (Todd's requests).
- [x] **v1.0.0 (released 2026-10-26):** the first full release (0.x were previews): the release workflow marks only 0.x tags as prereleases, and the notes no longer call it a preview. On the Features page, the Claude Code and Claude Desktop switches are disabled while the claude.ai connector covers the app and it has no local entry (turning one on could only add CMF's tools a second time); with a local entry the switch stays usable, to remove it.

### Not in scope

- Making Claude **Connectors** work with localhost (Anthropic's product rule). The app explains and offers the Remote access feature instead.
- Multi-user hosting (see PUBLIC-PREVIEW-ONBOARDING.md, Phase 4).
- **Messaging apps** (Gmail redesign, Slack, Teams): a separate design. The app shows them as "coming soon" Sources.
- New knowledge providers for Google Drive and Microsoft 365 / SharePoint ("coming soon" under option C).
- Installing Docker for the user. The app detects it's missing and links to Docker Desktop (or OrbStack / Colima on macOS).

---

## 5. Definition of done

A new tester on a clean Mac or Windows PC, with Docker Desktop installed and one API key, without help from the author:

1. downloads Context Memory Fabric management, opens it, picks features and clicks Apply; about 10 minutes total;
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
