from datetime import datetime, timezone
"""Shared utility functions."""

import logging
import mimetypes
from pathlib import Path

import requests


logger = logging.getLogger("util")


_REFUSAL_MARKERS = ("can't help with this", "cannot help with this",
                    "legal/aup")


def is_policy_refusal(text) -> bool:
    """A provider-side policy refusal, as opposed to Claude declining.

    Distinct from a limit: rotating accounts cannot fix it, because every
    account sees the same context. Lives here rather than in the engine
    because the sidecars need it too — the distiller has been eating these
    silently, retrying one refused chunk every ten minutes for weeks.
    """
    t = str(text).lower()
    return any(m in t for m in _REFUSAL_MARKERS)


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


# --- Time rendering -----------------------------------------------------
# The resident had NO clock: turn prompts carried no timestamp and the
# system prompt no date, while channel history was stamped in raw UTC ISO
# and the humans around him speak in local time. Everything below renders
# ONE consistent frame — the household's local zone — so "an hour ago" and
# "last Tuesday" are distinguishable to him (2026-07-29).

def _as_local(ts) -> datetime | None:
    """Parse an epoch or ISO-8601 stamp into local time. Naive stamps are
    treated as UTC, which is what every writer in this harness emits."""
    if ts is None or ts == "":
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts).astimezone()
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except (ValueError, OSError, OverflowError):
        return None


def fmt_log_time(ts) -> str:
    """Compact stamp for history lines: 'Jul 29 2:23 PM'."""
    dt = _as_local(ts)
    if dt is None:
        return str(ts)[:16]
    return dt.strftime("%b %-d %-I:%M %p")


def fmt_now(ts=None) -> str:
    """Full stamp for the turn header: 'Tue Jul 29, 2:23 PM EDT'."""
    dt = _as_local(ts) if ts is not None else datetime.now().astimezone()
    if dt is None:
        dt = datetime.now().astimezone()
    return dt.strftime("%a %b %-d, %-I:%M %p %Z")


def human_gap(seconds: float) -> str:
    """'just now' / '14m' / '3h 20m' / '2 days' / '3 weeks'."""
    s = max(0, int(seconds))
    if s < 90:
        return "just now"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    if d < 14:
        return f"{d} day{'s' if d > 1 else ''}" + (f" {h}h" if h else "")
    return f"{d // 7} weeks"
