"""API client construction — direct Anthropic API (OAuth) or Bedrock.

The resident historically ran on AWS Bedrock via an aws profile. It now
defaults to the direct Anthropic API on its own subscription, authenticated
with a Claude Code OAuth token that tokeman manages in
~/.config/tokeman/tokens.toml. tokeman rotates tokens in place, so on a 401
the caller should refresh_oauth_token() and retry rather than dying.

Backend selection lives in config.API_BACKEND (oauth when the tokeman file
exists, else bedrock; force with CLAUDE_RESIDENT_BACKEND=bedrock|oauth).
"""

import logging
import tomllib

import anthropic

from claude_resident.config import (
    API_BACKEND,
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


def build_anthropic_client(http_client=None,
                           aws_profile: str = "commonquant-ember"):
    """Construct the API client for the active backend.

    OAuth tokens ride on Authorization: Bearer (the SDK's auth_token) and
    need the oauth beta header alongside the cache-TTL beta.
    """
    if API_BACKEND == "oauth":
        token = read_oauth_token()
        if not token:
            raise RuntimeError(
                f"OAuth backend selected but no token for {TOKEMAN_ACCOUNT} "
                f"in {TOKEMAN_TOKENS_PATH}. Set CLAUDE_RESIDENT_BACKEND="
                f"bedrock to fall back.")
        betas = ",".join([OAUTH_BETA, *ANTHROPIC_BETAS])
        logger.info(f"API backend: direct Anthropic (OAuth, {TOKEMAN_ACCOUNT})")
        return anthropic.Anthropic(
            auth_token=token,
            http_client=http_client,
            default_headers={"anthropic-beta": betas},
        )

    default_headers = {}
    if ANTHROPIC_BETAS:
        default_headers["anthropic-beta"] = ",".join(ANTHROPIC_BETAS)
    logger.info(f"API backend: AWS Bedrock (profile {aws_profile})")
    return anthropic.AnthropicBedrock(
        aws_profile=aws_profile,
        aws_region="us-east-1",
        http_client=http_client,
        default_headers=default_headers,
    )


def refresh_oauth_token(client) -> bool:
    """Re-read the tokeman file and update the client's bearer token.

    tokeman rotates tokens in place; a 401 usually means the file holds a
    fresher token than the one the client was built with. Returns True when
    the token actually changed (i.e. a retry is worth attempting).
    """
    if API_BACKEND != "oauth":
        return False
    token = read_oauth_token()
    if token and token != getattr(client, "auth_token", None):
        client.auth_token = token
        logger.info("OAuth token refreshed from tokeman")
        return True
    return False
