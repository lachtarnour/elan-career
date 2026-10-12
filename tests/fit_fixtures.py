"""Synthetic structured diagnostics for workflow tests, not semantic evaluations."""

import json

from smartapply.config import get_llm_settings
from smartapply.llm_policy import resolve_route
from smartapply.pipeline.process.scope_screening import scope_context


def screening_context():
    settings = get_llm_settings()
    route = resolve_route(settings, "job_scope_screening")
    return scope_context(
        provider=route.provider,
        model=route.model,
        settings=settings,
        max_completion_tokens=route.max_output_tokens,
        effort=route.reasoning,
    )


def decision_response(*, states=("strong_fit", "strong_fit", "credible_fit")):
    return {
        "decision": {
            **dict(zip(("missions", "essentials", "responsibilities"), states, strict=True)),
        }
    }


def global_response(*, score=0.88, reasons=None):
    return {"fit_score": score, "match_reasons": reasons or ["Travail documenté"], "risks": []}


def evaluation_from_user(user, *, score=0.88, reasons=None):
    from smartapply.llm.schemas import JobFitAssessment

    candidate = json.loads(
        user.split("=== CANDIDATE PROFILE JSON ===\n", 1)[1].split("\n\n===", 1)[0]
    )
    offer = json.loads(
        user.split("=== IMMUTABLE OFFER ANALYSIS JSON ===\n", 1)[1].split("\n\n", 1)[0]
    )
    assert candidate["experiences"] and "main_tasks" in offer
    return JobFitAssessment(**global_response(score=score, reasons=reasons))
