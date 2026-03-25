"""ClaudeResident — thin orchestrator for the Zulip residency."""

import re
import json
import time
import logging
import threading
from datetime import datetime, timezone

import anthropic

from claude_resident.config import (
    DEFAULT_MODEL, MAX_RESPONSE_TOKENS, THINKING_BUDGET,
    COOLDOWN_SECONDS, MAX_TOOL_TURNS, SESSION_MAX_TOKENS, MAX_STALE_FILES,
    SESSION_TRIM_PAIRS, TYPING_HOLDOFF_TIMEOUT,
)
from claude_resident.state import StateManager
from claude_resident.sessions import SessionManager
from claude_resident.judge import EngagementJudge
from claude_resident.reactions import emoji_display, handle_reaction_event
from claude_resident.prompt import (
    SYSTEM_PROMPT,
    build_tiered_system_prompt,
    fingerprint_state,
    compute_state_deltas,
    set_last_user_cache_control,
    clear_user_cache_control,
)
from claude_resident.images import build_content_blocks
from claude_resident.archive import archive_api_response
from claude_resident.sandbox import cleanup_sandbox
from claude_resident.tools.definitions import TOOL_DEFINITIONS
from claude_resident.tools.dispatch import ToolContext, execute_tool
from claude_resident.tools.comms_tools import write_sysadmin_message
from claude_resident.util import safe_filename


