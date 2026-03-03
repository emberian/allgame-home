"""Harness self-modification tool."""

import logging

from claude_resident.selfmod import edit_harness

logger = logging.getLogger("tools.harness")


def edit_harness_tool(inp: dict, state) -> dict:
    return edit_harness(
        old_string=inp.get("old_string", ""),
        new_string=inp.get("new_string", ""),
        commit_message=inp.get("commit_message", "Claude self-edit"),
        state=state,
        file=inp.get("file"),
    )
