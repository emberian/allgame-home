"""Her tools as she sees them: her subset of the shared definitions, with
descriptions that are true for her. Kept apart from mcp_server so the
engine can count them without importing the MCP SDK."""

from kimi_resident import config


def her_definitions() -> list[dict]:
    from claude_resident.tools.definitions import TOOL_DEFINITIONS
    from claude_resident.tools.sibling_descriptions import adapt
    return [dict(t, description=adapt(t["name"], t["description"]))
            for t in TOOL_DEFINITIONS if t["name"] in config.TOOL_SUBSET]
