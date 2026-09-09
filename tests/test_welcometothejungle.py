"""Tests for the Welcome to the Jungle scraper prototype."""

from __future__ import annotations

import pytest
import requests

from smartapply.offers import RawJob
from smartapply.scrapers.welcometothejungle import (
    WTTJ_SOURCE,
    WelcomeToTheJungleScraper,
    WTTJAuthenticationError,
    parse_company_html,
    parse_detail_api_job,
    parse_detail_html,
    scrape_matches_requests,
)
from smartapply.scrapers.wttj import matches_api
from smartapply.scrapers.wttj.contracts import WTTJJobLink, WTTJScraperError


def test_wttj_scraper_requires_cookie() -> None:
    scraper = WelcomeToTheJungleScraper(cookie_header="")

    assert scraper.is_available() is False


def test_wttj_scraper_search_adds_audit_metadata(mocker) -> None:  # noqa: ANN001
    raw = mocker.MagicMock()
    raw.source_data = {"company_domain": "acme.com"}
    scrape = mocker.patch(
        "smartapply.scrapers.welcometothejungle.scrape_matches_requests",
        return_value=iter([raw]),
    )

    scraper = WelcomeToTheJungleScraper(
        cookie_header="wttj_session=abc",
        pages=5,
        per_page=10,
        include_company_profile=True,
        delay_seconds=0,
    )
    jobs = list(scraper.search("Data Scientist", location="Paris", max_results=20))

    scrape.assert_called_once_with(
        pages=range(1, 6),
        cookie_header="wttj_session=abc",
        max_jobs=20,
        progress_target=20,
        per_page=10,
        published_since=None,
        include_company_profile=True,
        skip_failed_jobs=True,
        timeout=30,
        delay_seconds=0.0,
        stop_requested=None,
        progress_callback=mocker.ANY,
    )
    assert jobs == [raw]
    assert raw.source_data["_smartapply_search"] == {
        "source_mode": "personalized_matches",
        "query": "Data Scientist",
        "location": "Paris",
        "pages": 5,
        "per_page": 10,
        "date_posted": None,
        "published_since": None,
        "include_company_profile": True,
    }


def test_wttj_scraper_keeps_partial_failure_warnings(mocker) -> None:  # noqa: ANN001
    raw = mocker.MagicMock()
    raw.source_data = {}

    def fake_scrape(**kwargs):  # noqa: ANN003, ANN202
        kwargs["progress_callback"](
            {
                "event": "warning",
                "code": "job_detail_failed",
                "message": "WTTJ : une fiche était illisible.",
            }
        )
        yield raw

    mocker.patch(
        "smartapply.scrapers.welcometothejungle.scrape_matches_requests",
        side_effect=fake_scrape,
    )
    external_progress = mocker.Mock()
    scraper = WelcomeToTheJungleScraper(
        cookie_header="wttj_session=abc",
        delay_seconds=0,
    )

    jobs = list(
        scraper.search(
            "Data Scientist",
            max_results=1,
            progress_callback=external_progress,
        )
    )

    assert jobs == [raw]
    assert scraper.last_warnings == ["WTTJ : une fiche était illisible."]
    assert any(call.args[0].get("event") == "warning" for call in external_progress.call_args_list)


