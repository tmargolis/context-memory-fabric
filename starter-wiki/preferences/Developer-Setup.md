# Developer Environment & Coding Conventions

Guidelines and configuration standards across development workstations and AI client tooling.

## Primary Tooling

- **Operating Systems:** macOS (M-series Apple Silicon) and Debian GNU/Linux 12 (Bookworm).
- **Shell:** zsh with starship prompt and zoxide for navigation.
- **Python Toolchain:**
  - Runtime management: `uv` and `pyenv` (Python 3.12+ standard).
  - Formatting & Linting: `ruff` for linting and formatting; `mypy --strict` for type checking.
  - Dependency isolation: Virtual environments managed via `uv sync`.
- **Containers:** Docker Engine with Compose V2. Colima or OrbStack preferred on macOS for lightweight Linux virtualization.

## Model Context Protocol (MCP) Setup

- Context Memory Fabric is configured as the unified memory and context server across clients (Claude Desktop, Cursor, local IDE extensions).
- **Transport:**
  - Desktop clients on the same machine connect via `stdio` using `uv run python -m server.mcp`.
  - Remote or containerized agents connect via `streamable-http` or `sse` with mutual bearer token authentication (`CMF_MCP_AUTH_TOKEN`).
- **Context Routing Rule:**
  - Call `get_context` first whenever a prompt requires past project history or established system architecture.
  - Use `search_wiki` when querying specific technical designs or configuration values.
  - Use `recall_mem` when checking recent decisions, debugging outcomes, or meeting notes.
  - Checkpoint decisions using `capture_session` or explicit `remember` calls.
