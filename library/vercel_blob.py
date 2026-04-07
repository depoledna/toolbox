"""
Vercel Blob library helpers.

Import in repl:
from library.vercel_blob import blob_list, blob_put, blob_get, blob_delete, blob_head
"""
import json
import os
import tempfile
from pathlib import Path

import requests
import vercel_blob


def _load_token(env_path: str) -> str:
    """Load BLOB_READ_WRITE_TOKEN from a .env file and set it in os.environ."""
    path = Path(env_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f".env file not found: {path}")

    token = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "BLOB_READ_WRITE_TOKEN":
            token = value.strip().strip("\"'")
            break

    if not token:
        raise ValueError(f"BLOB_READ_WRITE_TOKEN not found in {path}")

    os.environ["BLOB_READ_WRITE_TOKEN"] = token
    return token


def blob_list(env_path: str, prefix: str = "", limit: int = 100) -> str:
    """List files in Vercel Blob storage."""
    _load_token(env_path)

    options = {"limit": limit}
    if prefix:
        options["prefix"] = prefix

    result = vercel_blob.list(options=options, timeout=30)
    blobs = result.get("blobs", [])

    return json.dumps(
        [
            {
                "pathname": b.get("pathname"),
                "size": b.get("size"),
                "uploadedAt": b.get("uploadedAt"),
                "url": b.get("url"),
            }
            for b in blobs
        ],
        indent=2,
    )


def blob_put(env_path: str, pathname: str, content: str = "", file_path: str = "") -> str:
    """Upload content or a local file to Vercel Blob storage."""
    _load_token(env_path)

    if file_path:
        data = Path(file_path).expanduser().read_bytes()
    elif content:
        data = content.encode()
    else:
        raise ValueError("Provide either content or file_path")

    result = vercel_blob.put(
        pathname,
        data,
        options={"access": "public", "addRandomSuffix": "false", "allowOverwrite": "true"},
        timeout=60,
    )

    return json.dumps(
        {
            "url": result.get("url"),
            "pathname": result.get("pathname"),
            "downloadUrl": result.get("downloadUrl"),
            "contentType": result.get("contentType"),
        },
        indent=2,
    )


def blob_get(env_path: str, url: str) -> str:
    """Download and return the content of a blob."""
    _load_token(env_path)

    meta = vercel_blob.head(url, timeout=30)
    content_type = meta.get("contentType", "")

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    if content_type.startswith("text/") or content_type in (
        "application/json",
        "application/xml",
        "application/javascript",
    ):
        return resp.text

    suffix = Path(meta.get("pathname", "file")).suffix or ".bin"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(resp.content)
    tmp.close()
    return f"Binary file saved to: {tmp.name} ({len(resp.content)} bytes, {content_type})"


def blob_delete(env_path: str, urls: str) -> str:
    """Delete one or more blobs by URL."""
    _load_token(env_path)

    url_list = [u.strip() for u in urls.split(",") if u.strip()]
    if not url_list:
        raise ValueError("No URLs provided")

    if len(url_list) == 1:
        vercel_blob.delete(url_list[0], timeout=30)
    else:
        vercel_blob.delete(url_list, timeout=30)

    return json.dumps({"deleted": url_list, "count": len(url_list)}, indent=2)


def blob_head(env_path: str, url: str) -> str:
    """Get metadata for a blob without downloading it."""
    _load_token(env_path)

    result = vercel_blob.head(url, timeout=30)

    return json.dumps(
        {
            "pathname": result.get("pathname"),
            "contentType": result.get("contentType"),
            "size": result.get("size"),
            "uploadedAt": result.get("uploadedAt"),
            "url": result.get("url"),
            "downloadUrl": result.get("downloadUrl"),
            "cacheControl": result.get("cacheControl"),
        },
        indent=2,
    )
