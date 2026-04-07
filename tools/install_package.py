import subprocess
from pathlib import Path

_project_root = Path(__file__).parent.parent
_uv_path = Path.home() / ".local" / "bin" / "uv"


async def install_package(package: str, venv: str = "") -> str:
    """Install a Python package into the REPL environment using UV.

    Use before python_repl when you need a package that isn't installed yet.
    Installs into the REPL venv by default, or a project venv if path is provided.
    Do NOT use for server dependencies — those go in the server's .venv manually.

    Args:
        package: Package spec (e.g. "requests", "pandas==2.0.0", "numpy>=1.24")
        venv: Path to a venv to install into (e.g. "/path/to/project/.venv").
              Empty = REPL environment.

    Returns:
        Success/failure message. Times out after 120s.
    """
    if venv:
        target_python = Path(venv).expanduser().resolve() / "bin" / "python"
        if not target_python.exists():
            return f"Error: venv python not found at '{target_python}'. Create the venv first (e.g., uv venv {venv})."
        target_label = str(Path(venv).expanduser().resolve())
    else:
        target_python = _project_root / "repl_venv" / "bin" / "python"
        target_label = "REPL environment"

    try:
        result = subprocess.run(
            [str(_uv_path), "pip", "install", package, "--python", str(target_python)],
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode == 0:
            output = result.stdout.strip()
            if "already installed" in output.lower() or "already satisfied" in output.lower():
                return f"Package '{package}' is already installed in {target_label}"
            return f"Successfully installed '{package}' into {target_label}"
        else:
            return f"Failed to install '{package}':\n{result.stderr.strip()}"

    except subprocess.TimeoutExpired:
        return f"Installation timed out for '{package}'"
    except FileNotFoundError:
        return "Error: UV is not installed. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    except Exception as e:
        return f"Error installing '{package}': {str(e)}"
