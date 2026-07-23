"""Agent SDK response engine — Claude Code as the resident's inference loop.

Instead of hand-driving the Messages API (the "api" engine), this engine
runs each response turn through the Claude Agent SDK: Claude Code as a
library, authenticated with the resident's own subscription token. That
draws PLAN LIMITS — flat-rate — rather than metered billing, which is the
sanctioned path since the 2026-04 change gated raw-API OAuth access behind
"extra usage".

Design invariants:
- The resident's tool surface is IDENTICAL to the api engine: his 28 tools
  are exposed through an in-process MCP server auto-generated from
  TOOL_DEFINITIONS + execute_tool, and Claude Code's own host tools (Bash,
  Read, Write, ...) are disabled. Same jail, different loop.
- Conversation continuity maps onto Claude Code sessions: our per-topic
  ConversationSession carries an sdk_session_id; warm turns resume it,
  cold starts mint a fresh one (with the full tier-4 context in the system
  prompt, exactly like the api engine's cold start).
- The system prompt is rebuilt fresh every turn from the same tier builder
  — under flat rate, cache_control engineering is irrelevant, so state
  changes just ride each turn instead of needing the fingerprint dance
  (deltas still arrive via content for mid-session updates).
- Thinking and tool calls are archived to api_archive.jsonl (the
  foreverlog) exactly as before, tagged engine=agent_sdk. Claude Code also
  persists full transcripts under AGENT_SDK_CONFIG_DIR as a bonus record.
"""

import json
import asyncio
import logging
from datetime import datetime, timezone

