"""Per-topic conversation session management (KV cache optimization)."""

import time
import logging
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
    last_active: float = field(default_factory=time.time)
    message_count: int = 0
    estimated_message_tokens: int = 0

    def touch(self):
        self.last_active = time.time()

    @property
    def age_seconds(self) -> float:
        return time.time() - self.last_active


class SessionManager:
    """Manages per-topic conversation sessions with TTL eviction."""

    def __init__(self):
        self._sessions: dict[tuple[str, str], ConversationSession] = {}
        self.logger = logging.getLogger("session_manager")

    def get(self, stream: str, topic: str) -> Optional[ConversationSession]:
        key = (stream, topic)
        session = self._sessions.get(key)
        if session is None:
            return None
        if session.age_seconds > SESSION_TTL:
            self.logger.info(f"Session expired: #{stream}>{topic} "
                             f"(idle {session.age_seconds:.0f}s)")
            del self._sessions[key]
            return None
        return session

    def create(self, stream: str, topic: str) -> ConversationSession:
        self._evict_if_needed()
        session = ConversationSession(stream=stream, topic=topic)
        self._sessions[(stream, topic)] = session
        self.logger.info(f"Session created: #{stream}>{topic}")
        return session

    def remove(self, stream: str, topic: str):
        key = (stream, topic)
        if key in self._sessions:
            del self._sessions[key]
            self.logger.info(f"Session removed: #{stream}>{topic}")

    def _evict_if_needed(self):
        expired = [k for k, s in self._sessions.items()
                   if s.age_seconds > SESSION_TTL]
        for k in expired:
            del self._sessions[k]
        while len(self._sessions) >= SESSION_MAX_COUNT:
            oldest = min(self._sessions,
                         key=lambda k: self._sessions[k].last_active)
            self.logger.info(f"Evicting LRU session: #{oldest[0]}>{oldest[1]}")
            del self._sessions[oldest]
