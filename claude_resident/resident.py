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
    SESSION_TRIM_PAIRS, TYPING_HOLDOFF_TIMEOUT, ALLGAME_CORE_FILES,
    CACHE_TTL, BURST_SWEEP_ROUNDS, BURST_SWEEP_INTERVAL,
    INTERIM_MESSAGES_MAX_CHARS,
)
from claude_resident.auth import refresh_oauth_token
from claude_resident.state import StateManager
from claude_resident.sessions import SessionManager
from claude_resident.judge import EngagementJudge
from claude_resident.handled import HandledSet
from claude_resident.reactions import emoji_display, handle_reaction_event
from claude_resident.prompt import (
    SYSTEM_PROMPT,
    allgame_context_files,
    build_tiered_system_prompt,
    fingerprint_state,
    compute_state_deltas,
)
from claude_resident.images import youtube_annotations
from claude_resident.sandbox import cleanup_sandbox
from claude_resident.tools.definitions import TOOL_DEFINITIONS
from claude_resident.tools.dispatch import ToolContext, execute_tool
from claude_resident.tools.comms_tools import write_sysadmin_message
from claude_resident.util import (safe_filename, fmt_now,
                                  fmt_log_time, human_gap)


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
        from claude_resident.agent_engine import AgentEngine
        self.agent_engine = AgentEngine(self)
        self._metacog_runner = None
        self._restart_requested = False
        self.logger = logging.getLogger("claude_resident")
        # Reaction tracking (capped to prevent unbounded growth)
        self.reactions: dict[int, dict[str, int]] = {}
        self._msg_index: dict[int, dict] = {}
        self._pending_reactions: dict[tuple[str, str], list[str]] = {}
        self._MSG_INDEX_CAP = 50_000  # logs are foreverlogs now; index scales with them
        # Durable across-restart ledger of message ids already handled, so
        # replay/restart never re-responds to a message we already processed.
        self.handled = HandledSet(self.state)
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

    def handle_message(self, message: dict, followups: list | None = None,
                       sweep=None):
        """Process incoming message(s) with per-topic session persistence.

        `followups` are additional messages from the SAME (stream, topic)
        that stacked up behind the trigger; `sweep()` (provided by the event
        loop) pulls in any more that arrive during the pre-response wait.
        The whole batch lands in ONE turn: the model sees every message
        labeled by sender and chooses the response shape itself — one reply,
        several, or a reaction — instead of the harness forcing one full
        response per queued message.
        """
        msg_type = message.get("type", "stream")
        if msg_type == "stream":
            stream = message.get("display_recipient", "unknown")
            topic = message.get("subject", "")
        else:
            stream = "dm"
            topic = message.get("sender_email", "unknown")

        timestamp = datetime.now(timezone.utc).isoformat()
        bot_name = self.judge.bot_name

        def _prepare(m: dict) -> bool:
            """Durable dedup + logging + indexing for one message. Returns
            False when the message shouldn't ride in the batch (already
            handled, or one of the resident's own echoes)."""
            mid = m.get("id")
            m_sender = m.get("sender_full_name", "unknown")
            m_content = m.get("content", "")
            if mid and mid in self.handled:
                self.logger.info(
                    f"Skipping #{stream}>{topic} from {m_sender}: already "
                    f"handled (msg {mid})")
                return False
            if not (mid and mid in self._msg_index):
                self.state.log_message(stream, topic, m_sender, m_content,
                                       timestamp, msg_id=mid)
            if mid:
                self._msg_index[mid] = {
                    "stream": stream, "topic": topic, "sender": m_sender,
                    "preview": m_content[:80].replace("\n", " "),
                }
                self._cap_indexes()
            if bot_name in m_sender.lower():
                # Own echo — logged/indexed above if needed, never content.
                self.handled.mark(mid)
                return False
            return True

        batch = [m for m in [message, *(followups or [])] if _prepare(m)]
        if not batch:
            return

        # Judge scan: the first message that clears the judge is the trigger.
        # Earlier ones are marked handled (judge-NO, same as before) but stay
        # in the batch as conversational context; everything after the
        # trigger rides along without its own judge pass — we're responding
        # to the turn either way.
        trigger_idx = None
        judge_remark = ""
        for i, m in enumerate(batch):
            if self.judge.should_respond(m, stream):
                trigger_idx = i
                # Capture the System-1 remark now, before anything else can
                # touch the judge.
                judge_remark = getattr(self.judge, "last_remark", "") or ""
                break
            self.logger.info(
                f"Skipping #{stream}>{topic} from "
                f"{m.get('sender_full_name', '?')}: {self.judge.last_reason}")
            self.handled.mark(m.get("id"))
        if trigger_idx is None:
            return
        trigger = batch[trigger_idx]
        sender = trigger.get("sender_full_name", "unknown")

        # --- Minimum pause before any visible action (typing indicator etc).
        # Gives the human a beat to see their own message land. ---
        time.sleep(COOLDOWN_SECONDS)

        # --- Typing hold-off: wait if someone is actively typing ---
        self._wait_for_typing(stream, topic)

        # --- Burst sweep: gather messages that arrived while we waited (or
        # while the previous response was generating). Finding new ones means
        # the burst may still be live — pause a beat and sweep again. ---
        if sweep is not None:
            for _ in range(BURST_SWEEP_ROUNDS):
                new = [m for m in (sweep() or []) if _prepare(m)]
                if not new:
                    break
                batch.extend(new)
                self.logger.info(
                    f"Swept {len(new)} stacked message(s) into the turn")
                time.sleep(BURST_SWEEP_INTERVAL)

        self.logger.info(
            f"Responding to {sender} in #{stream}>{topic}"
            + (f" ({len(batch)} messages batched)" if len(batch) > 1 else ""))

        # --- Session lookup ---
        session = self.sessions.get(stream, topic)
        is_first_message = session is None
        if is_first_message:
            session = self.sessions.create(stream, topic)
        session.touch()
        session.message_count += len(batch)
        for m in batch:
            session.participants.add(m.get("sender_full_name", "unknown"))

        # --- Fingerprint current state ---
        new_fingerprints = fingerprint_state(self.state, stream, sender)

        # --- Staleness check ---
        if not is_first_message:
            # Core identity files plus whatever allgame set is loaded for
            # THIS stream (full core in #allgame, digest/lite elsewhere).
            # Delta-only files (crosstopic, journal tail, postmark) are
            # excluded — they churn by design and shouldn't reset sessions.
            tier23_keys = {"identity", "scratchpad", "inbox",
                           *allgame_context_files(self.state, stream)}
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
                session.participants.add(sender)
                is_first_message = True

        # --- Build system prompt ---
        if is_first_message:
            system = build_tiered_system_prompt(
                self.state, stream, topic, trigger,
                is_first_message=True, reactions=self.reactions)
            session.system_blocks = system
        else:
            system = session.system_blocks

        # --- Build user message ---
        batch_ids = [m.get("id") for m in batch if m.get("id")]
        content_blocks = []
        # Warm sessions never saw the chatter the judge declined between
        # turns — surface it as labeled context so the conversation stays
        # coherent. (Cold starts get it via topic history instead.)
        if not is_first_message and session.last_seen_msg_id:
            interim = self.state.get_topic_messages_since(
                stream, topic, since_id=session.last_seen_msg_id,
                exclude_ids=set(batch_ids), exclude_sender=bot_name,
                max_chars=INTERIM_MESSAGES_MAX_CHARS)
            if interim:
                content_blocks.append({"type": "text", "text":
                    "<interim_messages>\nSaid in this topic since your last "
                    "turn (you already chose not to reply to these — context "
                    "only):\n" + interim + "\n</interim_messages>"})
        # --- Temporal header ---------------------------------------------
        # He had no clock at all: turn prompts carried no timestamp and the
        # system prompt no date, so in a persistent session "five minutes
        # ago" and "yesterday" were indistinguishable (Sentyr kept catching
        # time-tracking errors). Time goes in the PROMPT, never the system
        # block — a clock in the system prompt would invalidate the cached
        # prefix every single turn.
        trigger_ts = trigger.get("timestamp") or time.time()
        header = f"[now: {fmt_now(trigger_ts)}"
        prior_ts = session.last_msg_ts or self.state.previous_message_time(
            stream, topic, exclude_ids=set(batch_ids))
        if prior_ts:
            gap = trigger_ts - prior_ts
            if gap >= 90:
                header += (f" · {human_gap(gap)} since the last message "
                           f"you saw here")
        elif not is_first_message:
            header += " · elapsed time unknown"
        header += "]"
        content_blocks.append({"type": "text", "text": header})

        if len(batch) > 1:
            content_blocks.append({"type": "text", "text":
                f"[{len(batch)} messages arrived together — respond however "
                f"fits: one reply, several, or a reaction.]"})
        for m in batch:
            stamp = fmt_log_time(m.get("timestamp") or trigger_ts)
            # Raw text: image/PDF links stay intact so he can fetch_url
            # them (the MCP bridge returns real image blocks). YouTube
            # links get their title/author appended — that annotation was
            # orphaned on 2026-07-22 when this path switched to raw text,
            # while the system prompt still promised it.
            body = m.get("content", "")
            note = youtube_annotations(body)
            content_blocks.append({
                "type": "text",
                "text": (f"[{stamp}] "
                         f"{m.get('sender_full_name', 'unknown')}: {body}"
                         + (f"\n{note}" if note else ""))})
        if batch_ids:
            session.last_seen_msg_id = max(
                [session.last_seen_msg_id, *batch_ids])
        batch_ts = [m.get("timestamp") for m in batch if m.get("timestamp")]
        if batch_ts:
            session.last_msg_ts = max([session.last_msg_ts, *batch_ts])

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

        # System-1 backchannel: the Haiku engagement judge let this message
        # through and left a note. Appended after the message so it reads as a
        # margin note on what was just received — your reflex layer radioing in.
        if judge_remark:
            remark_text = ("<judge_remark>\n"
                           "A note from your engagement judge (Haiku / your "
                           "System 1), which decided this message was worth "
                           "your attention:\n"
                           f"{judge_remark}\n"
                           "</judge_remark>")
            content_blocks = content_blocks + [
                {"type": "text", "text": remark_text}]

        session.state_fingerprints = new_fingerprints

        # Verbosity signal: if Claude has sent 3+ messages this session,
        # inject average word count so he can see his own drift.
        if len(session.send_word_counts) >= 3:
            avg_words = sum(session.send_word_counts) / len(session.send_word_counts)
            last_words = session.send_word_counts[-1]
            verbosity_note = (
                f"[session stats: your last {len(session.send_word_counts)} "
                f"messages averaged {avg_words:.0f} words "
                f"(last: {last_words})]"
            )
            content_blocks = [
                {"type": "text", "text": verbosity_note}
            ] + content_blocks

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
            internal_text, posted_ids = self.agent_engine.respond(
                session, stream, topic, sender, batch[-1], content_blocks)
        except Exception as e:
            self.logger.error(
                f"Response generation failed: {e}", exc_info=True)
            self.sessions.remove(stream, topic)
            return
        finally:
            typing_stop.set()
            self._send_typing(stream, topic, "stop")

        # The response turn completed (posted or deliberate silence) — record
        # every batched message as handled so a restart never replays them.
        # (On the exception path above we returned without marking, so a
        # crashed turn is still eligible for a retry on the next run.)
        for mid in batch_ids:
            self.handled.mark(mid)

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
