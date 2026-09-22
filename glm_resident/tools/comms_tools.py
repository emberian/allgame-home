"""Communication tools: sysadmin messages."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("tools.comms")


def send_sysadmin_message(inp: dict, state, sender_ctx: str,
                          channel_ctx: str) -> dict:
    message = inp["message"]
    write_sysadmin_message(state, message, sender_ctx, channel_ctx)
    return {"content": "Sysadmin message written to outbox."}


def write_sysadmin_message(state, message: str, sender_context: str,
                           channel_context: str):
    """Write a sysadmin message to the outbox as a timestamped file."""
    ts = datetime.now(timezone.utc)
    filename = ts.strftime("%Y%m%d_%H%M%S") + ".md"
    content = f"# Sysadmin Message\n\n"
    content += f"**Time:** {ts.isoformat()}\n"
    content += f"**Triggered by:** {sender_context} in #{channel_context}\n\n"
    content += f"---\n\n{message}\n"

    state.write_file(f"outbox/{filename}", content)
    logger.info(f"Sysadmin message written to outbox/{filename}")
