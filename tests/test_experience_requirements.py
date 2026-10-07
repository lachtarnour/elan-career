"""Public contract for the neutral candidate-experience assessment."""

from __future__ import annotations

from smartapply.filtering.experience_requirements import (
    ExperienceAssessment,
    assess_candidate_experience,
)
from smartapply.filtering.facts import FilterFacts


def _assess(
    description: str,
    *,
    facts: FilterFacts | None = None,
) -> ExperienceAssessment:
    return assess_candidate_experience(
        title="Data Scientist",
        description=description,
        facts=facts or FilterFacts(),
        max_required_years=5,
    )


def test_assessment_preserves_source_and_numeric_requirement() -> None:
    assessment = _assess(
        "Vous devez justifier de 5 ans d'expérience en machine learning."
    )

    assert assessment.conflict is None
    assert assessment.resolved_mandatory_years == (5.0,)
    assert len(assessment.visible) == 1
    requirement = assessment.visible[0]
    assert requirement.required is True
    assert requirement.source == "visible_description"
    assert requirement.field == "description"
    assert requirement.quote == (
        "vous devez justifier de 5 ans d'experience en machine learning"
    )


def test_optional_requirement_remains_visible_without_becoming_mandatory() -> None:
    assessment = _assess("Idéalement 7 ans d'expérience en data science.")

    assert len(assessment.visible) == 1
    assert assessment.visible[0].required is False
    assert assessment.mandatory == ()
    assert assessment.resolved_mandatory_years == ()


def test_conflict_suppresses_numeric_signal_but_preserves_both_sources() -> None:
    assessment = _assess(
        "Vous devez justifier de 3 ans d'expérience.",
        facts=FilterFacts(
            experience_min_years=5,
            experience_required=True,
            experience_requirement="5_TO_10_YEARS",
            experience_source="ats:experience_level",
        ),
    )

    assert assessment.conflict == "structured=5y;visible=3y"
    assert assessment.structured is not None
    assert assessment.structured.source == "ats:experience_level"
    assert len(assessment.visible) == 1
    assert assessment.resolved_mandatory_years == ()
