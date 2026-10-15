"""Construct the canonical local filter from the active candidate profile."""

from __future__ import annotations

from smartapply.filtering.filters import DeterministicRejector, ruleset_from_preferences
from smartapply.filtering.hard_reject import CandidateQualifications
from smartapply.profile import Profile, get_profile


def build_rejector(profile: Profile | None = None) -> DeterministicRejector:
    """Build the same deterministic rejector used by the production pipeline."""
    candidate = profile or get_profile()
    return DeterministicRejector(
        ruleset_from_preferences(candidate.preferences),
        qualifications=CandidateQualifications.from_profile(candidate),
    )
