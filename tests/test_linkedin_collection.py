"""LinkedIn collection keeps per-query limits and deterministic results."""

from __future__ import annotations

from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest
import requests

from smartapply.database import session_scope
from smartapply.database.models import Job
from smartapply.offers import RawJob
from smartapply.pipeline.ingest import collect_round_robin
from smartapply.pipeline.ingestor import Ingestor
from smartapply.scrapers import LinkedInJobsScraper
from smartapply.scrapers.base import ScraperConfigError, ScraperError, ScraperStopError


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
    if fetch is not None:
        monkeypatch.setattr(
            scraper,
            "_fetch",
            lambda payload, **kwargs: fetch(payload),
        )
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


def test_linkedin_uses_bounded_parallel_cohorts() -> None:
    lock = Lock()
    two_started = Event()
    active = 0
    peak_active = 0
    calls: list[str] = []

    class Scraper:
        name = "linkedin"
        max_concurrent_runs = 2

        def search(self, query, **kwargs):
            nonlocal active, peak_active
            with lock:
                calls.append(query)
                active += 1
                peak_active = max(peak_active, active)
                if active == 2:
                    two_started.set()
            assert two_started.wait(timeout=2)
            try:
                yield _raw(query)
            finally:
                with lock:
                    active -= 1

    result = collect_round_robin(
        scraper=Scraper(),
        source="linkedin",
        queries=["first", "second", "third", "fourth"],
        location=None,
        max_results=1,
        search_kwargs={},
    )

    assert peak_active == 2
    assert set(calls) == {"first", "second", "third", "fourth"}
    assert [job.external_id for job in result.raw_jobs] == [
        "linkedin:first",
        "linkedin:second",
        "linkedin:third",
        "linkedin:fourth",
    ]


