"""Tool definitions and dispatch for the Claude resident."""

from sonnet_resident.tools.definitions import TOOL_DEFINITIONS
from sonnet_resident.tools.dispatch import execute_tool

__all__ = ["TOOL_DEFINITIONS", "execute_tool"]
