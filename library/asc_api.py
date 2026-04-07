"""
App Store Connect API helper with automatic JWT authentication.

Usage:
    from library import asc_api
    apps = asc_api("GET", "/v1/apps")
    asc_api("PATCH", f"/v1/ageRatingDeclarations/{id}", {"data": {"attributes": {...}}})
"""
import json
import os
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

_cached_token = ""
_token_expiry = 0


def _get_token() -> str:
    """Get a valid JWT token, generating a new one if expired."""
    global _cached_token, _token_expiry

    if _cached_token and time.time() < _token_expiry - 60:
        return _cached_token

    if pyjwt is None:
        raise ImportError("PyJWT is required: pip install PyJWT")

    key_id = os.getenv("XCODE_TESTFLIGHT_API_KEY_ID", DEFAULT_API_KEY_ID)
    issuer_id = os.getenv("XCODE_TESTFLIGHT_ISSUER_ID", DEFAULT_ISSUER_ID)
    key_path = os.getenv("XCODE_TESTFLIGHT_API_KEY_PATH", DEFAULT_API_KEY_PATH)

    private_key = Path(key_path).read_text()
    now = int(time.time())
    payload = {
        "iss": issuer_id,
        "iat": now,
        "exp": now + 1200,
        "aud": "appstoreconnect-v1",
    }
    _cached_token = pyjwt.encode(payload, private_key, algorithm="ES256", headers={"kid": key_id})
    _token_expiry = now + 1200
    return _cached_token


def asc_api(method: str, path: str, body: dict | None = None) -> dict:
    """Make an authenticated App Store Connect API call.

    asc_api("GET", "/v1/apps")
    asc_api("GET", "/v1/apps?filter[bundleId]=dp.My-App")
    asc_api("PATCH", "/v1/appStoreVersionLocalizations/123", {"data": {"type": "...", "attributes": {...}}})

    Args:
        method: HTTP method (GET, POST, PATCH, DELETE)
        path: API path starting with /v1/
        body: Request body dict (auto-serialized to JSON)

    Returns:
        Response JSON as dict (empty dict for 204 No Content)

    Raises:
        urllib.error.HTTPError: On API errors (includes response body in message)
    """
    token = _get_token()
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
    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status == 204:
                return {}
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else ""
        raise RuntimeError(f"ASC API {method} {path} → {e.code}: {error_body[:500]}") from None
