"""Tests for the dedup module."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from smartapply.dedup import Deduplicator, fuzzy_location_score


@dataclass
class FakeJob:
    external_id: str
    title: str
    company: str
    description: str = ""
    published_date: datetime | None = None
    scraped_at: datetime | None = None
    location: str | None = None
    source_data: dict[str, object] | None = None
    id: int | None = None
    possible_duplicate_of_id: int | None = None
    canonical_job_id: int | None = None


def test_deduplicator_groups_identical_jobs_across_sources() -> None:
    desc = "Build ML pipelines, deploy to AWS. PyTorch, Hugging Face."
    jobs = [
        FakeJob("serpapi:1", "Data Scientist H/F", "Acme SAS", desc),
        FakeJob("francetravail:1", "Data Scientist", "ACME", desc),
        FakeJob("manual:1", "ML Engineer", "Acme", "Different role focused on infra"),
    ]
    rep = Deduplicator().deduplicate(jobs)
    assert len(rep.unique) == 2
    assert len(rep.duplicate_groups) == 1
    assert {j.external_id for j in rep.duplicate_groups[0]} == {
        "serpapi:1",
        "francetravail:1",
    }


def test_deduplicator_keeps_distinct_companies_apart() -> None:
    jobs = [
        FakeJob("a", "Data Scientist", "Acme", "build pipelines"),
        FakeJob("b", "Data Scientist", "Beta", "build pipelines"),
    ]
    rep = Deduplicator().deduplicate(jobs)
    assert len(rep.unique) == 2
    assert rep.duplicate_groups == []


def test_dedup_report_counts_removed() -> None:
    desc = "pipeline pytorch"
    jobs = [
        FakeJob("a", "Data Scientist", "Acme", desc),
        FakeJob("b", "Data Scientist H/F", "Acme SAS", desc),
        FakeJob("c", "Data Scientist", "Acme Inc", desc),
        FakeJob("d", "ML Engineer", "Beta", "completely different"),
    ]
    rep = Deduplicator().deduplicate(jobs)
    assert len(rep.unique) == 2
    assert rep.n_duplicates_removed == 2


def test_deduplicator_keeps_republication_over_one_month_apart() -> None:
    published = datetime(2026, 8, 1, tzinfo=timezone.utc)
    jobs = [
        FakeJob("old", "Data Scientist", "Acme", "pipeline pytorch", published_date=published),
        FakeJob(
            "new",
            "Data Scientist",
            "Acme",
            "pipeline pytorch",
            published_date=published + timedelta(days=31),
        ),
    ]

    report = Deduplicator().deduplicate(jobs)

    assert report.unique == jobs
    assert report.duplicate_groups == []


def test_probable_duplicate_uses_fetch_date_when_publication_date_is_missing() -> None:
    fetched = datetime(2026, 8, 1, tzinfo=timezone.utc)
    old = FakeJob(
        "old",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        scraped_at=fetched,
    )
    new = FakeJob(
        "new",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        scraped_at=fetched + timedelta(days=31),
    )

    assert Deduplicator().find_probable_duplicate(new, [old]) is None


def test_exactly_thirty_days_remains_inside_duplicate_window() -> None:
    published = datetime(2026, 8, 1, tzinfo=timezone.utc)
    old = FakeJob(
        "old",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        published_date=published,
    )
    new = FakeJob(
        "new",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        published_date=published + timedelta(days=30),
    )

    assert Deduplicator().find_probable_duplicate(new, [old]) is not None


def test_anonymous_company_is_never_used_as_fuzzy_identity() -> None:
    jobs = [
        FakeJob(
            "first",
            "Data Scientist",
            "Entreprise non communiquée",
            "pipeline pytorch machine learning",
            location="75 - Paris",
        ),
        FakeJob(
            "second",
            "Data Scientist",
            "Entreprise non communiquée",
            "pipeline pytorch machine learning",
            location="75 - Paris",
        ),
    ]
    matcher = Deduplicator()

    assert matcher.find_probable_duplicate(jobs[1], jobs[:1]) is None
    report = matcher.deduplicate(jobs)
    assert report.unique == jobs
    assert report.duplicate_groups == []


def test_fuzzy_match_rejects_different_precise_communes() -> None:
    first = FakeJob(
        "first",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="35 - Sainte-Marie",
        source_data={"lieuTravail": {"commune": "35294"}},
    )
    second = FakeJob(
        "second",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="35 - Pont-Péan",
        source_data={"lieuTravail": {"commune": "35363"}},
    )
    matcher = Deduplicator()

    assert matcher.find_probable_duplicate(second, [first]) is None
    assert matcher.deduplicate([first, second]).unique == [first, second]


def test_fuzzy_match_understands_english_french_region_label() -> None:
    toulouse = FakeJob(
        "toulouse",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="Toulouse, Occitanie, France",
    )
    rennes = FakeJob(
        "rennes",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="Rennes, Brittany, France",
    )

    assert Deduplicator().find_probable_duplicate(toulouse, [rennes]) is None


def test_fuzzy_match_accepts_equivalent_city_formats_and_caps_confidence() -> None:
    first = FakeJob(
        "first",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="75 - Paris 9e Arrondissement",
    )
    second = FakeJob(
        "second",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="Paris, Île-de-France, France",
    )

    match = Deduplicator().find_probable_duplicate(second, [first])

    assert match is not None
    assert match.match_type == "fuzzy"
    assert match.location_score == 100.0
    assert match.confidence == 0.99


def test_broad_location_does_not_block_fuzzy_review() -> None:
    regional = FakeJob(
        "regional",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="Île-de-France",
    )
    paris = FakeJob(
        "paris",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="75 - Paris",
    )

    match = Deduplicator().find_probable_duplicate(paris, [regional])

    assert match is not None
    assert match.location_score is None


def test_same_precise_location_increases_fuzzy_confidence() -> None:
    target = FakeJob(
        "target",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines cloud",
        location="75 - Paris",
    )
    same_city = FakeJob(
        "same-city",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines production",
        location="Paris, Île-de-France, France",
    )
    unknown_location = FakeJob(
        "unknown-location",
        "Data Scientist",
        "Acme",
        same_city.description,
        location=None,
    )
    matcher = Deduplicator()

    located_match = matcher.find_probable_duplicate(target, [same_city])
    neutral_match = matcher.find_probable_duplicate(target, [unknown_location])

    assert located_match is not None
    assert neutral_match is not None
    assert located_match.location_score == 100.0
    assert neutral_match.location_score is None
    neutral_text_score = 0.55 * neutral_match.title_score + 0.45 * neutral_match.description_score
    expected_neutral = round(neutral_text_score / 100.0, 4)
    assert abs(neutral_match.confidence - expected_neutral) <= 0.0002
    expected_located = min(
        0.99,
        round(
            (
                0.9 * (0.55 * located_match.title_score + 0.45 * located_match.description_score)
                + 0.1 * located_match.location_score
            )
            / 100.0,
            4,
        ),
    )
    assert abs(located_match.confidence - expected_located) <= 0.0002
    assert located_match.confidence > neutral_match.confidence
    assert matcher.find_probable_duplicate(target, [unknown_location, same_city]).job is same_city


def test_same_france_travail_commune_code_has_full_location_score() -> None:
    first = FakeJob(
        "first",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines cloud",
        location="75 - Paris",
        source_data={"lieuTravail": {"commune": "75056"}},
    )
    second = FakeJob(
        "second",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines production",
        location="Paris, Île-de-France, France",
        source_data={"lieuTravail": {"commune": "75056"}},
    )

    match = Deduplicator().find_probable_duplicate(second, [first])

    assert match is not None
    assert match.location_score == 100.0


def test_location_cache_tracks_changed_location_evidence() -> None:
    left = FakeJob(
        "left",
        "Data Scientist",
        "Acme",
        location="Paris",
        source_data={"lieuTravail": {"commune": "75056"}},
    )
    right = FakeJob(
        "right",
        "Data Scientist",
        "Acme",
        location="Paris",
        source_data={"lieuTravail": {"commune": "75056"}},
    )

    assert fuzzy_location_score(left, right) == 100.0

    right.location = "Lyon"
    right.source_data = {"lieuTravail": {"commune": "69123"}}

    assert fuzzy_location_score(left, right) == 0.0


def test_same_city_label_does_not_override_different_commune_codes() -> None:
    mainland = FakeJob(
        "mainland",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines",
        location="Saint-Denis",
        source_data={"lieuTravail": {"commune": "93066"}},
    )
    overseas = FakeJob(
        "overseas",
        "Data Scientist",
        "Acme",
        mainland.description,
        location="Saint-Denis",
        source_data={"lieuTravail": {"commune": "97411"}},
    )

    assert fuzzy_location_score(mainland, overseas) == 0.0
    assert Deduplicator().find_probable_duplicate(mainland, [overseas]) is None


def test_same_city_label_in_different_departments_is_incompatible() -> None:
    brittany = FakeJob(
        "brittany",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines",
        location="35 - Sainte-Marie",
    )
    reunion = FakeJob(
        "reunion",
        "Data Scientist",
        "Acme",
        brittany.description,
        location="974 - Sainte-Marie",
    )

    assert fuzzy_location_score(brittany, reunion) == 0.0
    assert Deduplicator().find_probable_duplicate(brittany, [reunion]) is None


def test_administrative_context_disambiguates_homonymous_cities() -> None:
    mainland = FakeJob(
        "mainland",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines",
        location="Saint-Denis, Seine-Saint-Denis, France",
    )
    reunion = FakeJob(
        "reunion",
        "Data Scientist",
        "Acme",
        mainland.description,
        location="Saint-Denis, La Réunion, France",
    )
    bordeaux_department = FakeJob(
        "bordeaux-department",
        "Data Scientist",
        "Acme",
        mainland.description,
        location="Bordeaux, Gironde, France",
    )
    bordeaux_region = FakeJob(
        "bordeaux-region",
        "Data Scientist",
        "Acme",
        mainland.description,
        location="Bordeaux, Nouvelle-Aquitaine, France",
    )

    assert fuzzy_location_score(mainland, reunion) == 0.0
    assert fuzzy_location_score(bordeaux_department, bordeaux_region) == 100.0


def test_numeric_department_is_compared_with_textual_region() -> None:
    cases = (
        ("93 - Saint-Denis", "Saint-Denis, La Réunion, France", 0.0),
        ("974 - Saint-Denis", "Saint-Denis, Île-de-France, France", 0.0),
        ("35 - Sainte-Marie", "Sainte-Marie, La Réunion, France", 0.0),
        ("35 - Rennes", "Rennes, Brittany, France", 100.0),
    )

    for left_location, right_location, expected in cases:
        left = FakeJob("left", "Data Scientist", "Acme", "same description", location=left_location)
        right = FakeJob(
            "right",
            "Data Scientist",
            "Acme",
            "same description",
            location=right_location,
        )
        assert fuzzy_location_score(left, right) == expected


def test_corsican_postcodes_are_compatible_with_2a_and_2b_departments() -> None:
    cases = (
        ("2A - Ajaccio", "20000 Ajaccio", 100.0),
        ("2B - Bastia", "20200 Bastia", 100.0),
        ("2A - Ajaccio", "20200 Bastia", 0.0),
    )

    for left_location, right_location, expected in cases:
        left = FakeJob("left", "Data Scientist", "Acme", location=left_location)
        right = FakeJob("right", "Data Scientist", "Acme", location=right_location)
        assert fuzzy_location_score(left, right) == expected


def test_structured_location_without_commune_keeps_department_and_region_evidence() -> None:
    france_travail_department = FakeJob(
        "ft-department",
        "Data Scientist",
        "Acme",
        "same description",
        location="33 - Gironde",
        source_data={"lieuTravail": {"libelle": "Gironde"}},
    )
    other_department = FakeJob(
        "other-department",
        "Data Scientist",
        "Acme",
        "same description",
        location="35 - Chartres-de-Bretagne",
    )
    same_region = FakeJob(
        "same-region",
        "Data Scientist",
        "Acme",
        "same description",
        location="Bordeaux, Nouvelle-Aquitaine, France",
    )
    other_region = FakeJob(
        "other-region",
        "Data Scientist",
        "Acme",
        "same description",
        location="Guyancourt, Île-de-France, France",
    )

    assert fuzzy_location_score(france_travail_department, other_department) == 0.0
    assert fuzzy_location_score(france_travail_department, same_region) is None
    assert fuzzy_location_score(france_travail_department, other_region) == 0.0


def test_region_only_and_named_department_labels_are_compared() -> None:
    cases = (
        ("Pays de la Loire, France", "Grand Est, France", 0.0),
        ("Occitanie, France", "Bordeaux, Nouvelle-Aquitaine, France", 0.0),
        ("Yvelines, Île-de-France, France", "Seine-et-Marne, Île-de-France, France", 0.0),
        ("Bretagne, France", "Rennes, Brittany, France", None),
        ("La Réunion, Nouvelle-Aquitaine, France", "Bordeaux, Nouvelle-Aquitaine, France", None),
    )

    for left_location, right_location, expected in cases:
        left = FakeJob("left", "Data Scientist", "Acme", location=left_location)
        right = FakeJob("right", "Data Scientist", "Acme", location=right_location)
        assert fuzzy_location_score(left, right) == expected


def test_explicit_foreign_country_conflicts_with_french_department() -> None:
    belgium = FakeJob(
        "belgium",
        "Data Scientist",
        "Acme",
        location="Belgique",
        source_data={"lieuTravail": {"libelle": "Belgique"}},
    )
    massy = FakeJob("massy", "Data Scientist", "Acme", location="91 - Massy")

    assert fuzzy_location_score(belgium, massy) == 0.0


def test_multisite_location_remains_neutral_when_one_site_matches() -> None:
    multisite = FakeJob(
        "multisite",
        "Data Scientist",
        "Acme",
        location="Nantes, Loire-Atlantique, FR; Paris, Paris, FR",
    )
    paris = FakeJob(
        "paris",
        "Data Scientist",
        "Acme",
        location="Paris, Île-de-France, France",
    )

    assert fuzzy_location_score(multisite, paris) is None


def test_paris_arrondissement_codes_share_the_city_location_score() -> None:
    first = FakeJob(
        "first",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines cloud",
        location="75 - Paris 1er arrondissement",
        source_data={"lieuTravail": {"commune": "75101"}},
    )
    second = FakeJob(
        "second",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines production",
        location="Paris 9e Arrondissement",
        source_data={"lieuTravail": {"commune": "75109"}},
    )

    assert fuzzy_location_score(first, second) == 100.0


def test_department_or_metropolitan_area_is_not_treated_as_a_precise_city() -> None:
    bordeaux = FakeJob(
        "bordeaux",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="Bordeaux, Nouvelle-Aquitaine, France",
    )
    gironde = FakeJob(
        "gironde",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="Gironde, Nouvelle-Aquitaine, France",
    )
    rennes_area = FakeJob(
        "rennes-area",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="Greater Rennes Metropolitan Area",
    )
    sophia_antipolis = FakeJob(
        "sophia-antipolis",
        "Data Scientist",
        "Acme",
        "pipeline pytorch machine learning",
        location="Sophia Antipolis, Provence-Alpes-Côte d'Azur, France",
    )

    matcher = Deduplicator()
    same_region_match = matcher.find_probable_duplicate(bordeaux, [gironde])
    assert same_region_match is not None
    assert same_region_match.location_score is None
    assert matcher.find_probable_duplicate(bordeaux, [sophia_antipolis]) is None
    broad_area_match = matcher.find_probable_duplicate(bordeaux, [rennes_area])
    assert broad_area_match is not None
    assert broad_area_match.location_score is None


def test_metropolitan_area_stays_neutral_without_explicit_region() -> None:
    paris = FakeJob(
        "paris",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines",
        location="Paris, Île-de-France, France",
    )
    lyon = FakeJob(
        "lyon",
        "Data Scientist",
        "Acme",
        paris.description,
        location="Lyon, Auvergne-Rhône-Alpes, France",
    )

    for label in (
        "Greater Paris Metropolitan Area",
        "Greater Paris Metropolitan Region",
        "Greater Paris Area",
        "Paris Metropolitan Area",
        "Paris Metropolitan Region",
    ):
        greater_paris = FakeJob(
            f"area:{label}",
            "Data Scientist",
            "Acme",
            paris.description,
            location=label,
        )
        match = Deduplicator().find_probable_duplicate(paris, [greater_paris])
        assert match is not None
        assert match.location_score is None
        assert fuzzy_location_score(lyon, greater_paris) is None


def test_work_mode_qualifier_does_not_change_city_identity() -> None:
    paris = FakeJob(
        "paris",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines",
        location="Paris",
    )
    hybrid = FakeJob(
        "hybrid",
        "Data Scientist",
        "Acme",
        paris.description,
        location="Paris (hybride)",
    )
    remote = FakeJob(
        "remote",
        "Data Scientist",
        "Acme",
        paris.description,
        location="Remote (Work from Anywhere)",
    )

    assert fuzzy_location_score(paris, hybrid) == 100.0
    assert fuzzy_location_score(paris, remote) is None


def test_unknown_location_cannot_bridge_conflicting_precise_group() -> None:
    paris = FakeJob(
        "paris",
        "Data Scientist",
        "Acme",
        "python pytorch machine learning data pipelines",
        location="Paris",
        id=1,
    )
    unknown = FakeJob(
        "unknown",
        "Data Scientist",
        "Acme",
        paris.description,
        location=None,
        id=2,
        possible_duplicate_of_id=1,
    )
    lyon = FakeJob(
        "lyon",
        "Data Scientist",
        "Acme",
        paris.description,
        location="Lyon",
        id=3,
    )
    matcher = Deduplicator()

    assert matcher.find_probable_duplicate(lyon, [paris, unknown]) is None
    report = matcher.deduplicate([paris, unknown, lyon])
    assert len(report.unique) == 2
    assert all(
        not ({"paris", "lyon"} <= {job.external_id for job in group})
        for group in report.duplicate_groups
    )


@pytest.mark.parametrize(
    "label",
    [
        "Lieu à confirmer",
        "Campus principal",
        "Paris - bureau central",
        "Site 42",
        "Site 75001",
        "Paris/Lyon",
        "Paris|Lyon",
        "Paris;Lyon",
    ],
)
def test_unknown_or_multisite_label_cannot_block_or_boost_a_match(label: str) -> None:
    target = FakeJob("target", "Data Scientist", "Acme", "python machine learning", location=label)
    paris = FakeJob("paris", target.title, target.company, target.description, location="Paris")
    same_label = FakeJob("same-label", target.title, target.company, target.description, location=label)

    assert fuzzy_location_score(target, paris) is None
    assert fuzzy_location_score(target, same_label) is None
    match = Deduplicator().find_probable_duplicate(target, [paris])
    assert match is not None
    assert match.location_score is None


def test_same_unknown_label_does_not_change_confidence_or_candidate_selection() -> None:
    target = FakeJob(
        "target", "Data Scientist", "Acme",
        "python machine learning data pipelines cloud", location="Lieu à confirmer",
    )
    neutral = FakeJob(
        "neutral", target.title, target.company,
        "python machine learning statistics dashboards", location=None,
    )
    unknown = FakeJob("unknown", target.title, target.company, neutral.description, location=target.location)
    matcher = Deduplicator()
    neutral_match = matcher.find_probable_duplicate(target, [neutral])
    unknown_match = matcher.find_probable_duplicate(target, [unknown])

    assert neutral_match is not None and unknown_match is not None
    assert unknown_match.location_score is None
    assert unknown_match.confidence == neutral_match.confidence
    assert matcher.find_probable_duplicate(target, [neutral, unknown]).job is neutral


@pytest.mark.parametrize(
    ("area", "commune"),
    [
        ("Greater Paris Metropolitan Area", "Courbevoie, Île-de-France, France"),
        ("Métropole de Rennes", "35 - Saint-Grégoire"),
        ("Greater Lyon Area", "Villeurbanne, Auvergne-Rhône-Alpes, France"),
    ],
)
def test_metropolitan_area_stays_neutral_for_a_commune_in_the_same_region(
    area: str, commune: str
) -> None:
    left = FakeJob("area", "Data Scientist", "Acme", "python machine learning", location=area)
    right = FakeJob("commune", left.title, left.company, left.description, location=commune)

    assert fuzzy_location_score(left, right) is None
    assert fuzzy_location_score(right, left) is None
    match = Deduplicator().find_probable_duplicate(left, [right])
    assert match is not None
    assert match.location_score is None


def test_numeric_and_named_departments_reject_same_region_homonyms() -> None:
    gironde = FakeJob("gironde", "Data Scientist", "Acme", location="33 - Saint-Sauveur")
    dordogne = FakeJob(
        "dordogne", gironde.title, gironde.company,
        location="Saint-Sauveur, Dordogne, France",
    )

    assert fuzzy_location_score(gironde, dordogne) == 0.0
    assert fuzzy_location_score(dordogne, gironde) == 0.0


def test_metropolitan_area_can_reject_an_explicitly_different_region() -> None:
    area = FakeJob(
        "area", "Data Scientist", "Acme",
        location="Greater Paris Metropolitan Area, Île-de-France, France",
    )
    lyon = FakeJob("lyon", area.title, area.company, location="Lyon")

    assert fuzzy_location_score(area, lyon) == 0.0
    assert fuzzy_location_score(lyon, area) == 0.0


def test_postal_department_comes_from_official_mapping_rather_than_its_prefix() -> None:
    postal = FakeJob(
        "postal", "Data Scientist", "Acme", location="Paray-Vieille-Poste 94390",
    )
    administrative = FakeJob(
        "administrative", postal.title, postal.company, location="91 - Paray-Vieille-Poste",
    )

    assert fuzzy_location_score(postal, administrative) == 100.0
    assert fuzzy_location_score(administrative, postal) == 100.0


def test_postal_area_covering_multiple_departments_stays_compatible() -> None:
    postal_area = FakeJob("postal", "Data Scientist", "Acme", location="01200")
    department = FakeJob(
        "department", postal_area.title, postal_area.company, location="Haute-Savoie, France",
    )
    commune = FakeJob("commune", postal_area.title, postal_area.company, location="Éloise 01200")
    administrative = FakeJob(
        "administrative", postal_area.title, postal_area.company, location="74 - Éloise",
    )

    assert fuzzy_location_score(postal_area, department) is None
    assert fuzzy_location_score(department, postal_area) is None
    assert fuzzy_location_score(commune, administrative) == 100.0


@pytest.mark.parametrize("label", ["Saint-Denis", "Saint-Sauveur, Nouvelle-Aquitaine, France"])
def test_ambiguous_official_names_do_not_identify_a_commune(label: str) -> None:
    ambiguous = FakeJob("ambiguous", "Data Scientist", "Acme", location=label)
    other = FakeJob("other", ambiguous.title, ambiguous.company, location=label)

    assert fuzzy_location_score(ambiguous, other) is None


@pytest.mark.parametrize(
    "label",
    ["Saint-Denis 93200", "Saint-Denis, Seine-Saint-Denis, France", "Saint-Denis, Île-de-France, France"],
)
def test_official_context_can_disambiguate_a_commune(label: str) -> None:
    target = FakeJob("target", "Data Scientist", "Acme", location="93 - Saint-Denis")
    other = FakeJob("other", target.title, target.company, location=label)

    assert fuzzy_location_score(target, other) == 100.0


def test_unknown_structured_commune_code_does_not_prove_identity() -> None:
    payload = {"lieuTravail": {"commune": "00000"}}
    left = FakeJob("left", "Data Scientist", "Acme", location=None, source_data=payload)
    right = FakeJob("right", left.title, left.company, location=None, source_data=payload)

    assert fuzzy_location_score(left, right) is None


@pytest.mark.parametrize(
    ("code", "label"),
    [("75101", "Paris 75009"), ("69381", "Lyon 69009"), ("13201", "Marseille 13016")],
)
def test_official_arrondissement_code_matches_a_city_postal_label(code: str, label: str) -> None:
    structured = FakeJob(
        "structured", "Data Scientist", "Acme", location=None,
        source_data={"lieuTravail": {"commune": code}},
    )
    displayed = FakeJob("displayed", structured.title, structured.company, location=label)

    assert fuzzy_location_score(structured, displayed) == 100.0


@pytest.mark.parametrize("content", [None, "broken JSON", '{"schema_version":1,"communes":null}'])
def test_missing_or_corrupt_official_reference_keeps_city_labels_neutral(
    tmp_path, monkeypatch, content: str | None
) -> None:
    from smartapply.dedup import geography
    from smartapply.dedup.deduplicator import _cached_precise_location

    if content is not None:
        data = tmp_path / "data"
        data.mkdir()
        (data / "french_communes.json").write_text(content, encoding="utf-8")
    monkeypatch.setattr(geography, "files", lambda _package: tmp_path)
    geography._reference.cache_clear()
    _cached_precise_location.cache_clear()
    try:
        paris = FakeJob("paris", "Data Scientist", "Acme", location="Paris")
        lyon = FakeJob("lyon", paris.title, paris.company, location="Lyon")
        assert fuzzy_location_score(paris, lyon) is None
        assert fuzzy_location_score(paris, paris) is None
    finally:
        geography._reference.cache_clear()
        _cached_precise_location.cache_clear()
