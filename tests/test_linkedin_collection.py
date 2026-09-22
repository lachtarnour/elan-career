"""Apify's request budget must not truncate already-fetched result batches."""

from __future__ import annotations

import pytest
import requests

from smartapply.database import session_scope
from smartapply.database.models import Job
from smartapply.offers import RawJob
from smartapply.pipeline.ingest import collect_round_robin
from smartapply.pipeline.ingestor import Ingestor
from smartapply.scrapers import LinkedInJobsScraper
from smartapply.scrapers.base import ScraperConfigError, ScraperError


def _raw(identifier: str) -> RawJob:
    return RawJob(
        external_id=f"linkedin:{identifier}",
        title=f"Specialist {identifier}",
        company=f"Company {identifier}",
        description="Build data systems.",
        source="linkedin",
    )


def _scraper(monkeypatch, fetch):
    scraper = LinkedInJobsScraper(token="test-token")
    scraper.default_max_results = 100
    scraper.default_experience_level = []
    monkeypatch.setattr(scraper, "_fetch", fetch)
    monkeypatch.setattr("smartapply.pipeline.ingestor.get_scraper", lambda source: scraper)
    return scraper


def _actor_row(identifier: str) -> dict:
    return {
        "id": identifier,
        "title": f"Specialist {identifier}",
        "companyName": f"Company {identifier}",
        "description": "Build data systems.",
    }


def test_all_21_apify_batches_are_consumed_including_aliases(monkeypatch, isolated_db):
    calls = []

    def fetch(payload):
        calls.append(payload)
        return [_actor_row(f"{payload['keywords']}-{index}") for index in range(100)]

    scraper = _scraper(monkeypatch, fetch)
    result = collect_round_robin(
        scraper=scraper,
        source="linkedin",
        queries=[f"query-{index}" for index in range(21)],
        primary_query_count=2,
        location="France",
        max_results=100,
        search_kwargs={},
    )

    assert len(calls) == 21
    assert all(payload["limit"] == 100 for payload in calls)
    assert len(result.raw_jobs) == 2100
    assert len({job.external_id for job in result.raw_jobs}) == 2100
    assert not result.hit_raw_seen_cap
    assert not result.warnings


def test_known_and_repeated_offers_do_not_hide_new_offers_at_end():
    class Scraper:
        name = "linkedin"

        def search(self, query, **kwargs):
            yield from [_raw("known"), _raw("shared"), _raw(query)]

    result = collect_round_robin(
        scraper=Scraper(),
        queries=["first", "last"],
        location=None,
        max_results=3,
        search_kwargs={},
        known_external_ids={"linkedin:known"},
    )

    assert [job.external_id for job in result.raw_jobs] == [
        "linkedin:shared",
        "linkedin:first",
        "linkedin:last",
    ]
    assert result.skipped_known == 1
    assert not result.hit_raw_seen_cap


def test_all_returned_offers_are_persisted_even_if_actor_exceeds_limit(monkeypatch, isolated_db):
    calls = []

    def fetch(payload):
        calls.append(payload)
        return [_actor_row(f"{payload['keywords']}-{index}") for index in range(3)]

    _scraper(monkeypatch, fetch)
    report = Ingestor().from_source("linkedin", "Unmapped One OR Unmapped Two", max_results=2)

    assert [payload["limit"] for payload in calls] == [2, 2]
    assert report.fetched == report.persisted == 6
    with session_scope() as session:
        jobs = session.query(Job).all()
        assert len(jobs) == 6
        assert {job.source_data["id"] for job in jobs} == {
            f"{query}-{index}" for query in ("Unmapped One", "Unmapped Two") for index in range(3)
        }


def test_timeout_preserves_previous_batch_and_continues_other_queries(monkeypatch, isolated_db):
    calls = []
    scraper = _scraper(monkeypatch, None)
    # Exercise the real timeout conversion, with no network or paid actor runs.
    monkeypatch.delattr(scraper, "_fetch")

    def post(url, *, json, **kwargs):
        calls.append(json["keywords"])
        if json["keywords"] == "Unmapped Failed":
            raise requests.Timeout()
        response = requests.Response()
        response.status_code = 200
        monkeypatch.setattr(response, "json", lambda: [_actor_row(json["keywords"])])
        return response

    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.post", post)
    report = Ingestor().from_source(
        "linkedin", "Unmapped First OR Unmapped Failed OR Unmapped Last", max_results=1
    )

    assert calls == ["Unmapped First", "Unmapped Failed", "Unmapped Last"]
    assert report.fetched == report.persisted == 2
    assert len(report.warnings) == 1
    assert "Unmapped Failed" in report.warnings[0]
    assert "délai de réponse Apify dépassé" in report.warnings[0]
    with session_scope() as session:
        assert session.query(Job).count() == 2


def test_experience_fallback_failure_does_not_discard_primary_response(monkeypatch, isolated_db):
    calls = []

    def fetch(payload):
        calls.append(payload)
        if len(calls) == 2:
            raise ScraperError("fallback unavailable")
        return [_actor_row("primary")]

    scraper = _scraper(monkeypatch, fetch)
    scraper.default_experience_level = ["2", "3", "4"]
    report = Ingestor().from_source("linkedin", "Unmapped", max_results=3)

    assert len(calls) == 2
    assert report.fetched == report.persisted == 1
    assert "fallback unavailable" in report.warnings[0]


def test_configuration_error_stops_new_queries_but_preserves_results(monkeypatch, isolated_db):
    calls = []

    def fetch(payload):
        calls.append(payload["keywords"])
        if len(calls) == 2:
            raise ScraperConfigError("quota exhausted")
        return [_actor_row("first")]

    _scraper(monkeypatch, fetch)
    report = Ingestor().from_source(
        "linkedin", "Unmapped First OR Unmapped Failed OR Unmapped Last", max_results=1
    )

    assert calls == ["Unmapped First", "Unmapped Failed"]
    assert report.persisted == 1
    assert any("quota exhausted" in warning for warning in report.warnings)
    assert any("restantes" in warning for warning in report.warnings)


def test_initial_configuration_error_is_not_reported_as_empty_success():
    class Scraper:
        def search(self, *args, **kwargs):
            raise ScraperConfigError("invalid token")

    with pytest.raises(ScraperConfigError, match="invalid token"):
        collect_round_robin(
            scraper=Scraper(),
            source="linkedin",
            queries=["first"],
            location=None,
            max_results=100,
            search_kwargs={},
        )


def test_cancellation_preserves_collected_offers_and_stops_new_queries():
    stopped = False
    calls = []

    class Scraper:
        def search(self, query, **kwargs):
            nonlocal stopped
            calls.append(query)
            yield _raw("first")
            stopped = True

    result = collect_round_robin(
        scraper=Scraper(),
        source="linkedin",
        queries=["first", "second"],
        location=None,
        max_results=100,
        search_kwargs={},
        stop_requested=lambda: stopped,
    )

    assert calls == ["first"]
    assert len(result.raw_jobs) == 1
    assert result.cancelled
