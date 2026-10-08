#!/usr/bin/env bash
# Install CMF's transcript pollers (and optionally the MCP server) as launchd
# user agents. See deploy/pollers/README.md.
#
#   deploy/pollers/macos/install.sh                      # claude-code + codex pollers
#   deploy/pollers/macos/install.sh --pollers "claude-code codex cowork" --mcp-server
#   deploy/pollers/macos/install.sh --dry-run            # print the plists, change nothing
#   deploy/pollers/macos/install.sh --uninstall
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
PYTHON="$REPO/.venv/bin/python3"
POLLERS="claude-code codex"
INTERVAL=900
PORT=8000
PREFIX="com.cmf"
COMMAND="tail"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"
MCP_SERVER=0
UNINSTALL=0
DRY_RUN=0
REPLACE=0

usage() {
  cat <<USAGE
Options:
  --pollers "a b"   which pollers: claude-code codex cowork antigravity (default: "$POLLERS")
  --all             all four pollers
  --mcp-server      also install the MCP server (streamable HTTP on 127.0.0.1)
  --port N          MCP server port (default $PORT)
  --interval SECS   seconds between polls (default $INTERVAL)
  --python PATH     interpreter (default $PYTHON)
  --prefix LABEL    launchd label prefix (default $PREFIX)
  --command CMD     poller subcommand (default tail; "status" is read-only, for testing)
  --log-dir DIR     where logs go (default $LOG_DIR)
  --replace         replace an already-loaded agent with the same label
  --uninstall       unload and remove the agents
  --dry-run         print what would be written, change nothing
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --pollers) POLLERS="$2"; shift 2 ;;
    --all) POLLERS="claude-code codex cowork antigravity"; shift ;;
    --mcp-server) MCP_SERVER=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --python) PYTHON="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --command) COMMAND="$2"; shift 2 ;;
    --log-dir) LOG_DIR="$2"; shift 2 ;;
    --replace) REPLACE=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

module_for() {
  case "$1" in
    claude-code) echo "server.adapters.claude_code.cli" ;;
    codex) echo "server.adapters.codex.cli" ;;
    cowork) echo "server.adapters.claude_cowork.cli" ;;
    antigravity) echo "server.adapters.antigravity.cli" ;;
    *) echo "unknown poller: $1 (use claude-code, codex, cowork, antigravity)" >&2; exit 2 ;;
  esac
}

render() {  # render TEMPLATE LABEL NAME MODULE
  sed -e "s|{{LABEL}}|$2|g" -e "s|{{NAME}}|$3|g" -e "s|{{MODULE}}|$4|g" \
      -e "s|{{PYTHON}}|$PYTHON|g" -e "s|{{REPO}}|$REPO|g" -e "s|{{LOG_DIR}}|$LOG_DIR|g" \
      -e "s|{{INTERVAL}}|$INTERVAL|g" -e "s|{{PORT}}|$PORT|g" -e "s|{{COMMAND}}|$COMMAND|g" "$1"
}

DOMAIN="gui/$(id -u)"

is_loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }

install_one() {  # install_one TEMPLATE LABEL NAME MODULE
  local plist="$AGENTS_DIR/$2.plist"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "--- would write $plist"; render "$1" "$2" "$3" "$4"; return
  fi
  if is_loaded "$2"; then
    if [ "$REPLACE" -ne 1 ]; then
      echo "skip $2: already loaded (pass --replace to swap it for this template)" >&2; return
    fi
    launchctl bootout "$DOMAIN/$2" 2>/dev/null || true
  fi
  mkdir -p "$AGENTS_DIR" "$LOG_DIR"
  render "$1" "$2" "$3" "$4" > "$plist"
  plutil -lint "$plist" >/dev/null
  launchctl bootstrap "$DOMAIN" "$plist"
  echo "installed $2 -> $plist"
}

uninstall_one() {  # uninstall_one LABEL
  local plist="$AGENTS_DIR/$1.plist"
  if [ "$DRY_RUN" -eq 1 ]; then echo "--- would remove $1 ($plist)"; return; fi
  launchctl bootout "$DOMAIN/$1" 2>/dev/null || true
  rm -f "$plist"
  echo "removed $1"
}

if [ "$UNINSTALL" -eq 0 ] && [ "$DRY_RUN" -eq 0 ] && [ ! -x "$PYTHON" ]; then
  echo "no interpreter at $PYTHON: create the venv first (uv sync), or pass --python" >&2; exit 1
fi

for name in $POLLERS; do
  module="$(module_for "$name")"
  label="$PREFIX.$name-poller"
  if [ "$UNINSTALL" -eq 1 ]; then uninstall_one "$label"
  else install_one "$HERE/poller.plist.template" "$label" "$name" "$module"; fi
done

if [ "$MCP_SERVER" -eq 1 ]; then
  if [ "$UNINSTALL" -eq 1 ]; then uninstall_one "$PREFIX.mcp-server"
  else install_one "$HERE/mcp-server.plist.template" "$PREFIX.mcp-server" "mcp-server" "server.mcp"; fi
fi
