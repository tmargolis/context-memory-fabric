# Background capture: poller and server templates

CMF captures your coding sessions by polling their transcripts on disk. One background job, the capture runner (`python -m server.adapters.capture_runner tail`), runs every 15 minutes. Each pass goes through every transcript source in turn, journals new turns, and extracts reviewable episodes and doc proposals from them. These templates install it, and optionally the MCP server, as background jobs on macOS (launchd), Linux (systemd `--user`) or Windows (Task Scheduler).

The runner's sources:

| Source | Reads | Captured when you use |
|---|---|---|
| `claude_code` | `~/.claude/projects/` (Claude Code CLI and the desktop app's Code tab, including subagents) | Claude Code |
| `codex` | `$CODEX_HOME/sessions`, default `~/.codex/sessions` | Codex CLI |
| `claude_cowork` | Claude Desktop's local Cowork session folder | Cowork sessions that run on this computer. Cloud Cowork sessions leave no transcript on disk; only their calls to CMF's tools are captured |
| `antigravity` | `~/.gemini/antigravity/` | Antigravity IDE |

By default (`CMF_CAPTURE_SOURCES=auto`) the runner uses every source whose folder exists on this computer. Set `CMF_CAPTURE_SOURCES=claude_code,codex` in `.env` to choose them yourself. `python -m server.adapters.capture_runner status` shows which sources are on.

The older layout, one job per source (`--pollers "claude-code codex"`), still installs, but separate jobs fire in the same second and compete for the extraction model. Prefer the single runner.

### Concurrency

Extraction on a local model server (`CMF_CAPTURE_LLM_PROVIDER=local`) can only take one big job at a time. Capture passes, backfills, the wiki relationship extraction and `edit_memory` re-extracts share one slot, a file lock on `imports/journal/spark_job.lock` (`server/adapters/spark_lock.py`). A capture pass that finds the slot taken skips before reading anything, its log names the job holding it, and the next pass catches up. Nothing is lost.

With a hosted provider (`anthropic`, `openai`, `gemini`) for both capture and interactive use, the slot does nothing. `CMF_LLM_SLOT=always` forces it on, e.g. for a self-hosted OpenAI-compatible server; `never` turns it off.

The pollers read every transcript in full. To capture without reading transcripts, use capture checkpoints, a Stop hook that makes the agent itself call `capture_session` ([docs/SETUP.md, Background Capture](../../docs/SETUP.md#background-capture)). The two can run together.

## Before you install

1. **Set up CMF first** (docs/SETUP.md): the virtualenv (`uv sync`), FalkorDB, and a `.env`. The jobs run the repo's own interpreter, `.venv/bin/python3` (Windows: `.venv\Scripts\python.exe`), from the repo folder, so they read the same `.env`.
2. **Pick the extraction LLM for background capture.** The pollers extract with `CMF_CAPTURE_LLM_PROVIDER`, not `CMF_LLM_PROVIDER`, and it defaults to `local`. Without a local model server, set it in `.env` to the provider whose key you have (`anthropic`, `openai` or `gemini`). Unattended capture spends that provider's quota: one long session is dozens of extraction calls.
3. **On Linux and Windows, set `CMF_PROJECT_ROOTS`** in `.env` to the folders your projects live in, e.g. `CMF_PROJECT_ROOTS=~/code,~/work` or `C:\Users\you\Dev`. The built-in default only knows the macOS layout (`/Users/<you>/Dev`, `/Users/<you>/Documents`), and without it sessions land in project `other`.
4. **Try one pass by hand** before scheduling it, to see it work and catch configuration errors:
   ```bash
   .venv/bin/python3 -m server.adapters.capture_runner status   # read-only
   .venv/bin/python3 -m server.adapters.capture_runner tail     # one real pass
   ```
   The first `tail` reads every existing transcript, so it can take a while.

## macOS (launchd)

```bash
deploy/pollers/macos/install.sh --dry-run          # print the plists, change nothing
deploy/pollers/macos/install.sh                    # the capture runner
deploy/pollers/macos/install.sh --migrate          # same, and retire per-source agents from an older install
deploy/pollers/macos/install.sh --mcp-server       # also the MCP server
```

The agent goes in `~/Library/LaunchAgents/com.cmf.capture-poller.plist`, logs in `~/Library/Logs/cmf-capture-poller.log` (each run prints a JSON report, one section per source) and `-err.log`. `--migrate` unloads the old `com.cmf.<name>-poller` / `local.cmf.<name>-poller` agents and moves their plists to `~/Library/LaunchAgents/cmf-retired/`, so you can put one back by moving it back and running `launchctl bootstrap gui/$(id -u) <plist>`. The script refuses to replace an agent that is already loaded under the same label unless you pass `--replace`. Remove them with `--uninstall` (same `--pollers` / `--mcp-server` flags). `--uninstall` unloads by label, so it also stops any older agent you installed under that label by hand.

## Linux (systemd --user)

```bash
deploy/pollers/linux/install.sh --dry-run
deploy/pollers/linux/install.sh                    # the capture runner (--migrate removes per-source units)
loginctl enable-linger "$USER"                     # keep polling while logged out
```

The runner is `cmf-capture-poller.service` (one pass) started by `cmf-capture-poller.timer`, in `~/.config/systemd/user/`. Logs: `journalctl --user -u cmf-capture-poller.service`. Status: `systemctl --user list-timers 'cmf-*'`. Remove with `--uninstall`.

## Windows (Task Scheduler)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\pollers\windows\install.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File deploy\pollers\windows\install.ps1
powershell -ExecutionPolicy Bypass -File deploy\pollers\windows\install.ps1 -McpServer
```

Tasks are registered under `\CMF\` in Task Scheduler, run as you with the S4U logon type (no stored password, no console window on each poll), and append to `%LOCALAPPDATA%\cmf\logs\cmf-<name>-poller.log`. If registration is refused, run the same command from an elevated PowerShell. Remove with `-Uninstall`.

Windows notes:
- Claude Code and Codex keep their transcripts under `%USERPROFILE%\.claude` and `%USERPROFILE%\.codex`, which is where the runner looks. An older install's per-source tasks (`\CMF\cmf-<name>-poller`) are not removed automatically: run `install.ps1 -Uninstall -Pollers claude-code,codex,cowork,antigravity`.
- The pollers' file locks use `msvcrt` on Windows (`server/adapters/file_lock.py`), so the shared Spark/extraction slot works the same as on macOS and Linux.
- The optional *hooks* that trigger a pass right after a session ends (Codex: `python -m server.adapters.codex.cli hooks install`; Claude Code and Antigravity: the helpers in `server/adapters/*/hooks.py`) write POSIX shell commands, so skip them on Windows. The poller alone captures everything, just up to one interval later.

## Settings the templates fill in

| Placeholder | macOS / Linux flag | Windows parameter | Default |
|---|---|---|---|
| repo path | (the checkout the script is in) | (same) | — |
| interpreter | `--python` | `-Python` | the repo's `.venv` |
| interval | `--interval SECS` | `-IntervalMinutes` | 900 s / 15 min |
| pollers | `--pollers "a b"`, `--all`, `--migrate` | `-Pollers a,b`, `-All` | `capture` |
| MCP server | `--mcp-server`, `--port` | `-McpServer`, `-Port` | off, port 8000 |
| subcommand | `--command` | `-Command` | `tail` (`status` is read-only, for testing) |

Templates: `macos/*.plist.template`, `linux/*.template`; the Windows script builds its tasks directly. Anything that only one deployment needs, such as an SSH tunnel to a remote model server, stays out of these templates.

## After it runs

New episodes and doc proposals wait in the review queue; nothing reaches memory or your wiki until you approve it. Review with `list_review_conversations` from any connected client (docs/CLIENTS.md), or `uv run python -m server.review.cli queue --tier 1` from a terminal.
