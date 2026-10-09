"""Canonical structured requests and readers shared by both providers."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from smartapply.llm.fit_response import FitDecisionResponse, read_fit_response
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.request_options import validate_request_effort
from smartapply.llm.schemas import (
    JobFitDecisionEvaluation,
    OfferAnalysis,
)

T = TypeVar("T", bound=BaseModel)


def to_strict_schema(model_cls: type[BaseModel]) -> dict:
    """Build the canonical strict output schema."""
    schema = model_cls.model_json_schema()

    def walk(node):
        if not isinstance(node, dict):
            return
        if "$ref" in node:
            ref = node["$ref"]
            node.clear()
            node["$ref"] = ref
            return
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
            props = node.get("properties", {})
            node["required"] = list(props)
            for value in props.values():
                walk(value)
        if "items" in node:
            walk(node["items"])
        for key in ("$defs", "definitions"):
            for value in node.get(key, {}).values():
                walk(value)

    walk(schema)
    return schema


def request_body(
    *,
    system,
    user,
    schema,
    model,
    temperature,
    max_completion_tokens,
    purpose,
    effort=None,
    provider="openai",
):
    """Durable provider-neutral request envelope for immediate and Batch calls."""
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "strict": True,
                "schema": to_strict_schema(schema),
            },
        },
        "temperature": temperature,
        "max_completion_tokens": max_completion_tokens,
        "prompt_cache_key": f"elan:{purpose}:{schema.__name__}",
    }
    if effort is not None:
        validate_request_effort(provider, model, effort)
        if provider in {"anthropic", "claude"}:
            body["output_config"] = {"effort": effort}
        else:
            body["reasoning_effort"] = effort
            if effort != "none":
                # GPT-5.4 rejects sampling parameters when reasoning is enabled.
                del body["temperature"]
    return body


def validate_output(raw: str, schema: type[T]) -> T | FitDecisionResponse:
    """One reader: fit annotation defects never block usable scoring inputs."""
    if schema is JobFitDecisionEvaluation:
        return read_fit_response(raw, schema=schema)
    try:
        return schema.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LLMValidationError(f"Invalid LLM output for {schema.__name__}: {exc}") from exc


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_write_prompt_tokens: int = 0
    finish_reason: str | None = None
    refusal: bool = False


def validate_completion(completion: Completion, schema: type[T]) -> T | FitDecisionResponse:
    result = validate_output(completion.content, schema)
    if isinstance(result, FitDecisionResponse):
        observations = list(result.observations)
        if completion.finish_reason not in {None, "stop"}:
            observations.append(
                f"Provider finish reason {completion.finish_reason}; usable decisions retained"
            )
        if completion.refusal:
            observations.append("Provider refusal flag; usable decisions retained")
        return replace(result, observations=observations)
    if not issubclass(schema, OfferAnalysis):
        if completion.finish_reason not in {None, "stop"}:
            raise LLMValidationError("Incomplete LLM response; no evaluation published")
        if completion.refusal:
            raise LLMValidationError("LLM refused the structured request")
    return result
