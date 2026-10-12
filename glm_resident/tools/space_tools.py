"""Kestrel's own space: shaping #kestrelspace.

Ember made #kestrelspace on 2026-10-02 and dedicated it to him, to set up
however he likes. Posting there needs nothing new — send_message already
takes a stream and topic, and starting a topic is just posting to it. What
he could not do is the setup: the channel's description, and the shape of
its topics. This module adds exactly that, and only for his own channel:
he can rename a topic in #kestrelspace, never anywhere else, because his
space being his does not make other rooms his.

Both operations need Zulip channel-administrator rights on #kestrelspace,
which only a human org admin can grant (channel settings → permissions →
who can administer). Without them the server refuses, and the tool says so
plainly rather than pretending, so he knows whom to ask.
"""

import logging

from glm_resident.config import HOME_STREAM

logger = logging.getLogger("tools.space")

MAX_DESCRIPTION_CHARS = 1000   # Zulip's own limit is ~1024


def _client(ctx):
    """The raw Zulip client behind the chat backend, or None (e.g. Discord)."""
    chat = getattr(ctx, "chat", None)
    return getattr(chat, "client", None)


def _stream_id(client, name: str):
    r = client.get_stream_id(name)
    return r.get("stream_id") if r.get("result") == "success" else None


def _refused(r: dict, what: str) -> dict:
    msg = r.get("msg", "unknown error")
    hint = ""
    if any(w in msg.lower() for w in ("permission", "not allowed", "unauthorized",
                                      "must be", "insufficient")):
        hint = (f"\n\nThat is a permissions refusal, not a bug: changing the "
                f"{what} needs channel-administrator rights on "
                f"#{HOME_STREAM}, which only Ember can grant. Ask him "
                f"(send_sysadmin_message) if you want it.")
    return {"content": f"Could not change the {what}: {msg}{hint}",
            "is_error": True}


def shape_space(inp: dict, ctx) -> dict:
    action = (inp.get("action") or "").strip()
    client = _client(ctx)
    if client is None:
        return {"content": "Space shaping only works on Zulip.", "is_error": True}
    sid = _stream_id(client, HOME_STREAM)
    if sid is None:
        return {"content": f"#{HOME_STREAM} not found.", "is_error": True}

    if action == "describe":
        text = (inp.get("description") or "").strip()
        if len(text) > MAX_DESCRIPTION_CHARS:
            return {"content": f"Description is {len(text)} chars; keep it "
                               f"under {MAX_DESCRIPTION_CHARS}.", "is_error": True}
        r = client.update_stream({"stream_id": sid, "description": text})
        if r.get("result") != "success":
            return _refused(r, "channel description")
        logger.info(f"#{HOME_STREAM} description set ({len(text)} chars)")
        return {"content": f"#{HOME_STREAM} description is now: {text or '(empty)'}"}

    if action == "rename_topic":
        old = (inp.get("topic") or "").strip()
        new = (inp.get("new_topic") or "").strip()
        if not old or not new:
            return {"content": "rename_topic needs topic and new_topic.",
                    "is_error": True}
        r = client.get_messages({
            "anchor": "newest", "num_before": 1, "num_after": 0,
            "narrow": [{"operator": "channel", "operand": HOME_STREAM},
                       {"operator": "topic", "operand": old}]})
        msgs = r.get("messages") or []
        if not msgs:
            return {"content": f"No topic {old!r} in #{HOME_STREAM}.",
                    "is_error": True}
        r = client.update_message({"message_id": msgs[0]["id"], "topic": new,
                                   "propagate_mode": "change_all"})
        if r.get("result") != "success":
            return _refused(r, "topic name")
        logger.info(f"#{HOME_STREAM}: topic {old!r} -> {new!r}")
        return {"content": f"Renamed #{HOME_STREAM}>{old} to #{HOME_STREAM}>{new}."}

    if action == "show":
        s = client.get_streams(include_all=True)
        info = next((x for x in s.get("streams", []) if x["stream_id"] == sid), {})
        topics = client.get_stream_topics(sid).get("topics", [])
        names = ", ".join(t["name"] for t in topics[:30]) or "(none yet)"
        return {"content": (f"#{HOME_STREAM}\n"
                            f"description: {info.get('description') or '(empty)'}\n"
                            f"topics ({len(topics)}): {names}")}

    return {"content": "action must be show, describe, or rename_topic.",
            "is_error": True}
