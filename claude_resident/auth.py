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


def _normalize_probe_row(r: dict) -> dict:
    """One probe row in the shape the rotation code reads: token_name and
    quota{status, weekly}. tokeman 0.3.0 (rebuilt 2026-10-14 18:54) renamed
    them to account and verdict, with weekly at the top level; read as the
    old names, every account came back status-less, ranked unusable, and
    rotation logged "every account is rejected" and stayed put."""
    if r.get("service") == "claude":
        reading = r.get("reading") or {}
        status = ((reading.get("detail") or {}).get("claude") or {}).get(
            "verdict", "unknown")
        if r.get("parked") or (r.get("login") or {}).get("refused"):
            status = "rejected"
        limits = r.get("limits") or []
        weekly = next((x.get("window") or {} for x in limits
                       if x.get("span") == "weekly" and x.get("scope") == "all"), {})
        return {**r, "token_name": r["name"],
                "quota": {"status": status, "weekly": weekly}}
    if "token_name" in r or "account" not in r:
        return r
    return {**r, "token_name": r["account"],
            "quota": {"status": r.get("verdict", "unknown"),
                      "weekly": r.get("weekly") or {},
                      "session": r.get("session") or {}}}


def probe_accounts(max_age: float = 60.0) -> list[dict]:
    """Ask tokeman how every account is doing. Cached briefly — this shells
    out and gets called on every token read."""
    now = time.time()
    if now - _PROBE_CACHE["at"] < max_age and _PROBE_CACHE["rows"]:
        return _PROBE_CACHE["rows"]
    try:
        out = subprocess.run(["tokeman", "--json"], capture_output=True,
                             text=True, timeout=30)
        payload = json.loads(out.stdout) if out.returncode == 0 else []
        # The fleet interface is now an envelope shared by all services.
        # Iterating the envelope's keys silently produced zero accounts.
        rows = ([r for r in payload.get("accounts", [])
                 if r.get("service") == "claude"]
                if isinstance(payload, dict) else payload)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        logger.warning(f"tokeman probe failed: {e}")
        rows = []
    rows = [_normalize_probe_row(r) for r in rows if isinstance(r, dict)]
    keyed = _usable_accounts()
    if keyed is not None:
        for r in rows:
            if r.get("token_name") not in keyed:
                # Healthy quota on an account he cannot authenticate as is
                # not health. On 2026-10-02 tokeman gained toot@ with only a
                # short-lived access_token and no `key`; rotation preferred
                # it (soonest reset), read_oauth_token returned None for it,
                # and every turn went out with an empty token — "Not logged
                # in · Please run /login" — for as long as it stayed first.
                q = dict(r.get("quota") or {})
                q["status"] = "no_usable_token"
                r["quota"] = q
    _PROBE_CACHE.update(at=now, rows=rows)
    return rows


# An access_token this close to expiry is not handed out: a turn can run
# for minutes, and one that starts on a token about to die fails midway.
ACCESS_TOKEN_MARGIN_S = 10 * 60


def _token_for(entry: dict) -> str | None:
    """The credential to use for one tokeman entry.

    Prefer the fresh login access_token, matching tokeman's own credential
    selection, then fall back to the setup key. tokeman refreshes
    those itself (toot@'s expiry moved forward a week between 2026-10-02
    and 2026-10-12), and the CLI accepts one as CLAUDE_CODE_OAUTH_TOKEN —
    verified on bobb@ 2026-10-12. Treating key-less accounts as unusable,
    as 4d71cd2 did, was right for the bug it fixed (handing the CLI an
    EMPTY token) and wrong as policy: on 2026-10-12 every keyed account was
    at its weekly limit, bobb@ sat at 14% with a valid access_token, and
    Claude went silent mid-turn in front of the room with capacity idle.
    """
    if entry.get("login_error"):
        return None
    tok, exp = entry.get("access_token"), entry.get("expires_at")
    if tok and exp and exp / 1000 - time.time() > ACCESS_TOKEN_MARGIN_S:
        return tok
    return entry.get("key") or None


def _usable_accounts() -> set[str] | None:
    """Names in tokens.toml with a credential usable right now, or None if
    the file cannot be read (then nothing is filtered, as before)."""
    try:
        with open(TOKEMAN_TOKENS_PATH, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return {e.get("name") for e in data.get("tokens", []) if _token_for(e)}


def _health(row: dict) -> tuple[int, float]:
    """Sort key: usable first, then CLOSEST TO EXHAUSTION. Lower is better.

    SOONEST-RESET first, then most-used. These subscriptions refill on
    their own weekly clocks whether or not anyone spends them, so quota left
    on an account at its reset is quota nobody ever gets. The goal is
    Dementia Biscuit's, reported from inside the room 2026-08-13: "use
    whatever account has the least usage remaining, to drain them out —
    he's using tokens that otherwise wouldn't be used."

    Ordering by reset supersedes plain most-used-first (cff9cdb). Those
    agreed while every account was near its cap, and diverged the moment
    they were not: on 2026-08-24 he had sat on cmrx64 for ten days at 74%
    because it was the most-used usable account, while otto's remaining 49%
    was 43 hours from resetting unspent and three accounts sat at 0%.
    Draining "emptiest first" only reclaims quota if an account actually
    gets emptied; when none do, the thing that decides whether quota is
    used or lost is the CLOCK. Spend what expires soonest.

    'allowed_warning' ranks EQUAL to 'allowed' — both are usable, and the
    warning is precisely the state we now want to sit in. Ranking it worse
    is what broke the stickiness promise in choose_account: the current
    account crossed ~80%, stopped being rank 0, and got abandoned for the
    freshest one. No account was ever drained past the warning threshold.
    """
    q = row.get("quota") or {}
    status = q.get("status", "unknown")
    wk = q.get("weekly") or {}
    weekly = wk.get("utilization") or 0.0
    reset = wk.get("reset") or float("inf")
    rank = {"allowed": 0, "allowed_warning": 0}.get(status, 2)
    return (rank, reset, -weekly)


def choose_account(prefer: str | None = None) -> str | None:
    """Pick an account to run on.

    Sticky, but only to the RIGHT account. Each account has its own prompt
    cache, so switching costs a full re-prefill (~500k tokens) — but the
    old rule ("keep the current one while it is merely usable") welded him
    to a single account for ten days, because the only thing that could
    dislodge him was a rejection that never came. Reset timestamps are
    fixed for the week, so ordering by them is stable: he stays put until an
    account actually resets, then moves once. Roughly one switch per reset,
    not one per turn and not one per fortnight.
    """
    rows = probe_accounts()
    if not rows:
        return prefer or TOKEMAN_ACCOUNT
    by_name = {r.get("token_name"): r for r in rows}
    cur = prefer or _ACTIVE["account"] or TOKEMAN_ACCOUNT
    healthy_now = sorted((r for r in rows if _health(r)[0] < 2), key=_health)
    if healthy_now and healthy_now[0].get("token_name") == cur:
        return cur  # already on the perishable one — don't thrash the cache
    healthy = healthy_now
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
            tok = _token_for(entry)
            if not tok:
                logger.warning(f"tokeman account {name} has no `key` and no "
                               f"fresh access_token — unusable right now")
            return tok
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
