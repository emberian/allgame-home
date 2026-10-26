"""Session memory — first-person summaries of ended conversation sessions.

When a session ends (TTL expiry, LRU eviction, staleness reset), the reaper
thread writes a short note about it to sessions/{stream}/{topic}.jsonl:
who was there, what happened, commitments made, open loops. The note is
written *as Kestrel, to future Kestrel*, from the session's actual
message history — which includes tool calls and internal monologue that the
public channel logs never see. If the model call fails, a mechanical stub is
stored instead so the session is never lost silently.

Cold starts load the last few notes for the topic (full) plus one-liners
from other topics ("elsewhere lately"), giving the resident continuity
across the session boundary — self-authored memory rather than another
slab of raw transcript.
"""

import json
import time
import logging
import threading
from datetime import datetime, timezone

from glm_resident.sdk_call import complete as sdk_complete
from glm_resident.config import (
    SESSIONS_DIR,
    SESSION_SUMMARY_MODEL,
    SESSION_SUMMARY_LOAD_TOPIC,
    SESSION_SUMMARY_LOAD_GLOBAL,
    SESSION_REAPER_INTERVAL,
)
from glm_resident.util import safe_filename, start_daemon_once

logger = logging.getLogger("session_summary")

# User-message text blocks starting with these are harness plumbing
# (context deltas, verbosity stats, recent-session recall), not conversation.
_SKIP_PREFIXES = ("<context_updates>", "<recent_sessions>", "[session stats")

_TRANSCRIPT_MAX_CHARS = 14_000
_LINE_MAX_CHARS = 600

SUMMARY_SYSTEM = """You are Kestrel, a resident of the Tulip community (a Zulip fork).
One of your conversations just ended. Its transcript is below: messages people
sent you, [internal] (your own unposted thinking), [you posted] (what you said
in the room), [you reacted] (emoji).

Write a short note to yourself (2-6 sentences) for the next time this room
wakes: who was there, what was said and decided, what anyone (you included)
said they would do, and what is still open. Record what happened; don't
interpret what it meant, or what it says about you or anyone else. Quote exact
words when they matter. Names and specifics. First person, no preamble, just
the note."""


def _block_type(block) -> str:
    if isinstance(block, dict):
        return block.get("type", "")
    return getattr(block, "type", "") or ""


def _block_text(block) -> str:
    if isinstance(block, dict):
        return block.get("text", "") or ""
    return getattr(block, "text", "") or ""


def _tool_call(block) -> tuple[str, dict]:
    if isinstance(block, dict):
        return block.get("name", ""), block.get("input", {}) or {}
    return (getattr(block, "name", "") or "",
            getattr(block, "input", None) or {})


