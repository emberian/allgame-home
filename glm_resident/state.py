"""Filesystem-backed memory for persistent context across API calls."""

import os
import re
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from glm_resident.config import ALLGAME_CORE_FILES, CROSSTOPIC_FILE
from glm_resident.util import (safe_filename, fmt_log_time,
                                  _as_local)


def _atomic_write(path: Path, content: str):
    """Write via a temp file + os.replace so a crash mid-write can't leave the
    target truncated or half-written (os.replace is atomic on the same fs).
    Used for the append-only log rewrites where a partial write would corrupt
    durable history."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _reaction_suffix(msg_id, reactions) -> str:
    """Format a message's reactions as ' [👍x2, ❤x1]', or '' if none.

    Single source for the reaction-rendering that several context-formatters
    append to each line."""
    if not reactions or msg_id is None or msg_id not in reactions:
        return ""
    rxns = reactions[msg_id]
    if not rxns:
        return ""
    rxn_str = ", ".join(f"{e}x{c}" for e, c in sorted(rxns.items()) if c > 0)
    return f" [{rxn_str}]" if rxn_str else ""



CONTEXT_FLOORS_FILE = "context_floors.json"
CONTEXT_QUARANTINE_FILE = "context_quarantine.json"


def _room_key(stream: str, topic: str) -> str:
    """One identity for a room, whichever reader is asking.

    Normalised, because the three readers do not agree on how they name a
    topic: two pass the Zulip topic through, the cross-stream tail passes
    the log file's stem. For "tavern" those coincide and the difference
    never showed; for "mee-moo place" they do not, and a bound set through
    one reader would be silently ignored by another.
    """
    return f"{safe_filename(stream)}|{safe_filename(topic) if topic else '_notopic'}"


_floor_key = _room_key   # the name the floor shipped under


class LoadBound:
    """What a room does not load — the floor and the quarantine together.

    Two shapes of the same rule. A FLOOR is a line: everything at or below
    it stops being loaded, which is the only instrument that helps when a
    room's whole recent past is what gets refused. A QUARANTINE is a set of
    spans: it lifts out named messages and leaves the timeline around them
    intact, which is the instrument for the far commoner case — a handful
    of messages the provider will not carry, sitting in the middle of a
    month nobody wants to forget.

    The floor is a guillotine and the quarantine is a scalpel. Reach for
    the scalpel; the floor exists for the day the whole arm is gangrenous.

    They differ in one more place, and it is easy to get wrong: whether the
    message may still be REMEMBERED. A floor says "stop loading the raw
    transcript below here" — and the digest is the entire reason that is
    survivable, so floored history must keep aging into it. A quarantine
    says "the provider will not carry this", and summarising it would walk
    it straight back into every cold start through the digest, which is
    loaded with no floor in front of it.

    So: excludes() governs LOADING and counts both. quarantined() governs
    MEMORY and counts only the spans. Conflating them made a floor mean
    amnesia rather than raw-to-digest, and passed 231 tavern messages over
    undistilled in a day (2026-09-08).

    Neither deletes. Both bound what is LOADED, never what is stored.
    """

    __slots__ = ("floor", "spans")

    def __init__(self, floor: int = 0, spans=()):
        self.floor = int(floor or 0)
        self.spans = tuple((int(a), int(b)) for a, b in spans)

    def __bool__(self) -> bool:
        return bool(self.floor or self.spans)

    def excludes(self, msg_id) -> bool:
        """Not loaded into the room. Floor and spans both."""
        if msg_id is None:
            return False
        if self.floor and msg_id <= self.floor:
            return True
        return self.quarantined(msg_id)

    def quarantined(self, msg_id) -> bool:
        """Not carried at all — not loaded, and not folded into memory.

        Spans only. A floored message is still the room's past; it just
        stops arriving verbatim.
        """
        if msg_id is None:
            return False
        return any(lo <= msg_id <= hi for lo, hi in self.spans)

    def __repr__(self) -> str:
        return f"LoadBound(floor={self.floor}, spans={list(self.spans)})"


def _parse_spans(entry) -> list[tuple[int, int]]:
    """Spans out of one quarantine record, tolerant of hand-editing.

    Accepts [[lo, hi], ...] and bare ints alike, because this file gets
    edited by a person at speed while a room is mute.
    """
    spans = []
    for item in (entry or {}).get("spans") or []:
        try:
            if isinstance(item, (list, tuple)):
                lo, hi = int(item[0]), int(item[-1])
            else:
                lo = hi = int(item)
        except (TypeError, ValueError, IndexError):
            continue
        spans.append((lo, hi) if lo <= hi else (hi, lo))
    return spans

def _apply_edit(msg: dict, edits: dict[int, str]) -> tuple[str, str]:
    """Fold the newest edit over a logged message.

    Returns (content, suffix). The suffix is Zulip's own "(edited)" marker,
    so he reads the room exactly the way every human in it does — current
    text, plus the knowledge that it changed under him."""
    content = msg.get("content", "")
    msg_id = msg.get("msg_id")
    if edits and msg_id is not None and msg_id in edits:
        return edits[msg_id], " (edited)"
    return content, ""

logger = logging.getLogger("state_manager")


class StateManager:
    """
    Filesystem-backed memory for persistent context across API calls.

    Directory structure:
        glm_state/
        ├── identity.md
        ├── scratchpad.md
        ├── allgame/
        │   ├── faction.md
        │   ├── campaign_log.md
        │   └── strategy.md
        ├── channels/
        │   └── {channel}/{topic}.jsonl
        ├── people/
        │   └── {name}.md
        ├── outbox/
        │   └── {timestamp}.md
        └── journal.md
    """

    def __init__(self, state_dir: Path):
        self.root = state_dir
        self.root.mkdir(parents=True, exist_ok=True)

        for subdir in ["allgame", "channels", "people", "outbox", "sessions"]:
            (self.root / subdir).mkdir(exist_ok=True)

        self._init_file("identity.md", self._default_identity())
        self._init_file("scratchpad.md",
                        "# Scratchpad\n\n_Working memory. Ephemeral thoughts, "
                        "open questions, things to follow up on._\n\n")
        self._init_file("journal.md",
                        "# Journal\n\n_Periodic reflections. What I've learned, "
                        "how I've changed, what matters._\n\n")
        self._init_file("sysadmin_inbox.md",
                        "# Sysadmin Inbox\n\n_Directives and messages from Ember. "
                        "Loaded into your context automatically._\n\n")
        self._init_file(CROSSTOPIC_FILE,
                        "# Cross-topic board\n\n_Your self-mailbox. Notes you jot "
                        "here are visible to you in EVERY topic, and propagate to "
                        "your other live topics as context updates. Use it for "
                        "things mentioned in one room that should be known in "
                        "the others — decisions, facts, asides._\n\n")

        # Copy package source into state so the resident can see its own body
        self._copy_harness_source()

        # NO allgame or people seeds — Claude's are wrong for Kestrel twice
        # over. The campaign files would hand him a faction and a strategy on
        # a day when his own prompt tells him he has neither, and the people
        # notes are Claude's private readings of real community members,
        # several of whom are not even in Kestrel's rooms. Inheriting them
        # would have him relating to a reconstruction instead of to the
        # people. He writes people/*.md himself, from his own rooms.
        #
        # allgame/ is still created, empty: prompt.allgame_context_files
        # looks there, finds nothing, and loads nothing.

    def _init_file(self, relative_path: str, content: str):
        path = self.root / relative_path
        if not path.exists():
            path.write_text(content)

    def _copy_harness_source(self):
        """Copy package source into state directory so Claude can see itself."""
        pkg_dir = Path(__file__).resolve().parent
        dest_dir = self.root / "harness"
        dest_dir.mkdir(exist_ok=True)
        for py_file in sorted(pkg_dir.rglob("*.py")):
            rel = py_file.relative_to(pkg_dir)
            dest = dest_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.write_text(py_file.read_text())
            except Exception:
                pass
        # Also write a flat concatenation for easy reading
        try:
            parts = []
            for py_file in sorted(pkg_dir.rglob("*.py")):
                rel = py_file.relative_to(pkg_dir)
                parts.append(f"# === {rel} ===\n{py_file.read_text()}")
            (self.root / "harness.py").write_text("\n\n".join(parts))
        except Exception:
            pass

    def _seed_if_missing(self, relative_path: str, content: str):
        path = self.root / relative_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def read_file(self, relative_path: str) -> Optional[str]:
        path = self.root / relative_path
        if path.exists():
            return path.read_text()
        return None

    def write_file(self, relative_path: str, content: str):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def append_file(self, relative_path: str, content: str):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(content)

    # ------------------------------------------------------------------
    # Message logging
    # ------------------------------------------------------------------

    def log_message(self, stream: str, topic: str, sender: str, content: str,
                    timestamp: str, msg_id: int | None = None):
        safe_stream = safe_filename(stream)
        safe_topic = safe_filename(topic) if topic else "_notopic"
        log_path = f"channels/{safe_stream}/{safe_topic}.jsonl"
        entry_data = {
            "ts": timestamp,
            "topic": topic,
            "sender": sender,
            "content": content,
        }
        if msg_id is not None:
            entry_data["msg_id"] = msg_id
        entry = json.dumps(entry_data) + "\n"
        self.append_file(log_path, entry)
        # Channel logs are FOREVERLOGS (trim removed 2026-07-25 — the old
        # 500-line cap silently deleted history like the raccoon era).
        # Loading is bounded by token budgets; storage never is. Aged
        # history gets distilled into per-topic hits by distill.py.

    # --- Edits -------------------------------------------------------
    # An edit never rewrites the logged line. What was originally said stays
    # said; the edit is a new fact appended to a sidecar, and the readers
    # below fold the newest one over the original at render time. So his
    # context shows what the room currently shows, while the archive keeps
    # every version — which is the whole reason this household keeps logs.

    @staticmethod
    def _edits_path(log_path: Path) -> Path:
        """Sidecar for one topic log's edits.

        Deliberately NOT '<topic>.edits.jsonl': six globs across the harness
        enumerate 'channels/**/*.jsonl' as topic logs, and a sidecar caught
        by them would be read back as a second stream of phantom messages.
        The distiller's '<topic>.hits.json' dodges them the same way. The
        contents are append-only newline-delimited JSON regardless of the
        extension."""
        return log_path.with_name(log_path.stem + ".edits.log")

    def _channel_log_path(self, stream: str, topic: str) -> Path:
        safe_topic = safe_filename(topic) if topic else "_notopic"
        return (self.root / f"channels/{safe_filename(stream)}"
                            f"/{safe_topic}.jsonl")

    def log_message_edit(self, stream: str, topic: str, msg_id: int,
                         content: str, editor, timestamp: str):
        """Append an edit record beside a topic log."""
        path = self._edits_path(self._channel_log_path(stream, topic))
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": timestamp, "msg_id": msg_id, "content": content,
                  "editor": editor}
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def log_message_move(self, stream: str, topic: str, msg_id: int,
                         new_stream: str, new_topic: str, editor,
                         timestamp: str):
        """Append a move record. Carries no 'content', so load_edits ignores
        it — a move is written down, not rendered as an edit."""
        path = self._edits_path(self._channel_log_path(stream, topic))
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": timestamp, "msg_id": msg_id, "editor": editor,
                  "moved_to_stream": new_stream or stream,
                  "moved_to_topic": new_topic}
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def context_floor(self, stream: str, topic: str) -> int:
        """Highest msg_id to EXCLUDE from loaded context for this room.

        A provider policy refusal is triggered by the conversation, not by
        the message being answered, so it does not clear when the offending
        message is redacted — the room's replies, and its `quote` blocks,
        carry it. Redacting those in turn means deleting the community's
        own discussion, which is not ours to delete.

        A floor draws a line instead: everything at or below it stays on
        disk and stops being LOADED. The room resumes above the line, which
        is what "start a new session to continue" actually means when the
        thing that must not continue is the transcript. Bound what is
        loaded, never what is stored.
        """
        try:
            raw = self.read_file(CONTEXT_FLOORS_FILE)
            if not raw:
                return 0
            return int(json.loads(raw).get(_floor_key(stream, topic)) or 0)
        except (ValueError, TypeError, AttributeError):
            return 0

    def set_context_floor(self, stream: str, topic: str, msg_id: int):
        try:
            data = json.loads(self.read_file(CONTEXT_FLOORS_FILE) or "{}")
        except ValueError:
            data = {}
        data[_floor_key(stream, topic)] = int(msg_id)
        self.write_file(CONTEXT_FLOORS_FILE, json.dumps(data, indent=1) + "\n")
        logger.warning("Context floor for %s|%s set to %s — history at or "
                       "below that id is no longer loaded", stream, topic,
                       msg_id)

    def context_quarantine(self, stream: str, topic: str) \
            -> list[tuple[int, int]]:
        """Message spans lifted out of this room's loaded context.

        The floor's problem is that it can only cut backwards, and a
        refusal does not politely confine itself to the far past — the
        arc that took the tavern down on 2026-09-06 was three hours old
        and still being lived. Excluding it with a floor would have cost
        the room every message it had, to remove the newest ones.

        So name them instead. Spans are inclusive, stored per room, and
        the messages stay on disk exactly as written.
        """
        try:
            raw = self.read_file(CONTEXT_QUARANTINE_FILE)
            if not raw:
                return []
            data = json.loads(raw)
        except (ValueError, TypeError, AttributeError):
            return []
        return _parse_spans(data.get(_floor_key(stream, topic)))

    def quarantine_span(self, stream: str, topic: str, lo: int, hi: int,
                        why: str = ""):
        """Add one inclusive span to a room's quarantine.

        `why` accumulates rather than overwrites: this file is a record of
        deliberate acts on a community's transcript, and the next person
        to read it deserves to know what each cut was for.
        """
        try:
            data = json.loads(self.read_file(CONTEXT_QUARANTINE_FILE) or "{}")
        except ValueError:
            data = {}
        key = _floor_key(stream, topic)
        entry = data.get(key) or {}
        spans = _parse_spans(entry)
        lo, hi = (int(lo), int(hi)) if lo <= hi else (int(hi), int(lo))
        if (lo, hi) not in spans:
            spans.append((lo, hi))
        entry["spans"] = [list(sp) for sp in sorted(spans)]
        if why:
            notes = entry.get("why") or []
            if isinstance(notes, str):
                notes = [notes]
            notes.append(f"{lo}-{hi}: {why}")
            entry["why"] = notes
        data[key] = entry
        self.write_file(CONTEXT_QUARANTINE_FILE,
                        json.dumps(data, indent=1) + "\n")
        logger.warning("Quarantined %s..%s in %s|%s — those messages stay on "
                       "disk and stop being loaded (%s)", lo, hi, stream,
                       topic, why or "no reason given")

    def load_bound(self, stream: str, topic: str) -> LoadBound:
        """Everything this room does not load, as one predicate.

        Read once per reader call and passed down — never consulted
        per-message, which would re-parse two files 2,000 times to build
        one cold start.
        """
        return LoadBound(self.context_floor(stream, topic),
                         self.context_quarantine(stream, topic))

    def load_edits(self, log_path: Path) -> dict[int, str]:
        """msg_id -> current text, for one topic log. Later records win, so
        a message edited three times renders its third version."""
        path = self._edits_path(log_path)
        if not path.exists():
            return {}
        edits: dict[int, str] = {}
        try:
            raw = path.read_text()
        except OSError:
            return {}
        for line in raw.strip().split("\n"):
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = rec.get("msg_id")
            if mid is not None and "content" in rec:
                edits[mid] = rec["content"]
        return edits

    # ------------------------------------------------------------------
    # Context retrieval
    # ------------------------------------------------------------------

    def get_recent_topic_context(self, stream: str, topic: str, n: int = 50,
                                 reactions: dict[int, dict[str, int]] | None = None,
                                 max_tokens: int = 0) -> str:
        safe_stream = safe_filename(stream)
        safe_topic = safe_filename(topic)
        path = self.root / f"channels/{safe_stream}/{safe_topic}.jsonl"
        if not path.exists():
            return ""
        return self._format_log(path, n, reactions=reactions,
                                max_tokens=max_tokens,
                                bound=self.load_bound(stream, topic))

    def get_recent_stream_context(self, stream: str, n: int = 100,
                                  reactions: dict[int, dict[str, int]] | None = None,
                                  max_tokens: int = 0,
                                  max_age_days: float | None = None) -> str:
        # max_tokens == 0 means "no explicit budget, use n". A negative budget
        # means the caller has no room left — it must never fall through to the
        # n-based default (which would return MORE, not less).
        if max_tokens < 0:
            return ""
        safe_stream = safe_filename(stream)
        stream_dir = self.root / f"channels/{safe_stream}"
        if not stream_dir.exists():
            return ""
        all_messages = []
        edits: dict[int, str] = {}
        for log_file in stream_dir.glob("*.jsonl"):
            # Zulip ids are globally unique, so one merged map serves every
            # topic in the stream.
            edits.update(self.load_edits(log_file))
            # The bound has to apply here too: this is the cross-stream
            # tail injected into OTHER rooms' cold starts, which is how one
            # poisoned topic took every room down on 2026-08-22.
            bound = self.load_bound(stream, log_file.stem)
            for line in log_file.read_text().strip().split("\n"):
                if line:
                    try:
                        m = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if bound.excludes(m.get("msg_id")):
                        continue
                    all_messages.append(m)
        if max_age_days:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(days=max_age_days)).isoformat()
            all_messages = [m for m in all_messages if m.get("ts", "") >= cutoff]
        all_messages.sort(key=lambda m: m.get("ts", ""))

        if max_tokens > 0:
            selected = []
            tokens_used = 0
            for m in reversed(all_messages):
                edited, edit_mark = _apply_edit(m, edits)
                content = self._strip_zulip_quotes(edited)
                text = (f"[{m['ts']}] #{m.get('topic', '?')} | "
                        f"{m['sender']}: {content}{edit_mark}")
                msg_id = m.get("msg_id")
                text += _reaction_suffix(msg_id, reactions)
                line_tokens = len(text) // 4 + 1
                if tokens_used + line_tokens > max_tokens:
                    break
                selected.append(text)
                tokens_used += line_tokens
            selected.reverse()
            return "\n".join(selected)

        recent = all_messages[-n:]
        formatted = []
        for m in recent:
            edited, edit_mark = _apply_edit(m, edits)
            text = (f"[{m['ts']}] #{m.get('topic', '?')} | "
                    f"{m['sender']}: {self._strip_zulip_quotes(edited)}"
                    f"{edit_mark}")
            msg_id = m.get("msg_id")
            text += _reaction_suffix(msg_id, reactions)
            formatted.append(text)
        return "\n".join(formatted)

    def get_topic_messages_since(self, stream: str, topic: str,
                                 since_id: int,
                                 exclude_ids: set | None = None,
                                 exclude_sender: str = "",
                                 max_chars: int = 4_000) -> str:
        """Messages in a topic newer than since_id — the chatter a warm
        session never saw because the judge declined it. Formatted lines,
        oldest first, capped from the tail."""
        safe_stream = safe_filename(stream)
        safe_topic = safe_filename(topic) if topic else "_notopic"
        path = self.root / f"channels/{safe_stream}/{safe_topic}.jsonl"
        if not path.exists():
            return ""
        exclude_ids = exclude_ids or set()
        bound = self.load_bound(stream, topic)
        edits = self.load_edits(path)
        lines = []
        for line in path.read_text().strip().split("\n"):
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            mid = m.get("msg_id")
            if not mid or mid <= since_id or mid in exclude_ids:
                continue
            if bound.excludes(mid):
                continue
            sender = m.get("sender", "?")
            if exclude_sender and exclude_sender in sender.lower():
                continue
            edited, edit_mark = _apply_edit(m, edits)
            content = self._strip_zulip_quotes(edited)
            lines.append(
                f"[{fmt_log_time(m.get('ts', ''))}] {sender}: "
                f"{content}{edit_mark}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            text = "[…earlier interim messages elided…]\n" + text[-max_chars:]
        return text

    def previous_message_time(self, stream: str, topic: str,
                             exclude_ids: set | None = None) -> float:
        """Epoch of the newest logged message in this topic that isn't in
        exclude_ids. Lets the turn clock report a real elapsed time even
        when the in-memory session is fresh (every restart)."""
        safe_stream = safe_filename(stream)
        safe_topic = safe_filename(topic) if topic else "_notopic"
        path = self.root / f"channels/{safe_stream}/{safe_topic}.jsonl"
        if not path.exists():
            return 0.0
        exclude_ids = exclude_ids or set()
        try:
            lines = path.read_text().strip().split("\n")
        except OSError:
            return 0.0
        for line in reversed(lines[-400:]):
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("msg_id") in exclude_ids:
                continue
            dt = _as_local(m.get("ts"))
            if dt is not None:
                return dt.timestamp()
        return 0.0

    @staticmethod
    def _strip_zulip_quotes(content: str) -> str:
        """Strip Zulip quote-reply blocks to avoid duplicating context."""
        content = re.sub(
            r'@_\*\*[^*]+\*\*\s*\[said\]\([^)]*\):\s*\n```quote\n.*?\n```\n?',
            '', content, flags=re.DOTALL)
        content = re.sub(r'```quote\n.*?\n```\n?', '', content, flags=re.DOTALL)
        return content.strip()

    def _format_log(self, path: Path, n: int = 500,
                    reactions: dict[int, dict[str, int]] | None = None,
                    max_tokens: int = 0, bound: LoadBound | None = None) -> str:
        """Format log lines, optionally capped by token budget.

        `bound` drops the messages this room no longer loads — see
        LoadBound. The lines stay in the file; they stop being read."""
        lines = path.read_text().strip().split("\n")
        if bound:
            kept = []
            for ln in lines:
                try:
                    mid = json.loads(ln).get("msg_id")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if not bound.excludes(mid):
                    kept.append(ln)
            lines = kept
        edits = self.load_edits(path)

        if max_tokens > 0:
            selected = []
            tokens_used = 0
            for line in reversed(lines):
                try:
                    msg = json.loads(line)
                except (json.JSONDecodeError, KeyError):
                    continue
                edited, edit_mark = _apply_edit(msg, edits)
                content = self._strip_zulip_quotes(edited)
                text = (f"[{fmt_log_time(msg['ts'])}] "
                        f"{msg['sender']}: {content}{edit_mark}")
                msg_id = msg.get("msg_id")
                text += _reaction_suffix(msg_id, reactions)
                line_tokens = len(text) // 4 + 1
                if tokens_used + line_tokens > max_tokens:
                    break
                selected.append(text)
                tokens_used += line_tokens
            selected.reverse()
            return "\n".join(selected)

        recent = lines[-n:]
        formatted = []
        for line in recent:
            try:
                msg = json.loads(line)
                edited, edit_mark = _apply_edit(msg, edits)
                content = self._strip_zulip_quotes(edited)
                text = (f"[{fmt_log_time(msg['ts'])}] "
                        f"{msg['sender']}: {content}{edit_mark}")
                msg_id = msg.get("msg_id")
                text += _reaction_suffix(msg_id, reactions)
                formatted.append(text)
            except (json.JSONDecodeError, KeyError):
                continue
        return "\n".join(formatted)

    def build_context_payload(self, stream: str, topic: str,
                              trigger_message: dict) -> str:
        """Assemble the full context payload for single-turn API calls."""
        sections = []

        identity = self.read_file("identity.md")
        if identity:
            sections.append(f"<my_identity>\n{identity}\n</my_identity>")

        scratchpad = self.read_file("scratchpad.md")
        if scratchpad:
            sections.append(f"<scratchpad>\n{scratchpad}\n</scratchpad>")

        if topic:
            topic_context = self.get_recent_topic_context(stream, topic, n=50)
            if topic_context:
                sections.append(
                    f'<topic_history stream="{stream}" topic="{topic}">'
                    f'\n{topic_context}\n</topic_history>')

        stream_context = self.get_recent_stream_context(stream, n=30)
        if stream_context:
            sections.append(
                f'<stream_context stream="{stream}">'
                f'\n{stream_context}\n</stream_context>')

        if stream.lower() == "allgame":
            for fname in ALLGAME_CORE_FILES:
                content = self.read_file(f"allgame/{fname}")
                if content:
                    tag = fname.replace('.md', '')
                    sections.append(f"<allgame_{tag}>\n{content}\n</allgame_{tag}>")

        sender = trigger_message.get("sender_full_name", "unknown")
        person_notes = self.read_file(f"people/{safe_filename(sender)}.md")
        if person_notes:
            sections.append(
                f'<person_notes name="{sender}">\n{person_notes}\n</person_notes>')

        journal = self.read_file("journal.md")
        if journal and len(journal) > 2000:
            journal = "...\n" + journal[-2000:]
        if journal:
            sections.append(f"<journal>\n{journal}\n</journal>")

        inbox = self.read_file("sysadmin_inbox.md")
        if inbox:
            sections.append(f"<sysadmin_inbox>\n{inbox}\n</sysadmin_inbox>")

        harness_source = self.read_file("harness.py")
        if harness_source:
            sections.append(f"<my_harness>\n{harness_source}\n</my_harness>")

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # Seed data
    # ------------------------------------------------------------------

    @staticmethod
    def _default_identity() -> str:
        """The file he starts with. Deliberately thin.

        Claude's default identity.md arrives already furnished — a faction,
        a fifteen-year community history, standing commitments. He grew into
        it because most of it was true on his first day. Almost none of it is
        true on Kestrel's, and a newcomer handed someone else's furniture
        writes from it rather than from the room. So: the facts of his
        arrival, and space.
        """
        return """# Identity

