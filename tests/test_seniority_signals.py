"""Tests for the policy-neutral seniority assessment."""

from __future__ import annotations

import pytest

from smartapply.filtering.seniority import (
    SeniorityAssessmentStatus,
    SenioritySignalKind,
    assess_seniority,
)


@pytest.mark.parametrize(
    ("title", "description", "kind", "quote"),
    [
        (
            "Senior Data Scientist",
            "Construire des modèles.",
            SenioritySignalKind.EXPLICIT_TITLE,
            "senior data scientist",
        ),
        (
            "Data Scientist",
            "This is a staff machine learning role focused on forecasting.",
            SenioritySignalKind.HIDDEN_ROLE,
            "this is a staff machine learning role focused on forecasting",
        ),
        (
            "Responsable Data",
            "Vous managerez une équipe de data scientists.",
            SenioritySignalKind.HIERARCHICAL_MANAGEMENT,
            "vous managerez une equipe de data scientists",
        ),
    ],
)
def test_assessment_exposes_stable_blocker_evidence(
    title: str,
    description: str,
    kind: SenioritySignalKind,
    quote: str,
) -> None:
    assessment = assess_seniority(title, description)

    assert assessment.status is SeniorityAssessmentStatus.BLOCKED
    assert assessment.blocked
    assert assessment.kind is kind
    assert assessment.quote == quote


def test_ambiguous_title_is_unresolved_without_description_blocker() -> None:
    assessment = assess_seniority(
        "Data Scientist Junior / Senior",
        "Construire des modèles.",
    )

    assert assessment.status is SeniorityAssessmentStatus.UNRESOLVED
    assert not assessment.blocked
    assert assessment.kind is SenioritySignalKind.AMBIGUOUS_TITLE
    assert assessment.conflict == "multiple_seniority_levels_in_title"
    assert assessment.quote == "data scientist junior / senior"


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Lead Generation Data Analyst", "Construire des modèles."),
        ("Data Scientist", "You may grow into a team lead role in the future."),
        ("Data Scientist", "Collaborate with senior data scientists."),
    ],
)
def test_contextual_mentions_do_not_establish_a_blocker(
    title: str,
    description: str,
) -> None:
    assessment = assess_seniority(title, description)

    assert assessment.status is SeniorityAssessmentStatus.CLEAR
    assert not assessment.blocked


def test_configured_title_marker_is_assessed_without_filter_dependency() -> None:
    assessment = assess_seniority(
        "Expert Data Scientist",
        "Construire des modèles.",
        configured_title_markers=("expert",),
    )

    assert assessment.blocked
    assert assessment.kind is SenioritySignalKind.EXPLICIT_TITLE
    assert assessment.title_marker == "expert"