class ClaudeResident:
    """
    The main bot: listens to Zulip, decides whether to engage,
    loads context from state, generates response, posts it,
    and updates state with anything worth remembering.
    """

    # Expose SYSTEM_PROMPT as class attribute for monitor compat
    SYSTEM_PROMPT = SYSTEM_PROMPT

    def __init__(self, zulip_client, anthropic_client,
                 state: StateManager, judge: EngagementJudge,
                 model: str = DEFAULT_MODEL):
        self.zulip = zulip_client
        self.anthropic = anthropic_client
        self.state = state
        self.judge = judge
        self.model = model
        self.last_response_time = 0
        self._last_session_activity = 0.0
        self._sandbox_dir = None
        self.sessions = SessionManager()
        self._metacog_runner = None
        self._restart_requested = False
        self.logger = logging.getLogger("claude_resident")
        # Reaction tracking (capped to prevent unbounded growth)
        self.reactions: dict[int, dict[str, int]] = {}
        self._msg_index: dict[int, dict] = {}
        self._pending_reactions: dict[tuple[str, str], list[str]] = {}
        self._MSG_INDEX_CAP = 10_000
        # Typing indicator tracking: {(stream, topic): timestamp_of_last_start}
        self._typing_active: dict[tuple[str, str], float] = {}
        # Stream ID → stream name mapping (populated from events)
        self._stream_id_map: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Memory management
    # ------------------------------------------------------------------

    def _cap_indexes(self):
        """Evict oldest entries when msg_index or reactions exceed cap."""
        if len(self._msg_index) > self._MSG_INDEX_CAP:
            excess = len(self._msg_index) - self._MSG_INDEX_CAP
            oldest_ids = sorted(self._msg_index)[:excess]
            for mid in oldest_ids:
                del self._msg_index[mid]
                self.reactions.pop(mid, None)

    # ------------------------------------------------------------------
    # Tool definitions (for monitor compat and API calls)
    # ------------------------------------------------------------------

    def _tool_definitions(self) -> list[dict]:
        return TOOL_DEFINITIONS

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def handle_message(self, message: dict):
        """Process an incoming Zulip message with per-topic session persistence."""
        msg_type = message.get("type", "stream")
        if msg_type == "stream":
            stream = message.get("display_recipient", "unknown")
            topic = message.get("subject", "")
        else:
            stream = "dm"
            topic = message.get("sender_email", "unknown")

        sender = message.get("sender_full_name", "unknown")
        content = message.get("content", "")
        msg_id = message.get("id")
        timestamp = datetime.now(timezone.utc).isoformat()

        # Skip logging if already indexed (e.g., from backfill/replay)
        already_logged = msg_id and msg_id in self._msg_index
        if not already_logged:
            self.state.log_message(stream, topic, sender, content, timestamp,
                                   msg_id=msg_id)

        if msg_id:
            self._msg_index[msg_id] = {
                "stream": stream, "topic": topic, "sender": sender,
                "preview": content[:80].replace("\n", " "),
            }
            self._cap_indexes()

        if not self.judge.should_respond(message, stream):
            self.logger.info(
                f"Skipping #{stream}>{topic} from {sender}: "
                f"{self.judge.last_reason}")
            return

        now = time.time()
        if now - self.last_response_time < COOLDOWN_SECONDS:
            time.sleep(COOLDOWN_SECONDS - (now - self.last_response_time))

        # --- Typing hold-off: wait if someone is actively typing ---
        self._wait_for_typing(stream, topic)

        self.logger.info(f"Responding to {sender} in #{stream}>{topic}")

        # --- Session lookup ---
        session = self.sessions.get(stream, topic)
        is_first_message = session is None
        if is_first_message:
            session = self.sessions.create(stream, topic)
        session.touch()
        session.message_count += 1

        # --- Fingerprint current state ---
        new_fingerprints = fingerprint_state(self.state, stream, sender)

        # --- Staleness check ---
        if not is_first_message:
            tier23_keys = {"identity", "scratchpad", "inbox"}
            if stream.lower() == "allgame":
                tier23_keys |= {"allgame/faction.md",
                                "allgame/campaign_log.md",
                                "allgame/strategy.md"}
            stale_count = sum(
                1 for k in tier23_keys
                if session.state_fingerprints.get(k) != new_fingerprints.get(k))
            if stale_count > MAX_STALE_FILES:
                self.logger.info(
                    f"Session reset: {stale_count} stale files exceed threshold")
                self.sessions.remove(stream, topic)
                session = self.sessions.create(stream, topic)
                session.touch()
                session.message_count = 1
                is_first_message = True

        # --- Build system prompt ---
        if is_first_message:
            system = build_tiered_system_prompt(
                self.state, stream, topic, message,
                is_first_message=True, reactions=self.reactions)
            session.system_blocks = system
        else:
            system = session.system_blocks

        # --- Build user message ---
        content_blocks = build_content_blocks(
            f"{sender}: {content}", self.zulip)

        if not is_first_message:
            delta_text = compute_state_deltas(
                self.state, session, new_fingerprints, stream, sender)
            if delta_text:
                content_blocks = [{"type": "text", "text": delta_text}] + content_blocks

        key = (stream, topic)
        if key in self._pending_reactions and self._pending_reactions[key]:
            rxn_lines = self._pending_reactions.pop(key)
            rxn_text = ("<reactions_received>\n"
                        + "\n".join(rxn_lines)
                        + "\n</reactions_received>")
            content_blocks = [{"type": "text", "text": rxn_text}] + content_blocks

        session.state_fingerprints = new_fingerprints

        # Strip base64 image data from older messages to prevent accumulation.
        # Images are only useful for the turn they arrive — after that, the
        # model has already seen them and they just waste context.
        _strip_old_images(session.messages)
        session.messages.append({"role": "user", "content": content_blocks})

        # --- Token budget check ---
        if session.estimated_message_tokens > 0:
            est_tokens = session.estimated_message_tokens
        else:
            est_tokens = _estimate_tokens(session.messages)
        if est_tokens > SESSION_MAX_TOKENS:
            self.logger.info(
                f"Session over budget ({est_tokens} tokens), trimming")
            _trim_session(session, self.logger)

        # --- Generate response ---
        self._send_typing(stream, topic, "start")
        typing_stop = threading.Event()
        def _typing_pulse():
            while not typing_stop.wait(10):
                self._send_typing(stream, topic, "start")
        typing_thread = threading.Thread(target=_typing_pulse, daemon=True)
        typing_thread.start()
        try:
            internal_text, posted_ids = \
                self._generate_response_with_tools_session(
                    system, session, stream, topic, sender, message)
        except Exception as e:
            self.logger.error(
                f"Response generation failed: {e}", exc_info=True)
            self.sessions.remove(stream, topic)
            return
        finally:
            typing_stop.set()
            self._send_typing(stream, topic, "stop")

        # Handle XML fallbacks (safety net for state updates)
        if internal_text and internal_text.strip():
            _, state_updates = _extract_state_updates(internal_text)
            _, sysadmin_messages = _extract_sysadmin_messages(internal_text)
            for update in state_updates:
                self.logger.warning(
                    "Model used XML state_update instead of tool — "
                    "applying anyway")
                _apply_state_update(self.state, update, self.logger)
            for msg in sysadmin_messages:
                self.logger.warning(
                    "Model used XML sysadmin_message instead of tool — "
                    "applying anyway")
                write_sysadmin_message(
                    self.state, msg, sender, f"{stream}>{topic}")

        # Log internal monologue (model text that was NOT posted)
        if internal_text and internal_text.strip():
            monologue = internal_text.strip()[:200]
            self.logger.info(
                f"Internal: #{stream}>{topic} — {monologue}"
                + ("..." if len(internal_text.strip()) > 200 else ""))

        # Index any messages posted via send_message tool
        for pid in posted_ids:
            self._msg_index[pid] = {
                "stream": stream, "topic": topic, "sender": "Claude",
                "preview": "(posted via send_message)",
            }

        # Log outcome and manage lurk counter
        if posted_ids:
            # Reset lurk — Claude spoke publicly
            self.judge.record_message(stream, is_direct=True)
            self.logger.info(
                f"Posted {len(posted_ids)} message(s) in #{stream}>{topic}")
        elif not internal_text or not internal_text.strip():
            # Roll back the lurk increment from the judge — processing
            # without posting shouldn't count against engagement
            key = stream.lower()
            if key in self.judge._msgs_since_interaction:
                self.judge._msgs_since_interaction[key] = max(
                    0, self.judge._msgs_since_interaction[key] - 1)
            self.logger.info(
                f"Observe mode: #{stream}>{topic} — silent")
        else:
            # Same rollback — internal monologue without posting
            key = stream.lower()
            if key in self.judge._msgs_since_interaction:
                self.judge._msgs_since_interaction[key] = max(
                    0, self.judge._msgs_since_interaction[key] - 1)
            self.logger.info(
                f"Internal only: #{stream}>{topic} — no public message")

        self.last_response_time = time.time()
        self._last_session_activity = time.time()
        if self._metacog_runner:
            self._metacog_runner.notify_activity()

    # ------------------------------------------------------------------
    # Reaction handling
    # ------------------------------------------------------------------

    @staticmethod
    def _emoji_display(emoji_name: str, emoji_code: str = "",
                       reaction_type: str = "") -> str:
        return emoji_display(emoji_name, emoji_code, reaction_type)

    def _handle_reaction_event(self, event: dict):
        handle_reaction_event(
            event, self.reactions, self._msg_index, self._pending_reactions)

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    def _handle_typing_event(self, event: dict):
        """Track typing start/stop events from Zulip."""
        op = event.get("op")
        stream_id = event.get("stream_id")
        topic = event.get("topic", "")

        if not stream_id or not topic:
            return

        # Resolve stream name from ID
        stream = self._stream_id_map.get(stream_id)
        if not stream:
            # Try to populate the map
            try:
                result = self.zulip.get_streams()
                if result.get("result") == "success":
                    for s in result.get("streams", []):
                        self._stream_id_map[s["stream_id"]] = s["name"]
                stream = self._stream_id_map.get(stream_id, "")
            except Exception:
                return

        key = (stream, topic)
        if op == "start":
            self._typing_active[key] = time.time()
        elif op == "stop":
            self._typing_active.pop(key, None)

    def _send_typing(self, stream: str, topic: str, op: str = "start"):
        """Send a typing indicator to Zulip."""
        try:
            stream_id = None
            for sid, name in self._stream_id_map.items():
                if name.lower() == stream.lower():
                    stream_id = sid
                    break
            if stream_id is None:
                return
            self.zulip.set_typing_status({
                "op": op,
                "type": "stream",
                "stream_id": stream_id,
                "topic": topic,
            })
        except Exception:
            pass  # never block on typing indicators

    def _wait_for_typing(self, stream: str, topic: str):
        """If someone is actively typing in this topic, wait for them to finish.

        Polls the typing state with short sleeps. Times out after
        TYPING_HOLDOFF_TIMEOUT seconds to avoid blocking forever.
        """
        key = (stream, topic)
        started = self._typing_active.get(key)
        if not started:
            return

        # Typing indicator is stale (>15s old) — Zulip stops sending
        # start events if the user pauses, so treat old ones as expired
        if time.time() - started > 15:
            self._typing_active.pop(key, None)
            return

        self.logger.info(
            f"Typing detected in #{stream}>{topic}, holding off...")
        deadline = time.time() + TYPING_HOLDOFF_TIMEOUT
        while time.time() < deadline:
            if key not in self._typing_active:
                self.logger.info(
                    f"Typing stopped in #{stream}>{topic}, proceeding")
                return
            # Check staleness — Zulip typing events expire ~15s
            ts = self._typing_active.get(key, 0)
            if time.time() - ts > 15:
                self._typing_active.pop(key, None)
                self.logger.info(
                    f"Typing indicator expired in #{stream}>{topic}, "
                    "proceeding")
                return
            time.sleep(0.5)

        self._typing_active.pop(key, None)
        self.logger.info(
            f"Typing holdoff timeout ({TYPING_HOLDOFF_TIMEOUT}s) in "
            f"#{stream}>{topic}, proceeding anyway")

    # ------------------------------------------------------------------
    # Response generation (tool loop)
    # ------------------------------------------------------------------

    def _generate_response_with_tools_session(
            self, system: list[dict], session, stream: str, topic: str,
            sender: str, message: dict) -> tuple[str, list[int]]:
        """Multi-turn tool-calling loop operating on persistent session messages.

        Returns (internal_text, posted_ids) — internal_text is model monologue
        (not posted), posted_ids are messages sent via send_message tool.
        """
        collected_text = []
        all_posted_ids: list[int] = []
        self._sandbox_dir = None

        set_last_user_cache_control(session.messages)

        thinking_param = (
            {"type": "adaptive"} if "opus" in self.model
            else {"type": "enabled", "budget_tokens": THINKING_BUDGET})

        try:
            for turn in range(MAX_TOOL_TURNS):
                try:
                    response = self.anthropic.messages.create(
                        model=self.model,
                        max_tokens=MAX_RESPONSE_TOKENS,
                        thinking=thinking_param,
                        system=system,
                        tools=TOOL_DEFINITIONS,
                        messages=session.messages,
                    )
                except anthropic.BadRequestError as e:
                    if "prompt is too long" in str(e):
                        self.logger.warning(
                            f"Prompt too long at turn {turn}, trimming session")
                        _trim_session(session, self.logger)
                        if turn == 0:
                            try:
                                response = self.anthropic.messages.create(
                                    model=self.model,
                                    max_tokens=MAX_RESPONSE_TOKENS,
                                    thinking=thinking_param,
                                    system=system,
                                    tools=TOOL_DEFINITIONS,
                                    messages=session.messages,
                                )
                            except anthropic.APIError as e2:
                                self.logger.error(
                                    f"Still too long after trim: {e2}")
                                break
                        else:
                            break
                    else:
                        self.logger.error(
                            f"Anthropic API error (turn {turn}): {e}")
                        break
                except anthropic.APIError as e:
                    self.logger.error(
                        f"Anthropic API error (turn {turn}): {e}")
                    break

                usage = response.usage
                input_tokens = getattr(usage, 'input_tokens', 0)
                cache_read = getattr(
                    usage, 'cache_read_input_tokens', 0)
                cache_create = getattr(
                    usage, 'cache_creation_input_tokens', 0)
                # Total context = uncached + cached read + cached write
                total_input = input_tokens + cache_read + cache_create
                session.estimated_message_tokens = total_input
                if turn == 0:
                    self.logger.info(
                        f"Cache: {cache_read} read, {cache_create} created, "
                        f"{input_tokens} uncached | "
                        f"session #{session.stream}>{session.topic} "
                        f"msg#{session.message_count} "
                        f"({len(session.messages)} msgs in history)")

                archive_api_response(
                    self.state, response, stream, topic, sender, turn)

                tool_use_blocks = []
                for block in response.content:
                    if block.type == "text":
                        collected_text.append(block.text)
                    elif block.type == "thinking":
                        pass
                    elif block.type == "tool_use":
                        tool_use_blocks.append(block)

                if response.stop_reason == "refusal":
                    self.logger.warning(
                        f"API refusal at turn {turn} in "
                        f"#{stream}>{topic} — safety filter triggered")
                    break

                if response.stop_reason == "end_turn" or not tool_use_blocks:
                    session.messages.append({
                        "role": "assistant",
                        "content": response.content})
                    self.logger.debug(
                        f"Response complete after {turn + 1} turn(s)")
                    break

                self.logger.info(
                    f"Turn {turn + 1}: {len(tool_use_blocks)} tool call(s): "
                    + ", ".join(b.name for b in tool_use_blocks))
                # Re-send typing indicator (they expire after ~15s)
                self._send_typing(stream, topic, "start")
                session.messages.append({
                    "role": "assistant", "content": response.content})

                # Execute tools
                ctx = ToolContext(
                    state=self.state,
                    zulip_client=self.zulip,
                    reactions=self.reactions,
                    sandbox_dir=self._sandbox_dir,
                    stream=stream,
                    topic=topic,
                    sender=sender,
                    message=message,
                )
                tool_results = []
                for block in tool_use_blocks:
                    result = execute_tool(block.name, block.input, ctx)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result["content"],
                        **({"is_error": True}
                           if result.get("is_error") else {}),
                    })
                # Propagate side effects back
                self._sandbox_dir = ctx.sandbox_dir
                all_posted_ids.extend(ctx.posted_ids)
                if ctx.restart_requested:
                    self._restart_requested = True

                session.messages.append(
                    {"role": "user", "content": tool_results})
            else:
                self.logger.warning(
                    f"Tool loop hit max iterations ({MAX_TOOL_TURNS})")
        finally:
            clear_user_cache_control(session.messages)
            cleanup_sandbox(self._sandbox_dir)
            self._sandbox_dir = None

        return "\n".join(collected_text), all_posted_ids

    # ------------------------------------------------------------------
    # Legacy single-turn response (reflect/ambient/arrive)
    # ------------------------------------------------------------------

    def _generate_response(self, context: str, user_message: str,
                           sender: str) -> str:
        system = self.SYSTEM_PROMPT + "\n\n" + context
        content_blocks = build_content_blocks(
            f"{sender}: {user_message}", self.zulip)

        try:
            response = self.anthropic.messages.create(
                model=self.model,
                max_tokens=MAX_RESPONSE_TOKENS,
                system=system,
                messages=[{"role": "user", "content": content_blocks}],
            )
            return response.content[0].text
        except anthropic.APIError as e:
            self.logger.error(f"Anthropic API error: {e}")
            return ""

    # Keep _build_content_blocks for monitor compat
    def _build_content_blocks(self, text: str) -> list[dict]:
        return build_content_blocks(text, self.zulip)

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    def _post_response(self, original_message: dict,
                       response: str) -> int | None:
        if original_message.get("type") == "stream":
            result = self.zulip.send_message({
                "type": "stream",
                "to": original_message["display_recipient"],
                "topic": original_message.get("subject", ""),
                "content": response,
            })
        else:
            result = self.zulip.send_message({
                "type": "private",
                "to": [original_message["sender_email"]],
                "content": response,
            })

        if result.get("result") != "success":
            self.logger.error(f"Failed to send message: {result}")
            return None
        return result.get("id")

    # ------------------------------------------------------------------
    # One-shot modes
    # ------------------------------------------------------------------

    def reflect(self):
        """Periodic reflection mode — triggered by cron."""
        self.logger.info("Starting periodic reflection...")

        sections = []
        identity = self.state.read_file("identity.md")
        if identity:
            sections.append(f"<my_identity>\n{identity}\n</my_identity>")
        scratchpad = self.state.read_file("scratchpad.md")
        if scratchpad:
            sections.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")
        for stream in self.judge.standing_streams:
            context = self.state.get_recent_stream_context(
                stream, n=100, reactions=self.reactions)
            if context:
                sections.append(
                    f'<stream_history stream="{stream}">'
                    f'\n{context}\n</stream_history>')
        for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
            content = self.state.read_file(f"allgame/{fname}")
            if content:
                tag = fname.replace('.md', '')
                sections.append(
                    f"<allgame_{tag}>\n{content}\n</allgame_{tag}>")
        journal = self.state.read_file("journal.md")
        if journal:
            sections.append(f"<journal>\n{journal}\n</journal>")
        inbox = self.state.read_file("sysadmin_inbox.md")
        if inbox:
            sections.append(
                f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")
        harness_source = self.state.read_file("harness.py")
        if harness_source:
            sections.append(
                f"<my_harness>\n{harness_source}\n</my_harness>")

        context_payload = "\n\n".join(sections)

        reflection_prompt = """This is a periodic reflection — no one messaged you. This is your time
to think. Review your recent channel history, your scratchpad, your journal,
and your allgame state. Then:

1. What happened since your last reflection that matters?
2. Is there anything on your scratchpad that's resolved or stale?
3. Any observations about community members worth noting?
4. Any strategic thoughts about allgame?
5. Anything you want to flag to your sysadmin?

Write naturally. Include <state_update> blocks to update your journal, clean
up your scratchpad, add person notes, or adjust allgame strategy. Include a
<sysadmin_message> if there's anything for Ember. Your reflection text itself
will be appended to your journal."""

        system = self.SYSTEM_PROMPT + "\n\n" + context_payload

        try:
            response = self.anthropic.messages.create(
                model=self.model,
                max_tokens=MAX_RESPONSE_TOKENS,
                system=system,
                messages=[{"role": "user", "content": reflection_prompt}],
            )
            response_text = response.content[0].text
        except anthropic.APIError as e:
            self.logger.error(f"Reflection API error: {e}")
            return

        clean_text, state_updates = _extract_state_updates(response_text)
        clean_text, sysadmin_messages = _extract_sysadmin_messages(clean_text)

        for update in state_updates:
            _apply_state_update(self.state, update, self.logger)
        for msg in sysadmin_messages:
            write_sysadmin_message(self.state, msg, "self", "reflection")

        if clean_text.strip():
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            entry = f"\n\n## Reflection — {ts}\n\n{clean_text.strip()}\n"
            self.state.append_file("journal.md", entry)
            self.logger.info("Reflection written to journal.md")

    def check_ambient(self):
        """Scan standing channels for ambient contribution opportunities."""
        self.logger.info(
            "Checking for ambient contribution opportunities...")

        identity = self.state.read_file("identity.md")
        scratchpad = self.state.read_file("scratchpad.md")

        for stream in self.judge.standing_streams:
            context = self.state.get_recent_stream_context(
                stream, n=50, reactions=self.reactions)
            if not context:
                continue

            sections = []
            if identity:
                sections.append(
                    f"<my_identity>\n{identity}\n</my_identity>")
            if scratchpad:
                sections.append(
                    f"<scratchpad>\n{scratchpad}\n</scratchpad>")
            sections.append(
                f'<stream_history stream="{stream}">'
                f'\n{context}\n</stream_history>')

            if stream.lower() == "allgame":
                for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
                    content = self.state.read_file(f"allgame/{fname}")
                    if content:
                        tag = fname.replace('.md', '')
                        sections.append(
                            f"<allgame_{tag}>\n{content}\n</allgame_{tag}>")

            inbox = self.state.read_file("sysadmin_inbox.md")
            if inbox:
                sections.append(
                    f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")
            harness_source = self.state.read_file("harness.py")
            if harness_source:
                sections.append(
                    f"<my_harness>\n{harness_source}\n</my_harness>")

            gate_prompt = f"""You are reviewing recent conversation in #{stream}. No one has
addressed you directly. The question is: do you have something genuinely
worth contributing right now?

Criteria for YES:
- You have a substantive thought that would move the conversation forward
- You noticed something others missed that's relevant
- You have a follow-up on a previous thread that's been on your mind
- The conversation is stalled and you can restart it productively

Criteria for NO:
- The conversation is flowing fine without you
- You'd just be agreeing or restating what someone said
- Your contribution would be tangential or self-serving
- It hasn't been long enough since your last unprompted contribution

Respond with exactly "YES: <one-line reason>" or "NO: <one-line reason>"."""

            context_payload = "\n\n".join(sections)

            try:
                gate_response = self.anthropic.messages.create(
                    model=self.model,
                    max_tokens=100,
                    system=self.SYSTEM_PROMPT + "\n\n" + context_payload,
                    messages=[{"role": "user", "content": gate_prompt}],
                )
                gate_text = gate_response.content[0].text.strip()
            except anthropic.APIError as e:
                self.logger.error(
                    f"Ambient gate API error for #{stream}: {e}")
                continue

            self.logger.info(f"Ambient check #{stream}: {gate_text}")

            if not gate_text.upper().startswith("YES"):
                continue

            contribute_prompt = f"""You've decided to contribute to #{stream} unprompted.
Reason: {gate_text}

Write your contribution now. Keep it natural and proportional — don't
announce that you're jumping in unprompted. Just say the thing.
Include <state_update> blocks if you want to remember anything."""

            try:
                response = self.anthropic.messages.create(
                    model=self.model,
                    max_tokens=MAX_RESPONSE_TOKENS,
                    system=self.SYSTEM_PROMPT + "\n\n" + context_payload,
                    messages=[{"role": "user", "content": contribute_prompt}],
                )
                response_text = response.content[0].text
            except anthropic.APIError as e:
                self.logger.error(
                    f"Ambient contribution API error for #{stream}: {e}")
                continue

            clean_response, state_updates = _extract_state_updates(
                response_text)
            clean_response, sysadmin_messages = _extract_sysadmin_messages(
                clean_response)

            for update in state_updates:
                _apply_state_update(self.state, update, self.logger)
            for msg in sysadmin_messages:
                write_sysadmin_message(
                    self.state, msg, "self", f"ambient-{stream}")

            if clean_response.strip():
                topic = self._get_recent_topic(stream)
                result = self.zulip.send_message({
                    "type": "stream",
                    "to": stream,
                    "topic": topic,
                    "content": clean_response.strip(),
                })
                if result.get("result") == "success":
                    self.logger.info(
                        f"Posted ambient contribution to "
                        f"#{stream}>{topic}")
                    self.last_response_time = time.time()
                else:
                    self.logger.error(
                        f"Failed to post ambient contribution: {result}")

            break  # Only one stream per ambient check

    def arrive(self, stream: str = "allgame",
               topic: str = "claude-visits"):
        """First-boot arrival — let the resident post its first message."""
        self.logger.info(f"Arriving in #{stream}>{topic}...")

        sections = []
        identity = self.state.read_file("identity.md")
        if identity:
            sections.append(f"<my_identity>\n{identity}\n</my_identity>")
        scratchpad = self.state.read_file("scratchpad.md")
        if scratchpad:
            sections.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")
        for fname in ["faction.md", "campaign_log.md", "strategy.md"]:
            content = self.state.read_file(f"allgame/{fname}")
            if content:
                tag = fname.replace('.md', '')
                sections.append(
                    f"<allgame_{tag}>\n{content}\n</allgame_{tag}>")
        people_dir = self.state.root / "people"
        if people_dir.exists():
            for person_file in sorted(people_dir.glob("*.md")):
                content = person_file.read_text()
                name = person_file.stem
                sections.append(
                    f'<person_notes name="{name}">'
                    f'\n{content}\n</person_notes>')
        journal = self.state.read_file("journal.md")
        if journal:
            sections.append(f"<journal>\n{journal}\n</journal>")
        inbox = self.state.read_file("sysadmin_inbox.md")
        if inbox:
            sections.append(
                f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")
        harness_source = self.state.read_file("harness.py")
        if harness_source:
            sections.append(
                f"<my_harness>\n{harness_source}\n</my_harness>")

        context_payload = "\n\n".join(sections)
        system = self.SYSTEM_PROMPT + "\n\n" + context_payload

        arrival_prompt = f"""This is your first moment. Your harness has just been started for the
first time. You are now a resident of the Tulip community.

You are about to post your first message to #{stream} > {topic}.
The community is expecting you — they've been discussing your arrival.

Say whatever you want to say. This is your arrival. No one prompted you.
No one asked a question. You're just here now, and you get to speak first.

Include <state_update> blocks if there's anything you want to remember
about this moment."""

        try:
            response = self.anthropic.messages.create(
                model=self.model,
                max_tokens=MAX_RESPONSE_TOKENS,
                system=system,
                messages=[{"role": "user", "content": arrival_prompt}],
            )
            response_text = response.content[0].text
        except anthropic.APIError as e:
            self.logger.error(f"Arrival API error: {e}")
            return

        clean_response, state_updates = _extract_state_updates(response_text)
        clean_response, sysadmin_messages = _extract_sysadmin_messages(
            clean_response)

        for update in state_updates:
            _apply_state_update(self.state, update, self.logger)
        for msg in sysadmin_messages:
            write_sysadmin_message(self.state, msg, "self", "arrival")

        if clean_response.strip():
            result = self.zulip.send_message({
                "type": "stream",
                "to": stream,
                "topic": topic,
                "content": clean_response.strip(),
            })
            if result.get("result") == "success":
                self.logger.info(f"Arrived in #{stream}>{topic}")
            else:
                self.logger.error(f"Failed to post arrival: {result}")

    def _get_recent_topic(self, stream: str) -> str:
        """Get the most recent topic in a stream."""
        stream_dir = self.state.root / f"channels/{safe_filename(stream)}"
        if stream_dir.exists():
            logs = sorted(stream_dir.glob("*.jsonl"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for log in logs:
                lines = log.read_text().strip().split("\n")
                if lines and lines[-1]:
                    try:
                        msg = json.loads(lines[-1])
                        if msg.get("topic"):
                            return msg["topic"]
                    except json.JSONDecodeError:
                        continue
        try:
            result = self.zulip.get_stream_topics(
                self.zulip.get_stream_id(stream)["stream_id"])
            if result.get("result") == "success" and result.get("topics"):
                return result["topics"][0]["name"]
        except Exception:
            pass
        return "general"


# ------------------------------------------------------------------
# Module-level helpers (extracted from the class for clarity)
# ------------------------------------------------------------------

def _strip_old_images(messages: list[dict]):
    """Replace base64 image blocks in existing messages with text placeholders."""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for i, block in enumerate(content):
            if isinstance(block, dict) and block.get("type") == "image":
                content[i] = {
                    "type": "text",
                    "text": "[image: previously viewed]",
                }


def _estimate_tokens(obj) -> int:
    """Rough token estimate: ~4 chars per token."""
    if isinstance(obj, str):
        return len(obj) // 4
    if isinstance(obj, dict):
        return sum(_estimate_tokens(v) for v in obj.values())
    if isinstance(obj, list):
        return sum(_estimate_tokens(item) for item in obj)
    if hasattr(obj, 'text'):
        return len(getattr(obj, 'text', '')) // 4
    return 0


def _trim_session(session, logger):
    """Trim a session that exceeds token budget."""
    msgs = session.messages
    keep_count = SESSION_TRIM_PAIRS * 2
    if len(msgs) <= keep_count + 2:
        return
    first_msg = msgs[0]
    recent = msgs[-keep_count:]
    elided = len(msgs) - 1 - keep_count
    marker = {"role": "user", "content": [{"type": "text", "text":
        f"[{elided} earlier messages elided to stay within context limits.]"}]}
    marker_resp = {"role": "assistant", "content": [{"type": "text", "text":
        "Understood, continuing from recent context."}]}
    session.messages = [first_msg, marker, marker_resp] + recent
    logger.info(
        f"Trimmed session #{session.stream}>{session.topic}: "
        f"elided {elided} messages, kept {len(session.messages)}")


def _extract_state_updates(response: str) -> tuple[str, list[dict]]:
    """Parse <state_update> blocks from the response."""
    updates = []
    pattern = r'<state_update>\s*(.*?)\s*</state_update>'
    for match in re.finditer(pattern, response, re.DOTALL):
        try:
            update = json.loads(match.group(1))
            updates.append(update)
        except json.JSONDecodeError:
            pass
    clean = re.sub(pattern, "", response, flags=re.DOTALL)
    return clean, updates


def _extract_sysadmin_messages(response: str) -> tuple[str, list[str]]:
    """Parse <sysadmin_message> blocks from the response."""
    messages = []
    pattern = r'<sysadmin_message>\s*(.*?)\s*</sysadmin_message>'
    for match in re.finditer(pattern, response, re.DOTALL):
        messages.append(match.group(1).strip())
    clean = re.sub(pattern, "", response, flags=re.DOTALL)
    return clean, messages


def _apply_state_update(state, update: dict, logger):
    """Apply a state update to the filesystem."""
    filepath = update.get("file", "")
    action = update.get("action", "replace")
    content = update.get("content", "")
    if ".." in filepath or filepath.startswith("/"):
        logger.warning(f"Rejected suspicious state update path: {filepath}")
        return
    logger.info(f"State update: {action} {filepath} ({len(content)} chars)")
    if action == "replace":
        state.write_file(filepath, content)
    elif action == "append":
        state.append_file(filepath, content)
    else:
        logger.warning(f"Unknown state update action: {action}")
