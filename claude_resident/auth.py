"""API client construction — the resident's own subscription, only.

Bedrock support was removed 2026-07-23 (the AWS key was revoked). All
API access now authenticates with the resident's Claude Code OAuth token,
which tokeman keeps fresh in ~/.config/tokeman/tokens.toml. tokeman
rotates tokens in place, so on a 401 the caller should
refresh_oauth_token() and retry rather than dying.

Who uses what:
- Main conversation turns: the Agent SDK engine (plan limits, flat rate).
- The engagement judge and session summaries: this module's raw-API
  client on Haiku (the one tier the raw Messages API serves for
  subscription OAuth tokens without an extra-usage balance).
- invoke_model (blind GM sub-calls): routed through the Agent SDK in
  lm_tools so Sonnet/Opus nested calls also draw plan limits.
"""

import logging
import tomllib

import anthropic

from claude_resident.config import (
    ANTHROPIC_BETAS,
    OAUTH_BETA,
    TOKEMAN_TOKENS_PATH,
    TOKEMAN_ACCOUNT,
)

logger = logging.getLogger("auth")


def read_oauth_token() -> str | None:
    """Read the resident's OAuth token from the tokeman file."""
    try:
        with open(TOKEMAN_TOKENS_PATH, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(f"Could not read tokeman tokens: {e}")
        return None
    for entry in data.get("tokens", []):
        if entry.get("name") == TOKEMAN_ACCOUNT:
            return entry.get("key") or None
    logger.warning(f"No tokeman token named {TOKEMAN_ACCOUNT}")
    return None


def build_anthropic_client(http_client=None):
    """Construct the raw-API client on the resident's subscription.

    OAuth tokens ride on Authorization: Bearer (the SDK's auth_token) and
    need the oauth beta header alongside the cache-TTL beta. NOTE: for
    subscription tokens the raw Messages API serves Haiku only (Sonnet and
    Opus 429 without an extra-usage balance) — which covers this client's
    consumers (judge, session summaries).
    """
    token = read_oauth_token()
    if not token:
        raise RuntimeError(
            f"No OAuth token for {TOKEMAN_ACCOUNT} in "
            f"{TOKEMAN_TOKENS_PATH} — the resident cannot start without it.")
    betas = ",".join([OAUTH_BETA, *ANTHROPIC_BETAS])
    logger.info(f"API client: direct Anthropic (OAuth, {TOKEMAN_ACCOUNT})")
    return anthropic.Anthropic(
        auth_token=token,
        http_client=http_client,
        default_headers={"anthropic-beta": betas},
    )


def refresh_oauth_token(client) -> bool:
    """Re-read the tokeman file and update the client's bearer token.

    tokeman rotates tokens in place; a 401 usually means the file holds a
    fresher token than the one the client was built with. Returns True when
    the token actually changed (i.e. a retry is worth attempting).
    """
    token = read_oauth_token()
    if token and token != getattr(client, "auth_token", None):
        client.auth_token = token
        logger.info("OAuth token refreshed from tokeman")
        return True
    return False
