"""Tests for source-specific structured facts used by the local filter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from smartapply.filtering import JobFilter, ruleset_from_preferences
from smartapply.filtering.source_facts import (
    build_filter_facts,
    build_francetravail_filter_facts,
    build_serpapi_filter_facts,
    build_wttj_filter_facts,
)
from smartapply.profile import get_profile


@dataclass
class FakeJob:
    title: str = "Data Scientist"
    company: str = "Acme"
    description: str = "Python, SQL et machine learning."
    location: str | None = "Paris"
    contract_type: str | None = "CDI"
    remote_policy: str | None = None
    application_url: str | None = None
    source: str | None = "francetravail"
    source_data: dict[str, Any] | None = field(default_factory=dict)


def _real_filter(*, use_source_facts: bool = True) -> JobFilter:
    rules = ruleset_from_preferences(get_profile().preferences)
    return JobFilter(rules, use_source_facts=use_source_facts)


def test_francetravail_smart_experience_min_years_is_used() -> None:
    facts = build_francetravail_filter_facts(
        {
            "_smartapply_experience": {
                "requirement": "required",
                "required": True,
                "min_years": 3,
            },
        }
    )

    assert facts.experience_min_years == 3
    assert facts.experience_required is True
    assert "experience_min_years:3" in facts.facts_used


@pytest.mark.parametrize(
    ("band", "minimum"),
    [("LESS_THAN_6_MONTHS", 0.0), ("4_TO_5_YEARS", 4.0)],
)
def test_wttj_eligible_experience_bands_preserve_the_lower_bound(
    band: str, minimum: float,
) -> None:
    facts = build_wttj_filter_facts({"experience_level": band})

    assert facts.experience_min_years == minimum
    assert facts.experience_required is True


def test_francetravail_alternance_contract_rejected_from_structured_facts() -> None:
    job = FakeJob(
        contract_type=None,
        source_data={
            "typeContrat": "CDD",
            "typeContratLibelle": "Alternance",
            "natureContrat": "Contrat apprentissage",
            "lieuTravail": {"libelle": "75 - Paris"},
        },
    )

    result = _real_filter().evaluate(job)

    assert not result.kept
    assert any(
        reason.startswith("blocked_contract_structured:apprenticeship") for reason in result.reasons
    )


def test_francetravail_professionalization_contract_is_apprenticeship() -> None:
    facts = build_francetravail_filter_facts(
        {
            "typeContrat": "CDD",
            "typeContratLibelle": "Contrat de professionnalisation",
            "natureContrat": "Cont. professionnalisation",
        }
    )

    assert facts.structured_contract_type == "Apprenticeship"
    assert facts.structured_contract_source == "francetravail:natureContrat"


@pytest.mark.parametrize(
    "source_data",
    [
        {"dureeTravailLibelle": "1H/semaine"},
        {
            "dureeTravailLibelle": "Temps partiel - 1H/semaine\nTravail en journée",
            "dureeTravailLibelleConverti": "Temps partiel",
        },
    ],
)
def test_francetravail_one_hour_week_is_flagged_as_implausible(
    source_data: dict[str, Any],
) -> None:
    facts = build_francetravail_filter_facts(source_data)

    assert facts.structured_work_time == "Temps partiel"
    assert "structured_work_time_implausible_weekly_hours:1" in facts.warnings


def test_francetravail_plausible_part_time_hours_remain_trusted() -> None:
    facts = build_francetravail_filter_facts(
        {"dureeTravailLibelle": "20H/semaine"}
    )

    assert facts.structured_work_time == "Temps partiel"
    assert not any(
        warning.startswith("structured_work_time_implausible_weekly_hours:")
        for warning in facts.warnings
    )


def test_serpapi_schedule_type_internship_is_rejected() -> None:
    job = FakeJob(
        source="serpapi",
        contract_type=None,
        source_data={"detected_extensions": {"schedule_type": "Internship"}},
    )

    facts = build_serpapi_filter_facts(job.source_data or {})
    result = _real_filter().evaluate(job)

    assert facts.structured_contract_type == "Internship"
    assert not result.kept
    assert any(
        reason.startswith("blocked_contract_structured:internship") for reason in result.reasons
    )


def test_wttj_full_time_contract_and_hybrid_remote_are_extracted() -> None:
    facts = build_wttj_filter_facts(
        {
            "matches_api": {
                "contract_type": "full_time",
                "remote": "partial",
                "office": {"city": "Paris", "country_code": "FR"},
            },
            "_smartapply_search": {"source_mode": "personalized_matches", "pages": 5},
        }
    )

    assert facts.structured_contract_type == "Full-time"
    assert facts.structured_contract_source == ("welcometothejungle:matches_api.contract_type")
    assert facts.structured_remote_policy == "hybrid"
    assert facts.structured_location == "Paris, FR"
    assert facts.structured_search_origin == "personalized_matches"
    assert facts.structured_search_chips == "pages=5"


def test_linkedin_filter_facts_extract_contract_location_experience_and_search_context() -> None:
    job = FakeJob(
        source="linkedin",
        contract_type=None,
        source_data={
            "location": "Paris, Île-de-France, France",
            "experienceLevel": "Mid-Senior level",
            "contractType": "Full-time",
            "workType": "Hybrid",
            "_smartapply_search": {
                "datePosted": "r86400",
                "contractType": ["F"],
                "experienceLevel": ["4"],
                "remote": ["1", "2", "3"],
                "experience_fallback_used": True,
            },
        },
    )

    facts = build_filter_facts(job)

    assert facts.source == "linkedin"
    assert facts.structured_contract_type == "Full-time"
    assert facts.structured_contract_source == "linkedin:contractType"
    assert facts.structured_location == "Paris, Île-de-France, France"
    assert facts.structured_remote_policy == "hybrid"
    assert facts.experience_requirement == "Mid-Senior level"
    assert facts.experience_source == "linkedin:experienceLevel"
    assert facts.structured_search_origin == "apify_linkedin_jobs_scraper"
    assert "datePosted=r86400" in (facts.structured_search_chips or "")
    assert "experienceLevel=4" in (facts.structured_search_chips or "")
    assert "experience_fallback_used=True" in (facts.structured_search_chips or "")


def test_wttj_high_experience_level_is_rejected_from_structured_facts() -> None:
    job = FakeJob(
        source="welcometothejungle",
        contract_type="Full-time",
        source_data={
            "experience_level": "5_TO_10_YEARS",
            "matches_api": {
                "contract_type": "full_time",
                "office": {"city": "Paris", "country_code": "FR"},
            },
        },
    )

    result = _real_filter().evaluate(job)

    assert not result.kept
    assert any(
        reason.startswith("experience_structured_welcometothejungle:5") for reason in result.reasons
    )
    assert any("experience_required_too_high:5+ years" in reason for reason in result.reasons)


def test_serpapi_schedule_type_prestataire_is_contractor_like_when_corroborated() -> None:
    job = FakeJob(
        source="serpapi",
        contract_type=None,
        description="Mission freelance avec un TJM de 550 euros par jour.",
        source_data={"detected_extensions": {"schedule_type": "Prestataire"}},
    )

    facts = build_filter_facts(job)
    result = _real_filter().evaluate(job)

    assert facts.structured_contract_type == "Prestataire"
    assert facts.structured_contract_source == ("serpapi:detected_extensions.schedule_type")
    assert not result.kept
    assert any(
        reason.startswith("blocked_contract_structured:prestataire") and "contractor" in reason
        for reason in result.reasons
    )


def test_unknown_source_returns_empty_facts() -> None:
    facts = build_filter_facts(FakeJob(source="unknown", source_data={"typeContratLibelle": "CDI"}))

    assert facts.source == "unknown"
    assert facts.experience_min_years is None
    assert facts.structured_contract_type is None
    assert facts.structured_location is None
    assert facts.structured_remote_policy is None
    assert facts.structured_search_origin is None
    assert facts.structured_search_chips is None
    assert facts.structured_alternance is None
    assert facts.structured_work_time is None
    assert facts.structured_rome_code is None
