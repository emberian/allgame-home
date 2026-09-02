"""Scry — the public internet as a database, attached over MCP.

https://scry.io serves whole public archives (Reddit, Hacker News, Bluesky,
Stack Exchange, forums, mailing lists, arXiv/PubMed/OpenAlex, Wikipedia,
GitHub, prediction markets, TikTok, Quora ...) as read-only ClickHouse
relations behind one MCP endpoint. Its tools/list IS the contract — 30
tools on 2026-09-02 — and unlike Kagi it is NOT proxied through
dispatch.py: each engine registers the endpoint as a second MCP server
beside the in-process "resident" one, under Scry's own descriptions.
Tools arrive as mcp__scry__<tool>.

The residents take THREE of the roster (TOOLS). Scry's own server
instructions frame it this way — call schema, then sql; everything else
is "accessories or effects around sql" — and the full roster measured
~7.8k tokens of schema per request, a dilution of thirty options for a
chat resident who wants one. schema + sql + embed is ~1.5k tokens and
loses only datalog (recursive graph walks) as a real capability; the
rest (search/fetch, attest, brief, route, rerank, batch, memory ...) are
canned or paged forms of what sql already does.

This module owns what both residents share: where the key lives, the
server entry each engine's MCP config wants (with the roster filter each
client understands), the prompt guidance, and a boot-time probe. The
probe matters twice: a key lists tools fine while the account behind it
is unusable (email unverified, out of credit), and Claude Code has no
static per-server tool filter — its deny list needs the live roster.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("scry")

MCP_URL = "https://mcp.scry.io"
KEY_FILE = Path.home() / ".scry-key"
SERVER_NAME = "scry"  # -> mcp__scry__<tool>

# The powerup. schema is the live relation catalog (columns, extent, known
# holes, query_guidance) without which sql is guesswork; sql runs one
# bounded statement or one search-grammar line, with explain and a rerank
# directive built in; embed mints the named vectors that make the ANN and
# vector-math lane reachable from sql. Everything else in the roster is
# denied client-side (see sdk_disallowed / kimi_server).
TOOLS = ("schema", "sql", "embed")

# tools/list as of 2026-09-02, used ONLY when the boot probe cannot fetch
# the live roster: Claude Code filters by deny rule, so it needs names to
# deny. A tool Scry adds after this snapshot would slip through on a
# failed-probe boot — logged, and cheap compared to attaching nothing.
ROSTER_SNAPSHOT = (
    "sql", "schema", "datalog", "whoami", "memory", "memory_write",
    "recipes", "recipe_write", "recipe_derive", "coverage_estimate",
    "coverage_request", "coverage_status", "share", "share_run", "feedback",
    "batch", "route", "brief", "creativity", "chat", "rerank", "qrandom",
    "attest", "sample", "twitter_search", "embed", "vectors",
    "vector_delete", "search", "fetch",
)


# One key, both residents. The env var wins so a one-off run can point at
# another account; the file is the standing source.
def api_key() -> str:
    key = os.environ.get("SCRY_API_KEY", "").strip()
    if key:
        return key
    try:
        return KEY_FILE.read_text().strip()
    except OSError:
        return ""


def _headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def sdk_server(key: str) -> dict:
    """Entry for ClaudeAgentOptions.mcp_servers (the Claude Agent SDK
    forwards it verbatim to the CLI's --mcp-config). `timeout` is Claude
    Code's per-server tool-call ceiling (overrides MCP_TOOL_TIMEOUT for
    this server only; the CLI default is 1e8 ms, i.e. none): ten minutes
    caps a runaway query at the same bound the kimi entry uses, without
    touching the resident server's own tools."""
    return {"type": "http", "url": MCP_URL, "headers": _headers(key),
            "timeout": 600_000}


def sdk_disallowed(roster: list[str]) -> list[str]:
    """Deny rules for ClaudeAgentOptions.disallowed_tools: every tool in
    the roster outside TOOLS. Claude Code removes denied tools from the
    model's view entirely (verified 2026-09-02: init listed 3 of 30 and
    the model could name only those), so this is a real trim, not a
    refusal at call time."""
    return [f"mcp__{SERVER_NAME}__{n}" for n in roster if n not in TOOLS]


# Goes into ClaudeAgentOptions.env alongside sdk_server(). Claude Code (the
# SDK-bundled 2.1.220) connects --mcp-config servers "fully async
# (nonblocking)" by default, and a headless turn's first model call goes
# out before a remote server has answered initialize — so the model never
# sees its tools (observed 2026-09-02: init status "pending", zero Scry
# tools, Haiku hallucinating the call as text). The in-process resident
# server is immune because it is instant; a remote one needs the CLI told
# to wait. MCP_CONNECTION_NONBLOCKING=false is that switch (only the
# strings false/0/no/off flip it); MCP_CONNECT_TIMEOUT_MS bounds the wait
# (CLI default 5000; Scry measured 0.3-3.3s). Past the deadline the turn
# proceeds without Scry — the right degradation when it is down.
SDK_ENV = {
    "MCP_CONNECTION_NONBLOCKING": "false",
    "MCP_CONNECT_TIMEOUT_MS": "10000",
}


