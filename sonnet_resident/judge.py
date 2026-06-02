"""Engagement heuristics — decides whether Claude should respond."""

import json
import logging
from datetime import datetime, timezone


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

    JUDGE_MODEL = "claude-haiku-4-5-20251001"

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
        # Lurk decay: messages since last direct interaction per stream
        self._msgs_since_interaction: dict[str, int] = {}

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
        content = message.get("content", "").lower()
        sender = message.get("sender_full_name", "").lower()

        if self.bot_name in sender:
            self.last_reason = "self-message"
            return False

        # Direct mentions engage — but only in standing streams (or DMs).
        # @-mentions in unsubscribed streams arrive via Zulip's notification
        # delivery but Claude has no channel history and shouldn't reply there.
        if f"@**{self.bot_name}**" in content or f"@{self.bot_name}" in content:
            if stream.lower() not in self.standing_streams:
                self.last_reason = f"@-mention in non-standing stream #{stream} — ignoring"
                return False
            self.last_reason = "direct @-mention"
            self.record_message(stream, is_direct=True)
            return True

        if message.get("type") == "private":
            self.last_reason = "DM"
            return True

        if stream.lower() in self.standing_streams:
            # Mention-only streams: skip ambient judge entirely
            if stream.lower() in self.mention_only_streams:
                self.last_reason = "mention-only stream, no @-mention"
                return False

            # Track this message for lurk decay (not direct)
            self.record_message(stream, is_direct=False)

            if self.anthropic and self.state:
                return self._judge_with_model(message, stream)
            if "claude" in content:
                self.last_reason = "name in standing stream (fallback)"
                return True

        self.last_reason = "no trigger"
        return False

    def _judge_with_model(self, message: dict, stream: str) -> bool:
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
- Claude already spoke recently in this topic and should let others talk
- The message is casual chatter that doesn't need Claude's input
- Someone is sharing something and the appropriate response is to just read it, not comment

Reply with exactly one line: YES or NO, followed by a brief reason.
Example: "YES — interesting topic Claude has perspective on"
Example: "NO — logistics between two humans"
"""

        try:
            response = self.anthropic.messages.create(
                model=self.JUDGE_MODEL,
                max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            answer = response.content[0].text.strip()
            should = answer.upper().startswith("YES")
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
                        "decision": "YES" if should else "NO",
                        "reason": answer,
                        "lurk_level": lurk,
                        "msgs_since_interaction": count,
                    }) + "\n"
                    self.state.append_file("api_archive.jsonl", entry)
                except Exception:
                    pass
            return should
        except Exception as e:
            self.logger.warning(f"Engagement judge model call failed: {e}")
            if "claude" in content.lower():
                self.last_reason = "name in standing stream (model fallback)"
                return True
            self.last_reason = f"model error, no trigger ({e})"
            return False