def test_wttj_partial_detail_failure_emits_warning_and_keeps_valid_job(
    mocker,
) -> None:  # noqa: ANN001
    valid_job = RawJob(
        external_id="wttj:valid",
        title="Data Scientist",
        company="Acme",
        description="Construire des modèles.",
        source=WTTJ_SOURCE,
        source_data={},
    )
    links = [
        WTTJJobLink(url="https://example.test/jobs/valid", title_hint="Valid"),
        WTTJJobLink(url="https://example.test/jobs/broken", title_hint="Broken"),
    ]
    response = mocker.Mock(text="<html></html>")
    response.raise_for_status.return_value = None
    mocker.patch.object(matches_api, "parse_matches_api_links", return_value=links)
    mocker.patch.object(matches_api.requests, "get", return_value=response)
    mocker.patch.object(
        matches_api,
        "parse_detail_html",
        side_effect=[valid_job, WTTJScraperError("HTML illisible")],
    )
    progress_events: list[dict] = []

    jobs = list(
        matches_api.scrape_matches_requests(
            pages=[1],
            cookie_header="wttj_session=abc",
            include_company_profile=False,
            delay_seconds=0,
            progress_callback=progress_events.append,
            fetch_matches_api_page_fn=lambda **kwargs: {  # noqa: ARG005
                "data": [{"id": "valid"}, {"id": "broken"}],
                "metadata": {"page_count": 1},
            },
            fetch_detail_api_job_fn=lambda *args, **kwargs: (  # noqa: ARG005
                (_ for _ in ()).throw(requests.ConnectionError("API offline"))
            ),
        )
    )

    assert jobs == [valid_job]
    warnings = [event for event in progress_events if event.get("event") == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["code"] == "job_detail_failed"
    assert "offre 2" in warnings[0]["message"]


def test_wttj_scraper_search_forwards_date_posted_filter(mocker) -> None:  # noqa: ANN001
    raw = mocker.MagicMock()
    raw.source_data = {}
    scrape = mocker.patch(
        "smartapply.scrapers.welcometothejungle.scrape_matches_requests",
        return_value=iter([raw]),
    )

    scraper = WelcomeToTheJungleScraper(cookie_header="wttj_session=abc", delay_seconds=0)

    jobs = list(scraper.search("Data Scientist", max_results=1, date_posted="week"))

    assert jobs == [raw]
    assert scrape.call_args.kwargs["published_since"] == "last_7d"
    assert raw.source_data["_smartapply_search"]["date_posted"] == "week"
    assert raw.source_data["_smartapply_search"]["published_since"] == "last_7d"


def test_wttj_scrape_matches_requests_forwards_published_since(mocker) -> None:  # noqa: ANN001
    inner = mocker.patch(
        "smartapply.scrapers.welcometothejungle._matches_api.scrape_matches_requests",
        return_value=iter([]),
    )

    list(
        scrape_matches_requests(
            pages=range(1, 2),
            cookie_header="wttj_session=abc",
            published_since="last_7d",
        )
    )

    assert inner.call_args.kwargs["published_since"] == "last_7d"


def test_wttj_scrape_matches_requests_fails_fast_on_auth_error(mocker) -> None:  # noqa: ANN001
    fetch_page = mocker.patch(
        "smartapply.scrapers.welcometothejungle.fetch_matches_api_page",
        side_effect=WTTJAuthenticationError("WTTJ API rejected the Cookie header."),
    )

    with pytest.raises(WTTJAuthenticationError, match="Cookie header"):
        list(
            scrape_matches_requests(
                pages=range(1, 4),
                cookie_header="wttj_session=expired",
            )
        )

    fetch_page.assert_called_once()


def test_wttj_rejects_success_payload_without_data_list(mocker) -> None:  # noqa: ANN001
    response = mocker.Mock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = {"metadata": {"page_count": 1}}
    mocker.patch.object(matches_api.requests, "get", return_value=response)

    with pytest.raises(WTTJScraperError, match="no data list"):
        matches_api.fetch_matches_api_page(
            page=1,
            cookie_header="wttj_session=abc",
            timeout=1,
        )


def test_wttj_scrape_matches_requests_stops_on_missing_page(mocker) -> None:  # noqa: ANN001
    response = requests.Response()
    response.status_code = 404
    missing_page = requests.HTTPError("404 Client Error", response=response)
    fetch_page = mocker.patch(
        "smartapply.scrapers.welcometothejungle.fetch_matches_api_page",
        side_effect=[
            missing_page,
            {"data": [], "metadata": {"page": 2}},
        ],
    )
    progress = mocker.Mock()

    list(
        scrape_matches_requests(
            pages=range(1, 4),
            cookie_header="wttj_session=abc",
            progress_callback=progress,
        )
    )

    fetched_pages = [call.kwargs["page"] for call in fetch_page.call_args_list]
    assert fetched_pages == [1]
    assert any(call.args[0].get("event") == "page_missing" for call in progress.call_args_list)


def test_wttj_network_error_is_not_reported_as_zero_results(mocker) -> None:  # noqa: ANN001
    fetch_page = mocker.patch(
        "smartapply.scrapers.welcometothejungle.fetch_matches_api_page",
        side_effect=requests.ConnectionError("WTTJ offline"),
    )

    with pytest.raises(requests.ConnectionError, match="WTTJ offline"):
        list(
            scrape_matches_requests(
                pages=range(1, 2),
                cookie_header="wttj_session=abc",
                skip_failed_jobs=True,
            )
        )

    fetch_page.assert_called_once()


def test_wttj_scrape_matches_requests_stops_when_page_exceeds_page_count(mocker) -> None:  # noqa: ANN001
    fetch_page = mocker.patch(
        "smartapply.scrapers.welcometothejungle.fetch_matches_api_page",
        return_value={"data": [], "metadata": {"page_count": 6}},
    )
    progress = mocker.Mock()

    list(
        scrape_matches_requests(
            pages=[1000],
            cookie_header="wttj_session=abc",
            progress_callback=progress,
        )
    )

    fetched_pages = [call.kwargs["page"] for call in fetch_page.call_args_list]
    assert fetched_pages == [1000]
    assert any(call.args[0].get("event") == "page_missing" for call in progress.call_args_list)


def test_parse_detail_html_reads_json_ld_and_metadata() -> None:
    html = """
    <html>
      <head>
        <title>Senior ML Engineer - Example Health - CDI à Paris</title>
        <script type="application/ld+json">
        {
          "@context": "http://schema.org",
          "@type": "JobPosting",
          "datePosted": "2026-05-27T22:01:01Z",
          "description": "<h2>What you'll do</h2><p>Build reliable AI evaluation systems.</p>",
          "employmentType": "FULL_TIME",
          "hiringOrganization": {
            "@type": "Organization",
            "name": "Example Health"
          },
          "industry": "Santé",
          "jobLocation": [{
            "@type": "Place",
            "address": {
              "@type": "PostalAddress",
              "addressLocality": "Paris",
              "addressRegion": "Paris",
              "addressCountry": "FR"
            }
          }],
          "title": "Senior ML Engineer",
          "validThrough": "2026-08-25T22:01:01.000Z"
        }
        </script>
      </head>
      <body>
        <div data-testid="job-metadata-block">
          Example Health Senior ML Engineer Résumé du poste CDI Paris Télétravail fréquent
          Salaire : Non spécifié
          <div>
            <div><span><span>Compétences & expertises</span></span></div>
            <div>
              <div><span>Python</span></div>
              <div><span>Communication</span></div>
              <div><span>+3</span></div>
            </div>
          </div>
        </div>
        <aside>
          <a href="/fr/companies/example-health">Explorer l’entreprise</a>
          <a href="https://about.health.example.com/">Voir le site</a>
          <h4><span>Qui sont-ils ?</span></h4>
          <p>Example Health builds healthcare software.</p>
          <h4><span>Le lieu de travail</span></h4>
          <a>Paris, France</a>
          <div data-testid="job-company-tag">120 collaborateurs</div>
          <div data-testid="job-company-tag">Créée en 2018</div>
        </aside>
      </body>
    </html>
    """

    job = parse_detail_html(
        html,
        url="https://www.welcometothejungle.com/fr/companies/example-health/jobs/senior-ml_paris",
    )

    assert job.source == WTTJ_SOURCE
    assert job.title == "Senior ML Engineer"
    assert job.company == "Example Health"
    assert job.location == "Paris, FR"
    assert job.contract_type == "CDI"
    assert job.remote_policy == "hybrid"
    assert "Build reliable AI evaluation systems." in job.description
    assert job.published_date is not None
    assert job.source_data is not None
    assert job.source_data["company_profile_url"] == (
        "https://www.welcometothejungle.com/fr/companies/example-health"
    )
    assert job.source_data["company_website"] == "https://about.health.example.com/"
    assert job.source_data["company_domain"] == "about.health.example.com"
    assert job.source_data["company_summary"] == "Example Health builds healthcare software."
    assert job.source_data["workplace"] == "Paris, France"
    assert job.source_data["skills"] == ["Python", "Communication"]
    assert job.source_data["skills_more_count"] == 3
    assert job.source_data["company_stats"] == {
        "employees": "120 collaborateurs",
        "founded": "Créée en 2018",
    }
    assert job.application_url == (
        "https://www.welcometothejungle.com/fr/companies/example-health/jobs/senior-ml_paris"
    )
    assert job.external_id.startswith("welcometothejungle:")


def test_parse_detail_api_job_reads_public_api_payload() -> None:
    job = parse_detail_api_job(
        {
            "name": "Business data analyst",
            "slug": "business-data-analyst_paris",
            "contract_type": "full_time",
            "remote": "partial",
            "published_at": "2026-05-22T07:35:43Z",
            "experience_level": "3_TO_4_YEARS",
            "description": "<p>Build business data products.</p>",
            "looking_for_candidate_description": "<p>Python and communication.</p>",
            "salary_min": 45000,
            "salary_max": 50000,
            "salary_currency": "EUR",
            "salary_period": "yearly",
            "offices": [{"city": "Paris", "country_code": "FR"}],
            "organization": {
                "name": "Phagos",
                "slug": "phagos-1",
                "description": "<p>Phagos builds biotech products.</p>",
                "nb_employees": 70,
                "creation_year": 2021,
            },
            "profession": {"category_name": "Data"},
        },
        url="https://www.welcometothejungle.com/fr/companies/phagos-1/jobs/business-data-analyst_paris",
    )

    assert job.title == "Business data analyst"
    assert job.company == "Phagos"
    assert job.location == "Paris, FR"
    assert job.contract_type == "Full-time"
    assert job.remote_policy == "hybrid"
    assert job.published_date is not None
    assert job.experience == {
        "level": "3_TO_4_YEARS",
        "min_years": 3.0,
        "required": True,
    }
    assert "Build business data products." in job.description
    assert job.source_data is not None
    assert job.source_data["salary"]["min"] == 45000
    assert job.source_data["company_profile"]["presentation"] == "Phagos builds biotech products."


def test_parse_company_html_reads_profile_fields() -> None:
    html = """
    <html>
      <head>
        <title>WeWard: pictures, videos and careers</title>
        <link rel="canonical" href="https://www.welcometothejungle.com/en/companies/weward" />
      </head>
      <body>
        <main data-testid="page-organization-profile">
          <header data-testid="showcase-header">
            WeWard Follow
            <div data-testid="showcase-header-sector">Application mobile, Santé</div>
            <div data-testid="showcase-header-office">Paris, New York</div>
            <a data-testid="showcase-header-website-link" href="https://www.weward.fr/">View website</a>
          </header>
          <span data-testid="stats-creation-year">2019</span>
          <span data-testid="stats-nb-employees">50</span>
          <span data-testid="stats-parity-women">45%</span>
          <span data-testid="stats-parity-men">55%</span>
          <span data-testid="stats-average-age">28 years old</span>
          <span data-testid="stats-turnover">0%</span>
          <div data-testid="organization-content-block-text">
            <h2>Presentation</h2>
            <p>WeWard rewards walking.</p>
          </div>
          <div data-testid="organization-content-block-text">
            <h2>Good to know</h2>
            <p>Hybrid working possible.</p>
          </div>
          <div data-testid="organization-content-block-map">83 Boulevard de Sébastopol 75002 Paris</div>
          <a data-testid="social-network-linkedin" href="https://www.linkedin.com/company/weward-app"></a>
        </main>
      </body>
    </html>
    """

    company = parse_company_html(html)

    assert company["name"] == "WeWard"
    assert company["url"] == "https://www.welcometothejungle.com/en/companies/weward"
    assert company["website"] == "https://www.weward.fr/"
    assert company["domain"] == "weward.fr"
    assert company["sectors"] == "Application mobile, Santé"
    assert company["offices"] == "Paris, New York"
    assert company["presentation"] == "WeWard rewards walking."
    assert company["good_to_know"] == "Hybrid working possible."
    assert company["addresses"] == ["83 Boulevard de Sébastopol 75002 Paris"]
    assert company["stats"]["employees"] == "50"
    assert company["social_links"]["linkedin"] == "https://www.linkedin.com/company/weward-app"
