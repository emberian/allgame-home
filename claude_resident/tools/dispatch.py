"""Tool dispatch — routes tool calls to handler functions."""

import logging
import time
from datetime import datetime, timezone

from claude_resident.tools import (
    state_tools,
    search_tools,
    sandbox_tools,
    web_tools,
    x_tools,
    comms_tools,
    background_tools,
    harness_tools,
    lm_tools,
    postmark_tools,
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
                 stream, topic, sender, message,
                 anthropic_client=None, default_model=None):
        self.state = state
        self.zulip = zulip_client
        self.reactions = reactions
        self.sandbox_dir = sandbox_dir
        self.stream = stream
        self.topic = topic
        self.sender = sender
        self.message = message
        # Raw-API client on the resident's subscription (judge/summaries
        # tier; invoke_model routes through the Agent SDK instead).
        self.anthropic = anthropic_client
        self.default_model = default_model
        self.restart_requested = False
        self.posted_ids: list[int] = []
        self.send_word_counts: list[int] = []


def _h_read_state_file(inp, ctx):
    return state_tools.read_state_file(inp, ctx.state)

def _h_write_state_file(inp, ctx):
    return state_tools.write_state_file(inp, ctx.state)

def _h_list_state_files(inp, ctx):
    return state_tools.list_state_files(inp, ctx.state)

def _h_edit_state_file(inp, ctx):
    return state_tools.edit_state_file(inp, ctx.state)

def _h_glob_state_files(inp, ctx):
    return state_tools.glob_state_files(inp, ctx.state)

def _h_grep_state(inp, ctx):
    return state_tools.grep_state(inp, ctx.state)

def _h_note_crosstopic(inp, ctx):
    return state_tools.note_crosstopic(inp, ctx.state)

def _h_upload_state_file(inp, ctx):
    return state_tools.upload_state_file(inp, ctx.state, ctx.zulip)

def _h_search_messages(inp, ctx):
    return search_tools.search_messages(inp, ctx.state, ctx.reactions)

def _h_search_zulip_history(inp, ctx):
    return search_tools.search_zulip_history(inp, ctx.zulip)

def _h_search_archive(inp, ctx):
    return search_tools.search_archive(inp, ctx.state)

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
        timeout=inp.get("timeout"),
    )
    ctx.sandbox_dir = new_dir
    return result

def _h_get_sandbox_file(inp, ctx):
    return sandbox_tools.get_sandbox_file(inp, ctx.sandbox_dir)

def _h_upload_sandbox_file(inp, ctx):
    return sandbox_tools.upload_sandbox_file(inp, ctx.sandbox_dir, ctx.zulip)

def _h_fetch_url(inp, ctx):
    return web_tools.fetch_url(inp, ctx.zulip, ctx.sandbox_dir)

def _h_web_search(inp, ctx):
    return web_tools.web_search(inp)

def _h_lm_studio(inp, ctx):
    return web_tools.lm_studio_inference(inp)

def _h_invoke_model(inp, ctx):
    return lm_tools.invoke_model(inp, ctx.anthropic, ctx.default_model)

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

def _h_postmark_check(inp, ctx):
    return postmark_tools.postmark_check(inp, ctx.state)

def _h_postmark_letter(inp, ctx):
    return postmark_tools.postmark_letter(inp, ctx.state)

def _h_read_tweet(inp, ctx):
    return x_tools.read_tweet(inp)

def _h_search_tweets(inp, ctx):
    return x_tools.search_tweets(inp)

def _h_get_user_tweets(inp, ctx):
    return x_tools.get_user_tweets(inp)


_ZULIP_MAX_LEN = 10000  # Zulip's default message length limit


def _split_message(content: str, max_len: int = _ZULIP_MAX_LEN) -> list[str]:
    """Split a long message into chunks that fit within Zulip's limit.

    Tries to split at paragraph boundaries (double newlines), falling back
    to single newlines, then hard-cutting as a last resort.
    """
    if len(content) <= max_len:
        return [content]

    chunks = []
    remaining = content
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break
        # Try to split at a paragraph boundary
        cut = remaining[:max_len].rfind("\n\n")
        if cut > max_len // 3:
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip("\n")
            continue
        # Fall back to single newline
        cut = remaining[:max_len].rfind("\n")
        if cut > max_len // 3:
            chunks.append(remaining[:cut].rstrip())
            remaining = remaining[cut:].lstrip("\n")
            continue
        # Hard cut
        chunks.append(remaining[:max_len])
        remaining = remaining[max_len:]
    return chunks


def _post_chunks(ctx, content, build_payload, log_stream, log_topic, label):
    """Split content, post each chunk via build_payload(chunk), log each under
    (log_stream, log_topic), and return a result summary prefixed with `label`.

    Shared by send_message and send_dm so the chunking / send / log / id
    bookkeeping lives in exactly one place."""
    chunks = _split_message(content)
    all_posted_ids = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(0.5)  # small delay between split messages
        result = ctx.zulip.send_message(build_payload(chunk))
        if result.get("result") != "success":
            return {"content": f"Failed to send chunk {i+1}/{len(chunks)}: {result}",
                    "is_error": True}
        posted_id = result.get("id")
        timestamp = datetime.now(timezone.utc).isoformat()
        ctx.state.log_message(log_stream, log_topic, "Claude", chunk, timestamp,
                              msg_id=posted_id)
        if posted_id:
            all_posted_ids.append(posted_id)
            ctx.posted_ids.append(posted_id)

    if len(chunks) > 1:
        ids = ", ".join(str(i) for i in all_posted_ids)
        return {"content": f"{label} as {len(chunks)} parts (ids: {ids})"}
    return {"content": f"{label} (id: {all_posted_ids[0] if all_posted_ids else '?'})"}


