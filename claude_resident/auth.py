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

import json
import time
import logging
import tomllib
import subprocess

import anthropic

from claude_resident.config import (
    ANTHROPIC_BETAS,
    OAUTH_BETA,
    TOKEMAN_TOKENS_PATH,
    TOKEMAN_ACCOUNT,
)

logger = logging.getLogger("auth")


_PROBE_CACHE: dict = {"at": 0.0, "rows": []}
_ACTIVE: dict = {"account": None}


def probe_accounts(max_age: float = 60.0) -> list[dict]:
    """Ask tokeman how every account is doing. Cached briefly — this shells
    out and gets called on every token read."""
    now = time.time()
    if now - _PROBE_CACHE["at"] < max_age and _PROBE_CACHE["rows"]:
        return _PROBE_CACHE["rows"]
    try:
        out = subprocess.run(["tokeman", "--json"], capture_output=True,
                             text=True, timeout=30)
        rows = json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        logger.warning(f"tokeman probe failed: {e}")
        rows = []
    _PROBE_CACHE.update(at=now, rows=rows)
    return rows


def _health(row: dict) -> tuple[int, float]:
    """Sort key: usable first, then CLOSEST TO EXHAUSTION. Lower is better.

    Most-used-first, not least-used-first. These subscriptions refill on
    their own weekly clocks whether or not anyone spends them, so quota left
    on an account at reset time is quota nobody ever gets. Draining the
    fullest account first spreads everyone to ~80% and lets every remainder
    expire; draining the emptiest finishes accounts one at a time and wastes
    nothing. (Reported from inside the room by Dementia Biscuit, 2026-08-13:
    "he's supposed to just use whatever account has the least usage
    remaining, to drain them out.")

    'allowed_warning' ranks EQUAL to 'allowed' — both are usable, and the
    warning is precisely the state we now want to sit in. Ranking it worse
    is what broke the stickiness promise in choose_account: the current
    account crossed ~80%, stopped being rank 0, and got abandoned for the
    freshest one. No account was ever drained past the warning threshold.
    """
    q = row.get("quota") or {}
    status = q.get("status", "unknown")
    weekly = ((q.get("weekly") or {}).get("utilization") or 0.0)
    rank = {"allowed": 0, "allowed_warning": 0}.get(status, 2)
    return (rank, -weekly)


def choose_account(prefer: str | None = None) -> str | None:
    """Pick an account to run on.

    STICKY on purpose: each account has its own prompt cache, so switching
    costs a full re-prefill of whatever room is talking (~500k tokens).
    We keep the current account while it is merely busy, and move only when
    it is actually rejected. Stickiness now actually reaches that far —
    'allowed_warning' is rank 0, so a busy account is kept instead of
    swapped out at ~80%. One switch per account per week, not one per
    threshold crossing.
    """
    rows = probe_accounts()
    if not rows:
        return prefer or TOKEMAN_ACCOUNT
    by_name = {r.get("token_name"): r for r in rows}
    cur = prefer or _ACTIVE["account"] or TOKEMAN_ACCOUNT
    if cur in by_name and _health(by_name[cur])[0] == 0:
        return cur  # still healthy — don't thrash the cache
    healthy = sorted((r for r in rows if _health(r)[0] < 2), key=_health)
    if not healthy:
        logger.error("tokeman: every account is rejected")
        return cur
    pick = healthy[0].get("token_name")
    if pick != cur:
        q = healthy[0].get("quota") or {}
        wk = ((q.get("weekly") or {}).get("utilization") or 0) * 100
        logger.warning(f"Rotating account: {cur} → {pick} "
                       f"(weekly {wk:.0f}% used)")
    return pick


def rotate_account() -> str | None:
    """Force a re-probe and move off the current account."""
    _PROBE_CACHE["at"] = 0.0
    rows = probe_accounts()
    cur = _ACTIVE["account"] or TOKEMAN_ACCOUNT
    healthy = sorted((r for r in rows
                      if _health(r)[0] < 2 and r.get("token_name") != cur),
                     key=_health)
    if not healthy:
        logger.error(f"No account available to rotate to (current {cur})")
        return None
    _ACTIVE["account"] = healthy[0].get("token_name")
    logger.warning(f"Rotated off {cur} → {_ACTIVE['account']}")
    return _ACTIVE["account"]


def read_oauth_token(account: str | None = None) -> str | None:
    """The OAuth token for the healthiest available account."""
    try:
        with open(TOKEMAN_TOKENS_PATH, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning(f"Could not read tokeman tokens: {e}")
        return None
    name = account or _ACTIVE["account"] or choose_account()
    _ACTIVE["account"] = name
    for entry in data.get("tokens", []):
        if entry.get("name") == name:
            return entry.get("key") or None
    logger.warning(f"No tokeman token named {name}")
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
    # TOKEMAN_ACCOUNT is only the starting PREFERENCE; read_oauth_token
    # resolves the account actually in use and records it in _ACTIVE.
    # Reporting the constant meant the boot line read "boto@ember.software"
    # on the very boot that had just rotated away from it — a log that lies
    # about which account is live costs an hour the next time this breaks.
    account = _ACTIVE["account"] or TOKEMAN_ACCOUNT
    if not token:
        raise RuntimeError(
            f"No OAuth token for {account} in "
            f"{TOKEMAN_TOKENS_PATH} — the resident cannot start without it.")
    betas = ",".join([OAUTH_BETA, *ANTHROPIC_BETAS])
    logger.info(f"API client: direct Anthropic (OAuth, {account})")
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
