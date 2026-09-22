"""Tool definitions and dispatch for the Claude resident."""

from glm_resident.tools.definitions import TOOL_DEFINITIONS
from glm_resident.tools.dispatch import execute_tool

__all__ = ["TOOL_DEFINITIONS", "execute_tool"]
