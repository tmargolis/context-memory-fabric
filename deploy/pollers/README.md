# Background capture: poller and server templates

CMF captures your coding sessions by polling their transcripts on disk. Each poller runs every 15 minutes, journals new turns, and extracts reviewable episodes and doc proposals from them. These templates install the pollers, and optionally the MCP server, as background jobs on macOS (launchd), Linux (systemd `--user`) or Windows (Task Scheduler).

| Poller | Reads | Install when you use |
|---|---|---|
| `claude-code` | `~/.claude/projects/` (Claude Code CLI and the desktop app's Code tab, including subagents) | Claude Code |
| `codex` | `$CODEX_HOME/sessions`, default `~/.codex/sessions` | Codex CLI |
| `cowork` | Claude Desktop's local Cowork session folder | Cowork sessions that run on this computer. Cloud Cowork sessions leave no transcript on disk; only their calls to CMF's tools are captured |
| `antigravity` | `~/.gemini/antigravity/` | Antigravity IDE |

The default install is `claude-code` and `codex`.

## Before you install

1. **Set up CMF first** (docs/SETUP.md): the virtualenv (`uv sync`), FalkorDB, and a `.env`. The jobs run the repo's own interpreter, `.venv/bin/python3` (Windows: `.venv\Scripts\python.exe`), from the repo folder, so they read the same `.env`.
2. **Pick the extraction LLM for background capture.** The pollers extract with `CMF_CAPTURE_LLM_PROVIDER`, not `CMF_LLM_PROVIDER`, and it defaults to `local`. Without a local model server, set it in `.env` to the provider whose key you have (`anthropic`, `openai` or `gemini`). Unattended capture spends that provider's quota: one long session is dozens of extraction calls.
3. **On Linux and Windows, set `CMF_PROJECT_ROOTS`** in `.env` to the folders your projects live in, e.g. `CMF_PROJECT_ROOTS=~/code,~/work` or `C:\Users\you\Dev`. The built-in default only knows the macOS layout (`/Users/<you>/Dev`, `/Users/<you>/Documents`), and without it sessions land in project `other`.
4. **Try one pass by hand** before scheduling it, to see it work and catch configuration errors:
   ```bash
   .venv/bin/python3 -m server.adapters.claude_code.cli status   # read-only
   .venv/bin/python3 -m server.adapters.claude_code.cli tail     # one real pass
   ```
   The first `tail` reads every existing transcript, so it can take a while.

## macOS (launchd)

```bash
deploy/pollers/macos/install.sh --dry-run          # print the plists, change nothing
deploy/pollers/macos/install.sh                    # claude-code + codex
deploy/pollers/macos/install.sh --pollers "claude-code codex cowork" --mcp-server
```

Agents go in `~/Library/LaunchAgents/com.cmf.<name>-poller.plist`, logs in `~/Library/Logs/cmf-<name>-poller.log` (each run prints a JSON report) and `-err.log`. The script refuses to replace an agent that is already loaded under the same label unless you pass `--replace`. Remove them with `--uninstall` (same `--pollers` / `--mcp-server` flags). `--uninstall` unloads by label, so it also stops any older agent you installed under that label by hand.

## Linux (systemd --user)

```bash
deploy/pollers/linux/install.sh --dry-run
deploy/pollers/linux/install.sh                    # claude-code + codex
loginctl enable-linger "$USER"                     # keep polling while logged out
```

Each poller is a `cmf-<name>-poller.service` (one pass) started by `cmf-<name>-poller.timer`, in `~/.config/systemd/user/`. Logs: `journalctl --user -u cmf-claude-code-poller.service`. Status: `systemctl --user list-timers 'cmf-*'`. Remove with `--uninstall`.

## Windows (Task Scheduler)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\pollers\windows\install.ps1 -WhatIf
powershell -ExecutionPolicy Bypass -File deploy\pollers\windows\install.ps1
powershell -ExecutionPolicy Bypass -File deploy\pollers\windows\install.ps1 -Pollers claude-code,codex -McpServer
```

Tasks are registered under `\CMF\` in Task Scheduler, run as you with the S4U logon type (no stored password, no console window on each poll), and append to `%LOCALAPPDATA%\cmf\logs\cmf-<name>-poller.log`. If registration is refused, run the same command from an elevated PowerShell. Remove with `-Uninstall`.

Windows notes:
- Claude Code and Codex keep their transcripts under `%USERPROFILE%\.claude` and `%USERPROFILE%\.codex`, which is where the pollers look.
- The pollers' file locks use `msvcrt` on Windows (`server/adapters/file_lock.py`), so the shared Spark/extraction slot works the same as on macOS and Linux.
- The optional *hooks* that trigger a pass right after a session ends (Codex: `python -m server.adapters.codex.cli hooks install`; Claude Code and Antigravity: the helpers in `server/adapters/*/hooks.py`) write POSIX shell commands, so skip them on Windows. The poller alone captures everything, just up to one interval later.

## Settings the templates fill in

| Placeholder | macOS / Linux flag | Windows parameter | Default |
|---|---|---|---|
| repo path | (the checkout the script is in) | (same) | — |
| interpreter | `--python` | `-Python` | the repo's `.venv` |
| interval | `--interval SECS` | `-IntervalMinutes` | 900 s / 15 min |
| pollers | `--pollers "a b"`, `--all` | `-Pollers a,b`, `-All` | `claude-code codex` |
| MCP server | `--mcp-server`, `--port` | `-McpServer`, `-Port` | off, port 8000 |
| subcommand | `--command` | `-Command` | `tail` (`status` is read-only, for testing) |

Templates: `macos/*.plist.template`, `linux/*.template`; the Windows script builds its tasks directly. Anything that only one deployment needs, such as an SSH tunnel to a remote model server, stays out of these templates.

## After it runs

New episodes and doc proposals wait in the review queue; nothing reaches memory or your wiki until you approve it. Review with `list_review_conversations` from any connected client (docs/CLIENTS.md), or `uv run python -m server.review.cli queue --tier 1` from a terminal.