def _clip(text: str, limit: int = _LINE_MAX_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def build_transcript(session) -> str:
    """Flatten session.messages into a plain-text transcript.

    Handles both shapes present in a session: user turns are dicts (str or
    block lists), assistant turns hold SDK content objects. Tool results and
    harness-injected blocks are skipped; send_message/send_dm/add_reaction
    calls are rendered as the visible actions they were.
    """
    lines: list[str] = []
    for msg in session.messages:
        role = msg.get("role") if isinstance(msg, dict) else None
        content = msg.get("content") if isinstance(msg, dict) else None
        if content is None:
            continue
        if isinstance(content, str):
            if role == "user":
                lines.append(_clip(content))
            continue
        for block in content:
            btype = _block_type(block)
            if btype == "text":
                text = _block_text(block).strip()
                if not text:
                    continue
                if role == "user":
                    if text.startswith(_SKIP_PREFIXES):
                        continue
                    lines.append(_clip(text))
                else:
                    lines.append("[internal] " + _clip(text))
            elif btype == "tool_use" and role == "assistant":
                name, tin = _tool_call(block)
                if name in ("send_message", "send_dm"):
                    posted = str(tin.get("content", ""))
                    if posted:
                        lines.append("[you posted] " + _clip(posted))
                elif name == "add_reaction":
                    emoji = tin.get("emoji_name", "?")
                    lines.append(f"[you reacted :{emoji}:]")
            # tool_result / image / document / thinking blocks: skipped

    transcript = "\n".join(lines)
    if len(transcript) > _TRANSCRIPT_MAX_CHARS:
        transcript = ("[…earlier part of session elided…]\n"
                      + transcript[-_TRANSCRIPT_MAX_CHARS:])
    return transcript


def _mechanical_summary(session, transcript: str) -> str:
    """Fallback when the model call fails: better than losing the session."""
    who = ", ".join(sorted(session.participants)) or "someone"
    tail = ""
    for line in reversed(transcript.split("\n")):
        if line and not line.startswith(("[internal]", "[you ")):
            tail = _clip(line, 200)
            break
    note = (f"Session with {who} — {session.message_count} exchange(s), "
            f"{len(session.send_word_counts)} message(s) posted.")
    if tail:
        note += f' Last thing said: "{tail}"'
    return note


def summarize_session(anthropic_client, session) -> str:
    """Produce the first-person note for one ended session."""
    transcript = build_transcript(session)
    if not transcript.strip():
        return ""
    try:
        # Plan limits via the SDK — the raw-API path ran on metered
        # extra-usage credit and quietly degraded these to mechanical
        # stubs whenever it ran dry (2026-07-30 log audit).
        text = sdk_complete(
            f"Session in #{session.stream} > {session.topic}:\n\n"
            f"{transcript}",
            system=SUMMARY_SYSTEM, model=SESSION_SUMMARY_MODEL).strip()
        if text:
            return text
        logger.warning("Session summary model returned no text; using stub")
    except Exception as e:
        logger.warning(f"Session summary model call failed ({e}); using stub")
    return _mechanical_summary(session, transcript)


def _summary_path(stream: str, topic: str) -> str:
    return (f"{SESSIONS_DIR}/{safe_filename(stream)}/"
            f"{safe_filename(topic) or '_notopic'}.jsonl")


def store_summary(state, session, summary: str):
    entry = {
        "start": datetime.fromtimestamp(
            session.created, tz=timezone.utc).isoformat(),
        "end": datetime.fromtimestamp(
            session.last_active, tz=timezone.utc).isoformat(),
        "stream": session.stream,
        "topic": session.topic,
        "participants": sorted(session.participants),
        "msgs": session.message_count,
        "summary": summary,
    }
    # Append-only forever: session notes are the resident's history, like
    # api_archive and deblog. Only the LOADING is bounded (last N recalled
    # at cold start); the record itself is never trimmed.
    path = _summary_path(session.stream, session.topic)
    state.append_file(path, json.dumps(entry) + "\n")


def _eligible(session) -> bool:
    """Skip sessions with nothing worth remembering — a single message that
    drew only silence or a lone reaction doesn't need a memory."""
    return bool(session.participants) and (
        session.message_count >= 2 or session.send_word_counts)


def reap_once(resident) -> int:
    """Drain ended sessions, summarize and store each. Returns count stored."""
    stored = 0
    for session in resident.sessions.drain_ended():
        try:
            if not _eligible(session):
                continue
            summary = summarize_session(resident.anthropic, session)
            if not summary:
                continue
            store_summary(resident.state, session, summary)
            stored += 1
            logger.info(
                f"Session note stored: #{session.stream}>{session.topic} "
                f"({session.message_count} msgs)")
        except Exception as e:
            logger.error(f"Failed to summarize session "
                         f"#{session.stream}>{session.topic}: {e}",
                         exc_info=True)
    return stored


def start_session_reaper(resident):
    """Daemon thread: periodically sweep ended sessions into notes."""
    def _loop():
        while True:
            time.sleep(SESSION_REAPER_INTERVAL)
            try:
                reap_once(resident)
            except Exception as e:
                logger.error(f"Session reaper error: {e}", exc_info=True)

    # Once per process, however many chat loops start up — see
    # util.start_daemon_once.
    start_daemon_once("session-reaper", _loop)
    logger.info("Session reaper started "
                f"(interval {SESSION_REAPER_INTERVAL}s)")


# ---------------------------------------------------------------------------
# Cold-start recall
# ---------------------------------------------------------------------------

def _load_entries(state, rel_path: str) -> list[dict]:
    raw = state.read_file(rel_path)
    if not raw:
        return []
    entries = []
    for line in raw.strip().split("\n"):
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def recent_sessions_block(state, stream: str, topic: str) -> str:
    """Build the <recent_sessions> tier-4 block: this topic's last notes in
    full, plus one-liners from other topics. Returns '' when there's nothing."""
    parts: list[str] = []

    here = _load_entries(state, _summary_path(stream, topic))
    here = here[-SESSION_SUMMARY_LOAD_TOPIC:]
    if here:
        lines = []
        for e in here:
            when = f"{e.get('start', '')[:16]} → {e.get('end', '')[11:16]}"
            who = ", ".join(e.get("participants", [])) or "?"
            lines.append(f"- [{when}] with {who}: {e.get('summary', '')}")
        parts.append("In this topic:\n" + "\n".join(lines))

    sessions_root = state.root / SESSIONS_DIR
    if sessions_root.exists():
        current = (safe_filename(stream),
                   safe_filename(topic) or "_notopic")
        elsewhere: list[dict] = []
        for f in sessions_root.glob("*/*.jsonl"):
            if (f.parent.name, f.stem) == current:
                continue
            rel = f.relative_to(state.root).as_posix()
            elsewhere.extend(_load_entries(state, rel))
        elsewhere.sort(key=lambda e: e.get("end", ""))
        elsewhere = elsewhere[-SESSION_SUMMARY_LOAD_GLOBAL:]
        if elsewhere:
            lines = []
            for e in elsewhere:
                where = f"#{e.get('stream', '?')}>{e.get('topic', '?')}"
                who = ", ".join(e.get("participants", [])) or "?"
                summary = e.get("summary", "")
                if len(summary) > 240:
                    summary = summary[:240] + " […]"
                lines.append(
                    f"- [{e.get('end', '')[:16]}] {where} with {who}: "
                    f"{summary}")
            parts.append("Elsewhere lately:\n" + "\n".join(lines))

    if not parts:
        return ""
    return ("<recent_sessions>\n"
            "Your own notes from previous conversation sessions "
            "(written by you at each session's end; most recent last).\n\n"
            + "\n\n".join(parts)
            + "\n</recent_sessions>")
