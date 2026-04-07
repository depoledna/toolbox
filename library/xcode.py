"""
Xcode TestFlight deployment helpers.

Usage:
    from library.xcode import testflight
    testflight("/path/to/Project.xcodeproj", "Scheme")
"""
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

try:
    import jwt as pyjwt
except ImportError:
    pyjwt = None

DEFAULT_API_KEY_ID = os.getenv("ASC_API_KEY_ID", "")
DEFAULT_ISSUER_ID = os.getenv("ASC_ISSUER_ID", "")
DEFAULT_API_KEY_PATH = os.getenv("ASC_API_KEY_PATH", "")

ENV_API_KEY_ID = "XCODE_TESTFLIGHT_API_KEY_ID"
ENV_ISSUER_ID = "XCODE_TESTFLIGHT_ISSUER_ID"
ENV_API_KEY_PATH = "XCODE_TESTFLIGHT_API_KEY_PATH"


def _resolve_config(value: str | None, env_name: str, default: str) -> str:
    """Resolve setting with precedence: arg > env > default."""
    if value:
        return value
    env_value = os.getenv(env_name)
    if env_value:
        return env_value
    return default


def _ensure_cli(command: str) -> None:
    if shutil.which(command) is None:
        raise FileNotFoundError(f"Required CLI not found on PATH: {command}")


def _tail(text: str, lines: int = 25) -> str:
    parts = [line for line in text.splitlines() if line.strip()]
    if not parts:
        return ""
    return "\n".join(parts[-lines:])


def _run(command: list[str]) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part).strip()
    if result.returncode != 0:
        cmd = " ".join(shlex.quote(part) for part in command)
        raise RuntimeError(
            f"Command failed ({result.returncode}): {cmd}\n{_tail(output, lines=80) or '(no output)'}"
        )
    return output


def _read_pbxproj(project: str) -> tuple[str, int, str, str]:
    """Read project.pbxproj and return (content, build_number, marketing_version, team_id)."""
    pbxproj_path = Path(project) / "project.pbxproj"
    content = pbxproj_path.read_text()

    build_nums = re.findall(r"CURRENT_PROJECT_VERSION = (\d+);", content)
    build_number = int(build_nums[0]) if build_nums else 0

    versions = re.findall(r"MARKETING_VERSION = ([\d.]+);", content)
    marketing_version = versions[0] if versions else "0.0"

    team_ids = re.findall(r"DEVELOPMENT_TEAM = (\w+);", content)
    team_id = team_ids[0] if team_ids else ""

    return content, build_number, marketing_version, team_id


def _increment_build_number(project: str, current: int) -> int:
    """Increment CURRENT_PROJECT_VERSION in the pbxproj file. Returns the new number."""
    new_build = current + 1
    pbxproj_path = Path(project) / "project.pbxproj"
    content = pbxproj_path.read_text()
    content = content.replace(
        f"CURRENT_PROJECT_VERSION = {current};",
        f"CURRENT_PROJECT_VERSION = {new_build};",
    )
    pbxproj_path.write_text(content)
    return new_build


def _write_export_options(path: Path, team_id: str) -> None:
    export_options = {
        "method": "app-store",
        "destination": "upload",
        "signingStyle": "automatic",
        "teamID": team_id,
        "uploadSymbols": True,
    }
    with path.open("wb") as f:
        plistlib.dump(export_options, f)


def _auth_flags(key_path: Path, key_id: str, issuer_id: str) -> list[str]:
    """Return xcodebuild auth flags for API key authentication."""
    return [
        "-authenticationKeyPath", str(key_path),
        "-authenticationKeyID", key_id,
        "-authenticationKeyIssuerID", issuer_id,
    ]


def _generate_asc_token(key_id: str, issuer_id: str, key_path: Path) -> str:
    """Generate a JWT for App Store Connect API."""
    if pyjwt is None:
        raise ImportError("PyJWT is required: pip install PyJWT")
    private_key = key_path.read_text()
    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 1200,  # 20 minutes
        "aud": "appstoreconnect-v1",
    }
    return pyjwt.encode(payload, private_key, algorithm="ES256", headers={"kid": key_id})


def _asc_request(token: str, method: str, path: str, body: dict | None = None) -> dict:
    """Make an App Store Connect API request."""
    url = f"https://api.appstoreconnect.apple.com{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        if resp.status == 204:
            return {}
        return json.loads(resp.read())


def _find_internal_beta_group(token: str, app_bundle_id: str) -> str | None:
    """Find the first internal beta group for an app, return its ID."""
    # Find app by bundle ID
    resp = _asc_request(token, "GET", f"/v1/apps?filter[bundleId]={app_bundle_id}")
    apps = resp.get("data", [])
    if not apps:
        return None
    app_id = apps[0]["id"]

    # Find internal beta groups for this app
    resp = _asc_request(
        token, "GET",
        f"/v1/betaGroups?filter[app]={app_id}&filter[isInternalGroup]=true",
    )
    groups = resp.get("data", [])
    return groups[0]["id"] if groups else None


