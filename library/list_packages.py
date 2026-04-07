"""List installed packages in the REPL environment."""
import subprocess
from pathlib import Path

_project_root = Path(__file__).parent.parent
_uv_path = Path.home() / ".local" / "bin" / "uv"


def list_packages(filter: str = "") -> str:
    """List installed packages in the REPL env, optionally filtered by name substring."""
    repl_python = _project_root / "repl_venv" / "bin" / "python"

    try:
        result = subprocess.run(
            [str(_uv_path), "pip", "list", "--python", str(repl_python)],
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            return f"Failed to list packages:\n{result.stderr.strip()}"

        lines = result.stdout.strip().split("\n")

        # Skip header lines (Package, Version, -------)
        packages = []
        for line in lines[2:]:  # Skip "Package Version" and "------- -------"
            if line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    name, version = parts[0], parts[1]
                    if not filter or filter.lower() in name.lower():
                        packages.append(f"  {name} ({version})")

        if not packages:
            if filter:
                return f"No packages found matching '{filter}'"
            return "No packages installed"

        header = f"Installed packages ({len(packages)})"
        if filter:
            header += f" matching '{filter}'"
        header += ":"

        return header + "\n" + "\n".join(packages)

    except subprocess.TimeoutExpired:
        return "Timed out listing packages"
    except FileNotFoundError:
        return "Error: UV is not installed"
    except Exception as e:
        return f"Error listing packages: {str(e)}"
