"""Candidate evaluation prompts over immutable offer facts."""

from __future__ import annotations

from datetime import date

from smartapply.llm.fit_context import (
    build_fit_context,
    build_fit_user,
)
from smartapply.llm.fit_context import (
    candidate_facts as _candidate_facts,
)
from smartapply.llm.prompts.loader import load_prompt
from smartapply.llm.schemas import OfferAnalysis
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
    return build_fit_user(
        build_fit_context(profile, "prompt-preview"),
        offer_analysis,
        extraction_provenance=extraction_provenance,
    )


__all__ = ["build_system_prompt", "build_user_prompt", "_candidate_facts"]
