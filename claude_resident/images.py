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


def youtube_annotations(text: str) -> Optional[str]:
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

