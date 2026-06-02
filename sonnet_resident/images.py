"""Image fetching and content block building for the Anthropic API."""

import re
import base64
import logging
from typing import Optional
from urllib.parse import urljoin

logger = logging.getLogger("images")


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

    clean_text = text
    for pattern in [upload_pattern, url_pattern]:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            url = match.group(1)
            image_block = fetch_image_as_block(url, zulip_client)
            if image_block:
                blocks.append(image_block)
                clean_text = clean_text.replace(match.group(0), "", 1)

    clean_text = clean_text.strip()
    if clean_text:
        blocks.append({"type": "text", "text": clean_text})
    elif not blocks:
        blocks.append({"type": "text", "text": text})
    return blocks


def fetch_image_as_block(url: str,
                         zulip_client=None) -> Optional[dict]:
    """Fetch an image and return it as an Anthropic API image content block."""
    try:
        if url.startswith("/user_uploads"):
            if zulip_client is None:
                return None
            full_url = urljoin(zulip_client.base_url, url)
            response = zulip_client.session.get(full_url, timeout=10)
        else:
            import requests as _requests
            response = _requests.get(url, timeout=10)

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

        encoded = base64.b64encode(response.content).decode("utf-8")

        if len(encoded) > 5_000_000:
            logger.warning(f"Image too large, skipping: {url}")
            return None

        logger.info(
            f"Fetched image: {url} ({media_type}, {len(response.content)} bytes)")

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
