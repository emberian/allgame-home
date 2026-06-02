"""Recursive language-model invocation through the same Bedrock client.

`invoke_model` lets the resident make a nested model call over the exact
`AnthropicBedrock` client it itself runs on. Because it's the same API path
that keeps the resident alive, it's available whenever the resident is —
unlike `lm_studio` (the local server can be offline) or the sandbox (which
can run out of API credits). That availability is the whole point: the GM's
blind sub-model calls (e.g. handing a faction's situation to a fresh Sonnet
that sees only the given prompt) must not silently degrade to "the GM
reasoned it out himself" when an external path is down.
"""

import logging

logger = logging.getLogger("tools.lm")

# Confirmed-enabled inference profiles in this account (commonquant-ember,
# us-east-1) — verified via `aws bedrock list-inference-profiles`. The model
# arg also accepts any full inference-profile id verbatim.
MODEL_ALIASES = {
    "haiku": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "sonnet": "us.anthropic.claude-sonnet-4-6",
    "opus": "us.anthropic.claude-opus-4-6-v1",
}

MAX_TOKENS_CAP = 8192


def invoke_model(inp: dict, anthropic_client, default_model: str) -> dict:
    """Run a nested Claude call. Returns {"content": str[, "is_error": True]}.

    The sub-model receives a CLEAN context: only the provided `system` +
    `prompt`. No channel history, no memory of prior invocations — which is
    exactly what makes a blind GM delegation blind.
    """
    if anthropic_client is None:
        return {"content": "No model client available for invoke_model.",
                "is_error": True}

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
        temperature = float(inp.get("temperature", 1.0))
    except (TypeError, ValueError):
        temperature = 1.0
    temperature = max(0.0, min(temperature, 1.0))

    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    try:
        resp = anthropic_client.messages.create(**kwargs)
        text = "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", None) == "text").strip()
        usage = getattr(resp, "usage", None)
        meta_bits = [f"model={model}"]
        if usage is not None:
            meta_bits.append(
                f"tokens={getattr(usage, 'input_tokens', '?')}in/"
                f"{getattr(usage, 'output_tokens', '?')}out")
        meta = "[" + ", ".join(meta_bits) + "]"
        logger.info(f"invoke_model {meta}")
        if not text:
            return {"content": f"(sub-model returned no text)\n{meta}"}
        return {"content": f"{text}\n\n---\n{meta}"}
    except Exception as e:
        return {"content": (
            f"invoke_model failed for '{model}': {e}\n"
            f"Known aliases: {', '.join(sorted(MODEL_ALIASES))}. "
            f"You can also pass a full Bedrock inference-profile id."),
            "is_error": True}
