"""ZulipBackend — ChatBackend over python-zulip-api.

Deliberately thin. Every method here either forwards to `zulip.Client`
unchanged or wraps a helper that already existed, because this backend's
job is to change NOTHING about how the resident behaves on Tulip while
making room for a second platform beside it. If a diff in this file is
doing something interesting, it is probably wrong.

The two methods that are not pure forwarding, and why:

- `upload_file` routes through `util.zulip_upload_file` rather than the
  library's own `client.upload_file`, which sets the multipart field name
  to the file's full path instead of "file". Small files survive it; past a
  few MB the server returns HTTP 200 and stores zero bytes.
- `claim_url` / `fetch_authed` replace `fetch_url`'s previous habit of
  reaching into `zulip_client.session` and `.base_url` directly. Same
  behavior, but now it is a question the backend answers ("is this mine,
  and how do I read it?") rather than one the tool answers by knowing
  Zulip's internals.
"""

import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

from glm_resident.util import zulip_upload_file

logger = logging.getLogger("zulip_backend")

# Zulip's server-side message ceiling. The chunker asks the backend for
# this rather than hardcoding it — see backend.ChatBackend.max_message_chars.
ZULIP_MAX_MESSAGE_CHARS = 9_000


class ZulipBackend:
    name = "zulip"
    max_message_chars = ZULIP_MAX_MESSAGE_CHARS

    def __init__(self, client):
        self.client = client

    # --- identity / directory -----------------------------------------
    def get_profile(self) -> dict:
        return self.client.get_profile()

    def get_members(self) -> dict:
        return self.client.get_members()

    def get_streams(self) -> dict:
        return self.client.get_streams()

    # --- sending -------------------------------------------------------
    def send_message(self, payload: dict) -> dict:
        return self.client.send_message(payload)

    def add_reaction(self, payload: dict) -> dict:
        return self.client.add_reaction(payload)

    def set_typing_status(self, payload: dict) -> dict:
        return self.client.set_typing_status(payload)

    # --- reading -------------------------------------------------------
    def get_messages(self, request: dict) -> dict:
        return self.client.get_messages(request)

    # --- files / urls --------------------------------------------------
    def upload_file(self, filepath: Path) -> dict:
        return zulip_upload_file(Path(filepath), self.client)

    def claim_url(self, url: str) -> str | None:
        base = getattr(self.client, "base_url", "") or ""
        if url.startswith("/user_uploads") or url.startswith("/api/"):
            return urljoin(base, url) if base else None
        host = urlparse(base).netloc if base else ""
        if host and urlparse(url).netloc == host:
            return url
        return None

    def fetch_authed(self, url: str, timeout: int = 30):
        return self.client.session.get(url, timeout=timeout)

    # --- event plumbing (Zulip-only; zulip_loop owns this) -------------
    # Not part of ChatBackend: see backend.py on why the event loop is not
    # abstracted. Exposed so zulip_loop can hold the backend rather than
    # needing the raw client alongside it.
    def register(self, **kwargs) -> dict:
        return self.client.register(**kwargs)

    def get_events(self, **kwargs) -> dict:
        return self.client.get_events(**kwargs)
