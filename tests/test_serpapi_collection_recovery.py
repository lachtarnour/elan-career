"""Source failures preserve received pages and stop safely, without real HTTP."""

from threading import Event
from types import SimpleNamespace

import pytest
import requests

from smartapply.database import session_scope
from smartapply.database.models import Job
from smartapply.desktop.services import DesktopService
from smartapply.pipeline.ingestor import Ingestor
from smartapply.scrapers.serpapi import SerpApiGoogleJobsScraper

FAKE_KEY = "AUDIT_FAKE_API_KEY_123"


def _http_error(status):
    response = requests.Response()
    response.status_code = status
    response.reason = "Unauthorized" if status == 401 else "Internal Server Error"
    response.url = f"https://serpapi.com/search.json?q=engineer&api_key={FAKE_KEY}"
    with pytest.raises(requests.HTTPError) as error:
        response.raise_for_status()
    return error.value


def _desktop_search(monkeypatch, scraper):
    ingestor = Ingestor()
    pipeline = SimpleNamespace(
        collect_source=ingestor.collect_source,
        persist_collection=ingestor.persist_collection,
        filter_pending=lambda **kwargs: None,
    )
    monkeypatch.setattr("smartapply.pipeline.ingestor.get_scraper", lambda source: scraper)
    monkeypatch.setattr("smartapply.desktop.services._pipeline", lambda *args: pipeline)
    monkeypatch.setattr("smartapply.desktop.services.ensure_startup_maintenance_complete", lambda: None)
    service = DesktopService()
    monkeypatch.setattr(service, "source_diagnostics_stale", lambda: False)
    return service.search_jobs(
        query="Engineer", location="Paris", sources=["serpapi"],
        max_results=2, date_posted="any", serpapi_hl="fr",
    )


def test_desktop_search_traceback_does_not_disclose_serpapi_key(
    isolated_db, monkeypatch, mocker, caplog,
):
    scraper = SerpApiGoogleJobsScraper(api_key=FAKE_KEY, hl="fr")
    mocker.patch.object(scraper, "_fetch", side_effect=_http_error(401))

    result = _desktop_search(monkeypatch, scraper)

    assert len(result.errors) == 1
    assert "HTTP 401 Unauthorized" in result.errors[0]
    assert "Search collection failed" in caplog.text
    assert "HTTP 401 Unauthorized" in caplog.text
    assert FAKE_KEY not in caplog.text
    assert "api_key=" not in caplog.text


def test_late_serpapi_page_failure_persists_received_offers_with_warning(
    isolated_db, monkeypatch, mocker, caplog,
):
    scraper = SerpApiGoogleJobsScraper(
        api_key=FAKE_KEY, hl="fr", max_pages=2, low_result_fallback_target=0,
    )
    page = {
        "jobs_results": [{
            "job_id": "already-received",
            "title": "Machine Learning Engineer",
            "company_name": "Acme",
            "location": "Paris",
            "description": "Develop machine learning pipelines in Python.",
            "apply_options": [{"link": "https://example.test/jobs/already-received"}],
        }],
        "serpapi_pagination": {"next_page_token": "page2"},
    }
    fetch = mocker.patch.object(scraper, "_fetch", side_effect=[page, _http_error(500)])

    result = _desktop_search(monkeypatch, scraper)

    assert fetch.call_count == 2
    assert result.fetched == result.persisted == 1
    assert not result.errors
    assert len(result.reports) == 1
    assert "HTTP 500" in result.reports[0]["warnings"][0]
    assert "déjà reçues sont conservées" in result.reports[0]["warnings"][0]
    assert FAKE_KEY not in caplog.text
    assert "api_key=" not in caplog.text
    with session_scope() as session:
        jobs = session.query(Job).all()
        assert len(jobs) == 1
        assert jobs[0].title == "Machine Learning Engineer"


def test_serpapi_does_not_retry_a_timeout_after_stop(mocker):
    stopped = Event()

    def first_request(*args, **kwargs):
        stopped.set()
        raise requests.Timeout("User stopped during the first request")

    get = mocker.patch("smartapply.scrapers.serpapi.requests.get", side_effect=first_request)
    scraper = SerpApiGoogleJobsScraper(api_key=FAKE_KEY, hl="fr")

    assert list(scraper.search(
        "Engineer", max_results=1, date_posted="any", stop_requested=stopped.is_set,
    )) == []
    assert get.call_count == 1


def test_serpapi_stop_during_backoff_interrupts_the_wait(monkeypatch, mocker):
    stopped = Event()
    pauses = []

    def stop_during_sleep(delay):
        pauses.append(delay)
        stopped.set()

    get = mocker.patch(
        "smartapply.scrapers.serpapi.requests.get", side_effect=requests.Timeout("Timeout"),
    )
    monkeypatch.setattr("smartapply.scrapers.serpapi.time.sleep", stop_during_sleep)
    scraper = SerpApiGoogleJobsScraper(api_key=FAKE_KEY, hl="fr")

    assert list(scraper.search(
        "Engineer", max_results=1, date_posted="any", stop_requested=stopped.is_set,
    )) == []
    assert get.call_count == 1
    assert len(pauses) == 1
    assert 0 < pauses[0] <= 0.1


def test_serpapi_still_retries_transient_failure_without_stop(monkeypatch, mocker):
    response = mocker.Mock()
    response.json.return_value = {"jobs_results": []}
    get = mocker.patch(
        "smartapply.scrapers.serpapi.requests.get",
        side_effect=[requests.Timeout("Transient"), response],
    )
    monkeypatch.setattr("smartapply.scrapers.serpapi._wait_for_retry", lambda *args: None)
    scraper = SerpApiGoogleJobsScraper(api_key=FAKE_KEY, hl="fr", max_pages=1)

    assert scraper._fetch({"api_key": FAKE_KEY}) == {"jobs_results": []}
    assert get.call_count == 2


def test_serpapi_retry_attempt_count_stays_bounded(monkeypatch, mocker):
    get = mocker.patch(
        "smartapply.scrapers.serpapi.requests.get", side_effect=requests.Timeout("Timeout"),
    )
    monkeypatch.setattr("smartapply.scrapers.serpapi._wait_for_retry", lambda *args: None)
    scraper = SerpApiGoogleJobsScraper(api_key=FAKE_KEY, hl="fr")

    with pytest.raises(requests.Timeout):
        scraper._fetch({"api_key": FAKE_KEY})
    assert get.call_count == 3
