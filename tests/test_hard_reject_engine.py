"""Architecture tests for the deterministic rejection stage."""

from __future__ import annotations

from dataclasses import dataclass

from smartapply.filtering import (
    HardRejectDisposition,
    JobFilter,
    ruleset_from_preferences,
)
from smartapply.profile import get_profile


@dataclass
class FakeJob:
    title: str
    company: str
    description: str
    location: str | None = "Paris"
    contract_type: str | None = "CDI"
    remote_policy: str | None = None


def _rejector() -> JobFilter:
    return JobFilter(ruleset_from_preferences(get_profile().preferences))


def test_survivor_is_pending_without_a_relevance_decision_or_score() -> None:
    result = _rejector().evaluate(
        FakeJob(
            title="Machine Learning Engineer",
            company="Acme",
            description="Concevoir et déployer des modèles de machine learning avec Python.",
        )
    )

    assert result.kept
    assert not hasattr(result, "score")
    assert result.disposition is HardRejectDisposition.PENDING_RELEVANCE


def test_company_history_does_not_become_a_candidate_seniority_requirement() -> None:
    result = _rejector().evaluate(
        FakeJob(
            title="Data Scientist",
            company="Acme",
            description=(
                "We have 20+ years of experience building software. "
                "You will build machine-learning models."
            ),
        )
    )

    assert result.disposition is HardRejectDisposition.PENDING_RELEVANCE
    assert not any(reason.startswith("role_relevance:") for reason in result.reasons)
    assert {decision.gate for decision in result.decisions} == {
        "language",
        "contract",
        "experience",
        "qualification",
        "seniority",
        "role",
    }


def test_all_gates_run_and_every_blocker_contains_evidence() -> None:
    result = _rejector().evaluate(
        FakeJob(
            title="Senior Data Scientist - Stage",
            company="Acme",
            description="Stage demandant au minimum 7 ans d'expérience.",
            contract_type="Stage",
        )
    )

    blocked = [decision for decision in result.decisions if decision.blocked]
    assert result.rejected
    assert not hasattr(result, "score")
    assert result.disposition is HardRejectDisposition.REJECTED
    assert len(result.decisions) == 6
    assert {decision.gate for decision in blocked} >= {
        "contract",
        "experience",
        "seniority",
    }
    assert all(decision.evidence for decision in blocked)
    assert set(result.blocking_reasons) == {
        reason
        for decision in blocked
        for reason in decision.reason_codes
    }
    assert result.audit_payload()["ruleset_version"] == "hard-reject-v2"


def test_clear_gate_notes_are_not_exposed_as_blocking_reasons() -> None:
    result = _rejector().evaluate(
        FakeJob(
            title="Machine Learning Engineer",
            company="Acme",
            description=(
                "Stage demandant 7 ans d'expérience. "
                "Vous construirez des modèles de machine learning."
            ),
            contract_type="Stage",
        )
    )

    assert result.rejected
    role_note = next(
        reason for reason in result.reasons if reason.startswith("role_title_veto:")
    )
    assert role_note not in result.blocking_reasons
