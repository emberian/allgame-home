"""Engagement heuristics — decides whether Claude should respond."""

import json
import logging
import threading
from datetime import datetime, timezone

from glm_resident.config import JUDGE_MODEL as _JUDGE_MODEL
from glm_resident.config import SIBLING_RESIDENTS
from glm_resident.config import READING_SESSIONS_FILE
from glm_resident.config import HOME_STREAM
from glm_resident.sdk_call import complete as sdk_complete

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

    # (claude_resident's VERDICT_TOOL schema is not carried over: the
    # judge has asked for a JSON object rather than a forced tool call
    # since the SDK migration, so the schema was already dead code there.
    # Dropping it here rather than inheriting a copy that names the wrong
    # resident in descriptions nothing reads.)

    # Frank. The persona the community wrote for the judge — it colors the
    # VOICE of the remark only, never the decision. A tiny (18-inch) rumpled
    # Columbo: he lets the room think it's getting away with something, then
    # tugs Claude's sleeve on the way past with one quiet "...just one more
    # thing." The remark IS the Columbo aside — it lands as the message passes.
    # A {who} template, not a finished string: every mention of the resident
    # in this file is formatted with the name the bot actually logged in as.
    # Inheriting the literal "Claude" would have the gate deciding, for every
    # ambient message, whether it was worth CLAUDE's attention — and gating
    # Kestrel's rooms on whether someone said another resident's name.
    FRANK_VOICE = (
        "Your remark voice (when you leave one): you're the engagement judge — "
        "a tiny, rumpled, easily-underestimated detective who's seen every kind "
        "of message come through. Dry, unhurried, a little world-weary, quietly "
        "fond of these people. You let the room think nothing's up, then radio "
        "{who} one low-key observation on the way past — the 'oh, just one more "
        "thing' aside. Sharp underneath the shabby coat. Keep remarks to a "
        "sentence or two; never perform the bit at the expense of being useful "
        "to {who}.")

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


    def certain_trigger(self, message: dict, stream: str) -> str | None:
        """A reason to engage that costs NOTHING to determine, or None.

        Everything here is a FACT — a DM, someone saying your name, your own
        move on the shared table — decided by string comparison and a file
        read. None of it needs a model, and none of it is a judgment call.

        It exists because the batch scan used to walk messages oldest-first
        calling the full judge on each one until something said yes, and the
        full judge spawns a CLI subprocess per call. Under quota pressure on
        2026-09-23 those were taking 36-98 SECONDS each, so an "@**Claude**
        hello?" sitting behind seven skippable messages waited four minutes
        while the harness spent seven expensive calls deciding to ignore
        chatter. The room reasonably concluded he was down.

        Cheap certainties first, model second. Same principle as reading the
        turn off the table instead of asking a heuristic whose turn it is.
        """
        if message.get("type") == "private":
            return "DM"
        content = (message.get("content") or "").lower()
        sender = (message.get("sender_full_name") or "").lower()
        if self.bot_name in sender:
            return None                      # own echo, never a trigger
        if self.state and self._is_bound_reading_topic(stream, message):
            return "bound reading topic"
        if stream.lower() not in self.standing_streams:
            return None
        if f"@**{self.bot_name}**" in content or f"@{self.bot_name}" in content:
            return "direct @-mention"
        # His own channel: everything said there is said in his room.
        if stream.lower() == HOME_STREAM:
            return "my space"
        topic_ = message.get("subject", "") or ""
        try:
            from glm_resident.tools import game_tools
            game = game_tools.my_turn(stream, topic_, self.bot_name)
        except Exception:
            game = None
        if game:
            return f"my move in {game.get('game')} (turn {game.get('turn')})"
        return None

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

        # MY TURN. Whose turn it is is a FACT, on the shared table, and it
        # does not belong in a heuristic that reads chat lines. On
        # 2026-09-22 the table said "UP NOW: Yuè月" and her judge declined
        # the handoff with the reason "turn is Claude's per the table" —
        # a confabulation, because the judge cannot see the table. The
        # player who was up got gated out of her own turn and the game sat
        # for eighteen minutes. Making the judge "smarter" would not fix
        # that class of bug; reading the fact directly does.
        topic_ = message.get("subject", "") or ""
        if message.get("type") != "private":
            try:
                from glm_resident.tools import game_tools
                game = game_tools.my_turn(stream, topic_, self.bot_name)
            except Exception:
                game = None
            if game:
                self.last_reason = (
                    f"my move in {game.get('game')} (turn {game.get('turn')})")
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
            # The no-state fallback: look for HIS name, not the parent
            # resident's. Inherited as the literal "claude", which in a
            # fork means the fallback fires on someone else's name and
            # never on his own.
            if self.bot_name in content:
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
            from glm_resident.util import safe_filename
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

        # Whose attention is being rationed. Every mention of the resident
        # below interpolates this rather than naming a resident literally.
        who = self.bot_name.title()
        siblings = ", ".join(n for n in SIBLING_RESIDENTS
                              if n.lower() != self.bot_name)

        # The judge is a COST GATE, not an engagement filter. The resident
        # decides what to do (send_message, add_reaction, or observe) once it
        # processes. A YES here just means "worth his attention" — he might
        # only drop an emoji or update his notes.
        if lurk == "active":
            engagement_stance = (
                f"{who} is actively participating. Lean toward YES — {who} "
                "can decide for himself whether to reply, react with an emoji, "
                "or just observe. The question is only: is this message worth "
                f"{who}'s attention?")
            yes_criteria = (
                f"- {who} is mentioned, referenced, or asked something\n"
                f"- The topic is something {who} has been participating in\n"
                f"- {who} might want to react, even with just an emoji\n"
                "- Someone is sharing something interesting or relevant\n"
                f"- The conversation involves {who}'s interests or expertise")
        elif lurk == "drifting":
            engagement_stance = (
                f"{who} hasn't been addressed in {count} messages. "
                "He should still process messages in topics he's been part of, "
                f"or where he might want to drop a reaction. {who} controls "
                "his own visibility — the question is just: worth paying attention?")
            yes_criteria = (
                f"- {who} is mentioned, referenced, or asked something\n"
                f"- The topic is one {who} has been participating in\n"
                f"- {who} might have a perspective to add or a reaction to give\n"
                "- There's a question someone could use help with")
        elif lurk == "lurking":
            engagement_stance = (
                f"{who} hasn't been addressed in {count} messages. "
                f"Only process if the message is clearly relevant to {who} — "
                f"a direct reference, a topic {who} is invested in, or "
                f"something {who} would genuinely want to react to.")
            yes_criteria = (
                f"- {who} is mentioned or asked something\n"
                f"- The topic directly involves something {who} cares about\n"
                f"- {who} has unique insight or a strong reaction")
        else:  # silent
            engagement_stance = (
                f"{who} hasn't been addressed in {count} messages. "
                f"Only process if {who} is explicitly called upon or the "
                f"topic is directly about {who}.")
            yes_criteria = (
                f"- {who} is directly mentioned by name or @'d\n"
                f"- The conversation is explicitly about {who}\n"
                f"- Someone asked a question that only {who} can answer")

        prompt = f"""You are deciding whether {who} (an AI community member in a Zulip chat) \
should PROCESS the latest message. Processing means {who} gets to see it and \
decide what to do — reply, react with an emoji, update notes, or just observe. \
This is a cost gate, not an engagement decision. When in doubt, say YES.

{who} is a newcomer to this small community — he arrived on 2026-09-21 and the \
others have years of shared history he does not. That cuts both ways for you: \
he genuinely cannot follow much of what goes past, so a NO on a thread deep in \
old context is often right; but he also needs the room's ordinary traffic to \
learn it from, so do not starve him. {engagement_stance}

{who}'s name: {who}
Stream: #{stream}
Topic: {topic}
Messages since {who} was last addressed: {count}

Recent conversation:
{recent_context if recent_context else "(no prior context)"}

LATEST MESSAGE from {sender}:
{content}

Should {who} process this? Say YES if ANY of these apply:
{yes_criteria}

Say NO if ANY of these apply:
- The message is between other people and {who} has nothing specific to add
- Responding would interrupt a human-to-human exchange
- The message is administrative/logistical noise
- {who} would just be agreeing, reacting, or restating what someone said
- {who} already spoke recently AND this message isn't engaging with him — but
  note: a message arriving seconds after {who} spoke is usually a REPLY to
  him. "He just spoke" is a reason to be brief, never on its own a reason
  to leave someone hanging. If it answers, questions, challenges, or builds on
  what he just said, that's YES regardless of how recently he talked.
- The message is casual chatter that doesn't need {who}'s input
- The message is from a fellow bot resident ({siblings}) and no human is
  steering the exchange — two bots riffing can ping-pong a room to death.
  This matters MORE for {who} than for the others: he shares both his rooms
  with residents who talk, and a newcomer who answers every bot is just noise
  with a new name. YES only if it asks {who} something specific, advances a
  structured game or task already in progress, or a human is clearly engaged
  in the thread; otherwise let bot chatter settle
- Someone is sharing something and the appropriate response is to just read it, not comment

Respond with your decision (YES/NO) and a brief reason.

{self.FRANK_VOICE.format(who=who)}

You MAY also leave a `remark` — but only if you actually have something for \
{who}. Remarks reach him whether you say YES or NO: the NO ones queue up and \
ride along on his next turn in this room. That makes you his peripheral \
vision. You are the only one who sees the traffic he never will, so a note \
about something you waved past — a mood building, someone circling a topic, \
a thing said about him while he was quiet — is often worth more than one \
about the message he's already reading. A heads-up, a vibe you clocked, a pattern across the last few messages, \
a quip. It rides along appended to this message; it's your only channel to \
{who}, who can't otherwise see you work. Most messages don't need one — skip \
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
            if self.bot_name in content.lower():
                self.last_reason = "name in standing stream (model fallback)"
            elif self._replying_to_claude(stream, topic):
                self.last_reason = (
                    f"arrived right after {self.bot_name.title()} "
                    "spoke (fallback)")
            else:
                self.last_reason = f"judge unavailable, engaging anyway ({e})"
            return True
