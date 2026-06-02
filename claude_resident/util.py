"""Shared utility functions."""

import logging
import mimetypes
from pathlib import Path

import requests


logger = logging.getLogger("util")


def safe_filename(name: str) -> str:
    """Convert a display name to a safe filename."""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name).lower()


def zulip_upload_file(filepath: Path, zulip_client) -> dict:
    """Upload a file to Zulip using the proper multipart 'file' field name.

    python-zulip-api's client.upload_file() does `files=[(f.name, f)]`, which
    sets the multipart FIELD NAME to the file's full path instead of the
    expected "file". For small files the server tolerates this; for files
    bigger than a few MB the multipart parser fails silently — the server
    returns HTTP 200 but stores 0 bytes. This helper bypasses that bug.

    Returns the same dict shape upload_file does:
        {result, msg, uri, url, filename}
    """
    upload_url = zulip_client.base_url.rstrip('/') + '/v1/user_uploads'
    mime, _ = mimetypes.guess_type(filepath.name)
    if mime is None:
        mime = "application/octet-stream"
    with open(filepath, "rb") as f:
        resp = requests.post(
            upload_url,
            auth=(zulip_client.email, zulip_client.api_key),
            files={"file": (filepath.name, f, mime)},
            timeout=180,
        )
    try:
        body = resp.json()
    except ValueError:
        return {"result": "error",
                "msg": f"non-JSON response (HTTP {resp.status_code})"}
    if resp.status_code != 200:
        body.setdefault("result", "error")
    return body
