"""Tests for conservative, provider-specific duplicate identity evidence."""

from types import SimpleNamespace

import pytest

from smartapply.dedup.review_policy import strong_identity_keys


def _job(*, source="serpapi", source_data=None, url=None, external_id="shared-id"):
    return SimpleNamespace(
        source=source,
        source_data=source_data,
        application_url=url,
        external_id=external_id,
    )


def test_france_travail_native_id_and_direct_url_share_one_key() -> None:
    native = _job(source="francetravail", source_data={"id": "213HYVJ"})
    url = _job(url="https://candidat.francetravail.fr/offres/recherche/detail/213hyvj")

    assert strong_identity_keys(native) == strong_identity_keys(url) == frozenset(
        {"offer:francetravail:213hyvj"}
    )


def test_linkedin_slug_url_and_structured_native_id_share_one_key() -> None:
    native = _job(source="LinkedIn", source_data={"id": "4464028174"})
    url = _job(
        url=(
            "https://fr.linkedin.com/jobs/view/"
            "senior-data-scientist-at-example-4464028174?position=2"
        )
    )

    assert strong_identity_keys(native) == strong_identity_keys(url) == frozenset(
        {"offer:linkedin:4464028174"}
    )


def test_linkedin_source_url_is_used_when_application_url_points_to_external_ats() -> None:
    job = _job(
        source="linkedin",
        source_data={
            "_smartapply_normalized": {
                "url": "https://www.linkedin.com/jobs/view/4464028174",
            }
        },
        url="https://ats.example.test/apply/42",
    )

    assert strong_identity_keys(job) == frozenset({"offer:linkedin:4464028174"})


def test_generic_job_url_and_external_id_are_not_strong_identity() -> None:
    first = _job(url="https://careers.example.test/jobs/team-data", external_id="same")
    second = _job(url="https://careers.example.test/jobs/team-data", external_id="same")

    assert strong_identity_keys(first) == strong_identity_keys(second) == frozenset()


def test_native_ids_are_namespaced_by_trusted_source() -> None:
    linkedin = _job(source="linkedin", source_data={"id": "4464028174"})
    france_travail = _job(source="france-travail", source_data={"id": "4464028174"})

    assert strong_identity_keys(linkedin).isdisjoint(strong_identity_keys(france_travail))


def test_listing_and_malformed_urls_are_not_identity_evidence() -> None:
    assert strong_identity_keys(
        _job(url="https://www.linkedin.com/jobs/search/?keywords=data")
    ) == frozenset()
    assert strong_identity_keys(_job(url="https://[::1/jobs/42")) == frozenset()


def test_conflicting_native_and_url_ids_remove_all_identity_evidence() -> None:
    job = _job(
        source="linkedin",
        source_data={
            "id": "4464028174",
            "url": "https://www.linkedin.com/jobs/view/4464028175",
        },
        url="https://candidat.francetravail.fr/offres/recherche/detail/213HYVJ",
    )

    assert strong_identity_keys(job) == frozenset()


def test_conflicting_provider_urls_remove_all_identity_evidence() -> None:
    job = _job(
        source_data={
            "url": "https://candidat.francetravail.fr/offres/recherche/detail/213HYVJ",
            "_smartapply_normalized": {
                "url": "https://candidat.francetravail.fr/offres/recherche/detail/206GJTL",
            },
        },
    )

    assert strong_identity_keys(job) == frozenset()


def test_repeated_provider_id_across_fields_keeps_one_identity() -> None:
    job = _job(
        source="france travail",
        source_data={
            "id": "213HYVJ",
            "url": "https://candidat.francetravail.fr/offres/recherche/detail/213hyvj",
        },
        url="https://www.francetravail.fr/offres/recherche/detail/213HYVJ?tracking=1",
    )

    assert strong_identity_keys(job) == frozenset({"offer:francetravail:213hyvj"})


@pytest.mark.parametrize(
    "native_id",
    ["search", "abcdefg", "123456", "12345678", "search1"],
)
def test_france_travail_native_id_requires_exact_offer_shape(native_id) -> None:
    assert strong_identity_keys(
        _job(source="francetravail", source_data={"id": native_id})
    ) == frozenset()


@pytest.mark.parametrize("leaf", ["search", "results", "abcdefg", "123456", "12345678"])
def test_france_travail_generic_or_invalid_detail_leaf_is_not_identity(leaf) -> None:
    assert strong_identity_keys(
        _job(url=f"https://candidat.francetravail.fr/offres/recherche/detail/{leaf}")
    ) == frozenset()
