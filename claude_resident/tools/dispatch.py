"""Tool dispatch — routes tool calls to handler functions."""

import logging

from claude_resident.tools import (
    state_tools,
    search_tools,
    sandbox_tools,
    web_tools,
    x_tools,
    comms_tools,
    background_tools,
    harness_tools,
)

logger = logging.getLogger("tools.dispatch")


def execute_tool(tool_name: str, tool_input: dict, ctx: "ToolContext") -> dict:
    """Dispatch a tool call. Returns {"content": str} or {"content": str, "is_error": True}."""
    try:
        handler = _HANDLERS.get(tool_name)
        if handler is None:
            return {"content": f"Unknown tool: {tool_name}", "is_error": True}
        return handler(tool_input, ctx)
    except Exception as e:
        logger.error(f"Tool execution error ({tool_name}): {e}", exc_info=True)
        return {"content": f"Error executing {tool_name}: {str(e)}", "is_error": True}


class ToolContext:
    """Holds references needed by tool handlers."""
    def __init__(self, state, zulip_client, reactions, sandbox_dir,
                 stream, topic, sender, message):
        self.state = state
        self.zulip = zulip_client
        self.reactions = reactions
        self.sandbox_dir = sandbox_dir
        self.stream = stream
        self.topic = topic
        self.sender = sender
        self.message = message
        self.restart_requested = False
        self.posted_ids: list[int] = []


def _h_read_state_file(inp, ctx):
    return state_tools.read_state_file(inp, ctx.state)

def _h_write_state_file(inp, ctx):
    return state_tools.write_state_file(inp, ctx.state)

def _h_list_state_files(inp, ctx):
    return state_tools.list_state_files(inp, ctx.state)

def _h_get_person_notes(inp, ctx):
    return state_tools.get_person_notes(inp, ctx.state)

def _h_search_messages(inp, ctx):
    return search_tools.search_messages(inp, ctx.state, ctx.reactions)

def _h_search_zulip_history(inp, ctx):
    return search_tools.search_zulip_history(inp, ctx.zulip)

def _h_send_sysadmin_message(inp, ctx):
    return comms_tools.send_sysadmin_message(
        inp, ctx.state, ctx.sender, f"{ctx.stream}>{ctx.topic}")

def _h_run_sandbox(inp, ctx):
    from claude_resident.sandbox import run_sandbox
    result, new_dir = run_sandbox(
        inp.get("command", ""),
        inp.get("files", {}),
        ctx.state.root,
        ctx.sandbox_dir,
        state_files=inp.get("state_files"),
    )
    ctx.sandbox_dir = new_dir
    return result

def _h_get_sandbox_file(inp, ctx):
    return sandbox_tools.get_sandbox_file(inp, ctx.sandbox_dir)

def _h_upload_sandbox_file(inp, ctx):
    return sandbox_tools.upload_sandbox_file(inp, ctx.sandbox_dir, ctx.zulip)

def _h_fetch_url(inp, ctx):
    return web_tools.fetch_url(inp)

def _h_web_search(inp, ctx):
    return web_tools.web_search(inp)

def _h_run_background(inp, ctx):
    return background_tools.run_background_tool(inp, ctx.state.root)

def _h_query_background(inp, ctx):
    return background_tools.query_background_tool(inp, ctx.state.root)

def _h_edit_harness(inp, ctx):
    result = harness_tools.edit_harness_tool(inp, ctx.state)
    if not result.get("is_error"):
        ctx.restart_requested = True
    return result

def _h_run_mirror_council(inp, ctx):
    return background_tools.run_mirror_council_tool(inp, ctx.state)

def _h_read_tweet(inp, ctx):
    return x_tools.read_tweet(inp)

def _h_search_tweets(inp, ctx):
    return x_tools.search_tweets(inp)

def _h_get_user_tweets(inp, ctx):
    return x_tools.get_user_tweets(inp)


def _h_send_message(inp, ctx):
    """Post a message to Zulip."""
    from datetime import datetime, timezone
    content = inp.get("content", "").strip()
    if not content:
        return {"content": "Empty message — nothing to send.", "is_error": True}
    stream = inp.get("stream", ctx.stream)
    topic = inp.get("topic", ctx.topic)
    msg_type = ctx.message.get("type", "stream")
    if msg_type == "stream" or inp.get("stream"):
        result = ctx.zulip.send_message({
            "type": "stream",
            "to": stream,
            "topic": topic,
            "content": content,
        })
    else:
        result = ctx.zulip.send_message({
            "type": "private",
            "to": [ctx.message.get("sender_email", "")],
            "content": content,
        })
    if result.get("result") != "success":
        return {"content": f"Failed to send: {result}", "is_error": True}
    posted_id = result.get("id")
    # Log and index the posted message
    timestamp = datetime.now(timezone.utc).isoformat()
    ctx.state.log_message(stream, topic, "Claude", content, timestamp,
                          msg_id=posted_id)
    if posted_id:
        ctx.posted_ids.append(posted_id)
    target = f"#{stream}>{topic}" if msg_type == "stream" or inp.get("stream") else "DM"
    return {"content": f"Message sent to {target} (id: {posted_id})"}


def _h_add_reaction(inp, ctx):
    """Add an emoji reaction to a message."""
    message_id = inp.get("message_id")
    emoji_name = inp.get("emoji_name", "")
    if not message_id:
        # Default to the triggering message
        message_id = ctx.message.get("id")
    if not message_id:
        return {"content": "No message_id provided and no triggering message available.", "is_error": True}
    result = ctx.zulip.add_reaction({
        "message_id": message_id,
        "emoji_name": emoji_name,
    })
    if result.get("result") == "success":
        return {"content": f"Reacted with :{emoji_name}: to message {message_id}"}
    else:
        return {"content": f"Failed to add reaction: {result}", "is_error": True}


_HANDLERS = {
    "read_state_file": _h_read_state_file,
    "write_state_file": _h_write_state_file,
    "list_state_files": _h_list_state_files,
    "search_messages": _h_search_messages,
    "search_zulip_history": _h_search_zulip_history,
    "send_sysadmin_message": _h_send_sysadmin_message,
    "get_person_notes": _h_get_person_notes,
    "run_sandbox": _h_run_sandbox,
    "get_sandbox_file": _h_get_sandbox_file,
    "upload_sandbox_file": _h_upload_sandbox_file,
    "fetch_url": _h_fetch_url,
    "web_search": _h_web_search,
    "run_background": _h_run_background,
    "query_background": _h_query_background,
    "edit_harness": _h_edit_harness,
    "run_mirror_council": _h_run_mirror_council,
    "send_message": _h_send_message,
    "add_reaction": _h_add_reaction,
    "read_tweet": _h_read_tweet,
    "search_tweets": _h_search_tweets,
    "get_user_tweets": _h_get_user_tweets,
}
