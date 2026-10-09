"""Token pricing and cost estimation.

Prices are in USD per 1M tokens. Update as providers change them. Unknown
models use a generic estimate, which is not a guaranteed upper bound.
"""

from __future__ import annotations

# (input $/M, output $/M)
OPENAI_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
    "gpt-5.6-sol": (4.00, 20.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o4-mini": (1.10, 4.40),
}

# Cached input $/M. Models omitted here fall back to their normal input rate,
# which avoids claiming a discount that may not exist.
OPENAI_CACHED_INPUT_PRICING: dict[str, float] = {
    "gpt-4o-mini": 0.075,
    "gpt-4o": 1.25,
    "gpt-4o-2024-08-06": 1.25,
    "gpt-4.1": 0.50,
    "gpt-4.1-mini": 0.10,
    "o4-mini": 0.275,
    "gpt-5.4-mini": 0.075,
    "gpt-5.4-nano": 0.02,
    "gpt-5.4": 0.25,
    "gpt-5.6-luna": 0.02,
    "gpt-5.6-terra": 0.20,
    "gpt-5.6-sol": 0.40,
}

OPENAI_CACHE_WRITE_MULTIPLIER: dict[str, float] = {
    "gpt-5.6-luna": 1.25,
    "gpt-5.6-terra": 1.25,
    "gpt-5.6-sol": 1.25,
}

# Official first-party Claude pricing, checked 2026-10-09.
# https://platform.claude.com/docs/en/about-claude/pricing
# Only 5-minute ephemeral cache writes are requested by our transport.
ANTHROPIC_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-5": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-fable-5-1": (10.00, 50.00),
    "claude-mythos-5": (10.00, 50.00),
    "claude-mythos-5-1": (10.00, 50.00),
}
ANTHROPIC_CACHE_READ_MULTIPLIER = {
    "claude-opus-5-5": 0.05,
    "claude-sonnet-5-5": 0.05,
    "claude-fable-5-1": 0.025,
    "claude-mythos-5-1": 0.025,
}

FALLBACK_PRICING = (1.00, 4.00)


def _model_rate_key(model: str) -> str | None:
    """Resolve aliases and dated snapshots to the longest known model name."""
    if model in OPENAI_PRICING:
        return model
    for candidate in sorted(OPENAI_PRICING, key=len, reverse=True):
        if model.startswith(f"{candidate}-"):
            return candidate
    return None


def estimate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    cached_prompt_tokens: int = 0,
    cache_write_prompt_tokens: int = 0,
) -> float:
    claude_key = next(
        (
            key
            for key in sorted(ANTHROPIC_PRICING, key=len, reverse=True)
            if model == key or model.startswith(key + "-")
        ),
        None,
    )
    if claude_key is not None:
        in_price, out_price = ANTHROPIC_PRICING[claude_key]
        if claude_key == "claude-haiku-5-5" and prompt_tokens > 100_000:
            in_price, out_price = (0.50, 2.50)
        reads = min(max(cached_prompt_tokens, 0), max(prompt_tokens, 0))
        writes = min(max(cache_write_prompt_tokens, 0), max(prompt_tokens, 0) - reads)
        regular = max(prompt_tokens, 0) - reads - writes
        return (
            regular * in_price
            + reads * in_price * ANTHROPIC_CACHE_READ_MULTIPLIER.get(claude_key, 0.1)
            + writes * in_price * 1.25
            + max(completion_tokens, 0) * out_price
        ) / 1_000_000
    rate_key = _model_rate_key(model)
    in_price, out_price = OPENAI_PRICING[rate_key] if rate_key is not None else FALLBACK_PRICING
    cached_tokens = min(max(cached_prompt_tokens, 0), max(prompt_tokens, 0))
    cache_write_tokens = min(
        max(cache_write_prompt_tokens, 0),
        max(prompt_tokens, 0) - cached_tokens,
    )
    uncached_tokens = max(prompt_tokens, 0) - cached_tokens - cache_write_tokens
    cached_price = (
        OPENAI_CACHED_INPUT_PRICING.get(rate_key, in_price) if rate_key is not None else in_price
    )
    cache_write_price = in_price * (
        OPENAI_CACHE_WRITE_MULTIPLIER.get(rate_key, 1.0) if rate_key is not None else 1.0
    )
    return (
        uncached_tokens * in_price
        + cached_tokens * cached_price
        + cache_write_tokens * cache_write_price
        + max(completion_tokens, 0) * out_price
    ) / 1_000_000.0
