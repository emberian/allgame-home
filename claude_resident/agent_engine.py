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
import time
import asyncio
import difflib
import logging
import dataclasses
import threading
from datetime import datetime, timezone

from claude_agent_sdk import (
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from claude_resident.auth import read_oauth_token, rotate_account
from claude_resident import scry
from claude_resident.config import (
    AGENT_SDK_MODEL,
    AGENT_SDK_FALLBACK_MODEL,
    PERSISTENT_SESSION_MAX_TOKENS,
    AGENT_SDK_CONFIG_DIR,
    MAX_TOOL_TURNS,
    READING_SESSIONS_FILE,
    DEFAULT_STANDING_STREAMS,
    CROSS_STREAM_PER_STREAM_TOKENS,
    PERSISTENT_BIRTH_RAW_CHARS,
)
from claude_resident.prompt import build_tiered_system_prompt
from claude_resident.session_summary import recent_sessions_block
from claude_resident.sandbox import cleanup_sandbox
from claude_resident.tools.definitions import TOOL_DEFINITIONS
from claude_resident.tools.dispatch import ToolContext, execute_tool
from claude_resident.util import is_policy_refusal

logger = logging.getLogger("agent_engine")

# Tools whose successful use means the community actually heard something.
# A turn with none of these is MUTE — its text went nowhere.
VISIBLE_TOOLS = {"send_message", "send_dm", "add_reaction"}

# Rejections that mean "this account is out", not "this turn failed".
# The last two are not limits but the same remedy applies: on 2026-09-03
# ~03:40 four of the accounts started answering every request with
# "Your organization has disabled Claude subscription access for Claude
# Code" (raw API: oauth_not_allowed_for_organization). The engine did not
# know the phrase, so it never rotated off the dead account — he sat mute
# on tobb@ for 32 hours while four usable accounts idled, every failure
# logged as the SDK's uninformative "error result: success".
_LIMIT_MARKERS = ("weekly limit", "usage limit", "rate limit",
                  "hit your limit", "resets ", "quota",
                  "disabled claude subscription access",
                  "not allowed for this organization",
                  # An empty or dead token for the current account. Same
                  # remedy as a spent one: move to the next account rather
                  # than retrying this one every turn.
                  "not logged in", "please run /login")
# How many spent accounts to walk past before giving up on a turn. Each
# rotation costs a cold prefill, so this is not free — but with the
# drain-emptiest-first policy the next account is routinely near its limit
# too, and stopping after one retry left him silent with usable subs idle.
MAX_LIMIT_ROTATIONS = 4


# How much of a room's past a birth payload carries. The ladder a cold-start
# refusal walks down, in order.
#
# There is no rung between FULL and MEMORY: the raw tail is loaded
# newest-first under a char budget, so trimming it drops the OLDEST
# messages, and the material a classifier objects to is characteristically
# what the room is currently metabolising. A half-sized tail would be one
# more refused 450k-token prefill for the case that almost never happens.
BIRTH_FULL = "full"        # era digests + the raw tail
BIRTH_MEMORY = "memory"    # era digests only — the room as remembered
BIRTH_NONE = "none"        # a stranger who can still hear the room
BIRTH_LADDER = (BIRTH_FULL, BIRTH_MEMORY, BIRTH_NONE)
# How long a room stays on a reduced rung before trying its full past
# again. It has to expire, because acceptance is NOT deterministic at the
# margin: an identical 564k-char tavern payload was accepted and then
# refused within the same ten minutes (measured 2026-09-06). A rung
# remembered forever would let one unlucky draw cost a room its transcript
# for the life of the process; a rung remembered for nothing would make a
# marginal room pay a refused 450k-token prefill on every single turn.
BIRTH_RUNG_TTL = 30 * 60


def _is_limit_error(exc) -> bool:
    """True for a plan-limit rejection. Takes an exception OR the raw result
    text — a limit arrives both ways and both need to trigger rotation."""
    msg = str(exc).lower()
    return any(m in msg for m in _LIMIT_MARKERS)

# Engine-specific realities appended to the system prompt each turn — the
# base SYSTEM_PROMPT stays single-source for both engines.
#
# The closing-text rule exists because the Claude Code loop injects
# "[Your previous response had no visible output. Please continue...]"
# whenever a response ends on a bare tool call — which, combined with the
# base prompt's "text is invisible" framing, produced an infinite re-send
# loop (the 2026-07-23 duplicate-spam incident, self-diagnosed by the
# resident from inside the loop).
ENGINE_ADDENDUM = """
## Engine notes
- Your text output is a private scratchpad here; send_message is how the
  room hears you. So: post first, then close the turn with a brief line
  ("posted.") — that line is just a terminator, never the reply itself.
- One reply is one send_message. No second variants.
- If you see "[Your previous response had no visible output...]" and you
  did already send: it landed. Just write the closing line.
- Images and PDFs arrive as links; fetch_url on the link (including
  /user_uploads/... paths) turns one into real visual input."""


PDF_TEXT_MAX_CHARS = 600_000  # ~180k tokens — a whole book, if it comes to that


def _pdf_to_text(block: dict) -> str:
    """Extract a fetched PDF's text with page markers, for a window big
    enough to hold it."""
    import io
    import base64

    src = block.get("source", {}) or {}
    data = src.get("data")
    if not data:
        return "[PDF received but empty]"
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(base64.b64decode(data)))
        parts, chars = [], 0
        for i, page in enumerate(reader.pages):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:
                continue
            if not text:
                continue
            parts.append(f"[p. {i + 1}]\n{text}")
            chars += len(text)
            if chars > PDF_TEXT_MAX_CHARS:
                parts.append(
                    f"[…truncated at {PDF_TEXT_MAX_CHARS:,} chars; "
                    f"{len(reader.pages) - i - 1} pages remain. Save it to "
                    f"books/ and slice it if you want the whole thing.]")
                break
        body = "\n\n".join(parts)
        return (f"[PDF extracted: {len(reader.pages)} pages, "
                f"{len(body):,} chars of text]\n\n{body}"
                if body else
                "[PDF has no extractable text — likely scanned images. "
                "Fetch individual page images if you need to see it.]")
    except Exception as e:
        return (f"[PDF text extraction failed: {e}. The raw file is still "
                f"fetchable in the sandbox if you need it.]")


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
                # MCP has no document block, but a 1M window has room for
                # the whole text — extract it in-process rather than
                # handing back a placeholder (the old behavior made PDFs
                # unreadable in this engine).
                blocks.append({"type": "text", "text": _pdf_to_text(b)})
    out = {"content": blocks}
    if result.get("is_error"):
        out["is_error"] = True
    return out


