"""Recursive language-model invocation on the resident's own subscription.

`invoke_model` lets the resident make a nested model call. It routes
through the Claude Agent SDK (a one-shot, tool-less Claude Code turn) so
the sub-call draws PLAN LIMITS like the resident's own turns — the raw
Messages API only serves Haiku for subscription OAuth tokens, which would
have silently limited blind GM calls to the smallest model.

The sub-model gets a CLEAN context: only the provided `system` + `prompt`.
No channel history, no memory of prior invocations — which is exactly what
makes a blind GM delegation blind.
"""

import asyncio
import logging

from glm_resident.config import (
    MODEL_IDS,
    AGENT_SDK_CONFIG_DIR,
)

logger = logging.getLogger("tools.lm")

# Bare first-party model ids from config. The model arg also accepts any
# full model id verbatim.
MODEL_ALIASES = dict(MODEL_IDS)

MAX_TOKENS_CAP = 8192


async def _sdk_oneshot(prompt: str, system: str, model: str,
                       max_tokens: int) -> tuple[str, str]:
    """One tool-less Claude Code turn. Returns (text, meta)."""
    from claude_agent_sdk import (ClaudeAgentOptions, AssistantMessage,
                                  ResultMessage, query)
    from glm_resident.auth import sdk_env

    options = ClaudeAgentOptions(
        system_prompt=system or None,
        model=model,
        max_turns=1,
        tools=[],
        permission_mode="bypassPermissions",
        setting_sources=[],
        cwd=str(AGENT_SDK_CONFIG_DIR),
        env={
            **sdk_env(model),
            "CLAUDE_CONFIG_DIR": str(AGENT_SDK_CONFIG_DIR),
        },
    )
    texts: list[str] = []
    meta = f"[model={model}"
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                text = getattr(block, "text", None)
                if text:
                    texts.append(text)
        elif isinstance(msg, ResultMessage):
            usage = msg.usage or {}
            if isinstance(usage, dict):
                meta += (f", tokens={usage.get('input_tokens', '?')}in/"
                         f"{usage.get('output_tokens', '?')}out")
            if msg.is_error:
                raise RuntimeError(f"sub-model error: {msg.result!r}")
    meta += "]"
    # Note: max_tokens is advisory here (the SDK doesn't cap per-turn
    # output); prompts should ask for bounded responses.
    return "\n".join(texts).strip(), meta


def invoke_model(inp: dict, anthropic_client, default_model: str) -> dict:
    """Run a nested Claude call. Returns {"content": str[, "is_error": True]}."""
    prompt = (inp.get("prompt") or "").strip()
    if not prompt:
        return {"content": "No prompt provided.", "is_error": True}

    system = (inp.get("system") or "").strip()
    model_arg = (inp.get("model") or "sonnet").strip()
    model = MODEL_ALIASES.get(model_arg.lower(), model_arg)

    try:
        max_tokens = int(inp.get("max_tokens", 2048))
    except (TypeError, ValueError):
        max_tokens = 2048
    max_tokens = max(1, min(max_tokens, MAX_TOKENS_CAP))

    try:
        text, meta = asyncio.run(
            _sdk_oneshot(prompt, system, model, max_tokens))
        logger.info(f"invoke_model {meta}")
        if not text:
            return {"content": f"(sub-model returned no text)\n{meta}"}
        return {"content": f"{text}\n\n---\n{meta}"}
    except Exception as e:
        return {"content": (
            f"invoke_model failed for '{model}': {e}\n"
            f"Known aliases: {', '.join(sorted(MODEL_ALIASES))}. "
            f"You can also pass a full model id."),
            "is_error": True}
