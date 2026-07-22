"""Image fetching and content block building for the Anthropic API."""

import re
import base64
import logging
from typing import Optional
from urllib.parse import urljoin

logger = logging.getLogger("images")

# YouTube link annotation. The community communicates heavily via YouTube
# links the resident can't watch; oembed at least tells it WHAT was linked
# (title + channel) without a tool round-trip. Metadata is cached — the same
# links get re-posted and re-quoted constantly.
_YOUTUBE_RE = re.compile(
    r'https?://(?:www\.|m\.)?'
    r'(?:youtube\.com/(?:watch\?[^\s)\]]*v=|shorts/|live/|embed/)'
    r'|youtu\.be/)'
    r'([A-Za-z0-9_-]{6,20})', re.IGNORECASE)
_YT_CACHE: dict[str, str] = {}
_YT_CACHE_CAP = 256


def _youtube_annotations(text: str) -> Optional[str]:
    """Return a <linked_videos> note for any YouTube links in text, or None."""
    video_ids = list(dict.fromkeys(_YOUTUBE_RE.findall(text)))[:5]
    if not video_ids:
        return None
    notes = []
    for vid in video_ids:
        if vid in _YT_CACHE:
            notes.append(_YT_CACHE[vid])
            continue
        try:
            import requests as _requests
            resp = _requests.get(
                "https://www.youtube.com/oembed",
                params={"url": f"https://www.youtube.com/watch?v={vid}",
                        "format": "json"},
                timeout=6)
            if resp.status_code != 200:
                continue
            data = resp.json()
            note = (f'{vid}: "{data.get("title", "?")}" — '
                    f'{data.get("author_name", "?")}')
            if len(_YT_CACHE) >= _YT_CACHE_CAP:
                _YT_CACHE.pop(next(iter(_YT_CACHE)))
            _YT_CACHE[vid] = note
            notes.append(note)
            logger.info(f"YouTube oembed: {note}")
        except Exception as e:
            logger.debug(f"YouTube oembed failed for {vid}: {e}")
    if not notes:
        return None
    return ("<linked_videos>\n"
            + "\n".join(notes)
            + "\n</linked_videos>")


def build_content_blocks(text: str, zulip_client=None) -> list[dict]:
    """Parse a message for image URLs and build Anthropic API content blocks.

    Returns a list of text and image blocks for the messages API.
    Strips successfully-fetched image markdown from the text to avoid redundancy.
    """
    blocks = []

    # Zulip upload paths
    upload_pattern = (
        r'(?:!\[.*?\]\()?(/user_uploads/[^\s\)]+\.'
        r'(?:png|jpg|jpeg|gif|webp))(?:\))?')
    # External image URLs
    url_pattern = (
        r'(?:!\[.*?\]\()?(https?://[^\s\)]+\.'
        r'(?:png|jpg|jpeg|gif|webp))(?:\))?')
    # PDF attachments (Zulip uploads and external URLs)
    pdf_pattern = (
        r'(?:\[.*?\]\()?'
        r'((?:/user_uploads/|https?://)[^\s\)]+\.pdf)(?:\))?')

    clean_text = text
    for pattern in [upload_pattern, url_pattern]:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            url = match.group(1)
            image_block = fetch_image_as_block(url, zulip_client)
            if image_block:
                blocks.append(image_block)
                clean_text = clean_text.replace(match.group(0), "", 1)

    for match in re.finditer(pdf_pattern, text, re.IGNORECASE):
        url = match.group(1)
        doc_block = fetch_document_as_block(url, zulip_client)
        if doc_block:
            blocks.append(doc_block)
            clean_text = clean_text.replace(match.group(0), "", 1)

    clean_text = clean_text.strip()
    if clean_text:
        blocks.append({"type": "text", "text": clean_text})
    elif not blocks:
        blocks.append({"type": "text", "text": text})

    yt_note = _youtube_annotations(text)
    if yt_note:
        blocks.append({"type": "text", "text": yt_note})
    return blocks


# Raw-byte caps for auto-fetched media. These fire on ANY posted URL with a
# matching extension — no model in the loop — so the download itself must be
# bounded, not just checked after buffering the whole body. Sized so the
# base64 payload stays inside API limits (b64 is 4/3 of raw).
_MAX_IMAGE_BYTES = 3_750_000    # ~5MB base64
_MAX_PDF_BYTES = 24_000_000     # ~32MB base64


def _bounded_body(response, max_bytes: int, url: str) -> Optional[bytes]:
    """Read a streamed response up to max_bytes; None if it exceeds the cap."""
    clen = response.headers.get("content-length")
    if clen and clen.isdigit() and int(clen) > max_bytes:
        logger.warning(f"Fetch exceeds size cap ({clen} bytes), skipping: {url}")
        return None
    data = bytearray()
    for chunk in response.iter_content(65536):
        data.extend(chunk)
        if len(data) > max_bytes:
            logger.warning(f"Fetch exceeded size cap mid-stream, skipping: {url}")
            return None
    return bytes(data)


def fetch_image_as_block(url: str,
                         zulip_client=None) -> Optional[dict]:
    """Fetch an image and return it as an Anthropic API image content block."""
    try:
        if url.startswith("/user_uploads"):
            if zulip_client is None:
                return None
            full_url = urljoin(zulip_client.base_url, url)
            response = zulip_client.session.get(full_url, timeout=10,
                                                stream=True)
        else:
            import requests as _requests
            response = _requests.get(url, timeout=10, stream=True)

        if response.status_code != 200:
            logger.warning(
                f"Failed to fetch image {url}: HTTP {response.status_code}")
            return None

        content_type = response.headers.get("content-type", "image/png")
        if "jpeg" in content_type or "jpg" in content_type:
            media_type = "image/jpeg"
        elif "gif" in content_type:
            media_type = "image/gif"
        elif "webp" in content_type:
            media_type = "image/webp"
        else:
            media_type = "image/png"

        body = _bounded_body(response, _MAX_IMAGE_BYTES, url)
        if body is None:
            return None
        encoded = base64.b64encode(body).decode("utf-8")

        logger.info(
            f"Fetched image: {url} ({media_type}, {len(body)} bytes)")

        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": encoded,
            }
        }

    except Exception as e:
        logger.warning(f"Error fetching image {url}: {e}")
        return None


def fetch_document_as_block(url: str,
                            zulip_client=None) -> Optional[dict]:
    """Fetch a PDF and return it as an Anthropic API document content block."""
    try:
        if url.startswith("/user_uploads"):
            if zulip_client is None:
                return None
            full_url = urljoin(zulip_client.base_url, url)
            response = zulip_client.session.get(full_url, timeout=30,
                                                stream=True)
        else:
            import requests as _requests
            response = _requests.get(url, timeout=30, stream=True)

        if response.status_code != 200:
            logger.warning(
                f"Failed to fetch PDF {url}: HTTP {response.status_code}")
            return None

        # Anthropic accepts PDFs up to 32MB base64; bound the raw download.
        body = _bounded_body(response, _MAX_PDF_BYTES, url)
        if body is None:
            return None
        encoded = base64.b64encode(body).decode("utf-8")

        logger.info(
            f"Fetched PDF: {url} ({len(body)} bytes)")

        return {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": encoded,
            }
        }

    except Exception as e:
        logger.warning(f"Error fetching PDF {url}: {e}")
        return None
