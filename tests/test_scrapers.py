"""Tests for the scrapers module — manual, SerpApi, France Travail.

Real HTTP calls are forbidden in tests. Everything goes through mocks of
``requests.get`` / ``requests.post`` to keep tests offline and free.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

from smartapply.offers import ManualOfferInput, RawJob, make_external_id
from smartapply.scrapers import (
    FranceTravailScraper,
    LinkedInJobsScraper,
    ManualScraper,
    ScraperConfigError,
    ScraperError,
    SerpApiGoogleJobsScraper,
    available_scrapers,
)

# ============================ Helpers ============================


def _mock_response(payload: dict[str, Any], status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.text = ""
    response.raise_for_status = MagicMock()
    return response


# ============================ make_external_id ============================


def test_external_id_is_deterministic_and_namespaced() -> None:
    a = make_external_id("serpapi", "Data Scientist", "Acme", "Paris")
    b = make_external_id("serpapi", "data scientist", "ACME ", "Paris")
    c = make_external_id("francetravail", "Data Scientist", "Acme", "Paris")
    assert a == b  # case + whitespace insensitive
    assert a != c  # namespace matters
    assert a.startswith("serpapi:")


# ============================ Registry ============================


def test_registry_lists_available_scrapers() -> None:
    names = available_scrapers()
    assert "serpapi" in names
    assert "francetravail" in names
    assert "linkedin" in names
    assert "welcometothejungle" in names


# ============================ ManualScraper ============================


def test_manual_scraper_from_text() -> None:
    s = ManualScraper()
    job = s.from_structured(
        ManualOfferInput(
            company="Acme AI",
            title="Data Scientist",
            description="We are looking for a Data Scientist with PyTorch experience.",
            location="Paris",
        )
    )
    assert job.title == "Data Scientist"
    assert job.company == "Acme AI"
    assert job.location == "Paris"
    assert job.source == "manual"
    assert job.external_id.startswith("manual:")
    assert "We are looking for a Data Scientist" in job.description
    assert job.source_data
    assert job.source_data == {"input": "text"}


# ============================ SerpApi ============================


def _sample_serpapi_page(jobs: list[dict[str, Any]], next_token: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jobs_results": jobs}
    if next_token:
        payload["serpapi_pagination"] = {"next_page_token": next_token}
    return payload


def test_serpapi_maps_jobs_and_paginates(mocker) -> None:
    page1_jobs = [
        {
            "title": "Data Scientist",
            "company_name": "Acme",
            "location": "Paris, France",
            "description": "Build pipelines.",
            "detected_extensions": {"schedule_type": "Full-time", "work_from_home": True},
            "apply_options": [{"title": "LinkedIn", "link": "https://linkedin.com/jobs/1"}],
            "job_id": "abc123",
            "job_highlights": [
                {"title": "Qualifications", "items": ["PyTorch", "5+ years"]},
            ],
        }
    ]
    page2_jobs = [
        {
            "title": "ML Engineer",
            "company_name": "Beta",
            "location": "Remote",
            "description": "Work on RAG.",
            "detected_extensions": {"schedule_type": "Contract"},
            "apply_options": [],
            "share_link": "https://google.com/search?...",
            "job_id": "xyz789",
        }
    ]
    pages = [
        _mock_response(_sample_serpapi_page(page1_jobs, next_token="tok-2")),
        _mock_response(_sample_serpapi_page(page2_jobs, next_token=None)),
    ]
    mocker.patch("smartapply.scrapers.serpapi.requests.get", side_effect=pages)

    s = SerpApiGoogleJobsScraper(api_key="fake", hl="fr", low_result_fallback_target=10)
    jobs = list(s.search("data scientist", max_results=2))
    assert len(jobs) == 2
    first, second = jobs
    assert isinstance(first, RawJob)
    assert first.title == "Data Scientist"
    assert first.company == "Acme"
    assert first.contract_type == "Full-time"
    assert first.remote_policy == "remote"
    assert first.application_url == "https://linkedin.com/jobs/1"
    assert "PyTorch" in first.description  # highlights appended
    assert first.external_id.startswith("serpapi:")
    assert second.contract_type == "Contract"
    assert second.application_url.startswith("https://google.com")


def test_serpapi_uses_selected_world_location_without_france_country_bias(mocker) -> None:
    get_mock = mocker.patch(
        "smartapply.scrapers.serpapi.requests.get",
        return_value=_mock_response(
            _sample_serpapi_page(
                [
                    {
                        "job_id": "london-1",
                        "title": "Data Scientist",
                        "company_name": "Acme UK",
                        "location": "London, UK",
                        "description": "Build ML models.",
                    }
                ],
                None,
            )
        ),
    )
    scraper = SerpApiGoogleJobsScraper(api_key="fake", hl="en,fr", gl="fr")

    jobs = list(
        scraper.search(
            "Data Scientist OR Research Engineer",
            location="London, United Kingdom",
            max_results=1,
            use_configured_country_bias=False,
        )
    )

    assert len(jobs) == 1
    assert get_mock.call_count == 1
    params = get_mock.call_args.kwargs["params"]
    assert params["q"] == "Data Scientist OR Research Engineer"
    assert params["location"] == "London, United Kingdom"
    assert params["hl"] == "en"
    assert "gl" not in params


def test_serpapi_rejects_unbounded_max_results(mocker) -> None:
    get_mock = mocker.patch("smartapply.scrapers.serpapi.requests.get")
    s = SerpApiGoogleJobsScraper(api_key="fake", hl="fr")

    with pytest.raises(ScraperConfigError, match="requires max_results"):
        list(s.search("data", max_results=None))
    get_mock.assert_not_called()


def test_serpapi_network_error_is_not_reported_as_zero_results(mocker) -> None:
    scraper = SerpApiGoogleJobsScraper(api_key="fake", hl="fr")
    mocker.patch.object(
        scraper,
        "_fetch",
        side_effect=requests.ConnectionError("SerpAPI offline"),
    )

    with pytest.raises(ScraperError, match="ConnectionError") as exc_info:
        list(scraper.search("Data Scientist", location="Paris", max_results=5))
    assert "api_key" not in str(exc_info.value)


def test_serpapi_invalid_json_is_not_reported_as_zero_results(mocker) -> None:
    response = _mock_response({})
    response.json.side_effect = ValueError("invalid JSON")
    mocker.patch("smartapply.scrapers.serpapi.requests.get", return_value=response)

    scraper = SerpApiGoogleJobsScraper(api_key="fake-key")
    with pytest.raises(ScraperError, match="response invalid"):
        list(scraper.search("Data Scientist", location="Paris", max_results=5))


def test_serpapi_widens_zero_result_date_filter_before_giving_up(mocker) -> None:
    page_jobs = [
        {
            "title": "Machine Learning Engineer",
            "company_name": "Acme",
            "location": "Paris",
            "description": "Build ML models.",
            "apply_options": [{"link": "https://acme.test/jobs/ml"}],
            "detected_extensions": {"schedule_type": "Full-time"},
        }
    ]
    get_mock = mocker.patch(
        "smartapply.scrapers.serpapi.requests.get",
        side_effect=[
            _mock_response(_sample_serpapi_page([], None)),
            _mock_response(_sample_serpapi_page(page_jobs, None)),
        ],
    )

    s = SerpApiGoogleJobsScraper(api_key="fake", hl="fr")
    jobs = list(
        s.search(
            "Machine Learning Engineer",
            chips="employment_type:FULLTIME",
            date_posted="week",
            max_results=5,
        )
    )

    assert [job.title for job in jobs] == ["Machine Learning Engineer"]
    assert get_mock.call_args_list[0].kwargs["params"]["chips"] == (
        "employment_type:FULLTIME,date_posted:week"
    )
    assert get_mock.call_args_list[1].kwargs["params"]["chips"] == (
        "employment_type:FULLTIME,date_posted:month"
    )


# ============================ LinkedIn / Apify ============================


def test_linkedin_apify_uses_configured_default_max_results(mocker, monkeypatch) -> None:
    from smartapply.config import get_settings

    monkeypatch.setenv("LINKEDIN_MAX_RESULTS", "3")
    get_settings.cache_clear()
    post_mock = mocker.patch(
        "smartapply.scrapers.linkedin.requests.post",
        return_value=_mock_response([]),
    )

    s = LinkedInJobsScraper(token="fake-apify-token")
    list(s.search("Data Scientist", max_results=None))

    assert post_mock.call_args.kwargs["json"]["limit"] == 3
    get_settings.cache_clear()


def test_linkedin_apify_uses_configured_timeout(mocker, monkeypatch) -> None:
    from smartapply.config import get_settings

    monkeypatch.setenv("LINKEDIN_TIMEOUT", "17")
    get_settings.cache_clear()
    post_mock = mocker.patch(
        "smartapply.scrapers.linkedin.requests.post",
        return_value=_mock_response([]),
    )

    scraper = LinkedInJobsScraper(token="fake-apify-token")
    list(scraper.search("Data Scientist", max_results=1))

    assert post_mock.call_args.kwargs["timeout"] == 17
    get_settings.cache_clear()


def test_linkedin_apify_rejects_limit_above_configured_max(mocker, monkeypatch) -> None:
    from smartapply.config import get_settings

    monkeypatch.setenv("LINKEDIN_MAX_RESULTS", "1")
    get_settings.cache_clear()
    post_mock = mocker.patch("smartapply.scrapers.linkedin.requests.post")

    s = LinkedInJobsScraper(token="fake-apify-token")
    with pytest.raises(ScraperConfigError, match="LINKEDIN_MAX_RESULTS"):
        list(s.search("Data Scientist", max_results=3))
    post_mock.assert_not_called()
    get_settings.cache_clear()


def test_linkedin_apify_maps_jobs_and_payload(mocker) -> None:
    payload = [
        {
            "id": 1234567890,
            "url": "https://fr.linkedin.com/jobs/view/data-scientist-paris-at-example-energy-1234567890",
            "title": "Data Scientist- Paris",
            "location": "Paris, Île-de-France, France",
            "postedDate": "2026-06-30T00:00:00.000Z",
            "companyName": "Example Energy",
            "companyUrl": "https://cn.linkedin.com/company/example-energy",
            "recruiterName": "",
            "recruiterUrl": "",
            "experienceLevel": "Associate",
            "contractType": "Full-time",
            "workType": "Hybrid",
            "sector": "Energy Technology",
            "salary": "",
            "applyType": "EASY_APPLY",
            "postedTimeAgo": "18 hours ago",
            "applicationsCount": "Over 200 applicants",
            "description": "Build Python ML models for battery monitoring.",
            "descriptionHtml": "<p>Build Python ML models.</p>",
            "applyUrl": "",
        }
    ]
    post_mock = mocker.patch(
        "smartapply.scrapers.linkedin.requests.post",
        return_value=_mock_response(payload),
    )

    s = LinkedInJobsScraper(token="fake-apify-token")
    jobs = list(
        s.search(
            "Data Scientist",
            location="France",
            date_posted="today",
            max_results=1,
        )
    )

    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Data Scientist- Paris"
    assert job.company == "Example Energy"
    assert job.location == "Paris, Île-de-France, France"
    assert job.contract_type == "Full-time"
    assert job.remote_policy == "hybrid"
    assert job.application_url == payload[0]["url"]
    assert job.published_date is not None
    assert job.external_id.startswith("linkedin:")
    assert job.source_data
    assert job.source_data["_smartapply_search"]["datePosted"] == "r86400"

    sent = post_mock.call_args.kwargs
    assert sent["headers"] == {"Authorization": "Bearer fake-apify-token"}
    assert sent["json"] == {
        "title": "Data Scientist",
        "location": "France",
        "datePosted": "r86400",
        "contractType": ["F"],
        "experienceLevel": ["2", "3"],
        "remote": ["1", "2", "3"],
        "limit": 1,
        "urlPath": "/jobs/search",
    }


def test_linkedin_apify_prefers_html_description_and_stable_id(mocker) -> None:
    payload = [
        {
            "id": 1234567890,
            "url": "[https://fr.linkedin.com/jobs/view/data-scientist-paris-at-example-energy-1234567890](https://fr.linkedin.com/jobs/view/data-scientist-paris-at-example-energy-1234567890)",
            "title": "Data Scientist- Paris",
            "location": "Paris, Île-de-France, France",
            "postedDate": "2026-06-30T00:00:00.000Z",
            "companyName": "Example Energy",
            "companyUrl": "[https://cn.linkedin.com/company/example-energy](https://cn.linkedin.com/company/example-energy)",
            "recruiterName": "",
            "recruiterUrl": "",
            "experienceLevel": "Mid-Senior level",
            "contractType": "Full-time",
            "workType": "Project Management",
            "sector": "Energy Technology",
            "salary": "",
            "applyType": "EASY_APPLY",
            "postedTimeAgo": "18 hours ago",
            "applicationsCount": "Over 200 applicants",
            "description": "Job Responsibilities:Analyze and understand raw data",
            "descriptionHtml": (
                "<p>Job Responsibilities:</p>"
                "<ul><li>Analyze raw data.</li><li>Build Python ML models.</li></ul>"
                "<p>Requirements:</p><ul><li>3+ years of experience.</li></ul>"
            ),
            "applyUrl": "",
        }
    ]
    mocker.patch(
        "smartapply.scrapers.linkedin.requests.post",
        side_effect=[_mock_response([]), _mock_response(payload)],
    )

    s = LinkedInJobsScraper(token="fake-apify-token")
    jobs = list(
        s.search(
            "Data Scientist",
            location="France",
            date_posted="today",
            max_results=1,
        )
    )

    assert len(jobs) == 1
    job = jobs[0]
    assert job.description == (
        "Job Responsibilities:\n"
        "Analyze raw data.\n"
        "Build Python ML models.\n"
        "Requirements:\n"
        "3+ years of experience."
    )
    assert job.application_url == (
        "https://fr.linkedin.com/jobs/view/data-scientist-paris-at-example-energy-1234567890"
    )
    assert job.apply_options == [
        {
            "title": "LinkedIn Easy Apply",
            "link": "https://fr.linkedin.com/jobs/view/data-scientist-paris-at-example-energy-1234567890",
        }
    ]
    assert job.external_id == s._to_raw_job({**payload[0], "title": "Updated title"}).external_id
    assert job.source_data
    assert job.source_data["_smartapply_normalized"]["description_source"] == "descriptionHtml"
    assert job.source_data["_smartapply_search"]["experience_fallback_used"] is True


def test_linkedin_apify_uses_mid_senior_fallback_only_when_needed(mocker) -> None:
    first_payload = [
        {
            "id": "entry-1",
            "url": "https://fr.linkedin.com/jobs/view/entry-1",
            "title": "Junior Data Scientist",
            "location": "Paris, France",
            "postedDate": "2026-06-30T00:00:00.000Z",
            "companyName": "Acme",
            "experienceLevel": "Entry level",
            "contractType": "Full-time",
            "workType": "On-site",
            "description": "Build analytics models.",
        },
        {
            "id": "entry-2",
            "url": "https://fr.linkedin.com/jobs/view/entry-2",
            "title": "Associate Data Scientist",
            "location": "Paris, France",
            "postedDate": "2026-06-30T00:00:00.000Z",
            "companyName": "Delta",
            "experienceLevel": "Associate",
            "contractType": "Full-time",
            "workType": "Remote",
            "description": "Build data products.",
        },
        {
            "id": "mid-1",
            "url": "https://fr.linkedin.com/jobs/view/mid-1",
            "title": "Data Scientist",
            "location": "Paris, France",
            "postedDate": "2026-06-30T00:00:00.000Z",
            "companyName": "Beta",
            "experienceLevel": "Mid-Senior level",
            "contractType": "Full-time",
            "workType": "Hybrid",
            "description": "Build ML products.",
        },
    ]
    fallback_payload = [
        {
            "id": "mid-1",
            "url": "https://fr.linkedin.com/jobs/view/mid-1",
            "title": "Data Scientist",
            "location": "Paris, France",
            "postedDate": "2026-06-30T00:00:00.000Z",
            "companyName": "Beta",
            "experienceLevel": "Mid-Senior level",
            "contractType": "Full-time",
            "workType": "Hybrid",
            "description": "Build ML products.",
        }
    ]
    post_mock = mocker.patch(
        "smartapply.scrapers.linkedin.requests.post",
        side_effect=[
            _mock_response(first_payload),
            _mock_response(fallback_payload),
        ],
    )

    s = LinkedInJobsScraper(token="fake-apify-token")
    jobs = list(
        s.search(
            "Data Scientist",
            location="France",
            date_posted="today",
            max_results=3,
        )
    )

    assert [job.company for job in jobs] == ["Acme", "Delta", "Beta"]
    first_call = post_mock.call_args_list[0].kwargs["json"]
    second_call = post_mock.call_args_list[1].kwargs["json"]
    assert first_call["experienceLevel"] == ["2", "3"]
    assert first_call["limit"] == 3
    assert "skipJobId" not in first_call
    assert second_call["experienceLevel"] == ["4"]
    assert second_call["limit"] == 1
    assert second_call["skipJobId"] == ["entry-1", "entry-2"]
    assert jobs[2].source_data
    assert jobs[2].source_data["_smartapply_search"]["experience_fallback_used"] is True


def test_linkedin_apify_translates_selector_labels(mocker) -> None:
    post_mock = mocker.patch(
        "smartapply.scrapers.linkedin.requests.post",
        return_value=_mock_response([]),
    )

    s = LinkedInJobsScraper(token="fake-apify-token")
    list(
        s.search(
            "Data Scientist",
            location="France",
            contract_type="Full-time",
            experience_level=["Entry level", "Associate"],
            remote=["On-site", "Remote", "Hybrid"],
            max_results=1,
        )
    )

    sent = post_mock.call_args.kwargs["json"]
    assert sent["contractType"] == ["F"]
    assert sent["experienceLevel"] == ["2", "3"]
    assert sent["remote"] == ["1", "2", "3"]


def test_linkedin_apify_auth_error_is_actionable_and_not_retried(mocker) -> None:
    response = _mock_response(
        {
            "error": {
                "type": "access-denied",
                "message": "User is not allowed to run this actor.",
            }
        },
        status_code=403,
    )
    post_mock = mocker.patch(
        "smartapply.scrapers.linkedin.requests.post",
        return_value=response,
    )

    secret_token = "apify_api_secret123"
    s = LinkedInJobsScraper(token=secret_token)
    with pytest.raises(ScraperConfigError) as exc:
        list(s.search("Data Scientist", location="France", max_results=1))

    message = str(exc.value)
    assert "HTTP 403" in message
    assert "APIFY_TOKEN" in message
    assert "actor access/subscription" in message
    assert "User is not allowed" in message
    assert secret_token not in message
    assert post_mock.call_count == 1
    assert post_mock.call_args.kwargs["headers"] == {"Authorization": f"Bearer {secret_token}"}


def test_linkedin_network_error_is_not_reported_as_zero_results(mocker) -> None:
    scraper = LinkedInJobsScraper(token="fake-apify-token")
    mocker.patch.object(
        scraper,
        "_fetch",
        side_effect=requests.ConnectionError("Apify offline"),
    )

    with pytest.raises(requests.ConnectionError, match="Apify offline"):
        list(scraper.search("Data Scientist", location="London", max_results=5))


# ============================ France Travail ============================


def test_francetravail_authenticates_and_maps(mocker) -> None:
    token_response = _mock_response({"access_token": "T0K3N", "expires_in": 1500})
    search_response = _mock_response(
        {
            "resultats": [
                {
                    "id": "OFR-001",
                    "intitule": "Data Scientist H/F",
                    "entreprise": {"nom": "Acme SA"},
                    "lieuTravail": {"libelle": "75 - Paris"},
                    "description": "Construire des pipelines ML.",
                    "competences": [
                        {"libelle": "Python", "exigence": "E"},
                        {"libelle": "PyTorch", "exigence": "S"},
                    ],
                    "typeContratLibelle": "CDI",
                    "dateCreation": "2026-05-01T12:00:00.000Z",
                    "origineOffre": {
                        "urlOrigine": "https://candidat.francetravail.fr/offres/OFR-001"
                    },
                }
            ]
        }
    )
    empty_response = _mock_response({"resultats": []})

    mocker.patch(
        "smartapply.scrapers.francetravail.requests.post",
        return_value=token_response,
    )
    mocker.patch(
        "smartapply.scrapers.francetravail.requests.get",
        side_effect=[search_response, empty_response],
    )

    s = FranceTravailScraper(client_id="cid", client_secret="csec")
    jobs = list(s.search("data scientist", location="Paris"))
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Data Scientist H/F"
    assert j.company == "Acme SA"
    assert j.contract_type == "CDI"
    assert j.location == "75 - Paris"
    assert "PyTorch" in j.description
    assert j.published_date is not None
    assert j.application_url and "OFR-001" in j.application_url


def test_francetravail_network_error_is_not_reported_as_zero_results(mocker) -> None:
    scraper = FranceTravailScraper(client_id="cid", client_secret="csec")
    mocker.patch.object(scraper, "_get_token", return_value="token")
    mocker.patch.object(
        scraper,
        "_fetch_range",
        side_effect=requests.ConnectionError("France Travail offline"),
    )

    with pytest.raises(requests.ConnectionError, match="France Travail offline"):
        list(scraper.search("Data Scientist", location="Paris", max_results=5))


def test_francetravail_invalid_json_is_not_reported_as_zero_results(mocker) -> None:
    scraper = FranceTravailScraper(client_id="cid", client_secret="csec")
    mocker.patch.object(scraper, "_get_token", return_value="token")
    response = _mock_response({})
    response.json.side_effect = ValueError("invalid JSON")
    mocker.patch.object(
        scraper,
        "_request_with_cancel",
        return_value=response,
    )

    with pytest.raises(ValueError, match="invalid JSON"):
        list(scraper.search("Data Scientist", location="Paris", max_results=5))


def test_francetravail_date_posted_week_sends_creation_window(mocker) -> None:
    """FT API rejects minCreationDate alone — both bounds must be sent together."""
    from datetime import datetime, timezone

    from smartapply.scrapers.francetravail import _date_posted_to_creation_window

    fixed_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
    assert _date_posted_to_creation_window("week", now=fixed_now) == (
        "2026-05-28T12:00:00Z",
        "2026-06-04T12:00:00Z",
    )

    token_response = _mock_response({"access_token": "T0K3N", "expires_in": 1500})
    empty_response = _mock_response({"resultats": []})
    mocker.patch("smartapply.scrapers.francetravail.requests.post", return_value=token_response)
    get_mock = mocker.patch(
        "smartapply.scrapers.francetravail.requests.get", return_value=empty_response
    )
    mocker.patch(
        "smartapply.scrapers.francetravail.datetime",
        wraps=datetime,
        now=lambda tz=None: fixed_now.astimezone(tz) if tz else fixed_now,
    )

    s = FranceTravailScraper(client_id="cid", client_secret="csec")
    list(s.search("data scientist", date_posted="week"))

    params = get_mock.call_args.kwargs["params"]
    assert params["minCreationDate"] == "2026-05-28T12:00:00Z"
    assert params["maxCreationDate"] == "2026-06-04T12:00:00Z"
    assert params["motsCles"] == "data scientist"


# --- Location resolution -----------------------------------------------------
# The FT search used to silently append the free-text location to ``motsCles``,
# which collapsed Paris searches to ~71 offers (only those whose searchable
# text contained "Paris" literally). The resolver below converts known
# city/region names into structured FT params and leaves unknowns alone.


@pytest.fixture(autouse=True)
def _isolate_french_geo_cache(request, mocker, tmp_path):
    """Make sure FT scraper tests never touch the real geo cache or network.

    Each FT test starts with an empty in-memory cache, an isolated cache
    directory, and ``geo.api.gouv.fr`` forced offline. Tests that want to
    exercise the real path opt in by overriding
    ``smartapply.utils.geo.resolver.requests.get`` afterwards.
    Non-FT tests are untouched.
    """
    if "francetravail" not in request.node.name.lower():
        yield
        return
    from smartapply.utils.geo import resolver as _geo

    _geo.reset_cache_for_tests()
    cache_dir = tmp_path / "geo_cache"
    cache_dir.mkdir()
    fake_settings = mocker.Mock()
    fake_settings.cache_dir = cache_dir
    mocker.patch(
        "smartapply.utils.geo.resolver.get_settings",
        return_value=fake_settings,
        create=True,
    )
    mocker.patch("smartapply.config.get_settings", return_value=fake_settings)
    mocker.patch(
        "smartapply.utils.geo.resolver.requests.get",
        side_effect=requests.ConnectionError("blocked in tests by default"),
    )
    yield
    _geo.reset_cache_for_tests()


def _ft_search_params(mocker, query: str, **search_kwargs):
    """Run one mocked FT search and return the params sent to the API."""
    token_response = _mock_response({"access_token": "T0K3N", "expires_in": 1500})
    empty_response = _mock_response({"resultats": []})
    mocker.patch("smartapply.scrapers.francetravail.requests.post", return_value=token_response)
    get_mock = mocker.patch(
        "smartapply.scrapers.francetravail.requests.get", return_value=empty_response
    )
    s = FranceTravailScraper(client_id="cid", client_secret="csec")
    list(s.search(query, **search_kwargs))
    return get_mock.call_args.kwargs["params"]


def test_francetravail_paris_resolves_to_departement_75(mocker) -> None:
    """``Paris`` / ``Paris, France`` must trigger ``departement=75``, not a
    keyword append that excludes most Paris offers."""
    for location in ("Paris", "Paris, France", "paris", "Paris,France"):
        params = _ft_search_params(mocker, "data scientist", location=location)
        assert params["motsCles"] == "data scientist", (
            f"location={location!r} leaked into motsCles: {params['motsCles']!r}"
        )
        assert params.get("departement") == "75", (
            f"location={location!r} did not set departement=75: {params}"
        )
