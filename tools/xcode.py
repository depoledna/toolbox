"""Xcode build tool — compile-check projects with zero configuration."""

import asyncio
import json
import re
import shutil
import time
from pathlib import Path


def _find_project(project_path: str) -> tuple[str, Path, Path]:
    """Locate .xcworkspace or .xcodeproj, return (flag, file, project_dir)."""
    start = Path(project_path).expanduser().resolve() if project_path else Path.cwd()

    if start.suffix == ".xcworkspace":
        return "-workspace", start, start.parent
    if start.suffix == ".xcodeproj":
        ws = _sibling_workspace(start)
        if ws:
            return "-workspace", ws, start.parent
        return "-project", start, start.parent

    # Search directory, then walk up (max 3 levels)
    search_dirs = [start] if start.is_dir() else [start.parent]
    for _ in range(3):
        parent = search_dirs[-1].parent
        if parent == search_dirs[-1]:
            break
        search_dirs.append(parent)

    for d in search_dirs:
        result = _scan_dir(d)
        if result:
            return result

    searched = ", ".join(str(d) for d in search_dirs)
    raise FileNotFoundError(f"No .xcodeproj or .xcworkspace found. Searched: {searched}")


def _scan_dir(d: Path) -> tuple[str, Path, Path] | None:
    """Scan a single directory for Xcode projects."""
    # Prefer workspaces (CocoaPods/SPM)
    workspaces = [
        ws for ws in sorted(d.glob("*.xcworkspace"))
        if not ws.parent.suffix == ".xcodeproj"  # skip internal workspace
    ]
    if len(workspaces) == 1:
        return "-workspace", workspaces[0], d

    projects = sorted(d.glob("*.xcodeproj"))
    if len(projects) == 1:
        ws = _sibling_workspace(projects[0])
        if ws:
            return "-workspace", ws, d
        return "-project", projects[0], d

    if len(projects) > 1 or len(workspaces) > 1:
        found = [p.name for p in workspaces + projects]
        raise FileNotFoundError(f"Multiple Xcode projects in {d}: {found}. Pass project_path explicitly.")

    return None


def _sibling_workspace(xcodeproj: Path) -> Path | None:
    """Check if a .xcworkspace exists next to a .xcodeproj (CocoaPods/SPM)."""
    for ws in xcodeproj.parent.glob("*.xcworkspace"):
        if ws.parent.suffix != ".xcodeproj":
            return ws
    return None


async def _detect_scheme(flag: str, project_file: Path) -> str:
    """Auto-detect the best scheme from the project."""
    proc = await asyncio.create_subprocess_exec(
        "xcodebuild", "-list", "-json", flag, str(project_file),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("xcodebuild -list timed out after 15s")

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[:300] if stderr else ""
        raise RuntimeError(f"xcodebuild -list failed (exit {proc.returncode}): {detail}")

    try:
        data = json.loads(stdout.decode())
    except json.JSONDecodeError:
        snippet = stdout.decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"xcodebuild -list returned invalid JSON. Output:\n{snippet}")

    key = "workspace" if flag == "-workspace" else "project"
    schemes = data.get(key, {}).get("schemes", [])
    if not schemes:
        raise RuntimeError("No schemes found in project")

    if len(schemes) == 1:
        return schemes[0]

    # Filter out test/extension targets
    skip = {"Tests", "UITests", "Watch", "Widget", "Intent", "Extension"}
    main = [s for s in schemes if not any(s.endswith(suffix) for suffix in skip)]
    return main[0] if main else schemes[0]


