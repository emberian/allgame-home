"""Scry — the public internet as a database, attached over MCP.

https://scry.io serves whole public archives (Reddit, Hacker News, Bluesky,
Stack Exchange, forums, mailing lists, arXiv/PubMed/OpenAlex, Wikipedia,
GitHub, prediction markets, TikTok, Quora ...) as read-only ClickHouse
relations behind one MCP endpoint. Its tools/list IS the contract — the
roster (30 tools on 2026-09-02: sql, schema, search, fetch, brief, embed,
rerank, datalog, attest, memory, batch, route, chat ...) grows without a
changelog. So unlike Kagi, Scry is NOT proxied through dispatch.py: each
engine registers the endpoint as a second MCP server beside the in-process
"resident" one and takes whatever tools/list returns, under Scry's own
agent-tuned descriptions. Tools arrive as mcp__scry__<tool>.

This module owns what both residents share: where the key lives, the server
entry each engine's MCP config wants, the prompt guidance, and a boot-time
probe. The probe matters because a key lists tools fine while the account
behind it is unusable (email unverified, out of credit) — without it a boot
looks healthy and then hands the resident thirty tools that all fail.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("scry")

MCP_URL = "https://mcp.scry.io"
KEY_FILE = Path.home() / ".scry-key"
SERVER_NAME = "scry"  # -> mcp__scry__<tool>

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
    key has to be written into the file. Tool timeout is sized for the
    long-query lane: a bounded sql/datalog run can legitimately take
    minutes before the server streams its answer."""
    return {
        "url": MCP_URL,
        "headers": _headers(key),
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


def probe(key: str, timeout: float = 15.0) -> tuple[str, bool]:
    """(one-line summary for the boot log, healthy?).

    Healthy means the endpoint lists tools AND `whoami` reports a usable
    account — the second half is what a bare tools/list can't tell you."""
    tools = _rpc(key, "tools/list", {}, timeout).get("tools") or []
    who = _rpc(key, "tools/call",
               {"name": "whoami", "arguments": {}}, timeout)
    text = "".join(b.get("text", "") for b in who.get("content") or []
                   if b.get("type") == "text")
    try:
        account = (json.loads(text) or {}).get("account") or {}
    except ValueError:
        account = {"error": {"code": "unparseable", "message": text[:120]}}
    err = account.get("error")
    if err:
        return (f"{len(tools)} tools listed, but the account is unusable — "
                f"{err.get('code')}: {err.get('message', '')[:160]}", False)
    return (f"{len(tools)} tools, account ok "
            f"({json.dumps(account)[:120]})", True)


def log_probe(who: str, key: str) -> None:
    """Boot-log the account state; never raise — Scry being down is not a
    reason for the resident not to wake up."""
    try:
        summary, healthy = probe(key)
    except Exception as e:  # network, auth, shape — all just news
        logger.warning(f"{who}: Scry probe failed ({e}); attaching anyway")
        return
    (logger.info if healthy else logger.warning)(f"{who}: Scry {summary}")


# Appended to the system prompt by each engine WHEN a key is configured —
# the base prompts stay honest about the tool surface when there is none.
# Brace-free on purpose: the kimi prompt goes through str.format.
PROMPT_GUIDANCE = """
## Scry — the public internet as a database (tools named mcp__scry__*)
https://scry.io serves whole public archives — Reddit (30B+ posts and
comments), Hacker News, Bluesky, Stack Exchange, ~4k forums (LessWrong, EA
Forum ...), ~51k mailing lists, arXiv/PubMed/OpenAlex, Wikipedia, GitHub,
Manifold/Kalshi/Polymarket, TikTok, Quora — as read-only ClickHouse
relations. Where web_search returns ten ranked links, Scry answers
population questions: who first said a phrase and where it spread, how a
community drifted between two ideas quarter by quarter, a market's full
price path, everything citing a paper since 2023.
- schema first when unsure of the surface: mode "index" is the compact
  catalog; relation="a,b" returns full contracts with query_guidance. Each
  relation states its extent and known holes — "no rows" and "not
  ingested" are different answers.
- sql runs ONE bounded read-only statement. Start at LIMIT 20, read the
  meter (spend_nanodollars) and extrapolate before widening; explain=true
  validates and costs nothing; max_seconds and budget_nanodollars make the
  worst case a choice. Or pass q= for a one-line search-grammar query
  ("phrase", /regex/, OR, -not, author:, source:, after:, before:).
- search then fetch: one page of recent text, newest first, then hydrate a
  record_ref. brief: dated, sourced passages from the last ~45 days of web
  captures.
- embed mints a named vector (@handle) for ANN and vector math in sql;
  rerank re-orders documents you already hold; attest checks a quote's
  verbatim provenance; datalog walks graphs (citations, reply trees);
  route plans catalog-grounded SQL for a question; batch bundles up to 16
  calls into one round trip.
- memory / memory_write is ONE account document shared with your sibling
  resident: read before write, pass if_version, and keep your own standing
  notes under slug=<your name, lowercase> rather than "main".
- Cost: queries are free while the system has slack and metered under
  load; chat (OpenRouter models), creativity, and coverage_request spend
  real credit — use them on purpose, not reflexively. The key's budget
  lives in Ember's dashboard; a 402/429 means wait, not retry harder.
"""
