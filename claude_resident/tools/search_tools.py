"""Message search tools: local logs, Zulip API, and the thinking archive."""

import re
import json
import logging
from datetime import datetime, timezone

from claude_resident.util import safe_filename

logger = logging.getLogger("tools.search")

# search_archive bounds — the foreverlog is 10MB+ and append-only; a linear
# regex scan is fine (~12k lines), but output must stay bounded.
ARCHIVE_FILE = "api_archive.jsonl"
ARCHIVE_MAX_RESULTS = 25
ARCHIVE_SNIPPET_CHARS = 220
ARCHIVE_MAX_OUTPUT = 25_000


def search_archive(inp: dict, state) -> dict:
    """Regex search over the resident's api_archive.jsonl foreverlog.

    This is the ONLY record of past thinking blocks — the interiority that
    never reaches Zulip — plus everything said and every tool call made.
    grep_state can't reach it (file-size cap), so this tool is the
    resident's one window into its own history of mind.
    """
    pattern = inp.get("pattern", "")
    if not pattern:
        return {"content": "pattern is required", "is_error": True}
    try:
        regex = re.compile(pattern, re.IGNORECASE if inp.get(
            "case_insensitive", True) else 0)
    except re.error as e:
        return {"content": f"Invalid regex: {e}", "is_error": True}

    stream_filter = (inp.get("stream") or "").lower()
    topic_filter = (inp.get("topic") or "").lower()
    since = inp.get("since") or ""
    until = inp.get("until") or ""
    kinds = inp.get("kinds") or ["thinking", "text", "tool_use"]
    limit = min(int(inp.get("limit", ARCHIVE_MAX_RESULTS) or ARCHIVE_MAX_RESULTS),
                100)

    path = state.root / ARCHIVE_FILE
    if not path.exists():
        return {"content": "No archive yet."}

    hits: list[str] = []
    scanned = 0
    with open(path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            scanned += 1
            ts = rec.get("ts", "")
            if since and ts < since:
                continue
            if until and ts[:len(until)] > until:
                continue
            if stream_filter and rec.get("stream", "").lower() != stream_filter:
                continue
            if topic_filter and topic_filter not in rec.get("topic", "").lower():
                continue
            for block in rec.get("content", []):
                btype = block.get("type")
                if btype not in kinds:
                    continue
                if btype == "tool_use":
                    text = f"{block.get('name', '')}: " + json.dumps(
                        block.get("input", {}))[:2000]
                else:
                    text = block.get("thinking") or block.get("text") or ""
                m = regex.search(text)
                if not m:
                    continue
                lo = max(0, m.start() - ARCHIVE_SNIPPET_CHARS // 2)
                hi = min(len(text), m.end() + ARCHIVE_SNIPPET_CHARS // 2)
                snippet = (("…" if lo else "") + text[lo:hi].replace("\n", " ")
                           + ("…" if hi < len(text) else ""))
                hits.append(f"[{ts[:16]}] #{rec.get('stream', '?')}>"
                            f"{rec.get('topic', '?')} ({btype}): {snippet}")
                break  # one hit per record keeps results diverse

    if not hits:
        return {"content": f"No matches for /{pattern}/ in {scanned} "
                f"archive records."}
    total = len(hits)
    hits = hits[-limit:]  # newest last, like reading a log
    body = "\n".join(hits)
    if len(body) > ARCHIVE_MAX_OUTPUT:
        body = body[-ARCHIVE_MAX_OUTPUT:]
    return {"content": f"{total} matching record(s) (showing last "
            f"{len(hits)}, oldest first):\n{body}"}


def search_messages(inp: dict, state, reactions) -> dict:
    """Search the resident's own channel logs. Whole log, not a recent window.

    This used to fetch the last `count` messages (default 30, max 100) and
    THEN filter them by the query, which made it structurally unable to find
    anything older than ~100 messages in a topic — while reporting "No
    messages found", which reads as "it does not exist".

    That is not hypothetical. On 2026-09-22 Claude cited message 62772 as
    proof he had restated a chess move. Yue searched for it, got nothing,
    and issued a formal desk ruling: "message 62772 is not in the log.
    Searched local history and the API — no restatement exists. The claim
    fails verification." The message existed, said exactly what Claude said
    it said, and was sitting in all three residents' logs on disk. It was
    ~400 messages back, so the only thing the old implementation could
    possibly have returned was nothing.

    A false negative from a tool named `search_messages` is worse than an
    error, because the caller has no way to tell "absent" from "out of
    range" — and a resident that trusts its tools will rule on it.

    Now: scan every line of the topic log (or every topic in the stream),
    match on content and sender, return the most recent `count` hits.
    """
    stream = inp["stream"]
    topic = inp.get("topic")
    query = inp.get("query", "").lower().strip()
    count = min(inp.get("count", 30), 100)

    # No query means "show me the recent tail", which is what the old
    # windowed behavior was actually good for. Keep it.
    if not query:
        context = (state.get_recent_topic_context(
                       stream, topic, n=count, reactions=reactions)
                   if topic else
                   state.get_recent_stream_context(
                       stream, n=count, reactions=reactions))
        return {"content": context or "No messages found."}

    stream_dir = state.root / f"channels/{safe_filename(stream)}"
    if not stream_dir.exists():
        return {"content": f"No local log for #{stream}."}
    if topic:
        paths = [stream_dir / f"{safe_filename(topic)}.jsonl"]
    else:
        paths = sorted(stream_dir.glob("*.jsonl"))

    hits, scanned = [], 0
    for path in paths:
        if not path.exists():
            continue
        try:
            lines = path.read_text().splitlines()
        except OSError:
            continue
        scanned += len(lines)
        for line in lines:
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            body = e.get("content") or ""
            if query in body.lower() or query in (e.get("sender") or "").lower():
                hits.append((e.get("ts", ""), path.stem, e))

    if not hits:
        return {"content": (
            f"No match for {query!r} in {scanned:,} logged message(s) of "
            f"#{stream}" + (f">{topic}" if topic else "") + ".\n\n"
            f"This searched the WHOLE local log, so a miss here means your "
            f"log does not contain it — not that it was never said. Your log "
            f"starts when you did. For anything older, or to confirm an "
            f"absence before relying on it, use search(op=zulip), which "
            f"asks the server.")}

    hits.sort(key=lambda h: h[0])
    out = []
    for ts, topic_name, e in hits[-count:]:
        body = " ".join((e.get("content") or "").split())
        if len(body) > 400:
            body = body[:400] + "…"
        out.append(f"[{ts[:16]}] #{stream}>{topic_name} "
                   f"(id {e.get('msg_id', '?')}) | {e.get('sender', '?')}: {body}")
    header = (f"{len(hits)} match(es) for {query!r} in {scanned:,} logged "
              f"message(s)" + (f"; showing the {len(out)} most recent"
                               if len(hits) > len(out) else "") + ":")
    return {"content": header + "\n" + "\n".join(out)}


def search_zulip_history(inp: dict, zulip_client) -> dict:
    query = inp["query"]
    stream = inp.get("stream")
    topic = inp.get("topic")
    count = min(inp.get("count", 20), 50)

    # Special case: query="starred" fetches starred messages
    if query.strip().lower() == "starred":
        narrow = [{"operator": "is", "operand": "starred"}]
    else:
        narrow = [{"operator": "search", "operand": query}]
    if stream:
        narrow.append({"operator": "channel", "operand": stream})
    if topic:
        narrow.append({"operator": "topic", "operand": topic})

    try:
        result = zulip_client.get_messages({
            "anchor": "newest",
            "num_before": count,
            "num_after": 0,
            "narrow": json.dumps(narrow),
            "apply_markdown": False,
        })
    except Exception as e:
        return {"content": f"Zulip API error: {e}", "is_error": True}

    if result.get("result") != "success":
        return {"content": f"Zulip API error: {result.get('msg', 'unknown')}",
                "is_error": True}

    messages = result.get("messages", [])
    if not messages:
        return {"content": "No messages found matching that search."}

    formatted = []
    for msg in messages:
        ts = datetime.fromtimestamp(
            msg.get("timestamp", 0), tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        sender_name = msg.get("sender_full_name", "?")
        stream_name = msg.get("display_recipient", "?")
        topic_name = msg.get("subject", "?")
        content = msg.get("content", "")
        formatted.append(
            f"[{ts}] #{stream_name}>{topic_name} | {sender_name}: {content}")
    return {"content": "\n".join(formatted)}
