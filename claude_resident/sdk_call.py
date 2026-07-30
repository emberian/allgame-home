"""One-shot model calls for the harness's own machinery, on plan limits.

The resident's sidecars — the engagement judge, session summaries, the
distiller — used to run on the raw Messages API. On a subscription token
that path draws METERED extra-usage credit rather than plan limits, and
when the credit ran dry every one of them failed silently for days:
the distiller sealed zero era volumes between 2026-07-25 and 07-30, the
judge fell back to a keyword check hundreds of times, and session notes
became mechanical stubs (found in the 2026-07-30 log audit).

Everything here goes through the Agent SDK instead — the same door the
resident's own turns use — so the machinery lives on the same flat-rate
plan he does. No tools, no session, no state: one prompt in, text out.
"""

import asyncio
import logging

from claude_resident.config import AGENT_SDK_CONFIG_DIR, MODEL_IDS

logger = logging.getLogger("sdk_call")


async def _run(prompt: str, system: str, model: str, max_turns: int) -> str:
    from claude_agent_sdk import (ClaudeAgentOptions, AssistantMessage,
                                  ResultMessage, query)
    from claude_resident.auth import read_oauth_token

    options = ClaudeAgentOptions(
        system_prompt=system or None,
        model=model,
        max_turns=max_turns,
        tools=[],                       # no tools: this is a completion
        permission_mode="bypassPermissions",
        setting_sources=[],             # never inherit the host's CLAUDE.md
        cwd=str(AGENT_SDK_CONFIG_DIR),
        max_buffer_size=32_000_000,
        env={
            "CLAUDE_CODE_OAUTH_TOKEN": read_oauth_token() or "",
            "CLAUDE_CONFIG_DIR": str(AGENT_SDK_CONFIG_DIR),
            # An API key in the environment would shadow the OAuth token and
            # send us straight back to metered billing (2026-07-22).
            "ANTHROPIC_API_KEY": "",
        },
    )
    parts: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
        elif isinstance(msg, ResultMessage) and msg.is_error:
            raise RuntimeError(f"sdk_call error: {msg.result!r}")
    return "\n".join(parts).strip()


def complete(prompt: str, system: str = "", model: str | None = None,
             max_turns: int = 1) -> str:
    """Run one completion on plan limits. Raises on failure.

    Safe to call from the harness's background threads (reaper, distiller)
    and from the event loop — each call is its own subprocess and no event
    loop is running in those threads.
    """
    return asyncio.run(_run(prompt, system,
                            model or MODEL_IDS["sonnet"], max_turns))
