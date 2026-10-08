#!/usr/bin/env bash
# Install CMF's transcript pollers (and optionally the MCP server) as systemd
# --user units. See deploy/pollers/README.md.
#
#   deploy/pollers/linux/install.sh                      # claude-code + codex pollers
#   deploy/pollers/linux/install.sh --pollers "claude-code codex" --mcp-server
#   deploy/pollers/linux/install.sh --dry-run            # print the units, change nothing
#   deploy/pollers/linux/install.sh --uninstall
#
# Units run only while you are logged in unless lingering is on:
#   loginctl enable-linger "$USER"
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
PYTHON="$REPO/.venv/bin/python3"
POLLERS="claude-code codex"
INTERVAL=900
PORT=8000
COMMAND="tail"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
MCP_SERVER=0
UNINSTALL=0
DRY_RUN=0

usage() {
  cat <<USAGE
Options:
  --pollers "a b"   which pollers: claude-code codex cowork antigravity (default: "$POLLERS")
  --all             all four pollers
  --mcp-server      also install the MCP server (streamable HTTP on 127.0.0.1)
  --port N          MCP server port (default $PORT)
  --interval SECS   seconds between polls (default $INTERVAL)
  --python PATH     interpreter (default $PYTHON)
  --command CMD     poller subcommand (default tail; "status" is read-only, for testing)
  --uninstall       stop, disable and remove the units
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
    --command) COMMAND="$2"; shift 2 ;;
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

render() {  # render TEMPLATE NAME MODULE
  sed -e "s|{{NAME}}|$2|g" -e "s|{{MODULE}}|$3|g" -e "s|{{PYTHON}}|$PYTHON|g" \
      -e "s|{{REPO}}|$REPO|g" -e "s|{{INTERVAL}}|$INTERVAL|g" -e "s|{{PORT}}|$PORT|g" \
      -e "s|{{COMMAND}}|$COMMAND|g" "$1"
}

write_unit() {  # write_unit TEMPLATE UNIT_FILE NAME MODULE
  if [ "$DRY_RUN" -eq 1 ]; then echo "--- would write $UNIT_DIR/$2"; render "$1" "$3" "$4"; return; fi
  mkdir -p "$UNIT_DIR"
  render "$1" "$3" "$4" > "$UNIT_DIR/$2"
}

remove_unit() {  # remove_unit UNIT_FILE
  if [ "$DRY_RUN" -eq 1 ]; then echo "--- would remove $UNIT_DIR/$1"; return; fi
  systemctl --user disable --now "$1" 2>/dev/null || true
  rm -f "$UNIT_DIR/$1"
}

if [ "$UNINSTALL" -eq 0 ] && [ "$DRY_RUN" -eq 0 ] && [ ! -x "$PYTHON" ]; then
  echo "no interpreter at $PYTHON: create the venv first (uv sync), or pass --python" >&2; exit 1
fi

enable=()
for name in $POLLERS; do
  module="$(module_for "$name")"
  base="cmf-$name-poller"
  if [ "$UNINSTALL" -eq 1 ]; then
    remove_unit "$base.timer"; remove_unit "$base.service"
  else
    write_unit "$HERE/poller.service.template" "$base.service" "$name" "$module"
    write_unit "$HERE/poller.timer.template" "$base.timer" "$name" "$module"
    enable+=("$base.timer")
  fi
done

if [ "$MCP_SERVER" -eq 1 ]; then
  if [ "$UNINSTALL" -eq 1 ]; then remove_unit "cmf-mcp-server.service"
  else write_unit "$HERE/mcp-server.service.template" "cmf-mcp-server.service" "mcp-server" "server.mcp"; enable+=("cmf-mcp-server.service"); fi
fi

[ "$DRY_RUN" -eq 1 ] && exit 0
systemctl --user daemon-reload
if [ "${#enable[@]}" -gt 0 ]; then
  systemctl --user enable --now "${enable[@]}"
  echo "enabled: ${enable[*]}"
  echo "logs: journalctl --user -u cmf-<name>-poller.service"
fi
