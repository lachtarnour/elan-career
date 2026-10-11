"""Versioned transport options shared by immediate requests and frozen journals."""

from __future__ import annotations

from pydantic import BaseModel

from smartapply.llm_policy import (
    RequestEffort,
    stage_effort,
    supported_efforts,
    validate_request_effort,
)

__all__ = [
    "RequestEffort",
    "stage_effort",
    "supported_efforts",
    "validate_request_effort",
    "request_cache_extra",
]


def request_cache_extra(
    schema: type[BaseModel],
    temperature: float,
    effort: RequestEffort | None,
    max_completion_tokens: int,
) -> dict:
    """Include every response-affecting option in the exact-cache identity."""
    extra = {
        "temperature": temperature,
        "schema": schema.model_json_schema(),
        "max_completion_tokens": max_completion_tokens,
    }
    if effort is not None:
        extra["effort"] = effort
    return extra
