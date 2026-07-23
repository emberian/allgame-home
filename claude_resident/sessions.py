"""Per-topic conversation session management (KV cache optimization)."""

import time
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from claude_resident.config import SESSION_TTL, SESSION_MAX_COUNT, SESSION_TRIM_PAIRS


@dataclass
class ConversationSession:
    """Persistent conversation state for a (stream, topic) pair."""
    stream: str
    topic: str
    messages: list = field(default_factory=list)
    system_blocks: list = field(default_factory=list)
    state_fingerprints: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    message_count: int = 0
    estimated_message_tokens: int = 0
    send_word_counts: list = field(default_factory=list)  # word counts of send_message calls
    participants: set = field(default_factory=set)  # senders seen this session
    last_seen_msg_id: int = 0  # newest channel msg id already in this session's context

    def touch(self):
        self.last_active = time.time()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.last_active


class SessionManager:
    """Manages per-topic conversation sessions with TTL eviction.

    Ended sessions (TTL expiry, LRU eviction, explicit removal) are parked in
    a graveyard rather than dropped, so the session reaper can write a summary
    of each before it's forgotten — the raw channel log records what was said
    publicly, but only the session holds the tool calls and internal monologue.
    Dict operations are lock-guarded: the Zulip event thread and the reaper
    thread both touch the session table.
    """

    def __init__(self):
        self._sessions: dict[tuple[str, str], ConversationSession] = {}
        self._graveyard: list[ConversationSession] = []
        self._lock = threading.Lock()
        self.logger = logging.getLogger("session_manager")

    def get(self, stream: str, topic: str) -> Optional[ConversationSession]:
        key = (stream, topic)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if session.age_seconds > SESSION_TTL:
                self.logger.info(f"Session expired: #{stream}>{topic} "
                                 f"(idle {session.age_seconds:.0f}s)")
                self._graveyard.append(self._sessions.pop(key))
                return None
            return session

    def create(self, stream: str, topic: str) -> ConversationSession:
        with self._lock:
            self._evict_if_needed_locked()
            session = ConversationSession(stream=stream, topic=topic)
            self._sessions[(stream, topic)] = session
        self.logger.info(f"Session created: #{stream}>{topic}")
        return session

    def remove(self, stream: str, topic: str):
        key = (stream, topic)
        with self._lock:
            session = self._sessions.pop(key, None)
            if session is not None:
                self._graveyard.append(session)
        if session is not None:
            self.logger.info(f"Session removed: #{stream}>{topic}")

    def drain_ended(self) -> list[ConversationSession]:
        """Sweep expired sessions into the graveyard and return everything in
        it, clearing it. Called by the session reaper."""
        with self._lock:
            expired = [k for k, s in self._sessions.items()
                       if s.age_seconds > SESSION_TTL]
            for k in expired:
                self.logger.info(f"Session expired: #{k[0]}>{k[1]} (reaped)")
                self._graveyard.append(self._sessions.pop(k))
            ended = self._graveyard
            self._graveyard = []
        return ended

    def _evict_if_needed_locked(self):
        """Caller must hold self._lock."""
        expired = [k for k, s in self._sessions.items()
                   if s.age_seconds > SESSION_TTL]
        for k in expired:
            self._graveyard.append(self._sessions.pop(k))
        while len(self._sessions) >= SESSION_MAX_COUNT:
            oldest = min(self._sessions,
                         key=lambda k: self._sessions[k].last_active)
            self.logger.info(f"Evicting LRU session: #{oldest[0]}>{oldest[1]}")
            self._graveyard.append(self._sessions.pop(oldest))
