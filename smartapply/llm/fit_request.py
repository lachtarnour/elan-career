"""One evaluation request; usable levels are independent of annotation quality."""

from __future__ import annotations

import json

from smartapply.llm.fit_context import build_fit_user
from smartapply.llm.fit_scoring import evaluate_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.schemas import JobFitEvaluation

# Official model pages, verified 2026-10-08. Unknown aliases need explicit config.
# https://developers.openai.com/api/docs/models/gpt-5.4-mini
# https://developers.openai.com/api/docs/models/gpt-5.4
CONTEXT_WINDOWS = {"gpt-5.4-mini": 400_000, "gpt-5.4": 1_050_000}


def check_request_budget(context: dict, user: str) -> dict:
    limit = context["configured_context_window"]
    if limit is None:
        for alias in sorted(CONTEXT_WINDOWS, key=len, reverse=True):
            if context["model"] == alias or context["model"].startswith(alias + "-"):
                limit = CONTEXT_WINDOWS[alias]
                break
    if limit is None and context["model"].startswith("mock"):
        limit = 400_000
    if limit is None:
        raise LLMValidationError(
            "Unknown model context window; configure OPENAI_CONTEXT_WINDOW_TOKENS"
        )
    body = {
        "model": context["model"],
        "temperature": context["temperature"],
        "max_completion_tokens": context["max_completion_tokens"],
        "messages": [
            {"role": "system", "content": context["system_prompt"]},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "JobFitEvaluation",
                "strict": True,
                "schema": context["schema"],
            },
        },
    }
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
    """Return a score even when the evidence inspection reports warnings."""
    user = build_fit_user(context, offer)
    budget = check_request_budget(context, user)
    diagnostic = provider.complete_json(
        system=context["system_prompt"],
        user=user,
        schema=JobFitEvaluation,
        model=context["model"],
        temperature=context["temperature"],
        purpose="job_fit_assessment",
        job_id=job_id,
        max_completion_tokens=context["max_completion_tokens"],
    )
    result, metadata = evaluate_fit(
        diagnostic, context, offer, extraction_provenance=extraction_provenance
    )
    metadata["request_user"] = user
    metadata["request_budget"] = budget
    return result, metadata
