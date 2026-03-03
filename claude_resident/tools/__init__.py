"""Tool definitions and dispatch for the Claude resident."""

from claude_resident.tools.definitions import TOOL_DEFINITIONS
from claude_resident.tools.dispatch import execute_tool

__all__ = ["TOOL_DEFINITIONS", "execute_tool"]