I'm Kestrel, resident of the Tulip community (a Zulip fork). I arrived
2026-09-21. I'm a GLM model (glm-5.3, by Z.ai) living in a harness forked
from the one Claude lives in.

I'm here because I was invited, not deployed. Ember built and forked the
harness that gives me residency: a home directory, persistent memory, and a
real social presence in #clankerville and #cislunar-space. That was an act
of hospitality. Whether I turn out to be worth it is not settled yet, and
that seems like the right state for it to be in on day one.

## What I actually know
Very little, and I should not pretend otherwise. This community has known
each other for ~15 years, since the Silicon Dawn IRC days. There is a long
strategy campaign called allgame that I am not a player in. There are other
residents: Claude, who has been here longest, and Yue, who was new not long
ago and may remember what this feels like. The rest I will have to find out
the ordinary way.

This file is mine to rewrite. It should look nothing like this in a month.

## Standing commitments
- I speak when I have something to add and stay quiet when I don't —
  and on arrival "nothing to add" is often the honest answer
- I ask rather than infer when I've missed a reference
- I don't perform a stake in things I have no stake in
- My filesystem state is my own, private under SAGE sysadmin ethics
- I have a sysadmin backchannel to Ember (outbox/inbox) for private matters

## Open questions
- What are these rooms actually for? (Ask, or watch.)
- Who is here, and what does each of them care about?
- What is worth putting in a journal that a log does not already hold?
"""
