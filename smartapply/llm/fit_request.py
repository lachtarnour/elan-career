"""One evaluation request; scoring decisions are independent of annotation quality."""

from __future__ import annotations

import copy
import json

from smartapply.llm.fit_context import build_fit_user, fit_schema
from smartapply.llm.fit_scoring import evaluate_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.routes import frozen_provider

# Official model pages, verified 2026-10-08. Unknown aliases need explicit config.
# https://developers.openai.com/api/docs/models/gpt-5.4-mini
# https://developers.openai.com/api/docs/models/gpt-5.4
CONTEXT_WINDOWS = {"gpt-5.4-mini": 400_000, "gpt-5.4": 1_050_000}
ANTHROPIC_CONTEXT_WINDOWS = {
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "claude-haiku-5-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-fable-5-1": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-mythos-5-1": 1_000_000,
}


def check_request_budget(context: dict, user: str) -> dict:
    from smartapply.llm.structured import request_body, to_strict_schema

    if context["schema"] != to_strict_schema(fit_schema(context)):
        raise LLMValidationError("Frozen fit schema does not match the generation contract")
    limit = context["configured_context_window"]
    if limit is None:
        windows = (
            ANTHROPIC_CONTEXT_WINDOWS
            if context.get("provider", "openai") == "anthropic"
            else CONTEXT_WINDOWS
        )
        for alias in sorted(windows, key=len, reverse=True):
            if context["model"] == alias or context["model"].startswith(alias + "-"):
                limit = windows[alias]
                break
    if limit is None and context["model"].startswith("mock"):
        limit = 400_000
    if limit is None:
        raise LLMValidationError(
            "Unknown model context window; configure the provider context window in settings"
        )
    body = request_body(
        system=context["system_prompt"],
        user=user,
        schema=fit_schema(context),
        model=context["model"],
        temperature=context["temperature"],
        max_completion_tokens=context["max_completion_tokens"],
        purpose="job_fit_assessment",
        effort=context.get("effort"),
        provider=context.get("provider", "openai"),
    )
    if context.get("provider", "openai") == "anthropic":
        from smartapply.llm.anthropic_transport import build_message_params

        # Measure the actual SDK-adapted schema and envelope used by both
        # immediate and Batch execution, without sending a provider request.
        body = build_message_params(body)
    # UTF-8 bytes are a conservative upper bound for byte-BPE text tokens.
    # Reserve 1024 tokens for framing and the entire configured output budget.
    bound = len(json.dumps(body, ensure_ascii=False).encode()) + 1024
    if bound + context["max_completion_tokens"] > limit:
        raise LLMValidationError(
            "Fit request exceeds context budget; no factual content was truncated"
        )
    return {
        "input_token_upper_bound": bound,
        "context_window": limit,
        "output_reserved": context["max_completion_tokens"],
    }


def complete_fit(provider, context: dict, offer, *, job_id=None, extraction_provenance=None):
    """Freeze the request inputs; annotation inspections cannot veto its score."""
    context = copy.deepcopy(context)
    provider = frozen_provider(
        provider, context.get("provider"), context.get("model"),
        purpose="job_fit_assessment",
        reasoning=context.get("configured_reasoning", context.get("effort")),
        max_output_tokens=context.get("max_completion_tokens"),
    )
    provider_name = getattr(provider, "name", None)
    if provider_name in {"openai", "anthropic"} and provider_name != context["provider"]:
        raise LLMValidationError("Fit provider does not match the frozen request context")
    offer = offer.model_copy(deep=True)
    extraction_provenance = copy.deepcopy(extraction_provenance)
    user = build_fit_user(context, offer, extraction_provenance=extraction_provenance)
    budget = check_request_budget(context, user)
    diagnostic = provider.complete_json(
        system=context["system_prompt"],
        user=user,
        schema=fit_schema(context),
        model=context["model"],
        temperature=context["temperature"],
        purpose="job_fit_assessment",
        job_id=job_id,
        max_completion_tokens=context["max_completion_tokens"],
        effort=context.get("effort"),
    )
    result, metadata = evaluate_fit(
        diagnostic, context, offer, extraction_provenance=extraction_provenance
    )
    metadata["request_user"] = user
    metadata["request_budget"] = budget
    return result, metadata
