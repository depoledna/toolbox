#!/bin/zsh
# Restart backend(s) — proxies stay up, Claude Code stays connected.
# Only needed for server.py/server_pentest.py changes; tool file changes are handled by --reload automatically.
#
# Usage: reload.sh [toolbox|pentest|all]  (default: all)
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
source "$REPO/.venv/bin/activate"

TARGET="${1:-all}"

reload_toolbox() {
	pkill -f "fastmcp run server.py:mcp" && sleep 1 && pkill -9 -f "fastmcp run server.py:mcp" 2>/dev/null; echo "Toolbox backend stopped"
	sleep 1
	fastmcp run server.py:mcp -t http --host localhost --port 8765 --path /mcp --reload --reload-dir "$REPO/tools" --no-banner --skip-env >> "$REPO/server.log" 2>&1 &
	echo "Toolbox backend restarted (PID: $!)"
}

reload_pentest() {
	pkill -f "fastmcp run server_pentest.py:mcp" && sleep 1 && pkill -9 -f "fastmcp run server_pentest.py:mcp" 2>/dev/null; echo "Pentest backend stopped"
	sleep 1
	fastmcp run server_pentest.py:mcp -t http --host localhost --port 8766 --path /mcp --reload --reload-dir "$REPO/tools/pentest" --no-banner --skip-env >> "$REPO/server.log" 2>&1 &
	echo "Pentest backend restarted (PID: $!)"
}

case "$TARGET" in
	toolbox) reload_toolbox ;;
	pentest) reload_pentest ;;
	all)     reload_toolbox; reload_pentest ;;
	*)       echo "Usage: $0 [toolbox|pentest|all]"; exit 1 ;;
esac
