"""SerpApi Google Jobs scraper.

Documentation: https://serpapi.com/google-jobs-api
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from smartapply.config import get_source_settings
from smartapply.logging_setup import get_logger
from smartapply.offers import RawJob, make_external_id
from smartapply.scrapers.base import Scraper, ScraperConfigError, ScraperError
from smartapply.scrapers.serpapi_query import (
    SERPAPI_DATE_POSTED_LABELS,
    SERPAPI_DATE_POSTED_OPTIONS,
    _low_result_target,
    _should_widen_low_result,
    _with_search_audit,
    combine_chips,
    date_posted_chip,
    market_languages,
    normalize_date_posted,
    split_localization_values,
    zero_result_fallback_params,
)
from smartapply.utils.contracts import normalize_source_contract_type

logger = get_logger(__name__)

SERPAPI_URL = "https://serpapi.com/search.json"


def _should_stop(stop_requested: Callable[[], bool] | None) -> bool:
    return bool(stop_requested and stop_requested())


def _request_error_summary(exc: requests.RequestException) -> str:
    """Describe a failed request without exposing SerpAPI query credentials."""
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        reason = str(getattr(response, "reason", "") or "").strip()
        return " ".join(
            part for part in (f"HTTP {status}" if status else "HTTP error", reason) if part
        )
    return type(exc).__name__


class SerpApiGoogleJobsScraper(Scraper):
    name = "serpapi"

    def __init__(
        self,
        api_key: str | None = None,
        max_pages: int | None = None,
        *,
        google_domain: str | None = None,
        hl: str | None = None,
        gl: str | None = None,
        low_result_fallback_target: int | None = None,
    ):
        settings = get_source_settings()
        self.api_key = api_key or settings.serpapi_api_key
        self.max_pages = max_pages or settings.serpapi_max_pages
        self.low_result_fallback_target = (
            settings.serpapi_low_result_fallback_target
            if low_result_fallback_target is None
            else low_result_fallback_target
        )
        self.google_domain = google_domain or settings.serpapi_google_domain
        self.hl = hl or settings.serpapi_hl
        self.gl = gl or settings.serpapi_gl
        self.default_location = settings.serpapi_default_location
        self.date_posted = normalize_date_posted(settings.serpapi_date_posted)
        self.uds = settings.serpapi_uds.strip()

    def is_available(self) -> bool:
        return bool(self.api_key)

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _fetch(self, params: dict[str, Any]) -> dict[str, Any]:
        response = requests.get(SERPAPI_URL, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def search(
        self,
        query: str,
        location: str | None = None,
        *,
        max_results: int | None = None,
        ltype: str | None = None,
        chips: str | None = None,
        uds: str | None = None,
        date_posted: str | None = None,
        hl: str | None = None,
        gl: str | None = None,
        google_domain: str | None = None,
        use_configured_country_bias: bool = True,
        stop_requested: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> Iterator[RawJob]:
        if _should_stop(stop_requested):
            return
        if not self.api_key:
            raise ScraperConfigError("SERPAPI_API_KEY is not set")
        if max_results is None:
            raise ScraperConfigError(
                "SerpApi requires max_results to avoid unbounded paid pagination."
            )
        if max_results is not None and max_results <= 0:
            return

        freshness = self.date_posted if date_posted is None else normalize_date_posted(date_posted)
        search_query = query.strip()
        date_chip = date_posted_chip(freshness)
        search_chips = combine_chips(chips, date_chip)
        search_uds = (uds if uds is not None else self.uds).strip()
        languages = split_localization_values(hl, fallback=self.hl)
        countries: list[str | None] = (
            split_localization_values(gl, fallback=self.gl)
            if use_configured_country_bias
            else [None]
        )
        domains = split_localization_values(google_domain, fallback=self.google_domain)
        yielded = 0
        seen_external_ids: set[str] = set()
        for domain in domains:
            if _should_stop(stop_requested):
                return
            for country in countries:
                if _should_stop(stop_requested):
                    return
                for language in market_languages(
                    languages,
                    country=country,
                    location=location or self.default_location,
                ):
                    if _should_stop(stop_requested):
                        return
                    remaining = max_results - yielded if max_results is not None else None
                    if remaining is not None and remaining <= 0:
                        return
                    params: dict[str, Any] = {
                        "engine": "google_jobs",
                        "q": search_query,
                        "location": location or self.default_location,
                        "google_domain": domain,
                        "hl": language,
                        "api_key": self.api_key,
                    }
                    if country:
                        params["gl"] = country
                    if ltype:
                        params["ltype"] = ltype
                    if search_chips:
                        params["chips"] = search_chips
                    if search_uds:
                        params["uds"] = search_uds

                    for job in self._search_pages_with_fallback(
                        params,
                        max_results=remaining,
                        stop_requested=stop_requested,
                    ):
                        if _should_stop(stop_requested):
                            return
                        if job.external_id in seen_external_ids:
                            continue
                        seen_external_ids.add(job.external_id)
                        yielded += 1
                        yield job
                        if max_results is not None and yielded >= max_results:
                            return

    def _search_pages_with_fallback(
        self,
        params: dict[str, Any],
        *,
        max_results: int | None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> Iterator[RawJob]:
        seen_external_ids: set[str] = set()
        yielded = 0

        if _should_stop(stop_requested):
            return

        for job in self._search_pages(
            params,
            max_results=max_results,
            stop_requested=stop_requested,
        ):
            if _should_stop(stop_requested):
                return
            if job.external_id in seen_external_ids:
                continue
            seen_external_ids.add(job.external_id)
            yielded += 1
            yield _with_search_audit(job, params=params, result_origin="strict")

        if yielded == 0:
            for fallback_params in zero_result_fallback_params(params):
                if _should_stop(stop_requested):
                    return
                logger.info(
                    "SerpApi zero-result filter widening: q=%r hl=%s gl=%s chips=%r -> %r",
                    params.get("q"),
                    params.get("hl"),
                    params.get("gl"),
                    params.get("chips"),
                    fallback_params.get("chips"),
                )
                fallback_yielded = False
                for job in self._search_pages(
                    fallback_params,
                    max_results=max_results,
                    stop_requested=stop_requested,
                ):
                    if _should_stop(stop_requested):
                        return
                    if job.external_id in seen_external_ids:
                        continue
                    seen_external_ids.add(job.external_id)
                    fallback_yielded = True
                    yield _with_search_audit(
                        job,
                        params=params,
                        result_origin="fallback",
                        fallback_reason="zero_result_strict_filters",
                        fallback_params=fallback_params,
                    )
                if fallback_yielded:
                    return

            location = (params.get("location") or "").strip()
            query = (params.get("q") or "").strip()
            if not location or not query:
                return
            fallback_params = dict(params)
            fallback_params["q"] = f"{query} jobs in {location}"
            fallback_params.pop("location", None)
            logger.info(
                "SerpApi fallback search after empty result: q=%r hl=%s gl=%s chips=%r",
                fallback_params.get("q"),
                fallback_params.get("hl"),
                fallback_params.get("gl"),
                fallback_params.get("chips"),
            )
            for job in self._search_pages(
                fallback_params,
                max_results=max_results,
                stop_requested=stop_requested,
            ):
                if _should_stop(stop_requested):
                    return
                if job.external_id in seen_external_ids:
                    continue
                seen_external_ids.add(job.external_id)
                yield _with_search_audit(
                    job,
                    params=params,
                    result_origin="fallback",
                    fallback_reason="zero_result_contextual_location",
                    fallback_params=fallback_params,
                )
            return

        if not _should_widen_low_result(
            params,
            yielded,
            max_results,
            self.low_result_fallback_target,
        ):
            return

        target = _low_result_target(max_results, self.low_result_fallback_target)
        for fallback_params in zero_result_fallback_params(params):
            if _should_stop(stop_requested):
                return
            if yielded >= target:
                return
            remaining = target - yielded
            logger.info(
                "SerpApi low-result filter widening: q=%r hl=%s gl=%s count=%d chips=%r -> %r",
                params.get("q"),
                params.get("hl"),
                params.get("gl"),
                yielded,
                params.get("chips"),
                fallback_params.get("chips"),
            )
            for job in self._search_pages(
                fallback_params,
                max_results=remaining,
                stop_requested=stop_requested,
            ):
                if _should_stop(stop_requested):
                    return
                if job.external_id in seen_external_ids:
                    continue
                seen_external_ids.add(job.external_id)
                yielded += 1
                yield _with_search_audit(
                    job,
                    params=params,
                    result_origin="fallback",
                    fallback_reason="low_result_strict_filters",
                    fallback_params=fallback_params,
                )
                if yielded >= target:
                    return

    def _search_pages(
        self,
        params: dict[str, Any],
        *,
        max_results: int | None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> Iterator[RawJob]:
        pages_limit = self.max_pages
        pages_fetched = 0
        results_yielded = 0
        next_token: str | None = None

        while pages_fetched < pages_limit:
            if _should_stop(stop_requested):
                return
            page_params = dict(params)
            if next_token:
                page_params["next_page_token"] = next_token
            request_started = time.monotonic()
            logger.info(
                "SerpApi request start: q=%r page=%s timeout=%ss",
                params.get("q"),
                pages_fetched + 1,
                30,
            )
            try:
                payload = self._fetch(page_params)
            except requests.RequestException as e:
                summary = _request_error_summary(e)
                logger.error(
                    "SerpApi request failed: q=%r page=%s elapsed=%.1fs error=%s",
                    params.get("q"),
                    pages_fetched + 1,
                    time.monotonic() - request_started,
                    summary,
                    exc_info=True,
                )
                raise ScraperError(f"SerpAPI request failed: {summary}") from e
            except Exception as exc:
                logger.error(
                    "SerpApi response invalid: q=%r page=%s elapsed=%.1fs error=%s",
                    params.get("q"),
                    pages_fetched + 1,
                    time.monotonic() - request_started,
                    exc,
                    exc_info=True,
                )
                raise ScraperError(f"SerpAPI response invalid: {exc}") from exc

            if not isinstance(payload, dict):
                exc = ScraperError("SerpAPI response invalid: expected a JSON object")
                logger.error(
                    "SerpApi response invalid: q=%r page=%s elapsed=%.1fs error=%s",
                    params.get("q"),
                    pages_fetched + 1,
                    time.monotonic() - request_started,
                    exc,
                    exc_info=True,
                )
                raise exc
            api_error = payload.get("error")
            if api_error:
                detail = (
                    str(api_error.get("message") or api_error.get("type") or api_error)
                    if isinstance(api_error, dict)
                    else str(api_error)
                )
                exc = ScraperError(f"SerpAPI returned an API error: {detail[:500]}")
                logger.error(
                    "SerpApi API error: q=%r page=%s elapsed=%.1fs error=%s",
                    params.get("q"),
                    pages_fetched + 1,
                    time.monotonic() - request_started,
                    exc,
                )
                raise exc

            pages_fetched += 1
            jobs = payload.get("jobs_results") or []
            logger.info(
                "SerpApi request done: q=%r page=%s jobs=%d elapsed=%.1fs",
                params.get("q"),
                pages_fetched,
                len(jobs) if isinstance(jobs, list) else 0,
                time.monotonic() - request_started,
            )
            pagination = payload.get("serpapi_pagination") or {}
            next_token = pagination.get("next_page_token")
            if not jobs:
                logger.info(
                    "SerpApi page %d returned no jobs for hl=%s gl=%s q=%r chips=%r%s",
                    pages_fetched,
                    params.get("hl"),
                    params.get("gl"),
                    params.get("q"),
                    params.get("chips"),
                    " but next_page_token is present" if next_token else "",
                )
                if next_token:
                    continue
                break

            for raw in jobs:
                if _should_stop(stop_requested):
                    return
                job = self._to_raw_job(raw)
                if job is None:
                    continue
                yield job
                results_yielded += 1
                if max_results is not None and results_yielded >= max_results:
                    return

            if not next_token:
                break

    # -------------------- mapping --------------------

    def _to_raw_job(self, raw: dict[str, Any]) -> RawJob | None:
        title = raw.get("title")
        company = raw.get("company_name")
        if not title or not company:
            return None

        apply_options = raw.get("apply_options") or []
        app_url: str | None = None
        if apply_options:
            app_url = apply_options[0].get("link")
        if not app_url:
            app_url = raw.get("share_link")

        # Google Jobs exposes a stable ``job_id``.  Do not mix mutable labels
        # into the identity when it is available: a title/company refresh
        # must update the same persisted offer.
        if raw.get("job_id"):
            external_id = make_external_id(self.name, str(raw["job_id"]))
        elif app_url:
            external_id = make_external_id(self.name, app_url)
        else:
            external_id = make_external_id(
                self.name,
                title,
                company,
                raw.get("location") or "",
            )

        extensions = raw.get("detected_extensions") or {}
        contract_type = self._normalize_contract(extensions.get("schedule_type"))
        remote_policy = self._normalize_remote(extensions, raw.get("location"))

        description = raw.get("description") or ""
        highlights = raw.get("job_highlights") or []
        if highlights:
            extra_sections: list[str] = []
            for section in highlights:
                title_h = section.get("title") or ""
                items = section.get("items") or []
                if items:
                    extra_sections.append(f"\n\n{title_h}\n- " + "\n- ".join(items))
            description = description + "".join(extra_sections)

        return RawJob(
            external_id=external_id,
            title=title.strip(),
            company=company.strip(),
            location=raw.get("location"),
            contract_type=contract_type,
            remote_policy=remote_policy,
            description=description,
            application_url=app_url,
            apply_options=apply_options or None,
            source=self.name,
            source_data=raw,
        )

    @staticmethod
    def _normalize_contract(value: str | None) -> str | None:
        return normalize_source_contract_type(value)

    @staticmethod
    def _normalize_remote(extensions: dict[str, Any], location: str | None) -> str | None:
        if extensions.get("work_from_home"):
            return "remote"
        loc = (location or "").lower()
        if "anywhere" in loc or "remote" in loc:
            return "remote"
        if "hybrid" in loc:
            return "hybrid"
        return None


__all__ = [
    "SERPAPI_DATE_POSTED_LABELS",
    "SERPAPI_DATE_POSTED_OPTIONS",
    "SerpApiGoogleJobsScraper",
    "combine_chips",
    "date_posted_chip",
    "market_languages",
    "normalize_date_posted",
    "split_localization_values",
    "zero_result_fallback_params",
]