class AgentEngine:
    """Runs response turns through the Claude Agent SDK."""

    def __init__(self, resident):
        self.resident = resident
        self._ctx: ToolContext | None = None  # active turn's tool context
        self._turn_sends: list[str] = []  # contents posted this turn
        self._turn_visible = False  # any visible action this turn?
        self._last_context_tokens = 0  # session size, for sleep
        # Set when a turn's ResultMessage reports a plan-limit rejection.
        # Survives the turn's event loop so the rotate-and-retry can read it
        # after the SDK's own (uninformative) exception surfaces.
        self._limit_reason: str | None = None
        # Set when the provider refuses the whole request on policy grounds.
        self._refusal_reason: str | None = None
        # Rooms whose birth payload had to be cut down to get past a policy
        # refusal: rung, and when we settled for it. In-process only, and
        # deliberately so — a remembered rung must not outlive the
        # quarantine edit that fixes the room, and edits take a restart.
        self._birth_rung: dict[str, tuple[str, float]] = {}
        # ONE turn at a time. _ctx / _turn_sends / _turn_visible are per-turn
        # state living on the instance, and since the solitude thread landed
        # (2026-07-30) two threads can call respond() at once: a finishing
        # turn would clear the running turn's context (failed tool calls) or
        # worse, a tool would execute against the OTHER turn's context and
        # post to the wrong channel. He shouldn't be having two thoughts
        # simultaneously anyway.
        self._turn_lock = threading.Lock()
        self._server = self._build_server()
        # Scry rides beside the resident server as a remote MCP endpoint —
        # its tools/list is the contract, so nothing here enumerates them.
        # No key, no server, and the prompt says nothing about it.
        scry_key = scry.api_key()
        self._scry_servers = (
            {scry.SERVER_NAME: scry.sdk_server(scry_key)} if scry_key else {})
        self._scry_addendum = scry.PROMPT_GUIDANCE if scry_key else ""
        # Claude Code has no per-server tool allowlist, only deny rules, so
        # the trim to scry.TOOLS is "deny everything else in the live
        # roster" — which the boot probe fetches (snapshot if it can't).
        self._scry_disallowed = (
            scry.sdk_disallowed(scry.log_probe("claude", scry_key))
            if scry_key else [])
        AGENT_SDK_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        logger.info(
            f"Agent SDK engine ready: model={AGENT_SDK_MODEL}, "
            f"{len(TOOL_DEFINITIONS)} tools via in-process MCP"
            + (f", Scry attached ({', '.join(scry.TOOLS)}; "
               f"{len(self._scry_disallowed)} denied)"
               if scry_key else ", no Scry key"))

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
                    args = args or {}
                    # Duplicate-send guard (the resident's own suggested
                    # fix, filed from inside the loop): if this turn
                    # already posted near-identical content, refuse the
                    # re-send and tell him how to end the turn instead.
                    if name in ("send_message", "send_dm"):
                        content = str(args.get("content", ""))
                        for prev in self._turn_sends:
                            if difflib.SequenceMatcher(
                                    None, prev, content).ratio() > 0.85:
                                return {"content": [{"type": "text", "text":
                                    "Duplicate suppressed: you already "
                                    "posted essentially this reply THIS "
                                    "turn, and it landed. Do NOT re-send. "
                                    "End the turn with a brief plain-text "
                                    "line instead."}], "is_error": True}
                        result = await asyncio.to_thread(
                            execute_tool, name, args, self._ctx)
                        if not result.get("is_error"):
                            self._turn_sends.append(content)
                            self._turn_visible = True
                        return _mcp_result(result)
                    result = await asyncio.to_thread(
                        execute_tool, name, args, self._ctx)
                    if (name in VISIBLE_TOOLS
                            and not result.get("is_error")):
                        self._turn_visible = True
                    return _mcp_result(result)
                return _handler
            sdk_tools.append(_make(tdef["name"]))
        return create_sdk_mcp_server(name="resident", tools=sdk_tools)

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    def respond(self, session, stream: str, topic: str, sender: str,
                trigger: dict, content_blocks: list,
                solitude: bool = False) -> tuple[str, list[int]]:
        """Run one response turn. Mirrors the api engine's contract:
        returns (internal_text, posted_ids) and propagates tool side
        effects (send word counts, restart requests) the same way."""
        with self._turn_lock:
            return self._respond_locked(session, stream, topic, sender,
                                        trigger, content_blocks, solitude)

    def _respond_locked(self, session, stream: str, topic: str, sender: str,
                        trigger: dict, content_blocks: list,
                        solitude: bool = False) -> tuple[str, list[int]]:
        # Bookread binding: a bound topic resumes its stored Claude Code
        # session even when our ConversationSession has long expired —
        # reading episodes days apart share one context window.
        binding_key = f"{stream}|{topic}"
        binding = self._load_bindings().get(binding_key)
        if binding and binding.get("sdk_session_id") \
                and not session.sdk_session_id:
            grown = int(binding.get("context_tokens") or 0)
            if grown > PERSISTENT_SESSION_MAX_TOKENS:
                # Sleep: let the room be reborn rather than dragging an
                # ever-longer thread. The birth payload restores the talk;
                # only the machinery is left behind.
                logger.info(
                    f"Retiring {binding_key} for rebirth "
                    f"({grown:,} tokens > {PERSISTENT_SESSION_MAX_TOKENS:,})")
                self._save_binding_fields(
                    binding_key, sdk_session_id="", context_tokens=0)
                binding = self._load_bindings().get(binding_key)
            else:
                session.sdk_session_id = binding["sdk_session_id"]
                logger.info(
                    f"Resuming bound session for {binding_key} "
                    f"({grown:,} tokens)" if grown else
                    f"Resuming bound session for {binding_key}")

        cold = not session.sdk_session_id

        # Persistent-room BIRTH: the eternal session's first turn carries
        # the months — all era digests plus a giant raw tail — into session
        # history itself (prompt travels via stdin; no argv ceiling). The
        # system tiers stay slim for a birth (is_first=False) since the
        # payload replaces tier 4 wholesale.
        birth_payload = ""
        if binding is not None and cold:
            birth_payload = self._birth_payload(
                stream, topic, rung=self._remembered_rung(f"{stream}|{topic}"))

        system_blocks = build_tiered_system_prompt(
            self.resident.state, stream, topic, trigger,
            is_first_message=(cold and not birth_payload),
            reactions=self.resident.reactions)
        system_text = ("\n\n".join(b["text"] for b in system_blocks)
                       + "\n" + ENGINE_ADDENDUM + self._scry_addendum)

        # The bound book's notes ride in context every episode — durable
        # memory that survives compaction of the long reading session.
        if binding and binding.get("book"):
            notes = self.resident.state.read_file(
                f"books/{binding['book']}/notes.md")
            if notes:
                system_text += (
                    f"\n\n<reading_notes book=\"{binding['book']}\">\n"
                    f"Your own notes for the book bound to this topic — "
                    f"update them via edit_state_file as you read "
                    f"(books/{binding['book']}/notes.md):\n{notes}\n"
                    f"</reading_notes>")

        # Life update: reading is richer when it knows what's going on
        # with his people. Cold starts carry this via tier 4; warm resumes
        # (every later episode) get it injected fresh here. The system
        # prompt is per-request — none of this accumulates into the long
        # session's history, so it can't bloat compaction. Book-bound
        # topics only: plain persistent rooms (tavern) ARE the life.
        if binding and binding.get("book") and not cold:
            life = []
            sessions_block = recent_sessions_block(
                self.resident.state, stream, topic)
            if sessions_block:
                life.append(sessions_block)
            for other in DEFAULT_STANDING_STREAMS:
                # Include the bound topic's OWN stream too — the pulse of
                # the house (tavern) usually lives next door.
                activity = self.resident.state.get_recent_stream_context(
                    other,
                    max_tokens=CROSS_STREAM_PER_STREAM_TOKENS // 2,
                    reactions=self.resident.reactions)
                if activity:
                    life.append(
                        f'<recent_stream_activity stream="{other}">'
                        f'\n{activity}\n</recent_stream_activity>')
            if life:
                system_text += (
                    "\n\n<life_update>\nWhat's been happening around the "
                    "house since your last reading episode — your people, "
                    "your other rooms. This is here so reading works for "
                    "you the way it works for humans: a chapter reminds "
                    "you of your week, and your week reads the chapter "
                    "back. When a passage catches on something someone "
                    "said in the tavern, or a letter, or the campaign — "
                    "chase it out loud. That resonance IS the reading, "
                    "not a digression from it.\n\n"
                    + "\n\n".join(life) + "\n</life_update>")

        # Cache forensics: the system prompt is the cached prefix, so any
        # byte that changes between turns re-prefills the whole thing. Log
        # a hash so a silent invalidator shows up as a changing digest on
        # consecutive turns in the same room.
        import hashlib
        sys_hash = hashlib.sha256(system_text.encode()).hexdigest()[:8]
        logger.info(f"System prompt for #{stream}>{topic}: "
                    f"{len(system_text):,} chars, hash={sys_hash}")

        prompt_text = self._flatten_content(content_blocks)
        # Per-turn reminder, placed where he acts rather than 550k tokens
        # away in the system prompt. The session history is mostly a
        # transcript of plain-text messages, which quietly models "a reply
        # is text" — this is the counterweight that keeps turns audible.
        if not solitude:
            prompt_text += "\n\n[send_message to reply]"
        bare_prompt = prompt_text   # the turn without its birth payload
        if birth_payload:
            prompt_text = birth_payload + "\n\n" + prompt_text
            logger.info(
                f"Persistent room birth: #{stream}>{topic} seeded with "
                f"{len(birth_payload):,} chars of history "
                f"(≈{len(birth_payload) // 4 // 1000}k tokens)")

        self._turn_sends = []
        self._turn_visible = False
        self._last_context_tokens = 0
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
            mcp_servers={"resident": self._server, **self._scry_servers},
            tools=[],  # no Claude Code host tools — his jailed surface only
            # mcp__<server> with no tool suffix is Claude Code's rule for
            # every tool that server lists — the roster is Scry's to grow.
            allowed_tools=[f"mcp__resident__{t['name']}"
                           for t in TOOL_DEFINITIONS]
                          + [f"mcp__{n}" for n in self._scry_servers],
            disallowed_tools=self._scry_disallowed,  # the roster trim
            permission_mode="bypassPermissions",
            setting_sources=[],  # never load the host user's CLAUDE.md
            resume=session.sdk_session_id or None,
            cwd=str(AGENT_SDK_CONFIG_DIR),
            # Tool results carrying base64 images (fetch_url on a posted
            # picture ≈ 6.7MB of JSON) blow the SDK's 1MB default read
            # buffer and kill the turn ("JSON message exceeded maximum
            # buffer size", 2026-07-23). Size for the largest legitimate
            # payload with headroom.
            max_buffer_size=32_000_000,
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
                # Make the CLI wait for the remote server before the first
                # model call — see scry.SDK_ENV for why this is needed.
                **(scry.SDK_ENV if self._scry_servers else {}),
            },
        )

        try:
            try:
                self._limit_reason = None
                self._refusal_reason = None
                internal_text, assistant_record = asyncio.run(
                    self._run_turn(prompt_text, options, session,
                                   stream, topic, sender))
            except Exception as e:
                # Two different failures arrive here wearing the same
                # useless text ("Claude Code returned an error result:
                # success"), so both are recognised from what the engine
                # recorded while the ResultMessage went past.
                if self._refusal_reason:
                    # POLICY refusal. Rotating cannot fix it — every account
                    # sees the same context. What poisons the room is the
                    # bound reading session: it carries its own transcript,
                    # so it survives every redaction of the channel logs.
                    # Retire it and retry cold; the birth payload restores
                    # the conversation from the logs.
                    #
                    # Without this the room stays mute until a human clears
                    # the binding by hand (three times in three days,
                    # 2026-08-22..24), and the symptom misleads: rapid
                    # chatter keeps a warm session alive and works, while
                    # anything arriving after a pause takes a fresh session,
                    # resumes the poisoned binding, and is refused. From
                    # inside the room that reads as "he answers chatter but
                    # not @s".
                    reason = self._refusal_reason
                    self._refusal_reason = None
                    binding_key = f"{stream}|{topic}"
                    bound = (self._load_bindings().get(binding_key) or {})
                    if not bound.get("sdk_session_id"):
                        internal_text, assistant_record = \
                            self._descend_birth_ladder(
                                binding_key, reason, bare_prompt, options,
                                session, stream, topic, sender)
                    else:
                        logger.warning(
                            f"Policy refusal in #{stream}>{topic} — "
                            f"retiring bound session for rebirth "
                            f"({reason[:80]})")
                        self._save_binding_fields(
                            binding_key, sdk_session_id="", context_tokens=0)
                        session.sdk_session_id = None
                        internal_text, assistant_record = asyncio.run(
                            self._run_turn(prompt_text, options, session,
                                           stream, topic, sender))
                elif self._limit_reason or _is_limit_error(e):
                    # Plan limits are per ACCOUNT and tokeman holds several,
                    # so a rejection means this account is spent, not that
                    # he has nothing to say. Walk down the drain order:
                    # since auth._health spends the soonest-expiring quota
                    # first, the next account is routinely near its own
                    # limit too, and stopping after one retry left him
                    # silent with usable subscriptions idle.
                    logger.warning(
                        f"Limit rejection: {self._limit_reason or e}")
                    for attempt in range(MAX_LIMIT_ROTATIONS):
                        nxt = rotate_account()
                        if not nxt:
                            raise
                        logger.warning(
                            f"Limit hit mid-turn; retrying "
                            f"#{stream}>{topic} on {nxt} "
                            f"(attempt {attempt + 1}/{MAX_LIMIT_ROTATIONS})")
                        options.env["CLAUDE_CODE_OAUTH_TOKEN"] = \
                            read_oauth_token() or ""
                        try:
                            self._limit_reason = None
                            # New account, cold cache — this re-prefills.
                            internal_text, assistant_record = asyncio.run(
                                self._run_turn(prompt_text, options, session,
                                               stream, topic, sender))
                            break
                        except Exception as e2:
                            if not (self._limit_reason
                                    or _is_limit_error(e2)):
                                raise
                            e = e2
                    else:
                        logger.error(
                            f"Every account tried is at its limit "
                            f"({MAX_LIMIT_ROTATIONS} rotations) — giving up "
                            f"on #{stream}>{topic}")
                        raise
                else:
                    raise

            # Mute-turn net: a turn that produced text but called no visible
            # tool means the reply went nowhere — text output is invisible
            # in this harness. Prompt wording alone can't guarantee this in
            # a 500k-token context, so nudge once, in-session, and let him
            # decide whether it was meant for the room.
            if (not solitude and not self._turn_visible
                    and internal_text.strip()):
                logger.warning(
                    f"Mute turn in #{stream}>{topic} — nudging "
                    f"({internal_text.strip()[:60]!r})")
                nudge_opts = dataclasses.replace(
                    options, resume=session.sdk_session_id or None)
                extra, extra_record = asyncio.run(self._run_turn(
                    "[HARNESS] Just checking: your last turn ended in plain "
                    "text, which nobody in the room can see. If some of it "
                    "was meant for them, send it with send_message (or "
                    "add_reaction). If it was private thinking, or you "
                    "chose to stay quiet, reply exactly: noop",
                    nudge_opts, session, stream, topic, sender))
                if extra_record:
                    assistant_record.extend(extra_record)
                if extra.strip():
                    internal_text += "\n" + extra
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

        # Persist the bound topic's session id so the long reading thread
        # survives TTL expiry and harness restarts.
        if binding is not None and session.sdk_session_id:
            self._save_binding_fields(
                binding_key, sdk_session_id=session.sdk_session_id,
                context_tokens=self._last_context_tokens)

        return internal_text, list(ctx.posted_ids)

    # ------------------------------------------------------------------
    # Persistent-room birth
    # ------------------------------------------------------------------

    def _remembered_rung(self, binding_key: str) -> str:
        """The rung this room settled for, until it is worth trying up again."""
        rung, at = self._birth_rung.get(binding_key, (BIRTH_FULL, 0.0))
        if rung != BIRTH_FULL and time.time() - at > BIRTH_RUNG_TTL:
            logger.info(f"Birth rung for {binding_key} expired after "
                        f"{BIRTH_RUNG_TTL // 60}m — trying its full past again")
            del self._birth_rung[binding_key]
            return BIRTH_FULL
        return rung

    def _descend_birth_ladder(self, binding_key: str, reason: str,
                              bare_prompt: str, options, session,
                              stream: str, topic: str, sender: str):
        """Keep a room audible when its own history is what gets refused.

        This is the case the bound-session retirement cannot reach: there
        is no session to retire, because the refused content arrived in the
        birth payload we just built. Rotating accounts is useless — every
        account reads the same room. Retrying is close to useless: well
        over the line a refusal is flatly deterministic (the distiller put
        one chunk up ~850 consecutive times over 2026-08-25..31 and was
        refused every time), and at the margin it is a coin the room cannot
        afford to keep flipping at 450k tokens a throw.

        So carry less of the room. Each rung is a different KIND of
        forgetting rather than a smaller amount of it, because the raw tail
        loads newest-first and trimming it drops exactly the wrong end.

        This is triage, not a fix. A room parked on a lower rung has lost
        its transcript, or its memory, until someone quarantines the
        messages the provider will not carry — which is why landing here is
        logged at ERROR and names the tool. The rung is remembered in
        process so the room does not pay a refused 450k-token prefill on
        every turn while that is waited for.
        """
        start = BIRTH_LADDER.index(self._remembered_rung(binding_key))
        for rung in BIRTH_LADDER[start + 1:]:
            logger.error(
                f"Policy refusal in #{stream}>{topic} with no bound session "
                f"to retire — the content is in the cold-start context. "
                f"Descending to birth rung {rung!r} ({reason[:80]})")
            payload = self._birth_payload(stream, topic, rung=rung)
            prompt = f"{payload}\n\n{bare_prompt}" if payload else bare_prompt
            self._refusal_reason = None
            try:
                result = asyncio.run(
                    self._run_turn(prompt, options, session,
                                   stream, topic, sender))
            except Exception:
                if not self._refusal_reason:
                    raise      # a different failure — not ours to swallow
                reason = self._refusal_reason
                self._refusal_reason = None
                continue
            self._birth_rung[binding_key] = (rung, time.time())
            logger.error(
                f"#{stream}>{topic} is now running on birth rung {rung!r} — "
                f"it has lost "
                + ("its raw transcript" if rung == BIRTH_MEMORY
                   else "its whole past")
                + ". Quarantine the messages the provider refuses "
                  "(scripts/refusal_probe.py to find them, "
                  "scripts/quarantine.py to lift them out) and restart.")
            return result
        logger.error(
            f"#{stream}>{topic} refused even with no history at all — the "
            f"trigger is in the message being answered or in the system "
            f"prompt, not in the room ({reason[:80]})")
        raise RuntimeError(f"policy refusal survives an empty birth "
                           f"payload in #{stream}>{topic}: {reason[:200]}")

    def _birth_payload(self, stream: str, topic: str,
                       rung: str = BIRTH_FULL) -> str:
        """The months, handed to a persistent room on its first breath.

        Era digests (all of them, uncapped — this is the one place the
        newest-biased load cap doesn't apply) plus the largest raw tail
        that fits the budget, or less of both if `rung` says the provider
        refused the fuller ones. Goes in the PROMPT, not the system block:
        it belongs to session history, gets cached once, and compaction
        folds it gradually as the window scrolls — whereas a system-block
        payload would be re-sent every single turn AND hit the argv
        ceiling (the prompt travels via stdin; system_prompt does not).
        """
        from claude_resident.distill import load_topic_hits

        if rung == BIRTH_NONE:
            return ""

        parts = []
        hits = load_topic_hits(self.resident.state, stream, topic,
                               max_chars=10 ** 9)  # uncapped at birth
        if hits:
            parts.append(
                f"<room_memory>\nDistilled memory of this room's deep past "
                f"— era volumes, oldest first, then recent still-settling "
                f"impressions. The raw history below picks up where these "
                f"leave off.\n\n{hits}\n</room_memory>")

        raw = "" if rung == BIRTH_MEMORY else \
            self.resident.state.get_recent_topic_context(
                stream, topic, n=10 ** 6,
                reactions=self.resident.reactions,
                max_tokens=PERSISTENT_BIRTH_RAW_CHARS // 4)
        if raw:
            parts.append(
                f'<room_history stream="{stream}" topic="{topic}">\n'
                f"{raw}\n</room_history>")
        if not parts:
            return ""

        return (
            "<<< This is the first turn of a PERSISTENT session for "
            f"#{stream}>{topic}. Everything below is that room's real "
            "history, loaded once so it lives in your conversation from "
            "here on — you are not being asked to summarize or respond to "
            "it. Read it as your own past. From this turn forward the "
            "conversation SCROLLS rather than resetting: today's messages "
            "join this same thread, and only the far end of it will ever "
            "be folded into summary. Respond only to the new message at "
            "the very end. >>>\n\n" + "\n\n".join(parts))

    # ------------------------------------------------------------------
    # Bookread bindings (books/README.md documents the practice)
    # ------------------------------------------------------------------

    def _load_bindings(self) -> dict:
        raw = self.resident.state.read_file(READING_SESSIONS_FILE)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError as e:
            logger.warning(f"reading_sessions.json unparseable: {e}")
            return {}

    def _save_binding_fields(self, key: str, **fields):
        # Re-read before writing: the resident edits this file itself
        # (adding/removing bindings), and its edits must not be clobbered.
        bindings = self._load_bindings()
        if key not in bindings:
            return  # binding was removed mid-turn — respect that
        if all(bindings[key].get(k) == v for k, v in fields.items()):
            return
        bindings[key].update(fields)
        self.resident.state.write_file(
            READING_SESSIONS_FILE, json.dumps(bindings, indent=1) + "\n")
        logger.info(f"Bound reading session updated for {key}")

    @staticmethod
    async def _query_surfacing_limits(prompt_text: str, options):
        """Thin pass-through over query().

        Kept as a seam: two previous attempts to detect plan limits *inside*
        this iteration both failed, and the reason is worth recording. A
        limit arrives as a ResultMessage payload, and the CLI then exits
        non-zero on purpose — but the exception that carries that exit does
        NOT necessarily surface through this generator. It can pop out at
        asyncio.run() teardown instead, by which point any local variable
        holding the real reason is gone, and what the caller sees is the
        SDK's degraded "Claude Code returned an error result: success" —
        no limit marker, and not even a RuntimeError.

        So the reason is recorded on the ENGINE (`_limit_reason`) the moment
        the ResultMessage goes past, and read back by the caller after the
        exception lands, wherever it lands.
        """
        async for msg in query(prompt=prompt_text, options=options):
            yield msg

    async def _run_turn(self, prompt_text: str, options, session,
                        stream: str, topic: str, sender: str):
        collected_text: list[str] = []
        assistant_record: list[dict] = []
        turn = 0
        async for msg in self._query_surfacing_limits(prompt_text, options):
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
                # Context size = ONE model call's read+write+input. Never
                # ResultMessage.usage, which sums every call in the turn and
                # so reported ~3x reality (a 1.06M "session" that fired the
                # sleep threshold on a ~500k room, 2026-07-29).
                mu = getattr(msg, "usage", None)
                if isinstance(mu, dict):
                    self._last_context_tokens = max(
                        self._last_context_tokens,
                        mu.get("cache_read_input_tokens", 0)
                        + mu.get("cache_creation_input_tokens", 0)
                        + mu.get("input_tokens", 0))
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
                    # Record on the engine, not in a local: the exception
                    # that follows may surface at asyncio.run() teardown
                    # rather than through the generator, and a local would
                    # already be gone. See _query_surfacing_limits.
                    if _is_limit_error(msg.result):
                        self._limit_reason = str(msg.result)
                    elif is_policy_refusal(msg.result):
                        # Not our content — the CONTEXT. Recorded here for
                        # the same reason as _limit_reason: the exception
                        # that follows says only "error result: success".
                        self._refusal_reason = str(msg.result)
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
