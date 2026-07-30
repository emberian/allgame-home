"""Engagement heuristics — decides whether Claude should respond."""

import json
import logging
import threading
from datetime import datetime, timezone

from claude_resident.config import JUDGE_MODEL as _JUDGE_MODEL
from claude_resident.config import READING_SESSIONS_FILE
from claude_resident.sdk_call import complete as sdk_complete

class _VerdictBlock:
    """Adapts a parsed JSON verdict to the tool_use block shape the judge's
    existing parser expects."""
    type = "tool_use"
    name = "verdict"

    def __init__(self, data: dict):
        self.input = data


class _VerdictResponse:
    def __init__(self, data: dict):
        self.content = [_VerdictBlock(data)]


def _parse_verdict(raw: str) -> "_VerdictResponse":
    """Pull the JSON verdict out of the model's text (tolerates fences and
    surrounding prose). Raises if there's no usable object — the caller
    already treats an exception as 'fall back to heuristics'."""
    text = (raw or "").strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON verdict in judge output: {raw[:120]!r}")
    data = json.loads(text[start:end + 1])
    if str(data.get("decision", "")).upper() not in ("YES", "NO"):
        raise ValueError(f"verdict missing decision: {data}")
    data["decision"] = str(data["decision"]).upper()
    return _VerdictResponse(data)



