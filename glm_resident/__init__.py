"""glm_resident — Persistent Claude residency harness for Tulip (Zulip fork)."""

from glm_resident.config import (
    DEFAULT_STATE_DIR,
    DEFAULT_MODEL,
    DEFAULT_STANDING_STREAMS,
    HARNESS_PATH,
    HARNESS_DIR,
    LOG_FORMAT,
)
from glm_resident.state import StateManager
from glm_resident.sessions import ConversationSession, SessionManager
from glm_resident.judge import EngagementJudge
from glm_resident.resident import GLMResident

__all__ = [
    "GLMResident",
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
