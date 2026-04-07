import importlib
import inspect
from pathlib import Path
from fastmcp import FastMCP
from dotenv import load_dotenv
from typing import Dict, Any as AnyType
import signal
import sys

load_dotenv(override=True)

mcp = FastMCP("Toolbox")

# Auto-discover tools: register all public async functions from tools/**/*.py
tools_dir = Path(__file__).parent / "tools"
for file in sorted(tools_dir.glob("*.py")):
    if file.name.startswith("_"):
        continue
    rel = file.relative_to(tools_dir)
    module_path = "tools." + ".".join(rel.with_suffix("").parts)
    module = importlib.import_module(module_path)
    for name, func in inspect.getmembers(module, inspect.iscoroutinefunction):
        if not name.startswith("_") and func.__module__ == module.__name__:
            mcp.tool()(func)


def run_servers():
    """Start the MCP server."""
    args: Dict[str, AnyType] = dict(
        host="localhost", transport="http", port=8765, path="/mcp", log_level="debug"
    )

    def _handle_sig(sig, frame):  # noqa: ARG001
        print("\n[server] Received signal, shutting down server.")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    mcp.run(**args)


if __name__ == "__main__":
    run_servers()