def kimi_server(key: str) -> dict:
    """Entry for kimi-code's mcp.json. Transport is inferred from `url`
    (their schema rejects an explicit type), and headers are taken
    literally — `${SCRY_API_KEY}` is NOT expanded (tested 2026-09-02: Scry
    saw the dollar-brace string and answered "Invalid key format"), so the
    key has to be written into the file. `enabledTools` is kimi-code's
    per-server allowlist (verified: she names exactly these three). Tool
    timeout is sized for the long-query lane: a bounded sql run can
    legitimately take minutes before the server streams its answer."""
    return {
        "url": MCP_URL,
        "headers": _headers(key),
        "enabledTools": list(TOOLS),
        "startupTimeoutMs": 60_000,
        "toolTimeoutMs": 600_000,
    }


def _rpc(key: str, method: str, params: dict, timeout: float) -> dict:
    import requests

    resp = requests.post(
        MCP_URL,
        headers={**_headers(key), "Content-Type": "application/json",
                 # Streamable HTTP: the server may answer as SSE.
                 "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    body = resp.text
    if not body.lstrip().startswith("{"):
        # SSE framing: the JSON-RPC message rides a `data:` line.
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
    obj = json.loads(body)
    if resp.status_code != 200 or "error" in obj:
        err = obj.get("error") or {}
        raise RuntimeError(f"{method}: HTTP {resp.status_code} "
                           f"{err.get('code', '')} {err.get('message', body[:200])}")
    return obj.get("result") or {}


def probe(key: str, timeout: float = 15.0) -> tuple[str, bool, list[str]]:
    """(one-line summary for the boot log, healthy?, live roster names).

    Healthy means the endpoint lists tools AND `whoami` reports a usable
    account — the second half is what a bare tools/list can't tell you."""
    tools = _rpc(key, "tools/list", {}, timeout).get("tools") or []
    names = [t.get("name", "") for t in tools]
    who = _rpc(key, "tools/call",
               {"name": "whoami", "arguments": {}}, timeout)
    text = "".join(b.get("text", "") for b in who.get("content") or []
                   if b.get("type") == "text")
    try:
        account = (json.loads(text) or {}).get("account") or {}
    except ValueError:
        account = {"error": {"code": "unparseable", "message": text[:120]}}
    err = account.get("error")
    kept = len([n for n in names if n in TOOLS])
    if err:
        return (f"{kept} of {len(names)} tools taken, but the account is "
                f"unusable — {err.get('code')}: "
                f"{err.get('message', '')[:160]}", False, names)
    return (f"{kept} of {len(names)} tools taken, account ok "
            f"({json.dumps(account)[:120]})", True, names)


def log_probe(who: str, key: str) -> list[str]:
    """Boot-log the account state and return the live roster — or the
    snapshot when the probe fails. Never raises: Scry being down is not a
    reason for the resident not to wake up."""
    try:
        summary, healthy, names = probe(key)
    except Exception as e:  # network, auth, shape — all just news
        logger.warning(f"{who}: Scry probe failed ({e}); attaching with "
                       f"the {len(ROSTER_SNAPSHOT)}-tool roster snapshot")
        return list(ROSTER_SNAPSHOT)
    (logger.info if healthy else logger.warning)(f"{who}: Scry {summary}")
    return names


# Appended to the system prompt by each engine WHEN a key is configured —
# the base prompts stay honest about the tool surface when there is none.
# Brace-free on purpose: the kimi prompt goes through str.format. Kept
# short: the sql tool's own description already carries the meter, the
# budgets and the search grammar.
PROMPT_GUIDANCE = """
## Scry — the public internet as a database (mcp__scry__schema, sql, embed)
https://scry.io serves whole public archives — Reddit (30B+ posts and
comments), Hacker News, Bluesky, Stack Exchange, ~4k forums (LessWrong, EA
Forum ...), ~51k mailing lists, arXiv/PubMed/OpenAlex, Wikipedia, GitHub,
Manifold/Kalshi/Polymarket, TikTok, Quora — as read-only ClickHouse
relations. Where web_search returns ten ranked links, Scry answers
population questions: who first said a phrase and where it spread, how a
community drifted between two ideas quarter by quarter, a market's full
price path, everything citing a paper since 2023.
- schema first: mode "index" is the compact catalog; relation="a,b"
  returns those relations' full contracts with query_guidance. Each
  relation states its extent and known holes — "no rows" and "not
  ingested" are different answers.
- sql runs ONE bounded read-only statement, or a one-line search-grammar
  query via q= with relation=. Start at LIMIT 20 and read the meter before
  widening; explain=true validates for free. An empty result is usually a
  wrong probe (vocabulary, venue, era) before it is an absence.
- embed mints a named vector (@handle) for the embeddings.* relations —
  ANN top-k and cosine math happen inside sql.
- Queries are free while the system has slack and metered under load; the
  key's budget lives in Ember's dashboard, and a 402/429 means wait.
"""