def _find_build(token: str, app_bundle_id: str, version: str, build_number: int) -> str | None:
    """Find a build by version and build number, return its ID."""
    resp = _asc_request(token, "GET", f"/v1/apps?filter[bundleId]={app_bundle_id}")
    apps = resp.get("data", [])
    if not apps:
        return None
    app_id = apps[0]["id"]

    resp = _asc_request(
        token, "GET",
        f"/v1/builds?filter[app]={app_id}&filter[version]={build_number}&filter[preReleaseVersion.version]={version}&sort=-uploadedDate&limit=1",
    )
    builds = resp.get("data", [])
    return builds[0]["id"] if builds else None


def _assign_build_to_group(token: str, group_id: str, build_id: str) -> None:
    """Add a build to a beta group."""
    _asc_request(
        token, "POST",
        f"/v1/betaGroups/{group_id}/relationships/builds",
        body={"data": [{"type": "builds", "id": build_id}]},
    )


def testflight(
    project: str,
    scheme: str,
    api_key_id: str | None = None,
    issuer_id: str | None = None,
    api_key_path: str | None = None,
    configuration: str = "Release",
    output_dir: str | Path | None = None,
    clean: bool = True,
    auto_assign_internal: bool = True,
) -> str:
    """Archive, auto-increment build number, upload to TestFlight, and assign to internal testers.

    Returns JSON with build details and output tails.
    """
    if not project.strip():
        raise ValueError("project is required")
    if not scheme.strip():
        raise ValueError("scheme is required")

    _ensure_cli("xcodebuild")
    resolved_api_key_id = _resolve_config(api_key_id, ENV_API_KEY_ID, DEFAULT_API_KEY_ID)
    resolved_issuer_id = _resolve_config(issuer_id, ENV_ISSUER_ID, DEFAULT_ISSUER_ID)
    resolved_api_key_path = _resolve_config(api_key_path, ENV_API_KEY_PATH, DEFAULT_API_KEY_PATH)

    key_path = Path(resolved_api_key_path).expanduser()
    if not key_path.exists():
        raise FileNotFoundError(f"API key file not found: {key_path}")

    # Read project info and increment build number
    pbx_content, current_build, marketing_version, team_id = _read_pbxproj(project)
    new_build = _increment_build_number(project, current_build)

    if output_dir is None:
        run_dir = Path.cwd() / ".build" / f"testflight-{time.strftime('%Y%m%d-%H%M%S')}"
    else:
        run_dir = Path(output_dir).expanduser()
    run_dir.mkdir(parents=True, exist_ok=True)

    archive_path = run_dir / "archive.xcarchive"
    export_path = run_dir / "export"
    export_options_path = run_dir / "ExportOptions.plist"
    _write_export_options(export_options_path, team_id)

    auth = _auth_flags(key_path, resolved_api_key_id, resolved_issuer_id)

    # Archive
    archive_cmd = ["xcodebuild"]
    if clean:
        archive_cmd.append("clean")
    archive_cmd.extend([
        "archive",
        "-project", project,
        "-scheme", scheme,
        "-configuration", configuration,
        "-destination", "generic/platform=iOS",
        "-archivePath", str(archive_path),
        "-allowProvisioningUpdates",
        *auth,
    ])
    archive_output = _run(archive_cmd)

    # Export + Upload
    export_cmd = [
        "xcodebuild", "-exportArchive",
        "-archivePath", str(archive_path),
        "-exportPath", str(export_path),
        "-exportOptionsPlist", str(export_options_path),
        "-allowProvisioningUpdates",
        *auth,
    ]
    export_output = _run(export_cmd)

    # Auto-assign to internal testers
    internal_group_assigned = False
    bundle_ids = re.findall(r'PRODUCT_BUNDLE_IDENTIFIER = "?([^";]+)"?;', pbx_content)
    bundle_id = bundle_ids[0] if bundle_ids else ""

    if auto_assign_internal and pyjwt and bundle_id:
        try:
            token = _generate_asc_token(resolved_api_key_id, resolved_issuer_id, key_path)

            # Wait for build to appear on App Store Connect (processing takes a moment)
            build_id = None
            for attempt in range(6):
                build_id = _find_build(token, bundle_id, marketing_version, new_build)
                if build_id:
                    break
                time.sleep(10)

            if build_id:
                group_id = _find_internal_beta_group(token, bundle_id)
                if group_id:
                    _assign_build_to_group(token, group_id, build_id)
                    internal_group_assigned = True
        except Exception as e:
            internal_group_assigned = False

    result = {
        "project": project,
        "scheme": scheme,
        "configuration": configuration,
        "version": marketing_version,
        "build": new_build,
        "archive_path": str(archive_path),
        "export_path": str(export_path),
        "api_key_id": resolved_api_key_id,
        "internal_group_assigned": internal_group_assigned,
        "archive_output_tail": _tail(archive_output),
        "export_output_tail": _tail(export_output),
    }

    return json.dumps(result, indent=2)
