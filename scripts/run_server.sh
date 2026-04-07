#!/bin/zsh
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
LOG="$REPO/server.log"

echo "[run_server.sh] Starting at $(date)" >> "$LOG"

source "$REPO/.venv/bin/activate" 2>> "$LOG"
if [ $? -ne 0 ]; then
	echo "[run_server.sh] ERROR: Failed to activate .venv" >> "$LOG"
	exit 1
fi

# --- Toolbox backend (core tools) on :8765 ---
echo "[run_server.sh] Starting toolbox backend (fastmcp --reload) on :8765" >> "$LOG"
fastmcp run server.py:mcp -t http --host localhost --port 8765 --path /mcp --reload --reload-dir "$REPO/tools" --no-banner --skip-env >> "$LOG" 2>&1 &
TOOLBOX_PID=$!
echo "[run_server.sh] Toolbox backend PID: $TOOLBOX_PID" >> "$LOG"

# --- Pentest backend (security tools) on :8766 ---
echo "[run_server.sh] Starting pentest backend (fastmcp --reload) on :8766" >> "$LOG"
fastmcp run server_pentest.py:mcp -t http --host localhost --port 8766 --path /mcp --reload --reload-dir "$REPO/tools/pentest" --no-banner --skip-env >> "$LOG" 2>&1 &
PENTEST_PID=$!
echo "[run_server.sh] Pentest backend PID: $PENTEST_PID" >> "$LOG"

# Wait for backends to be ready
sleep 3

# --- Toolbox proxy on :11000 ---
echo "[run_server.sh] Starting toolbox proxy on :11000" >> "$LOG"
python "$REPO/infra/proxy.py" >> "$LOG" 2>&1 &
TOOLBOX_PROXY_PID=$!
echo "[run_server.sh] Toolbox proxy PID: $TOOLBOX_PROXY_PID" >> "$LOG"

# --- Pentest proxy on :11001 ---
echo "[run_server.sh] Starting pentest proxy on :11001" >> "$LOG"
python "$REPO/infra/proxy_pentest.py" >> "$LOG" 2>&1 &
PENTEST_PROXY_PID=$!
echo "[run_server.sh] Pentest proxy PID: $PENTEST_PROXY_PID" >> "$LOG"

# --- Feedback watcher ---
echo "[run_server.sh] Starting feedback watcher" >> "$LOG"
python "$REPO/infra/watcher.py" >> "$LOG" 2>&1 &
WATCHER_PID=$!
echo "[run_server.sh] Feedback watcher PID: $WATCHER_PID" >> "$LOG"

# Clean shutdown on exit
cleanup() {
	echo "[run_server.sh] Shutting down all processes at $(date)" >> "$LOG"
	kill $TOOLBOX_PROXY_PID $PENTEST_PROXY_PID $TOOLBOX_PID $PENTEST_PID $WATCHER_PID 2>/dev/null
	wait $TOOLBOX_PROXY_PID $PENTEST_PROXY_PID $TOOLBOX_PID $PENTEST_PID $WATCHER_PID 2>/dev/null
	echo "[run_server.sh] Finished at $(date)" >> "$LOG"
}
trap cleanup EXIT INT TERM

# Wait for any child to exit
wait
