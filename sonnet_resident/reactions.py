"""Emoji reaction tracking and notification queueing."""

import logging

logger = logging.getLogger("reactions")


def emoji_display(emoji_name: str, emoji_code: str = "",
                  reaction_type: str = "") -> str:
    """Convert Zulip emoji info to a display character."""
    if reaction_type == "unicode_emoji" and emoji_code:
        try:
            return "".join(chr(int(c, 16)) for c in emoji_code.split("-"))
        except (ValueError, OverflowError):
            pass
    return f":{emoji_name}:"


def handle_reaction_event(event: dict,
                          reactions: dict[int, dict[str, int]],
                          msg_index: dict[int, dict],
                          pending_reactions: dict[tuple[str, str], list[str]]):
    """Track emoji reactions on messages and queue notifications for Claude's messages.

    Mutates reactions, msg_index, and pending_reactions in place.
    """
    op = event.get("op")  # "add" or "remove"
    msg_id = event.get("message_id")
    emoji = emoji_display(
        event.get("emoji_name", "?"),
        event.get("emoji_code", ""),
        event.get("reaction_type", ""),
    )

    if not msg_id:
        return

    if op == "add":
        if msg_id not in reactions:
            reactions[msg_id] = {}
        reactions[msg_id][emoji] = reactions[msg_id].get(emoji, 0) + 1

        info = msg_index.get(msg_id)
        if info and info["sender"] == "Claude":
            key = (info["stream"], info["topic"])
            if key not in pending_reactions:
                pending_reactions[key] = []
            preview = info.get("preview", "")
            pending_reactions[key].append(
                f'{emoji} on your message: "{preview}"')

    elif op == "remove":
        if msg_id in reactions and emoji in reactions[msg_id]:
            reactions[msg_id][emoji] -= 1
            if reactions[msg_id][emoji] <= 0:
                del reactions[msg_id][emoji]
            if not reactions[msg_id]:
                del reactions[msg_id]
