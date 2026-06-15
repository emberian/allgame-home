"""API response archiving — append-only JSONL foreverlog.

This is the ONLY surviving record of Claude's thinking blocks: they are never
sent back in later requests and exist nowhere else (Zulip has the public
messages, not the interiority). So this file is intentionally append-only and
NEVER trimmed/rotated — every rotation was permanent, unrecoverable data loss.
Append-only is also cheaper: the old per-write trim re-read the whole file on
every API response just to count lines."""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("archive")


def archive_api_response(state, response, stream: str, topic: str,
                         sender: str, turn: int):
    """Append a full API response to the api_archive.jsonl foreverlog."""
    try:
        usage = response.usage
        blocks = []
        for block in response.content:
            if block.type == "text":
                blocks.append({"type": "text", "text": block.text})
            elif block.type == "thinking":
                blocks.append({"type": "thinking",
                               "thinking": block.thinking})
            elif block.type == "tool_use":
                blocks.append({"type": "tool_use", "name": block.name,
                               "input": block.input, "id": block.id})
            else:
                blocks.append({"type": block.type})

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "stream": stream,
            "topic": topic,
            "sender": sender,
            "turn": turn,
            "model": response.model,
            "stop_reason": response.stop_reason,
            "usage": {
                "input_tokens": getattr(usage, 'input_tokens', 0),
                "output_tokens": getattr(usage, 'output_tokens', 0),
                "cache_read": getattr(usage, 'cache_read_input_tokens', 0),
                "cache_create": getattr(usage, 'cache_creation_input_tokens', 0),
            },
            "content": blocks,
        }
        state.append_file("api_archive.jsonl", json.dumps(entry) + "\n")
    except Exception as e:
        logger.debug(f"Archive write failed: {e}")
