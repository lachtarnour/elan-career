"""Candidate evaluation prompts over immutable offer facts."""

from __future__ import annotations

from datetime import date

from smartapply.config import get_llm_settings
from smartapply.llm.fit_context import (
    build_fit_context,
    build_fit_user,
)
from smartapply.llm.fit_context import (
    candidate_facts as _candidate_facts,
)
from smartapply.llm.prompts.loader import load_prompt
from smartapply.llm.schemas import OfferAnalysis
from smartapply.llm_policy import resolve_route
from smartapply.profile import Profile


def build_system_prompt() -> str:
    return load_prompt("job_fit/system.j2")


def build_user_prompt(
    *,
    profile: Profile,
    offer_analysis: OfferAnalysis,
    reference_date: date | None = None,
    extraction_provenance: dict | None = None,
) -> str:
    """reference_date is kept for callers; it is not a scoring criterion."""
    settings = get_llm_settings()
    route = resolve_route(settings, "job_fit_assessment")
    return build_fit_user(
        build_fit_context(
            profile, route.model, provider=route.provider,
            max_completion_tokens=route.max_output_tokens,
            settings=settings, effort=route.reasoning,
        ),
        offer_analysis,
        extraction_provenance=extraction_provenance,
    )


__all__ = ["build_system_prompt", "build_user_prompt", "_candidate_facts"]
