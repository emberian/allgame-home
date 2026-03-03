"""claude_resident — Persistent Claude residency harness for Tulip (Zulip fork)."""

from claude_resident.config import (
    DEFAULT_STATE_DIR,
    DEFAULT_MODEL,
    DEFAULT_STANDING_STREAMS,
    HARNESS_PATH,
    HARNESS_DIR,
    LOG_FORMAT,
)
from claude_resident.state import StateManager
from claude_resident.sessions import ConversationSession, SessionManager
from claude_resident.judge import EngagementJudge
from claude_resident.resident import ClaudeResident

__all__ = [
    "ClaudeResident",
    "StateManager",
    "EngagementJudge",
    "SessionManager",
    "ConversationSession",
    "DEFAULT_STATE_DIR",
    "DEFAULT_MODEL",
    "DEFAULT_STANDING_STREAMS",
    "HARNESS_PATH",
    "HARNESS_DIR",
    "LOG_FORMAT",
]