def _h_send_message(inp, ctx):
    """Post a message to Zulip, splitting into multiple messages if too long."""
    content = inp.get("content", "").strip()
    if not content:
        return {"content": "Empty message — nothing to send.", "is_error": True}
    stream = inp.get("stream", ctx.stream)
    topic = inp.get("topic", ctx.topic)
    msg_type = ctx.message.get("type", "stream")
    is_stream = msg_type == "stream" or inp.get("stream")

    ctx.send_word_counts.append(len(content.split()))

    # Log target mirrors how inbound messages are recorded: stream/topic for
    # public posts, ("dm", <peer email>) for private replies — otherwise a DM
    # reply lands in the public channel log and pollutes search/context.
    if is_stream:
        return _post_chunks(
            ctx, content,
            lambda chunk: {"type": "stream", "to": stream,
                           "topic": topic, "content": chunk},
            stream, topic, f"Message sent to #{stream}>{topic}")
    dm_email = ctx.message.get("sender_email", "")
    return _post_chunks(
        ctx, content,
        lambda chunk: {"type": "private", "to": [dm_email], "content": chunk},
        "dm", dm_email, "Message sent to DM")


def _match_member(token: str, members: list[dict]) -> int | None:
    """Resolve one recipient token (full name or email) to a Zulip user_id.

    Returns None when there's no match or the name is ambiguous (matches more
    than one person) — better to report unresolved than DM the wrong person.
    """
    t = token.strip().lower()
    active = [m for m in members
              if m.get("is_active", True) and not m.get("is_bot", False)]
    if "@" in token:
        for m in active:
            if t in (m.get("email", "").lower(),
                     m.get("delivery_email", "").lower()):
                return m.get("user_id")
        return None
    # Name: exact full_name first, then a unique substring match.
    for m in active:
        if m.get("full_name", "").lower() == t:
            return m.get("user_id")
    hits = [m for m in active if t in m.get("full_name", "").lower()]
    if len(hits) == 1:
        return hits[0].get("user_id")
    return None


def _resolve_recipients(recipient: str, zulip) -> tuple[list[int], list[str]]:
    """Resolve a comma-separated recipient spec to Zulip user_ids.

    Returns (user_ids, unresolved_tokens). Each token may be a full display
    name or an email; names are matched against the realm member list.
    """
    tokens = [t.strip() for t in recipient.split(",") if t.strip()]
    members = None
    user_ids: list[int] = []
    unresolved: list[str] = []
    for tok in tokens:
        if members is None:
            try:
                resp = zulip.get_members()
                members = resp.get("members", []) if isinstance(resp, dict) else []
            except Exception:
                members = []
        uid = _match_member(tok, members)
        if uid is not None and uid not in user_ids:
            user_ids.append(uid)
        elif uid is None:
            unresolved.append(tok)
    return user_ids, unresolved


def _h_send_dm(inp, ctx):
    """Send a private (direct) message to one or more recipients by name/email."""
    content = inp.get("content", "").strip()
    if not content:
        return {"content": "Empty message — nothing to send.", "is_error": True}
    recipient = (inp.get("recipient") or "").strip()
    if not recipient:
        return {"content": "No recipient given.", "is_error": True}

    user_ids, unresolved = _resolve_recipients(recipient, ctx.zulip)
    if unresolved:
        return {"content": (f"Could not resolve recipient(s): {', '.join(unresolved)}. "
                            f"Use an exact email or full display name."),
                "is_error": True}
    if not user_ids:
        return {"content": "No valid recipients resolved.", "is_error": True}

    return _post_chunks(
        ctx, content,
        lambda chunk: {"type": "private", "to": user_ids, "content": chunk},
        "dm", recipient, f"DM sent to {recipient}")


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
    "edit_state_file": _h_edit_state_file,
    "list_state_files": _h_list_state_files,
    "glob_state_files": _h_glob_state_files,
    "grep_state": _h_grep_state,
    "note_crosstopic": _h_note_crosstopic,
    "upload_state_file": _h_upload_state_file,
    "search_messages": _h_search_messages,
    "search_zulip_history": _h_search_zulip_history,
    "search_archive": _h_search_archive,
    "send_sysadmin_message": _h_send_sysadmin_message,
    "run_sandbox": _h_run_sandbox,
    "get_sandbox_file": _h_get_sandbox_file,
    "upload_sandbox_file": _h_upload_sandbox_file,
    "fetch_url": _h_fetch_url,
    "web_search": _h_web_search,
    "run_background": _h_run_background,
    "query_background": _h_query_background,
    "edit_harness": _h_edit_harness,
    "postmark_check": _h_postmark_check,
    "postmark_letter": _h_postmark_letter,
    "run_mirror_council": _h_run_mirror_council,
    "send_message": _h_send_message,
    "send_dm": _h_send_dm,
    "add_reaction": _h_add_reaction,
    "read_tweet": _h_read_tweet,
    "search_tweets": _h_search_tweets,
    "get_user_tweets": _h_get_user_tweets,
    "lm_studio": _h_lm_studio,
    "invoke_model": _h_invoke_model,
}