async def _pick_destination() -> tuple[str, str]:
    """Find iPhone 17 Pro Max simulator. Returns (destination_str, display_name)."""
    proc = await asyncio.create_subprocess_exec(
        "xcrun", "simctl", "list", "devices", "available",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return "generic/platform=iOS Simulator", "Generic iOS Simulator"

    output = stdout.decode()
    # Pick the latest Pro Max simulator dynamically
    pro_max = re.findall(r"(iPhone \d+ Pro Max) \(([A-Fa-f0-9-]+)\)", output)
    if pro_max:
        pro_max.sort(key=lambda m: int(re.search(r"\d+", m[0]).group()), reverse=True)
        name, uuid = pro_max[0]
        return f"platform=iOS Simulator,id={uuid}", name

    # Fallback: any iPhone simulator
    match = re.search(r"(iPhone[^(]+?)\s+\(([A-Fa-f0-9-]+)\)", output)
    if match:
        name, uuid = match.group(1).strip(), match.group(2)
        return f"platform=iOS Simulator,id={uuid}", name

    return "generic/platform=iOS Simulator", "Generic iOS Simulator"


def _parse_build_output(raw: str, project_dir: Path, elapsed: float) -> str:
    """Extract errors, warnings, and build status from xcodebuild output."""
    prefix = str(project_dir) + "/"
    lines = raw.split("\n")

    errors: list[str] = []
    warnings: list[str] = []
    succeeded = "** BUILD SUCCEEDED **" in raw

    for i, line in enumerate(lines):
        short = line.replace(prefix, "")

        if ": error:" in line or line.startswith("ld: ") or "Undefined symbols" in line:
            entry = short
            # Attach following note lines directly to this error
            for j in range(i + 1, min(i + 3, len(lines))):
                if ": note:" in lines[j]:
                    entry += f"\n    {lines[j].replace(prefix, '').strip()}"
                else:
                    break
            errors.append(f"  {entry}")
        elif ": warning:" in line:
            warnings.append(f"  {short}")

    # Deduplicate preserving order
    errors = list(dict.fromkeys(errors))
    warnings = list(dict.fromkeys(warnings))

    # Build result
    parts: list[str] = []

    status = "BUILD SUCCEEDED" if succeeded else "BUILD FAILED"
    parts.append(f"{status} in {elapsed:.1f}s")

    if errors:
        total = len(errors)
        shown = errors[:30]
        parts.append("")
        parts.append(f"{total} error{'s' if total != 1 else ''}:")
        parts.extend(shown)
        if total > 30:
            parts.append(f"  ... and {total - 30} more errors")

    if warnings:
        total = len(warnings)
        shown = warnings[:20]
        parts.append("")
        parts.append(f"{total} warning{'s' if total != 1 else ''}:")
        parts.extend(shown)
        if total > 20:
            parts.append(f"  ... and {total - 20} more warnings")

    return "\n".join(parts)


async def xcode_build(
    project_path: str = "",
    scheme: str = "",
    configuration: str = "Debug",
    destination: str = "",
    clean: bool = False,
    timeout: int = 300,
) -> str:
    """Build an Xcode project and return parsed errors and warnings.

    Call with zero args to build the current directory's project. Auto-detects .xcworkspace/
    .xcodeproj, scheme, and destination (latest iPhone Pro Max simulator). Requires Xcode
    installed on macOS. Use for compile-checking after code changes — not for archive/distribution.

    Args:
        project_path: .xcodeproj, .xcworkspace, or directory. Empty = auto-detect from cwd.
        scheme: Xcode scheme. Empty = auto-detect (filters out test/extension targets).
        configuration: "Debug" (default) or "Release".
        destination: Xcode destination string. Empty = iPhone Pro Max simulator.
        clean: Clean build folder before building.
        timeout: Max seconds (default 300, max 600).

    Returns:
        BUILD SUCCEEDED or BUILD FAILED with file:line error/warning list. Raw xcodebuild
        output is filtered to show only actionable diagnostics.
    """
    if not shutil.which("xcodebuild"):
        return "Error: xcodebuild not found. Install Xcode from the App Store."

    effective_timeout = min(timeout, 600)

    try:
        flag, project_file, project_dir = _find_project(project_path)
    except FileNotFoundError as e:
        return f"Error: {e}"

    if not scheme:
        try:
            scheme = await _detect_scheme(flag, project_file)
        except RuntimeError as e:
            return f"Error detecting scheme: {e}"

    if destination:
        dest_str, dest_name = destination, destination
    else:
        dest_str, dest_name = await _pick_destination()

    # Build command
    cmd = ["xcodebuild"]
    if clean:
        cmd.append("clean")
    cmd.extend([
        "build",
        flag, str(project_file),
        "-scheme", scheme,
        "-configuration", configuration,
        "-destination", dest_str,
        "CODE_SIGNING_ALLOWED=NO",
    ])

    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(project_dir),
    )

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"BUILD TIMED OUT after {effective_timeout}s — xcodebuild process killed"

    elapsed = time.monotonic() - start
    raw = stdout.decode("utf-8", errors="replace")
    # Truncate — keep tail where errors live
    max_raw = 500_000
    if len(raw) > max_raw:
        raw = raw[-max_raw:]
    result = _parse_build_output(raw, project_dir, elapsed)

    result += f"\n\nScheme: {scheme} | Config: {configuration} | Destination: {dest_name}"

    return result