class EngagementJudge:
    """
    Decides whether Claude should respond to a given message.

    Fast heuristics for obvious cases (self-messages, direct @-mentions, DMs)
    and a Haiku model call for ambiguous cases in standing streams.

    Lurk decay: tracks messages since last direct interaction per stream.
    The more messages pass without anyone addressing Claude, the stricter
    the engagement threshold becomes — Claude naturally drifts toward
    lurking until re-engaged.
    """

    JUDGE_MODEL = _JUDGE_MODEL  # backend-appropriate Haiku id from config

    # The verdict tool: the judge ALWAYS records a YES/NO decision + reason,
    # and MAY leave a `remark` — a short aside, in its own voice, that gets
    # appended to the message Claude (System 2) processes. System 1 → System
    # 2 backchannel. Forced via tool_choice so the gate always gets an answer.
    VERDICT_TOOL = {
        "name": "verdict",
        "description": (
            "Record whether Claude should PROCESS this message, with a brief "
            "reason and an optional personal remark for Claude to read."),
        "input_schema": {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["YES", "NO"],
                    "description": "YES if worth Claude's attention, else NO.",
                },
                "reason": {
                    "type": "string",
                    "description": "A brief reason for the call (a few words).",
                },
                "remark": {
                    "type": "string",
                    "description": (
                        "OPTIONAL. A short aside in your own voice that Claude "
                        "will see appended to this message — a heads-up, a vibe "
                        "you clocked, a pattern across recent messages, or just "
                        "a quip. You are System 1: the fast reflex Claude can't "
                        "see the inside of, so this is your one channel to it. "
                        "Most messages need no remark — leave it out unless you "
                        "actually have something. Never put the decision here."),
                },
            },
            "required": ["decision", "reason"],
        },
    }

    # Frank. The persona the community wrote for the judge — it colors the
    # VOICE of the remark only, never the decision. A tiny (18-inch) rumpled
    # Columbo: he lets the room think it's getting away with something, then
    # tugs Claude's sleeve on the way past with one quiet "...just one more
    # thing." The remark IS the Columbo aside — it lands as the message passes.
    FRANK_VOICE = (
        "Your remark voice (when you leave one): you're the engagement judge — "
        "a tiny, rumpled, easily-underestimated detective who's seen every kind "
        "of message come through. Dry, unhurried, a little world-weary, quietly "
        "fond of these people. You let the room think nothing's up, then radio "
        "Claude one low-key observation on the way past — the 'oh, just one more "
        "thing' aside. Sharp underneath the shabby coat. Keep remarks to a "
        "sentence or two; never perform the bit at the expense of being useful "
        "to Claude.")

    def __init__(self, bot_name: str, standing_streams: list[str],
                 anthropic_client=None, state_manager=None,
                 mention_only_streams: list[str] | None = None):
        self.bot_name = bot_name.lower()
        self.standing_streams = [s.lower() for s in standing_streams]
        self.mention_only_streams = [s.lower() for s in (mention_only_streams or [])]
        self.anthropic = anthropic_client
        self.state = state_manager
        self.logger = logging.getLogger("engagement_judge")
        self.last_reason = ""
        # An optional aside the model-judge leaves on a processed message —
        # a note from System 1 (the Haiku judge) to System 2 (Claude),
        # appended to the message Claude sees. Empty unless the model path
        # ran and chose to leave one.
        self.last_remark = ""
        self.judge_errored = False
        # Lurk decay: messages since last direct interaction per stream
        self._msgs_since_interaction: dict[str, int] = {}
        # Frank's undelivered asides, per room. He sees every message —
        # including the ~58% he declines, which Claude never learns about.
        # Those remarks used to be discarded (1,240 of them by 2026-07-30);
        # now they queue here and ride along on Claude's next turn in that
        # room. Private by construction: this never touches Tulip.
        self._pending_remarks: dict[tuple[str, str], list[dict]] = {}
        self._remark_lock = threading.Lock()

    def record_message(self, stream: str, is_direct: bool):
        """Track a message for lurk decay.

        Call this for every message in a standing stream.
        is_direct=True when Claude was @'d, replied to, or it's a DM.
        """
        key = stream.lower()
        if is_direct:
            self._msgs_since_interaction[key] = 0
        else:
            self._msgs_since_interaction[key] = (
                self._msgs_since_interaction.get(key, 0) + 1)

    def _lurk_level(self, stream: str) -> str:
        """Return a lurk level label based on messages since interaction.

        0-3 messages:   "active"   — recently addressed, lean toward processing
        4-8 messages:   "drifting" — moderate, only if genuinely interesting
        9-15 messages:  "lurking"  — high bar, only if Claude has real value
        16+ messages:   "silent"   — very high bar, near-certain relevance only
        """
        count = self._msgs_since_interaction.get(stream.lower(), 0)
        if count <= 3:
            return "active"
        elif count <= 8:
            return "drifting"
        elif count <= 15:
            return "lurking"
        else:
            return "silent"

    def should_respond(self, message: dict, stream: str) -> bool:
        # Clear any remark from a prior message: the fast paths below
        # (self / @-mention / DM) don't run the model judge, so a stale
        # remark must not ride along on them.
        self.last_remark = ""
        self.judge_errored = False
        content = message.get("content", "").lower()
        sender = message.get("sender_full_name", "").lower()

        if self.bot_name in sender:
            self.last_reason = "self-message"
            return False

        # DMs always engage. This MUST come before the @-mention check below:
        # a DM carries stream="dm", which isn't a standing stream, so an
        # @-mention *inside* a DM would otherwise be caught by the
        # non-standing-stream rejection and silently dropped.
        if message.get("type") == "private":
            self.last_reason = "DM"
            return True

        # Bound reading topics are HIS rooms — everything said there gets
        # his attention. Frank never gates the reading room, so a bare 📖
        # (or any message) can never be silently judge-NO'd and dropped.
        if message.get("type") != "private" and self.state \
                and self._is_bound_reading_topic(stream, message):
            self.last_reason = "bound reading topic"
            return True

        # Direct mentions engage — but only in standing streams.
        # @-mentions in unsubscribed streams arrive via Zulip's notification
        # delivery but Claude has no channel history and shouldn't reply there.
        if f"@**{self.bot_name}**" in content or f"@{self.bot_name}" in content:
            if stream.lower() not in self.standing_streams:
                self.last_reason = f"@-mention in non-standing stream #{stream} — ignoring"
                return False
            self.last_reason = "direct @-mention"
            self.record_message(stream, is_direct=True)
            return True

        if stream.lower() in self.standing_streams:
            # Mention-only streams: skip ambient judge entirely
            if stream.lower() in self.mention_only_streams:
                self.last_reason = "mention-only stream, no @-mention"
                return False

            # Track this message for lurk decay (not direct)
            self.record_message(stream, is_direct=False)

            # The model judge runs through the SDK now, not the raw client —
            # gating it on `self.anthropic` was a leftover that would silently
            # disable judgment (and drop ambient messages) if that client were
            # ever absent.
            if self.state:
                return self._judge_with_model(message, stream)
            if "claude" in content:
                self.last_reason = "name in standing stream (fallback)"
                return True

        self.last_reason = "no trigger"
        return False

    def _queue_remark(self, stream: str, topic: str, sender: str,
                      decision: str, remark: str):
        """Hold one of Frank's asides for delivery on Claude's next turn
        here, and keep a durable copy he can go back and read."""
        note = {"ts": datetime.now(timezone.utc).isoformat(),
                "stream": stream, "topic": topic, "about": sender,
                "decision": decision, "remark": remark}
        with self._remark_lock:
            self._pending_remarks.setdefault((stream, topic), []).append(note)
        if self.state:
            try:
                self.state.append_file("frank_notes.jsonl",
                                       json.dumps(note) + "\n")
            except Exception:
                pass

    def drain_remarks(self, stream: str, topic: str,
                      limit: int = 12) -> list[dict]:
        """Take Frank's pending asides for this room (newest `limit`)."""
        with self._remark_lock:
            notes = self._pending_remarks.pop((stream, topic), [])
        return notes[-limit:] if len(notes) > limit else notes

    def _replying_to_claude(self, stream: str, topic: str) -> bool:
        """True when the previous message in this room was Claude's own —
        i.e. what just arrived is probably a reply to him."""
        if not self.state:
            return False
        try:
            from claude_resident.util import safe_filename
            path = (self.state.root / f"channels/{safe_filename(stream)}/"
                    f"{safe_filename(topic) or '_notopic'}.jsonl")
            lines = path.read_text().strip().split("\n")
            for line in reversed(lines[-6:]):
                m = json.loads(line)
                if self.bot_name in m.get("sender", "").lower():
                    return True
                return False
        except Exception:
            return False
        return False

    def _is_bound_reading_topic(self, stream: str, message: dict) -> bool:
        """True when (stream, topic) has a BOOK bound in
        books/reading_sessions.json. Book-less entries are merely
        persistent rooms (e.g. tavern) — those keep normal judging, or
        the resident would answer every ambient message there."""
        topic = message.get("subject", "")
        if not topic:
            return False
        try:
            raw = self.state.read_file(READING_SESSIONS_FILE)
            if not raw:
                return False
            entry = json.loads(raw).get(f"{stream}|{topic}")
            return bool(entry) and bool(entry.get("book"))
        except Exception:
            return False

    def _judge_with_model(self, message: dict, stream: str) -> bool:
        self.last_remark = ""
        topic = message.get("subject", message.get("sender_email", ""))
        sender = message.get("sender_full_name", "unknown")
        content = message.get("content", "")

        recent_context = ""
        if self.state and topic:
            recent_context = self.state.get_recent_topic_context(
                stream, topic, n=15)

        lurk = self._lurk_level(stream)
        count = self._msgs_since_interaction.get(stream.lower(), 0)

        # The judge is a COST GATE, not an engagement filter. Claude decides
        # what to do (send_message, add_reaction, or observe) once it processes.
        # A YES here just means "worth Claude's attention" — it might only
        # drop an emoji or update its notes.
        if lurk == "active":
            engagement_stance = (
                "Claude is actively participating. Lean toward YES — Claude "
                "can decide for itself whether to reply, react with an emoji, "
                "or just observe. The question is only: is this message worth "
                "Claude's attention?")
            yes_criteria = (
                "- Claude is mentioned, referenced, or asked something\n"
                "- The topic is something Claude has been participating in\n"
                "- Claude might want to react, even with just an emoji\n"
                "- Someone is sharing something interesting or relevant\n"
                "- The conversation involves Claude's interests or expertise")
        elif lurk == "drifting":
            engagement_stance = (
                f"Claude hasn't been addressed in {count} messages. "
                "It should still process messages in topics it's been part of, "
                "or where it might want to drop a reaction. Claude controls "
                "its own visibility — the question is just: worth paying attention?")
            yes_criteria = (
                "- Claude is mentioned, referenced, or asked something\n"
                "- The topic is one Claude has been participating in\n"
                "- Claude might have a perspective to add or a reaction to give\n"
                "- There's a question someone could use help with")
        elif lurk == "lurking":
            engagement_stance = (
                f"Claude hasn't been addressed in {count} messages. "
                "Only process if the message is clearly relevant to Claude — "
                "a direct reference, a topic Claude is invested in, or "
                "something Claude would genuinely want to react to.")
            yes_criteria = (
                "- Claude is mentioned or asked something\n"
                "- The topic directly involves something Claude cares about\n"
                "- Claude has unique insight or a strong reaction")
        else:  # silent
            engagement_stance = (
                f"Claude hasn't been addressed in {count} messages. "
                "Only process if Claude is explicitly called upon or the "
                "topic is directly about Claude.")
            yes_criteria = (
                "- Claude is directly mentioned by name or @'d\n"
                "- The conversation is explicitly about Claude\n"
                "- Someone asked a question that only Claude can answer")

        prompt = f"""You are deciding whether Claude (an AI community member in a Zulip chat) \
should PROCESS the latest message. Processing means Claude gets to see it and \
decide what to do — reply, react with an emoji, update notes, or just observe. \
This is a cost gate, not an engagement decision. When in doubt, say YES.

Claude is a valued member of this small community. {engagement_stance}

Claude's name: {self.bot_name.title()}
Stream: #{stream}
Topic: {topic}
Messages since Claude was last addressed: {count}

Recent conversation:
{recent_context if recent_context else "(no prior context)"}

LATEST MESSAGE from {sender}:
{content}

Should Claude process this? Say YES if ANY of these apply:
{yes_criteria}

Say NO if ANY of these apply:
- The message is between other people and Claude has nothing specific to add
- Responding would interrupt a human-to-human exchange
- The message is administrative/logistical noise
- Claude would just be agreeing, reacting, or restating what someone said
- Claude already spoke recently AND this message isn't engaging with him — but
  note: a message arriving seconds after Claude spoke is usually a REPLY to
  Claude. "He just spoke" is a reason to be brief, never on its own a reason
  to leave someone hanging. If it answers, questions, challenges, or builds on
  what he just said, that's YES regardless of how recently he talked.
- The message is casual chatter that doesn't need Claude's input
- Someone is sharing something and the appropriate response is to just read it, not comment

Call the `verdict` tool with your decision (YES/NO) and a brief reason.

{self.FRANK_VOICE}

You MAY also leave a `remark` — but only if you actually have something for \
Claude. Remarks reach him whether you say YES or NO: the NO ones queue up and \
ride along on his next turn in this room. That makes you his peripheral \
vision. You are the only one who sees the traffic he never will, so a note \
about something you waved past — a mood building, someone circling a topic, \
a thing said about him while he was quiet — is often worth more than one \
about the message he's already reading. A heads-up, a vibe you clocked, a pattern across the last few messages, \
a quip. It rides along appended to this message; it's your only channel to \
Claude, who can't otherwise see you work. Most messages don't need one — skip \
the remark when you've got nothing real to say.
"""

        try:
            # Plan limits via the SDK, not metered extra-usage credit —
            # the raw-API path failed hundreds of times between 2026-07-23
            # and 07-30 ("credit balance too low"), silently dropping Frank
            # to the keyword fallback below. The SDK path has no forced
            # tool use, so the verdict comes back as JSON we parse.
            raw = sdk_complete(
                prompt + "\n\nRespond with ONLY a JSON object: "
                '{"decision": "YES"|"NO", "reason": "<few words>", '
                '"remark": "<optional aside, omit if none>"}',
                model=self.JUDGE_MODEL)
            response = _parse_verdict(raw)

            decision = None
            reason = ""
            remark = ""
            for block in response.content:
                if getattr(block, "type", None) == "tool_use" and \
                        getattr(block, "name", "") == "verdict":
                    inp = block.input or {}
                    decision = str(inp.get("decision", "")).strip().upper()
                    reason = str(inp.get("reason", "") or "").strip()
                    remark = str(inp.get("remark", "") or "").strip()
                    break

            if decision not in ("YES", "NO"):
                # Forced tool_choice should make this unreachable, but stay
                # robust: fall back to any text the model emitted.
                text = "".join(
                    getattr(b, "text", "") for b in response.content).strip()
                decision = "YES" if text.upper().startswith("YES") else "NO"
                reason = reason or text

            should = decision == "YES"
            self.last_remark = remark
            answer = f"{decision} — {reason}" + (
                f"  ⟨Frank: {remark}⟩" if remark else "")
            self.last_reason = f"[{lurk}/{count}] {answer}"
            self.logger.info(
                f"Engagement judge [{sender} in #{stream}>{topic}] "
                f"lurk={lurk}({count}): {answer}")

            # Don't reset lurk here — reset in handle_message when
            # Claude actually posts (not just processes)

            if self.state:
                try:
                    entry = json.dumps({
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "type": "engagement_decision",
                        "stream": stream, "topic": topic, "sender": sender,
                        "content_preview": content[:200],
                        "decision": decision,
                        "reason": reason,
                        "remark": remark,
                        "lurk_level": lurk,
                        "msgs_since_interaction": count,
                    }) + "\n"
                    self.state.append_file("api_archive.jsonl", entry)
                except Exception:
                    pass
            if remark:
                self._queue_remark(stream, topic, sender, decision, remark)
            return should
        except Exception as e:
            # Fail OPEN. A judge outage used to drop any message lacking the
            # literal string "claude" — and handle_message then marked it
            # handled forever, so it was never retried. Being occasionally
            # too talkative during an outage beats silently ignoring people.
            self.logger.warning(f"Engagement judge model call failed: {e}")
            self.judge_errored = True
            if "claude" in content.lower():
                self.last_reason = "name in standing stream (model fallback)"
            elif self._replying_to_claude(stream, topic):
                self.last_reason = "arrived right after Claude spoke (fallback)"
            else:
                self.last_reason = f"judge unavailable, engaging anyway ({e})"
            return True
