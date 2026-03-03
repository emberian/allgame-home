"""API response archiving — JSONL append with full response content."""

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("archive")

_ARCHIVE_MAX_LINES = 2000
_ARCHIVE_TRIM_TO = 1500


def archive_api_response(state, response, stream: str, topic: str,
                         sender: str, turn: int):
    """Archive full API response to JSONL for retroactive inspection."""
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
        _trim_archive(state)
    except Exception as e:
        logger.debug(f"Archive write failed: {e}")


def _trim_archive(state):
    """Trim api_archive.jsonl when it exceeds max lines."""
    path = state.root / "api_archive.jsonl"
    if not path.exists():
        return
    try:
        lines = path.read_text().strip().split("\n")
        if len(lines) > _ARCHIVE_MAX_LINES:
            path.write_text("\n".join(lines[-_ARCHIVE_TRIM_TO:]) + "\n")
            logger.info(f"Trimmed api_archive.jsonl: {len(lines)} → {_ARCHIVE_TRIM_TO}")
    except Exception:
        pass
