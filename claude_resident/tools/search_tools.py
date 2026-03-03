"""Message search tools: local logs and Zulip API."""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("tools.search")


def search_messages(inp: dict, state, reactions) -> dict:
    stream = inp["stream"]
    topic = inp.get("topic")
    query = inp.get("query", "").lower()
    count = min(inp.get("count", 30), 100)

    if topic:
        context = state.get_recent_topic_context(
            stream, topic, n=count, reactions=reactions)
    else:
        context = state.get_recent_stream_context(
            stream, n=count, reactions=reactions)

    if query and context:
        lines = context.split("\n")
        matched = [l for l in lines if query in l.lower()]
        context = "\n".join(matched[-count:])

    return {"content": context if context else "No messages found."}


def search_zulip_history(inp: dict, zulip_client) -> dict:
    query = inp["query"]
    stream = inp.get("stream")
    topic = inp.get("topic")
    count = min(inp.get("count", 20), 50)

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
