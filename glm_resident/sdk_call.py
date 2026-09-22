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

from glm_resident.config import AGENT_SDK_CONFIG_DIR, MODEL_IDS
from glm_resident.util import is_policy_refusal

logger = logging.getLogger("sdk_call")


class PolicyRefusal(RuntimeError):
    """The provider refused the whole request on policy grounds.

    Worth its own type because the caller's correct response is entirely
    different from every other failure here: retrying is pointless (the
    classifier is deterministic on a fixed payload) and rotating accounts
    is pointless (every account reads the same prompt). The only move is to
    send something else.

    The SDK makes this hard to see. A refusal it observed on the wire can
    reach the caller as "Claude Code returned an error result: success",
    with the reason stripped and the exception surfacing at asyncio.run()
    teardown rather than out of the generator — so the reason is recorded
    in a holder the caller owns, the same seam agent_engine uses. Without
    it the distiller spent 2026-08-25..31 retrying one refused chunk every
    ten minutes and logging it as an ordinary warning.
    """


async def _run(prompt: str, system: str, model: str, max_turns: int,
               seen: dict | None = None) -> str:
    from claude_agent_sdk import (ClaudeAgentOptions, AssistantMessage,
                                  ResultMessage, query)
    from glm_resident.auth import sdk_env

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
            **sdk_env(model),
            "CLAUDE_CONFIG_DIR": str(AGENT_SDK_CONFIG_DIR),
        },
    )
    parts: list[str] = []
    failure: str | None = None
    # Do NOT raise from inside the iteration: an exception there escapes
    # while the SDK's async generator is still running, and its cleanup
    # then dies with "aclose(): asynchronous generator is already running",
    # taking the whole process down instead of just this call. Record the
    # failure, let the generator finish, raise afterwards.
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
        elif isinstance(msg, ResultMessage) and msg.is_error:
            failure = f"sdk_call error: {msg.result!r}"
            if seen is not None and is_policy_refusal(msg.result):
                seen["refusal"] = str(msg.result)
    if failure:
        if seen is not None and seen.get("refusal"):
            raise PolicyRefusal(seen["refusal"])
        raise RuntimeError(failure)
    return "\n".join(parts).strip()


def complete(prompt: str, system: str = "", model: str | None = None,
             max_turns: int = 1, big_context: bool = False) -> str:
    """Run one completion on plan limits. Raises on failure.

    Safe to call from the harness's background threads (reaper, distiller)
    and from the event loop — each call is its own subprocess and no event
    loop is running in those threads.
    """
    chosen = model or MODEL_IDS["sonnet"]
    if big_context and not chosen.endswith("[1m]"):
        # A whole era of chat can exceed the 200k default window.
        chosen += "[1m]"
    # The holder must outlive the event loop: the exception carrying a
    # refusal can land at asyncio.run() teardown, by which point anything
    # local to the coroutine is gone.
    seen: dict = {}
    try:
        return asyncio.run(_run(prompt, system, chosen, max_turns, seen))
    except PolicyRefusal:
        raise
    except Exception as e:
        if seen.get("refusal") or is_policy_refusal(e):
            raise PolicyRefusal(seen.get("refusal") or str(e)) from e
        raise
