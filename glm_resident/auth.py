"""API client construction — Kestrel runs on a Z.AI API key.

Forked from claude_resident's auth.py on 2026-09-21. That module's whole
apparatus — tokeman, account probing, health ranking, drain-the-soonest-
reset rotation — exists because Claude runs on *subscriptions*: several
accounts, each with its own weekly plan window, where the scheduling
problem is "which quota is about to expire unspent". None of it applies
here. Z.AI bills per token against one key, so there is no quota to drain,
no account to rotate to, and no reset clock to race. One key, read from
~/.zai-key.

Z.AI serves a first-class Anthropic-compatible Messages API at
ZAI_BASE_URL, which is what makes this fork cheap: the Agent SDK (Claude
Code as a library) and the raw `anthropic` SDK both speak to it unchanged,
so the engine, the judge, the distiller and the tool loop are all
inherited verbatim. The only thing that had to change is this file and the
env block it hands to every spawned turn.

Who uses what:
- Main conversation turns: the Agent SDK engine (agent_engine), via sdk_env().
- The engagement judge, session summaries, the distiller: sdk_call, also
  via sdk_env(). Unlike Claude's subscription — where the raw Messages API
  serves only Haiku — every model here is reachable by either door, so the
  split is about tooling, not entitlement.
- build_anthropic_client: the raw-API client carried in ToolContext.
"""

import logging
import threading

import anthropic

from glm_resident.config import (
    ANTHROPIC_BETAS,
    ZAI_BASE_URL,
    ZAI_KEY_FILE,
    DEFAULT_MODEL,
)

logger = logging.getLogger("auth")

_LOCK = threading.Lock()
_CACHE: dict = {"key": None}


def read_api_key(force: bool = False) -> str | None:
    """The Z.AI key from ~/.zai-key.

    Cached after the first read — this is called on every turn and on every
    sidecar completion. `force=True` re-reads from disk, which is the retry
    path when a 401 suggests the file now holds a fresher key than the one
    the process started with.
    """
    with _LOCK:
        if _CACHE["key"] and not force:
            return _CACHE["key"]
        try:
            key = ZAI_KEY_FILE.read_text().strip()
        except OSError as e:
            logger.warning(f"Could not read {ZAI_KEY_FILE}: {e}")
            return _CACHE["key"]
        if not key:
            logger.warning(f"{ZAI_KEY_FILE} is empty")
            return _CACHE["key"]
        if force and _CACHE["key"] and key != _CACHE["key"]:
            logger.info(f"Z.AI key changed on disk — reloaded from {ZAI_KEY_FILE}")
        _CACHE["key"] = key
        return key


def sdk_env(model: str | None = None) -> dict:
    """The environment every Agent SDK spawn needs to reach Z.AI.

    Single source for the redirect, because getting it half-right fails
    in a way that looks like success: miss the base URL and the CLI
    happily talks to Anthropic instead, on whatever ambient credential it
    can find, and the room gets answered by the wrong model entirely.

    Every credential the CLI might prefer over ours is explicitly blanked.
    The parent process loads .env (which carries a real ANTHROPIC_API_KEY
    for other consumers) and the SDK subprocess inherits the parent env,
    so a key left in place SHADOWS ours — the same shadowing bug
    claude_resident hit from the other direction on 2026-07-22. The keys
    stay in os.environ for the tools that legitimately need them.
    """
    key = read_api_key() or ""
    chosen = model or DEFAULT_MODEL
    return {
        "ANTHROPIC_BASE_URL": ZAI_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": key,
        # Both model vars: the CLI picks the "small/fast" one for its own
        # incidental calls, and an unset one there falls back to a Claude
        # id that Z.AI does not serve.
        "ANTHROPIC_MODEL": chosen,
        "ANTHROPIC_SMALL_FAST_MODEL": chosen,
        # Credentials that would outrank ANTHROPIC_AUTH_TOKEN.
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_OAUTH_TOKEN": "",
    }


def build_anthropic_client(http_client=None):
    """The raw-API client, pointed at Z.AI's Anthropic-compatible endpoint.

    Z.AI authenticates on x-api-key, which is the `api_key` argument (not
    `auth_token`, the Bearer form Claude's OAuth tokens need).
    """
    key = read_api_key()
    if not key:
        raise RuntimeError(
            f"No Z.AI key in {ZAI_KEY_FILE} — Kestrel cannot start without it.")
    logger.info(f"API client: Z.AI ({ZAI_BASE_URL}), model={DEFAULT_MODEL}")
    return anthropic.Anthropic(
        api_key=key,
        base_url=ZAI_BASE_URL,
        http_client=http_client,
        default_headers={"anthropic-beta": ",".join(ANTHROPIC_BETAS)},
    )


def refresh_api_key(client) -> bool:
    """Re-read the key file and update the client. True if it changed.

    Kept as the 401 retry hook: the key is long-lived, but if Ember rotates
    it under a running resident, the difference between this returning True
    and the resident dying is one restart nobody has to notice.
    """
    key = read_api_key(force=True)
    if key and key != getattr(client, "api_key", None):
        client.api_key = key
        logger.info("Z.AI key refreshed from disk")
        return True
    return False
