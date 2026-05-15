#!/bin/zsh
# Restart backend — proxy stays up, Claude Code stays connected.
# Only needed for server.py changes; tool file changes are handled by --reload automatically.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
source "$REPO/.venv/bin/activate"

pkill -f "fastmcp run server.py:mcp" && sleep 1 && pkill -9 -f "fastmcp run server.py:mcp" 2>/dev/null; echo "Toolbox backend stopped"
sleep 1
fastmcp run server.py:mcp -t http --host localhost --port 8765 --path /mcp --reload --reload-dir "$REPO/tools" --no-banner --skip-env >> "$REPO/server.log" 2>&1 &
echo "Toolbox backend restarted (PID: $!)"
