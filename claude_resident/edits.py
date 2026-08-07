"""Zulip message edits and moves (`update_message` events).

Zulip fires one event type for three different things, and they want three
different treatments:

  * a content edit      — somebody rewrote what they said
  * a topic/stream move — the message is still the same, it lives elsewhere
  * a bare re-render    — Zulip added a link preview; nobody touched anything

Before this module the resident never registered for `update_message` at
all, so all three were invisible: he would answer the pre-edit text of a
message someone had already fixed, and his channel logs kept the original
wording forever with no record that the room had moved on.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("edits")


def _locate(msg_id: int, event: dict, msg_index: dict, stream_id_map: dict,
            zulip) -> tuple[str, str]:
    """Which (stream, topic) log this message lives in.

    The index is authoritative — it is where we actually wrote the line. But
    it is capped, and an update_message event for a pure content edit carries
    no topic of its own, so an edit to a message older than the cap would
    otherwise be filed under the empty topic. Ask Zulip in that case rather
    than guess: it costs one request on a rare path, and guessing wrong means
    writing the edit into a log the message isn't in.
    """
    known = msg_index.get(msg_id) or {}
    stream = known.get("stream") or stream_id_map.get(event.get("stream_id"), "")
    topic = known.get("topic") or event.get("orig_subject") or ""
    if stream and topic:
        return stream, topic

    if zulip is None:
        return stream, topic
    try:
        result = zulip.get_messages({
            "anchor": msg_id, "num_before": 0, "num_after": 0,
            "narrow": [], "apply_markdown": False,
        })
        for m in result.get("messages", []):
            if m.get("id") != msg_id:
                continue
            recipient = m.get("display_recipient")
            if not isinstance(recipient, str):
                # A DM: display_recipient is a list of users, and the logs
                # key DMs by ("dm", sender_email) instead.
                return "dm", (topic or m.get("sender_email", ""))
            return stream or recipient, topic or m.get("subject", "")
    except Exception as e:
        logger.warning(f"Could not locate edited message {msg_id}: {e}")
    return stream, topic


def handle_update_message_event(event: dict, state, msg_index: dict,
                                live_batches, stream_id_map: dict,
                                zulip=None) -> None:
    """Apply one update_message event.

    `live_batches` is every collection of not-yet-answered message dicts —
    the event loop's pending deque plus any batch currently being assembled
    for a turn. Patching those in place is what makes him answer the text
    the author actually meant, instead of the text they already fixed.
    """
    # A re-render is not an edit. Zulip re-fires update_message when it
    # attaches a link/image preview, with rendering_only set; stamping
    # "(edited)" on those would accuse people of edits they never made.
    if event.get("rendering_only"):
        return

    msg_id = event.get("message_id")
    if not msg_id:
        return

    known = msg_index.get(msg_id) or {}
    stream, topic = _locate(msg_id, event, msg_index, stream_id_map, zulip)

    editor = event.get("user_id")
    edit_ts = event.get("edit_timestamp")
    timestamp = (datetime.fromtimestamp(edit_ts, tz=timezone.utc).isoformat()
                 if edit_ts else datetime.now(timezone.utc).isoformat())

    # --- Content edit ------------------------------------------------
    # orig_content is present only when the text actually changed, which
    # makes it a cleaner signal than the presence of `content`.
    if "orig_content" in event:
        new_content = event.get("content", "")

        patched = 0
        for batch in live_batches:
            for m in batch:
                if m.get("id") == msg_id:
                    m["content"] = new_content
                    patched += 1
        if patched:
            logger.info(
                f"Edit to msg {msg_id} landed before the reply — answering "
                f"the edited text")

        # Both halves or neither: a record filed under the wrong topic is a
        # phantom edit in a log the message was never in.
        if stream and topic:
            state.log_message_edit(stream, topic, msg_id, new_content,
                                   editor, timestamp)
            if not patched:
                logger.info(f"Message {msg_id} edited in #{stream}>{topic} "
                            f"(already answered or unseen — logged)")
        elif not patched:
            logger.warning(
                f"Message {msg_id} was edited but could not be placed in a "
                f"topic log (stream={stream!r} topic={topic!r}) — his history "
                f"keeps the pre-edit text")

        if known:
            known["preview"] = new_content[:80].replace("\n", " ")

    # --- Move --------------------------------------------------------
    # Recorded, not followed. His logs are one file per topic, so actually
    # following a move means rewriting history across two foreverlogs —
    # a separate piece of work. Recording it at least means the divergence
    # from Zulip is written down instead of silent.
    new_topic = event.get("subject")
    new_stream_id = event.get("new_stream_id")
    if ("orig_subject" in event and new_topic) or new_stream_id:
        dest_stream = stream_id_map.get(new_stream_id, "") if new_stream_id else ""
        if stream and topic:
            state.log_message_move(stream, topic, msg_id, dest_stream,
                                   new_topic or topic, editor, timestamp)
        logger.info(
            f"Message {msg_id} moved: #{stream}>{topic} → "
            f"#{dest_stream or stream}>{new_topic or topic} "
            f"(recorded; topic logs do not follow moves yet)")
