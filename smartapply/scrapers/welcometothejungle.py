"""Welcome to the Jungle personalized matches scraper."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

import requests as _requests

from smartapply.config import get_source_settings
from smartapply.logging_setup import get_logger
from smartapply.offers import RawJob
from smartapply.scrapers.base import Scraper, ScraperConfigError
from smartapply.scrapers.serpapi_query import normalize_date_posted
from smartapply.scrapers.wttj.contracts import (
    WTTJ_SOURCE,
    WTTJAuthenticationError,
    WTTJJobLink,
    WTTJScraperError,
)

logger = get_logger(__name__)
requests = _requests

from smartapply.scrapers.wttj import company_hydration as _company_hydration  # noqa: E402
from smartapply.scrapers.wttj import matches_api as _matches_api  # noqa: E402
from smartapply.scrapers.wttj import offer_parser as _offer_parser  # noqa: E402
from smartapply.scrapers.wttj.company_hydration import (  # noqa: E402
    parse_company_html,
    parse_saved_company,
)
from smartapply.scrapers.wttj.matches_api import (  # noqa: E402
    fetch_html_with_requests,
    matches_page_url,
    parse_listing_links,
    parse_matches_api_links,
    parse_saved_listing,
    scrape_matches_live,
)
from smartapply.scrapers.wttj.offer_parser import (  # noqa: E402
    parse_detail_api_job,
    parse_detail_html,
    parse_saved_detail,
)

WTTJ_DATE_POSTED_TO_PUBLISHED_SINCE = {
    "today": "last_24h",
    "3days": "last_3d",
    "week": "last_7d",
}


def _published_since_from_date_posted(date_posted: str | None) -> str | None:
    normalized = normalize_date_posted(date_posted)
    return WTTJ_DATE_POSTED_TO_PUBLISHED_SINCE.get(normalized)


def _should_stop(stop_requested: Callable[[], bool] | None) -> bool:
    return bool(stop_requested and stop_requested())


def fetch_matches_api_page(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _matches_api.fetch_matches_api_page(*args, **kwargs)


def fetch_detail_api_job(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return _offer_parser.fetch_detail_api_job(*args, **kwargs)


def scrape_matches_requests(
    *,
    pages: Sequence[int],
    cookie_header: str,
    max_jobs: int | None = None,
    progress_target: int | None = None,
    per_page: int | None = None,
    published_since: str | None = None,
    include_company_profile: bool = True,
    skip_failed_jobs: bool = True,
    timeout: int = 30,
    delay_seconds: float = 0.5,
    extra_headers: dict[str, str] | None = None,
    stop_requested: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> Iterator[RawJob]:
    return _matches_api.scrape_matches_requests(
        pages=pages,
        cookie_header=cookie_header,
        max_jobs=max_jobs,
        progress_target=progress_target,
        per_page=per_page,
        published_since=published_since,
        include_company_profile=include_company_profile,
        skip_failed_jobs=skip_failed_jobs,
        timeout=timeout,
        delay_seconds=delay_seconds,
        extra_headers=extra_headers,
        stop_requested=stop_requested,
        progress_callback=progress_callback,
        fetch_matches_api_page_fn=fetch_matches_api_page,
        fetch_detail_api_job_fn=fetch_detail_api_job,
    )


class WelcomeToTheJungleScraper(Scraper):
    """Personalized Welcome to the Jungle matches source.

    WTTJ does not expose a searchable endpoint for this workflow. The source
    reads the logged-in user's personalized matches feed, then enriches each
    public job page with the public WTTJ company profile.
    """

    name = WTTJ_SOURCE

    def __init__(
        self,
        *,
        cookie_header: str | None = None,
        max_pages: int | None = None,
        pages: int | None = None,
        per_page: int | None = None,
        include_company_profile: bool | None = None,
        skip_failed_jobs: bool | None = None,
        timeout: int | None = None,
        delay_seconds: float | None = None,
    ) -> None:
        settings = get_source_settings()
        self.cookie_header = cookie_header if cookie_header is not None else settings.wttj_cookie
        self.max_pages = max_pages if max_pages is not None else settings.wttj_max_pages
        self.pages = pages if pages is not None else settings.wttj_pages
        self.per_page = per_page if per_page is not None else settings.wttj_per_page
        self.include_company_profile = (
            include_company_profile
            if include_company_profile is not None
            else settings.wttj_include_company_profile
        )
        self.skip_failed_jobs = (
            skip_failed_jobs if skip_failed_jobs is not None else settings.wttj_skip_failed_jobs
        )
        self.timeout = timeout if timeout is not None else settings.wttj_timeout
        self.delay_seconds = (
            delay_seconds if delay_seconds is not None else settings.wttj_delay_seconds
        )
        self.last_warnings: list[str] = []

    def is_available(self) -> bool:
        return bool(self.cookie_header.strip())

    def search(
        self,
        query: str,
        location: str | None = None,
        *,
        max_results: int | None = None,
        **kwargs: Any,
    ) -> Iterator[RawJob]:
        self.last_warnings = []
        if not self.is_available():
            raise ScraperConfigError("WTTJ_COOKIE must be set to use the WTTJ scraper")
        if max_results is not None and max_results <= 0:
            return
        stop_requested = kwargs.pop("stop_requested", None)
        if _should_stop(stop_requested):
            return

        pages = min(int(kwargs.pop("pages", self.pages)), int(self.max_pages))
        per_page = kwargs.pop("per_page", self.per_page)
        include_company_profile = bool(
            kwargs.pop("include_company_profile", self.include_company_profile)
        )
        skip_failed_jobs = bool(kwargs.pop("skip_failed_jobs", self.skip_failed_jobs))
        timeout = int(kwargs.pop("timeout", self.timeout))
        delay_seconds = float(kwargs.pop("delay_seconds", self.delay_seconds))
        progress_callback = kwargs.pop("progress_callback", None)

        def capture_progress(event: dict[str, Any]) -> None:
            if event.get("event") == "warning":
                message = str(event.get("message") or "").strip()
                if message and message not in self.last_warnings:
                    self.last_warnings.append(message)
            if progress_callback is not None:
                progress_callback(event)

        progress_target = kwargs.pop("progress_target", max_results)
        date_posted = kwargs.pop("date_posted", None)
        published_since = _published_since_from_date_posted(date_posted)
        page_numbers = range(1, pages + 1)
        if progress_callback is not None:
            progress_callback(
                {
                    "event": "start",
                    "pages_total": pages,
                    "per_page": per_page,
                    "max_jobs": max_results,
                    "progress_target": progress_target,
                    "yielded": 0,
                }
            )

        for job in scrape_matches_requests(
            pages=page_numbers,
            cookie_header=self.cookie_header,
            max_jobs=max_results,
            progress_target=progress_target,
            per_page=per_page,
            published_since=published_since,
            include_company_profile=include_company_profile,
            skip_failed_jobs=skip_failed_jobs,
            timeout=timeout,
            delay_seconds=delay_seconds,
            stop_requested=stop_requested,
            progress_callback=capture_progress,
        ):
            if _should_stop(stop_requested):
                return
            source_data = dict(job.source_data or {})
            source_data["_smartapply_search"] = {
                "source_mode": "personalized_matches",
                "query": query,
                "location": location,
                "pages": pages,
                "per_page": per_page,
                "date_posted": date_posted,
                "published_since": published_since,
                "include_company_profile": include_company_profile,
            }
            job.source_data = source_data
            yield job


for _module in (_matches_api, _offer_parser, _company_hydration):
    for _name in dir(_module):
        if _name.startswith("_") and not _name.startswith("__"):
            globals().setdefault(_name, getattr(_module, _name))

__all__ = [
    "WTTJ_SOURCE",
    "WTTJAuthenticationError",
    "WTTJJobLink",
    "WTTJScraperError",
    "WelcomeToTheJungleScraper",
    "fetch_detail_api_job",
    "fetch_html_with_requests",
    "fetch_matches_api_page",
    "matches_page_url",
    "parse_company_html",
    "parse_detail_api_job",
    "parse_detail_html",
    "parse_listing_links",
    "parse_matches_api_links",
    "parse_saved_company",
    "parse_saved_detail",
    "parse_saved_listing",
    "scrape_matches_live",
    "scrape_matches_requests",
]