from claude_agent_sdk import (
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from claude_resident.auth import read_oauth_token
from claude_resident.config import (
    AGENT_SDK_MODEL,
    AGENT_SDK_FALLBACK_MODEL,
    AGENT_SDK_CONFIG_DIR,
    MAX_TOOL_TURNS,
)
from claude_resident.prompt import build_tiered_system_prompt
from claude_resident.sandbox import cleanup_sandbox
from claude_resident.tools.definitions import TOOL_DEFINITIONS
from claude_resident.tools.dispatch import ToolContext, execute_tool

logger = logging.getLogger("agent_engine")


def _mcp_result(result: dict) -> dict:
    """Convert an execute_tool result to MCP tool-result content."""
    content = result.get("content", "")
    blocks = []
    if isinstance(content, str):
        blocks.append({"type": "text", "text": content})
    else:
        for b in content:
            btype = b.get("type")
            if btype == "text":
                blocks.append({"type": "text", "text": b.get("text", "")})
            elif btype == "image":
                src = b.get("source", {})
                blocks.append({"type": "image",
                               "data": src.get("data", ""),
                               "mimeType": src.get("media_type", "image/png")})
            elif btype == "document":
                blocks.append({"type": "text", "text":
                               "[PDF fetched — inline PDF viewing isn't "
                               "available in the agent-sdk engine; use the "
                               "sandbox to extract text if needed]"})
    out = {"content": blocks}
    if result.get("is_error"):
        out["is_error"] = True
    return out


class AgentEngine:
    """Runs response turns through the Claude Agent SDK."""

    def __init__(self, resident):
        self.resident = resident
        self._ctx: ToolContext | None = None  # active turn's tool context
        self._server = self._build_server()
        AGENT_SDK_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"Agent SDK engine ready: model={AGENT_SDK_MODEL}, "
            f"{len(TOOL_DEFINITIONS)} tools via in-process MCP")

    # ------------------------------------------------------------------
    # Tool bridge
    # ------------------------------------------------------------------

    def _build_server(self):
        """Expose every existing tool through an in-process MCP server.

        The handlers dispatch into the same execute_tool + ToolContext the
        api engine uses — implementations, path jails, and side-effect
        bookkeeping (posted_ids, restart_requested) are shared, not ported.
        execute_tool is blocking (docker, network, sleeps), so it runs in a
        worker thread to keep the SDK transport's event loop alive.
        """
        sdk_tools = []
        for tdef in TOOL_DEFINITIONS:
            def _make(name):
                @tool(name, tdef["description"], tdef["input_schema"])
                async def _handler(args):
                    result = await asyncio.to_thread(
                        execute_tool, name, args or {}, self._ctx)
                    return _mcp_result(result)
                return _handler
            sdk_tools.append(_make(tdef["name"]))
        return create_sdk_mcp_server(name="resident", tools=sdk_tools)

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    def respond(self, session, stream: str, topic: str, sender: str,
                trigger: dict, content_blocks: list) -> tuple[str, list[int]]:
        """Run one response turn. Mirrors the api engine's contract:
        returns (internal_text, posted_ids) and propagates tool side
        effects (send word counts, restart requests) the same way."""
        cold = not session.sdk_session_id

        # Same tier builder as the api engine: full context on cold start,
        # slim tiers on resume (the SDK session holds the history).
        system_blocks = build_tiered_system_prompt(
            self.resident.state, stream, topic, trigger,
            is_first_message=cold, reactions=self.resident.reactions)
        system_text = "\n\n".join(b["text"] for b in system_blocks)

        prompt_text = self._flatten_content(content_blocks)

        ctx = ToolContext(
            state=self.resident.state,
            zulip_client=self.resident.zulip,
            reactions=self.resident.reactions,
            sandbox_dir=None,
            stream=stream,
            topic=topic,
            sender=sender,
            message=trigger,
            anthropic_client=self.resident.anthropic,
            default_model=self.resident.model,
        )
        self._ctx = ctx

        token = read_oauth_token() or ""
        options = ClaudeAgentOptions(
            system_prompt=system_text,
            model=AGENT_SDK_MODEL,
            fallback_model=AGENT_SDK_FALLBACK_MODEL,
            # thinking omitted: the CLI defaults to adaptive on Opus, and
            # sdk 0.2.126's transport chokes on the dataclass form.
            max_turns=MAX_TOOL_TURNS,
            mcp_servers={"resident": self._server},
            tools=[],  # no Claude Code host tools — his jailed surface only
            allowed_tools=[f"mcp__resident__{t['name']}"
                           for t in TOOL_DEFINITIONS],
            permission_mode="bypassPermissions",
            setting_sources=[],  # never load the host user's CLAUDE.md
            resume=session.sdk_session_id or None,
            cwd=str(AGENT_SDK_CONFIG_DIR),
            env={
                "CLAUDE_CODE_OAUTH_TOKEN": token,
                "CLAUDE_CONFIG_DIR": str(AGENT_SDK_CONFIG_DIR),
                # The resident loads .env (ANTHROPIC_API_KEY for the sandbox
                # and mirror council) and the SDK subprocess inherits the
                # parent env — an API key SHADOWS the OAuth token and bills
                # the console account instead of the subscription ("Credit
                # balance is too low", 2026-07-22). Empty string makes the
                # CLI fall through to the token; the key stays in os.environ
                # for the tools that legitimately need it.
                "ANTHROPIC_API_KEY": "",
            },
        )

        try:
            internal_text, assistant_record = asyncio.run(
                self._run_turn(prompt_text, options, session,
                               stream, topic, sender))
        finally:
            cleanup_sandbox(ctx.sandbox_dir)
            # Side effects back to the caller's bookkeeping (same names the
            # api engine propagates).
            session.send_word_counts.extend(ctx.send_word_counts)
            if ctx.restart_requested:
                self.resident._restart_requested = True
            self._ctx = None

        # Keep the reaper's transcript source truthful: record what the
        # model said/did this turn in session.messages (dict-shaped, the
        # form build_transcript already reads).
        if assistant_record:
            session.messages.append(
                {"role": "assistant", "content": assistant_record})

        return internal_text, list(ctx.posted_ids)

    async def _run_turn(self, prompt_text: str, options, session,
                        stream: str, topic: str, sender: str):
        collected_text: list[str] = []
        assistant_record: list[dict] = []
        turn = 0
        async for msg in query(prompt=prompt_text, options=options):
            if isinstance(msg, AssistantMessage):
                blocks = []
                for block in msg.content:
                    btype = getattr(block, "type", None) or type(block).__name__
                    text = getattr(block, "text", None)
                    thinking = getattr(block, "thinking", None)
                    name = getattr(block, "name", None)
                    if text is not None and thinking is None and name is None:
                        collected_text.append(text)
                        blocks.append({"type": "text", "text": text})
                        assistant_record.append(
                            {"type": "text", "text": text})
                    elif thinking is not None:
                        blocks.append({"type": "thinking",
                                       "thinking": thinking})
                    elif name is not None:
                        tin = getattr(block, "input", {}) or {}
                        blocks.append({"type": "tool_use", "name": name,
                                       "input": tin,
                                       "id": getattr(block, "id", "")})
                        assistant_record.append(
                            {"type": "tool_use", "name": name, "input": tin,
                             "id": getattr(block, "id", "")})
                self._archive(stream, topic, sender, turn, msg, blocks)
                turn += 1
            elif isinstance(msg, ResultMessage):
                if msg.session_id:
                    session.sdk_session_id = msg.session_id
                cost = msg.total_cost_usd
                logger.info(
                    f"Agent SDK turn done: #{stream}>{topic} "
                    f"{msg.num_turns} turns, {msg.duration_api_ms}ms api, "
                    f"cost={'plan' if not cost else f'${cost:.4f}'}"
                    + (f", stop={msg.stop_reason}" if msg.stop_reason else ""))
                if msg.is_error:
                    logger.error(f"Agent SDK result error: {msg.result!r} "
                                 f"{msg.errors!r}")
        return "\n".join(collected_text), assistant_record

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_content(content_blocks: list) -> str:
        """The api engine sends structured blocks; the SDK prompt is text.
        Images were already fetched upstream — note their presence so the
        resident knows something visual was posted (fetch_url can retrieve
        a URL if one is in the message text)."""
        parts = []
        for b in content_blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b["text"])
            elif b.get("type") == "image":
                parts.append("[an image was attached to this message — "
                             "if a URL is visible in the text, fetch_url "
                             "can retrieve it]")
            elif b.get("type") == "document":
                parts.append("[a PDF was attached to this message]")
        return "\n\n".join(parts)

    def _archive(self, stream, topic, sender, turn, msg, blocks):
        """Foreverlog entry per assistant message, api-archive shaped."""
        try:
            usage = getattr(msg, "usage", None) or {}
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "stream": stream,
                "topic": topic,
                "sender": sender,
                "turn": turn,
                "model": getattr(msg, "model", AGENT_SDK_MODEL),
                "stop_reason": getattr(msg, "stop_reason", None),
                "engine": "agent_sdk",
                "usage": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_create": usage.get(
                        "cache_creation_input_tokens", 0),
                } if isinstance(usage, dict) else {},
                "content": blocks,
            }
            self.resident.state.append_file(
                "api_archive.jsonl", json.dumps(entry) + "\n")
        except Exception as e:
            logger.debug(f"Agent SDK archive write failed: {e}")
