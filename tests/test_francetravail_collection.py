"""Each France Travail title gets its own budget, without truncating the union."""

from __future__ import annotations

import pytest
import requests

from smartapply.database import session_scope
from smartapply.database.models import Job
from smartapply.offers import make_external_id
from smartapply.pipeline.ingest import collect_round_robin
from smartapply.pipeline.ingestor import Ingestor
from smartapply.scrapers import FranceTravailScraper


def _row(identifier):
    return {
        "id": identifier,
        "intitule": f"Specialist {identifier}",
        "entreprise": {"nom": f"Company {identifier}"},
        "description": "Build data systems.",
    }


def _scraper(monkeypatch, fetch):
    scraper = FranceTravailScraper(client_id="test-id", client_secret="test-secret")
    monkeypatch.setattr(scraper, "_get_token", lambda **kwargs: "test-token")
    monkeypatch.setattr(scraper, "_fetch_range", fetch)
    monkeypatch.setattr("smartapply.pipeline.ingestor.get_scraper", lambda source: scraper)
    return scraper


@pytest.mark.parametrize(
    ("limit", "ranges"),
    [(1, ["0-0"]), (75, ["0-49", "50-74"]), (100, ["0-49", "50-99"])],
)
def test_each_title_gets_its_own_paginated_budget(monkeypatch, limit, ranges):
    calls = []

    def fetch(*, params, **kwargs):
        query = params["motsCles"]
        calls.append((query, params["range"]))
        start, end = map(int, params["range"].split("-"))
        return {"resultats": [_row(f"{query}-{index}") for index in range(start, end + 1)]}

    scraper = _scraper(monkeypatch, fetch)
    queries = [f"Unmapped {index}" for index in range(4)]
    result = collect_round_robin(
        scraper=scraper,
        source="francetravail",
        queries=queries,
        primary_query_count=2,
        location=None,
        max_results=limit,
        search_kwargs={},
    )

    assert calls == [(query, page) for query in queries for page in ranges]
    assert len(result.raw_jobs) == len(queries) * limit
    assert len({job.external_id for job in result.raw_jobs}) == len(queries) * limit
    assert not result.hit_raw_seen_cap


def test_known_and_duplicate_rows_do_not_prevent_later_queries(monkeypatch):
    def fetch(*, params, **kwargs):
        return {"resultats": [_row("known"), _row("shared"), _row(params["motsCles"])]}

    result = collect_round_robin(
        scraper=_scraper(monkeypatch, fetch),
        queries=["first", "last"],
        location=None,
        max_results=3,
        search_kwargs={},
        known_external_ids={make_external_id("francetravail", "known")},
    )

    assert [job.source_data["id"] for job in result.raw_jobs] == ["shared", "first", "last"]
    assert result.skipped_known == 1
    assert not result.hit_raw_seen_cap


def test_all_received_rows_are_persisted_even_if_api_exceeds_range(monkeypatch, isolated_db):
    calls = []

    def fetch(*, params, **kwargs):
        calls.append(params["range"])
        return {"resultats": [_row(f"{params['motsCles']}-{index}") for index in range(3)]}

    _scraper(monkeypatch, fetch)
    report = Ingestor().from_source(
        "francetravail", "Unmapped First OR Unmapped Second", max_results=2
    )

    assert calls == ["0-1", "0-1"]
    assert report.fetched == report.persisted == 6
    with session_scope() as session:
        assert session.query(Job).count() == 6


def test_page_failure_preserves_first_page_and_later_queries(monkeypatch, isolated_db):
    calls = []

    def fetch(*, params, **kwargs):
        query = params["motsCles"]
        calls.append((query, params["range"]))
        if query == "Unmapped First" and params["range"] == "50-99":
            raise requests.Timeout("France Travail timed out")
        count = 50 if query == "Unmapped First" else 1
        return {"resultats": [_row(f"{query}-{index}") for index in range(count)]}

    _scraper(monkeypatch, fetch)
    report = Ingestor().from_source(
        "francetravail", "Unmapped First OR Unmapped Second", max_results=100
    )

    assert calls == [
        ("Unmapped First", "0-49"),
        ("Unmapped First", "50-99"),
        ("Unmapped Second", "0-49"),
    ]
    assert report.fetched == report.persisted == 51
    assert len(report.warnings) == 1
    assert "France Travail" in report.warnings[0]
    assert "Unmapped First" in report.warnings[0]
    with session_scope() as session:
        assert session.query(Job).count() == 51


def test_authorization_failure_preserves_rows_and_stops_remaining_queries(monkeypatch, isolated_db):
    calls = []

    def fetch(*, params, **kwargs):
        calls.append(params["motsCles"])
        if len(calls) == 2:
            response = requests.Response()
            response.status_code = 401
            raise requests.HTTPError("Access denied", response=response)
        return {"resultats": [_row("first")]}

    _scraper(monkeypatch, fetch)
    report = Ingestor().from_source(
        "francetravail", "Unmapped First OR Unmapped Second OR Unmapped Third", max_results=1
    )

    assert calls == ["Unmapped First", "Unmapped Second"]
    assert report.persisted == 1
    assert any("Access denied" in warning for warning in report.warnings)
    assert any("restantes" in warning for warning in report.warnings)


def test_cancellation_stops_new_queries_and_preserves_received_rows(monkeypatch):
    stopped = False
    calls = []

    def fetch(*, params, **kwargs):
        calls.append(params["motsCles"])
        return {"resultats": [_row("first")]}

    scraper = _scraper(monkeypatch, fetch)
    to_raw_job = scraper._to_raw_job

    def stop_after_mapping(row):
        nonlocal stopped
        job = to_raw_job(row)
        stopped = True
        return job

    monkeypatch.setattr(scraper, "_to_raw_job", stop_after_mapping)
    result = collect_round_robin(
        scraper=scraper,
        queries=["first", "second"],
        location=None,
        max_results=100,
        search_kwargs={},
        stop_requested=lambda: stopped,
    )

    assert calls == ["first"]
    assert len(result.raw_jobs) == 1
    assert result.cancelled
