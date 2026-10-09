"""Versioned transport options shared by immediate requests and frozen journals.

Effort changes how the model responds, so it belongs to the exact-cache identity.
No prompt, schema, scoring decision or annotation validation depends on it.
"""

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
) -> dict:
    """Keep historical identities intact when no effort was explicitly sent."""
    extra = {"temperature": temperature, "schema": schema.model_json_schema()}
    if effort is not None:
        extra["effort"] = effort
    return extra
