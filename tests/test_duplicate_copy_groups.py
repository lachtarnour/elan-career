"""Tests for pure strict-copy fingerprints and complete-link grouping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from smartapply.dedup.copy_groups import (
    MIN_STRICT_COPY_DESCRIPTION_CHARS,
    group_strict_copies,
    strict_copy_classification_key,
    strict_copy_fingerprint,
)

_BASE_TIME = datetime(2026, 10, 1, tzinfo=timezone.utc)
_DESCRIPTION = (
    "Build production machine learning pipelines with Python, PyTorch and SQL. "
    "Own evaluation, monitoring, deployment, documentation and collaboration "
    "with product teams. Preserve requirement 42 and the complete source text. "
) * 2


def _job(job_id: int, **updates):
    values = {
        "id": job_id,
        "title": "Senior Data Scientist C++",
        "company": "Example Énergie SAS",
        "location": "Paris",
        "contract_type": "CDI",
        "remote_policy": "Hybride",
        "description": _DESCRIPTION,
        "cleaned_description": "must never drive a strict-copy fingerprint",
        "published_date": _BASE_TIME,
        "scraped_at": _BASE_TIME,
        "source_data": None,
        "canonical_job_id": None,
        "status": "scraped",
    }
    values.update(updates)
    return SimpleNamespace(**values)


def test_fingerprint_normalizes_only_case_and_whitespace() -> None:
    first = _job(1)
    second = _job(
        2,
        title="  senior   data SCIENTIST c++ ",
        company="example énergie sas",
        contract_type=" cdi ",
        remote_policy="HYBRIDE",
        description=_DESCRIPTION.swapcase().replace(" ", "  "),
    )

    assert strict_copy_fingerprint(first) == strict_copy_fingerprint(second)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Junior Data Scientist C++"),
        ("title", "Senior Data Scientist C#"),
        ("company", "Example Énergie Groupe SAS"),
        ("contract_type", "CDD"),
        ("remote_policy", "Remote"),
        ("description", _DESCRIPTION.replace("requirement 42", "requirement 43")),
    ],
)
def test_fingerprint_preserves_role_and_original_offer_details(field, value) -> None:
    assert strict_copy_fingerprint(_job(1)) != strict_copy_fingerprint(
        _job(2, **{field: value})
    )


@pytest.mark.parametrize(
    "updates",
    [
        {"company": "Entreprise non communiquée"},
        {"location": "France"},
        {"contract_type": None},
        {"description": "x" * (MIN_STRICT_COPY_DESCRIPTION_CHARS - 1)},
    ],
)
def test_incomplete_or_low_information_offer_has_no_fingerprint(updates) -> None:
    assert strict_copy_fingerprint(_job(1, **updates)) is None


def test_grouping_uses_inclusive_window_without_transitive_bridge() -> None:
    jobs = [
        _job(3, published_date=_BASE_TIME + timedelta(days=40)),
        _job(1),
        _job(2, published_date=_BASE_TIME + timedelta(days=20)),
    ]
    snapshots = [vars(job).copy() for job in jobs]

    assert group_strict_copies(jobs) == ((1, 2),)
    assert group_strict_copies(reversed(jobs)) == ((1, 2),)
    assert [vars(job) for job in jobs] == snapshots


def test_grouping_keeps_the_thirty_day_boundary() -> None:
    assert group_strict_copies(
        [_job(1), _job(2, published_date=_BASE_TIME + timedelta(days=30))]
    ) == ((1, 2),)


def test_grouping_checks_explicit_geographic_conflicts_pairwise() -> None:
    structured_paris = {"lieuTravail": {"commune": "75056"}}
    france = _job(1, location="Paris, France", source_data=structured_paris)
    germany = _job(2, location="Paris, Allemagne", source_data=structured_paris)

    assert strict_copy_fingerprint(france) == strict_copy_fingerprint(germany)
    assert group_strict_copies([france, germany]) == ()


def test_explicit_employer_reference_separates_template_copies() -> None:
    first = _job(1, source_data={"employer_reference": "REQ-2026-42"})
    second = _job(2, source_data={"requisition_id": "req-2026-43"})

    assert strict_copy_fingerprint(first) != strict_copy_fingerprint(second)
    assert group_strict_copies([first, second]) == ()


def test_reference_aliases_match_but_portal_ids_do_not_affect_copy_content() -> None:
    first = _job(
        1,
        source_data={"id": "portal-row-1", "employer_reference": " REQ-2026-42 "},
    )
    second = _job(
        2,
        source_data={"id": "portal-row-2", "requisition_id": "req-2026-42"},
    )

    assert strict_copy_fingerprint(first) == strict_copy_fingerprint(second)
    assert group_strict_copies([first, second]) == ((1, 2),)


def test_conflicting_explicit_employer_references_are_not_fingerprinted() -> None:
    job = _job(
        1,
        source_data={"employer_reference": "REQ-42", "requisition_id": "REQ-43"},
    )

    assert strict_copy_fingerprint(job) is None


def test_classification_key_versions_content_profile_prompt_model_and_classifier() -> None:
    first = _job(1)
    copy = _job(2, description=_DESCRIPTION.swapcase())
    kwargs = {
        "profile_version": "profile-v4",
        "classifier_version": "classifier-v1",
        "prompt_version": "prompt-v2",
        "model_id": "model-a",
    }
    baseline = strict_copy_classification_key(first, **kwargs)

    assert baseline == strict_copy_classification_key(copy, **kwargs)
    for field, value in (
        ("profile_version", "profile-v5"),
        ("classifier_version", "classifier-v2"),
        ("prompt_version", "prompt-v3"),
        ("model_id", "model-b"),
    ):
        changed = {**kwargs, field: value}
        assert strict_copy_classification_key(first, **changed) != baseline


def test_classification_key_rejects_an_empty_profile_version() -> None:
    with pytest.raises(ValueError, match="profile_version"):
        strict_copy_classification_key(
            _job(1),
            profile_version="",
            classifier_version="classifier-v1",
            prompt_version="prompt-v1",
            model_id="model-a",
        )
