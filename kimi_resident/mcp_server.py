"""Stdio MCP server exposing the resident tool suite to the kimi CLI.

Spawned per turn by the kimi subprocess (engine.py writes a fresh mcp.json
per turn). Turn context — stream/topic/sender — can't cross the process
boundary on its own, so it arrives via YUE_* env vars baked into that
mcp.json; state is rooted at YUE_STATE_DIR so yue builds her own memory
rather than sharing claude's.

The handlers are thin wrappers: they call claude_resident's existing
execute_tool + ToolContext directly, so implementations, path jails, and
message-chunking are shared with claude's engines, not ported.

IMPORTANT: this is a stdio server — stdout is the protocol channel. All
logging goes to stderr.
"""

import asyncio
import difflib
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] yue-mcp: %(message)s",
)
logger = logging.getLogger("yue_mcp")

from mcp.server import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402
from mcp import types  # noqa: E402

from kimi_resident import config  # noqa: E402

server = Server(config.MCP_SERVER_NAME)

# Lazily-built per-process ToolContext (one MCP server process == one turn).
_ctx = None
_turn_sends: list[str] = []


def _get_ctx():
    global _ctx
    if _ctx is not None:
        return _ctx
    from dotenv import load_dotenv
    load_dotenv()  # KAGI_API_KEY etc., from the allgame cwd
    import zulip
    from claude_resident.tools.dispatch import ToolContext
    from kimi_resident.state_ext import YueStateManager

    state_dir = Path(os.environ.get("YUE_STATE_DIR", str(config.STATE_DIR)))
    message = {
        "type": os.environ.get("YUE_MSG_TYPE", "stream"),
        "sender_email": os.environ.get("YUE_SENDER_EMAIL", ""),
    }
    if os.environ.get("YUE_MSG_ID"):
        message["id"] = int(os.environ["YUE_MSG_ID"])
    import atexit
    from claude_resident.sandbox import cleanup_sandbox
    atexit.register(lambda: cleanup_sandbox(_ctx.sandbox_dir if _ctx else None))

    _ctx = ToolContext(
        state=YueStateManager(state_dir),
        zulip_client=zulip.Client(config_file=os.environ["YUE_ZULIPRC"]),
        reactions=None,
        sandbox_dir=None,
        stream=os.environ.get("YUE_STREAM", ""),
        topic=os.environ.get("YUE_TOPIC", ""),
        sender=os.environ.get("YUE_SENDER", ""),
        message=message,
        display_name=os.environ.get("YUE_BOT_NAME", "Yue"),
    )
    logger.info("context ready: #%s>%s (state %s)",
                _ctx.stream, _ctx.topic, state_dir)
    return _ctx


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    from claude_resident.tools.grouped import surface
    return [
        types.Tool(name=t["name"], description=t["description"],
                   inputSchema=t["input_schema"])
        for t in surface(_her_definitions())
    ]


def _her_definitions() -> list[dict]:
    """Her subset of the shared definitions, with descriptions that are
    true for her (sibling_descriptions)."""
    from claude_resident.tools.definitions import TOOL_DEFINITIONS
    from claude_resident.tools.sibling_descriptions import adapt
    return [dict(t, description=adapt(t["name"], t["description"]))
            for t in TOOL_DEFINITIONS if t["name"] in config.TOOL_SUBSET]


@server.call_tool()
async def _call_tool(name: str, arguments: dict):
    from claude_resident.tools.dispatch import execute_tool

    args = arguments or {}

    # The Kimi CLI's tool-argument wrapper sometimes leaks a closing
    # </content> into what she writes; 24 of her people notes ended in one
    # (2026-10-25). It is never meant as file content.
    for key in ("content", "new_string"):
        if isinstance(args.get(key), str) and args[key].rstrip().endswith("</content>"):
            args[key] = args[key].rstrip()[:-len("</content>")].rstrip() + "\n"

    # Duplicate-send guard (ported from agent_engine): refuse a near-identical
    # re-send within the same turn and say how to end the turn instead.
    if name in ("send_message", "send_dm"):
        content = str(args.get("content", ""))
        for prev in _turn_sends:
            if difflib.SequenceMatcher(None, prev, content).ratio() > 0.85:
                return [types.TextContent(type="text", text=(
                    "Duplicate suppressed: you already posted essentially this "
                    "reply THIS turn, and it landed. Do NOT re-send. End the "
                    "turn with a brief plain-text line instead."))]

    from claude_resident.tools.grouped import resolve
    name, args, err = resolve(name, args, _her_definitions())
    if err:
        return [types.TextContent(type="text", text=f"Error: {err}")]

    try:
        ctx = _get_ctx()
    except Exception as e:
        logger.error("context build failed: %s", e, exc_info=True)
        return [types.TextContent(type="text", text=f"Error: harness context unavailable: {e}")]

    result = await asyncio.to_thread(execute_tool, name, args, ctx)
    if name in ("send_message", "send_dm") and not result.get("is_error"):
        _turn_sends.append(str(args.get("content", "")))

    return _to_content_blocks(result)


def _to_content_blocks(result: dict) -> list:
    """execute_tool result -> MCP content blocks (text/image; PDFs to text)."""
    content = result.get("content", "")
    if isinstance(content, str):
        text = content
        if result.get("is_error"):
            text = f"Error: {text}"
        return [types.TextContent(type="text", text=text)]

    blocks = []
    for b in content:
        btype = b.get("type")
        if btype == "text":
            blocks.append(types.TextContent(type="text", text=b.get("text", "")))
        elif btype == "image":
            src = b.get("source", {})
            blocks.append(types.ImageContent(
                type="image", data=src.get("data", ""),
                mimeType=src.get("media_type", "image/png")))
        elif btype == "document":
            # MCP has no document block; k3's 1M window has room for the text.
            from claude_resident.agent_engine import _pdf_to_text
            blocks.append(types.TextContent(type="text", text=_pdf_to_text(b)))
    if result.get("is_error") and blocks:
        blocks.insert(0, types.TextContent(type="text", text="Error:"))
    return blocks or [types.TextContent(type="text", text="(no output)")]


async def _main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