def test_next_cohort_waits_until_every_current_query_finishes() -> None:
    second_finished = Event()
    release_first = Event()
    third_started = Event()
    result_box: dict[str, object] = {}

    class Scraper:
        name = "linkedin"
        max_concurrent_runs = 2

        def search(self, query, **kwargs):
            if query == "first":
                assert release_first.wait(timeout=2)
            elif query == "second":
                second_finished.set()
            else:
                third_started.set()
            yield _raw(query)

    def collect() -> None:
        try:
            result_box["result"] = collect_round_robin(
                scraper=Scraper(),
                source="linkedin",
                queries=["first", "second", "third"],
                location=None,
                max_results=1,
                search_kwargs={},
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            result_box["error"] = exc

    collection_thread = Thread(target=collect, daemon=True)
    collection_thread.start()
    try:
        assert second_finished.wait(timeout=2)
        assert not third_started.wait(timeout=0.1)
    finally:
        release_first.set()
    collection_thread.join(timeout=2)

    assert not collection_thread.is_alive()
    assert "error" not in result_box
    assert third_started.is_set()
    result = result_box["result"]
    assert [job.external_id for job in result.raw_jobs] == [
        "linkedin:first",
        "linkedin:second",
        "linkedin:third",
    ]


def test_recoverable_error_allows_the_next_cohort() -> None:
    calls: list[str] = []

    class Scraper:
        name = "linkedin"
        max_concurrent_runs = 2

        def search(self, query, **kwargs):
            calls.append(query)
            if query == "recoverable":
                raise ScraperError("temporary query failure")
            yield _raw(query)

    result = collect_round_robin(
        scraper=Scraper(),
        source="linkedin",
        queries=["first", "recoverable", "third"],
        location=None,
        max_results=1,
        search_kwargs={},
    )

    assert set(calls[:2]) == {"first", "recoverable"}
    assert calls[-1] == "third"
    assert [job.external_id for job in result.raw_jobs] == [
        "linkedin:first",
        "linkedin:third",
    ]
    assert len(result.warnings) == 1
    assert "temporary query failure" in result.warnings[0]


def test_fatal_error_finishes_its_cohort_and_stops_later_cohorts() -> None:
    peer_started = Event()
    fatal_seen = Event()
    calls: list[str] = []

    class Scraper:
        name = "linkedin"
        max_concurrent_runs = 2

        def search(self, query, **kwargs):
            calls.append(query)
            if query == "fatal":
                assert peer_started.wait(timeout=2)
                fatal_seen.set()
                raise ScraperStopError("quota exhausted")
            if query == "peer":
                peer_started.set()
                assert fatal_seen.wait(timeout=2)
            yield _raw(query)

    result = collect_round_robin(
        scraper=Scraper(),
        source="linkedin",
        queries=["fatal", "peer", "not-started"],
        location=None,
        max_results=1,
        search_kwargs={},
    )

    assert set(calls) == {"fatal", "peer"}
    assert [job.external_id for job in result.raw_jobs] == ["linkedin:peer"]
    assert len(result.warnings) == 2
    assert "quota exhausted" in result.warnings[0]
    assert "arrêtées" in result.warnings[1]


def test_fatal_cohort_keeps_completed_empty_dataset_for_commit_cleanup() -> None:
    class Scraper:
        name = "linkedin"
        max_concurrent_runs = 2

        def create_search_session(self, query, **kwargs):
            return query

        def run_search_pass(self, session):
            if session == "fatal":
                return SimpleNamespace(
                    raw_jobs=[],
                    error=ScraperStopError("quota exhausted"),
                    done=True,
                    completed_run_ids=(),
                )
            return SimpleNamespace(
                raw_jobs=[],
                error=None,
                done=True,
                completed_run_ids=("run-empty",),
            )

    result = collect_round_robin(
        scraper=Scraper(),
        source="linkedin",
        queries=["fatal", "empty", "not-started"],
        location=None,
        max_results=10,
        search_kwargs={},
    )

    assert result.raw_jobs == []
    assert result.completed_source_run_ids == ["run-empty"]
    assert len(result.warnings) == 2
    assert "quota exhausted" in result.warnings[0]
    assert "arrêtées" in result.warnings[1]


def test_unexpected_worker_error_waits_for_parallel_peer() -> None:
    peer_started = Event()
    release_peer = Event()
    peer_finished = Event()
    result_box: dict[str, object] = {}

    class Scraper:
        name = "linkedin"
        max_concurrent_runs = 2

        def search(self, query, **kwargs):
            if query == "broken":
                assert peer_started.wait(timeout=2)
                raise ValueError("unexpected mapping bug")
            peer_started.set()
            assert release_peer.wait(timeout=2)
            peer_finished.set()
            yield _raw(query)

    def collect() -> None:
        try:
            collect_round_robin(
                scraper=Scraper(),
                source="linkedin",
                queries=["broken", "peer"],
                location=None,
                max_results=1,
                search_kwargs={},
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            result_box["error"] = exc

    collection_thread = Thread(target=collect, daemon=True)
    collection_thread.start()
    assert peer_started.wait(timeout=2)
    assert collection_thread.is_alive()
    release_peer.set()
    collection_thread.join(timeout=2)

    assert not collection_thread.is_alive()
    assert peer_finished.is_set()
    assert isinstance(result_box.get("error"), ValueError)
    assert str(result_box["error"]) == "unexpected mapping bug"


def test_actor_fallback_pass_waits_for_full_three_run_cohort(monkeypatch, tmp_path) -> None:
    lock = Lock()
    three_primaries_started = Event()
    fast_primaries_finished = Event()
    release_slow_primary = Event()
    fallback_started = Event()
    calls: list[tuple[str, str]] = []
    active = 0
    peak_active = 0
    primary_started = 0
    fast_primary_count = 0
    result_box: dict[str, object] = {}

    scraper = LinkedInJobsScraper(
        token="test-token",
        max_concurrent_runs=3,
        recovery_path=tmp_path / "apify-runs.json",
    )
    scraper.default_max_results = 10

    def fetch(payload, **kwargs):
        nonlocal active, peak_active, primary_started, fast_primary_count
        assert payload["limit"] == 10
        query = payload["keywords"]
        experience = next(item["value"] for item in payload["urlParam"] if item["key"] == "f_E")
        with lock:
            calls.append((query, experience))
            active += 1
            peak_active = max(peak_active, active)
            if experience == "2,3":
                primary_started += 1
                if primary_started == 3:
                    three_primaries_started.set()
            else:
                fallback_started.set()
        try:
            if experience == "2,3":
                assert three_primaries_started.wait(timeout=2)
                if query == "first":
                    assert release_slow_primary.wait(timeout=2)
                else:
                    with lock:
                        fast_primary_count += 1
                        if fast_primary_count == 2:
                            fast_primaries_finished.set()
            return []
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(scraper, "_fetch", fetch)

    def collect() -> None:
        try:
            result_box["result"] = collect_round_robin(
                scraper=scraper,
                source="linkedin",
                queries=["first", "second", "third", "fourth"],
                location="France",
                max_results=10,
                search_kwargs={"experience_level": ["2", "3", "4"]},
            )
        except Exception as exc:  # pragma: no cover - surfaced below
            result_box["error"] = exc

    collection_thread = Thread(target=collect, daemon=True)
    collection_thread.start()
    try:
        assert fast_primaries_finished.wait(timeout=2)
        assert not fallback_started.wait(timeout=0.1)
        with lock:
            assert set(calls[:3]) == {
                ("first", "2,3"),
                ("second", "2,3"),
                ("third", "2,3"),
            }
            assert all(query != "fourth" for query, _ in calls)
    finally:
        release_slow_primary.set()
    collection_thread.join(timeout=3)

    assert not collection_thread.is_alive()
    assert "error" not in result_box
    assert fallback_started.is_set()
    assert peak_active == 3
    assert set(calls[:3]) == {
        ("first", "2,3"),
        ("second", "2,3"),
        ("third", "2,3"),
    }
    assert set(calls[3:6]) == {
        ("first", "4"),
        ("second", "4"),
        ("third", "4"),
    }
    assert calls[6:] == [
        ("fourth", "2,3"),
        ("fourth", "4"),
    ]


def test_parallel_completion_keeps_query_order_and_duplicate_winner() -> None:
    second_finished = Event()

    class Scraper:
        name = "linkedin"
        max_concurrent_runs = 2

        def search(self, query, **kwargs):
            if query == "first":
                assert second_finished.wait(timeout=2)
                yield _raw("shared").model_copy(update={"company": "First company"})
                yield _raw("first")
                return
            yield _raw("shared").model_copy(update={"company": "Second company"})
            yield _raw("second")
            second_finished.set()

    result = collect_round_robin(
        scraper=Scraper(),
        source="linkedin",
        queries=["first", "second"],
        location=None,
        max_results=2,
        search_kwargs={},
    )

    assert [job.external_id for job in result.raw_jobs] == [
        "linkedin:shared",
        "linkedin:first",
        "linkedin:second",
    ]
    assert result.raw_jobs[0].company == "First company"


def test_each_query_uses_its_own_limit_and_keeps_all_returned_offers(monkeypatch, isolated_db):
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


def test_uncertain_start_preserves_parallel_batch_and_continues_new_queries(monkeypatch, isolated_db):
    calls = []
    _scraper(monkeypatch, None)
    first_started = Event()
    failed = Event()

    def response(payload, status_code=200):
        value = requests.Response()
        value.status_code = status_code
        monkeypatch.setattr(value, "json", lambda: payload)
        return value

    def post(url, *, json, **kwargs):
        calls.append(json["keywords"])
        if json["keywords"] == "Unmapped Failed":
            assert first_started.wait(timeout=2)
            failed.set()
            raise requests.Timeout()
        first_started.set()
        assert failed.wait(timeout=2)
        return response(
            {
                "data": {
                    "id": "run-first",
                    "defaultDatasetId": "dataset-first",
                    "status": "SUCCEEDED",
                }
            },
            status_code=201,
        )

    def get(url, **kwargs):
        return response([_actor_row("Unmapped First")])

    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.post", post)
    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.get", get)
    report = Ingestor().from_source(
        "linkedin",
        "Unmapped First OR Unmapped Failed OR Unmapped Last OR Unmapped Final",
        max_results=1,
    )

    assert {"Unmapped First", "Unmapped Failed"}.issubset(calls)
    assert "Unmapped Final" in calls
    assert report.fetched == report.persisted == 1
    assert len(report.warnings) == 1
    assert "Unmapped Failed" in report.warnings[0]
    assert "démarrage Apify n’a pas répondu" in report.warnings[0]
    with session_scope() as session:
        assert session.query(Job).count() == 1


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
    first_started = Event()
    failed = Event()

    def fetch(payload):
        query = payload["keywords"]
        calls.append(query)
        if query == "Unmapped Failed":
            assert first_started.wait(timeout=2)
            failed.set()
            raise ScraperConfigError("quota exhausted")
        first_started.set()
        assert failed.wait(timeout=2)
        return [_actor_row("first")]

    _scraper(monkeypatch, fetch)
    report = Ingestor().from_source(
        "linkedin",
        "Unmapped First OR Unmapped Failed OR Unmapped Last OR Unmapped Final",
        max_results=1,
    )

    assert {"Unmapped First", "Unmapped Failed"}.issubset(calls)
    assert "Unmapped Final" not in calls
    assert report.persisted == 1
    assert any("quota exhausted" in warning for warning in report.warnings)
    assert any("arrêtées" in warning for warning in report.warnings)


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
