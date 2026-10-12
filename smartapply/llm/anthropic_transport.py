"""Claude wire adaptation only; canonical prompts and application schemas stay shared."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import replace

from smartapply.llm.anthropic_fit_transport import fit_transport_schema
from smartapply.llm.prompts.cache_layout import candidate_cache_prefix_end
from smartapply.llm.provider import LLMError
from smartapply.llm.request_options import validate_request_effort
from smartapply.llm.structured import Completion

# Messages API reference, checked 2026-10-09: temperature is deprecated and
# rejected (except 1.0) on models released after Opus 4.6. Keep the existing
# parameter only on known compatible models; newer/unknown IDs use API defaults.
_TEMPERATURE_MODELS = (
    "claude-haiku-4-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-opus-4-6",
)


def build_message_params(body: dict) -> dict:
    """Translate the exact journal request to the native Messages API.

    The official SDK adapts unsupported schema constraints at the wire boundary.
    The original schema still validates outputs in the shared application reader.
    No provider-specific instructions are added to either prompt.
    """
    from anthropic import transform_schema

    messages = body["messages"]
    if (
        len(messages) != 2
        or messages[0]["role"] != "system"
        or messages[1]["role"] != "user"
        or not all(isinstance(m["content"], str) for m in messages)
    ):
        raise LLMError("Invalid canonical structured request messages")
    contract = body["response_format"]["json_schema"]
    source_schema = fit_transport_schema(contract["schema"], name=contract["name"])
    schema = transform_schema(copy.deepcopy(source_schema))

    def preserve_constants(source, target):
        if not isinstance(source, dict) or not isinstance(target, dict):
            return
        if "const" in source:
            target["const"] = source["const"]
        # Small finite integer ranges can be enforced as enums by Claude too.
        low, high = source.get("minimum"), source.get("maximum")
        if (
            source.get("type") == "integer"
            and type(low) is int
            and type(high) is int
            and 0 <= high - low <= 100
        ):
            target["enum"] = list(range(low, high + 1))
        for k, value in source.items():
            if isinstance(value, dict) and isinstance(target.get(k), dict):
                preserve_constants(value, target[k])
            elif isinstance(value, list) and isinstance(target.get(k), list):
                for a, b in zip(value, target[k], strict=False):
                    preserve_constants(a, b)

    preserve_constants(source_schema, schema)
    user = messages[1]["content"]
    prefix_end = candidate_cache_prefix_end(user, schema_name=contract["name"])
    user_content = user
    if prefix_end is not None:
        # Adapt only the wire representation: journal/cache identity and every
        # prompt character stay unchanged. Never cache the offer-specific tail.
        user_content = [
            {"type": "text", "text": user[:prefix_end], "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": user[prefix_end:]},
        ]
    params = {
        "model": body["model"],
        "system": [
            {"type": "text", "text": messages[0]["content"], "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": user_content}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
        "max_tokens": body["max_completion_tokens"],
    }
    # Absence is meaningful for historical journals: never inject today's
    # effort policy into a request that was frozen without this parameter.
    effort = body.get("output_config", {}).get("effort")
    if effort is not None:
        try:
            validate_request_effort("anthropic", body["model"], effort)
        except ValueError as exc:
            raise LLMError("Invalid frozen Anthropic effort") from exc
        params["output_config"]["effort"] = effort
    if any(
        body["model"] == name or body["model"].startswith(name + "-")
        for name in _TEMPERATURE_MODELS
    ):
        params["temperature"] = body["temperature"]
    return params


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _tokens(value):
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LLMError("Claude response contains an invalid token count")
    return value


def parse_message(message) -> Completion:
    content = _get(message, "content", [])
    if not isinstance(content, list):
        raise LLMError("Claude response has invalid content blocks")
    # Thinking/tool content is never mistaken for the structured JSON output.
    texts = [_get(block, "text") for block in content if _get(block, "type") == "text"]
    if not all(isinstance(text, str) for text in texts):
        raise LLMError("Claude response has an invalid text block")
    usage = _get(message, "usage", {})
    reads = _tokens(_get(usage, "cache_read_input_tokens"))
    writes = _tokens(_get(usage, "cache_creation_input_tokens"))
    reason = _get(message, "stop_reason")
    normalized = {"end_turn": "stop", "max_tokens": "length"}.get(reason, reason)
    return Completion(
        content="".join(texts),
        model=_get(message, "model", ""),
        prompt_tokens=_tokens(_get(usage, "input_tokens")) + reads + writes,
        completion_tokens=_tokens(_get(usage, "output_tokens")),
        cached_prompt_tokens=reads,
        cache_write_prompt_tokens=writes,
        finish_reason=normalized,
        refusal=reason == "refusal",
    )


def with_requested_model(completion: Completion, model: str) -> Completion:
    return completion if completion.model else replace(completion, model=model)
