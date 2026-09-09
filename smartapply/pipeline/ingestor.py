"""Phase 1 — scrape and persist raw jobs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from smartapply.database import session_scope
from smartapply.database.models import JobDuplicateStatus, JobStatus
from smartapply.database.repository import (
    get_job_by_external_id,
    get_known_external_ids,
    list_known_jobs,
    upsert_job,
)
from smartapply.dedup import Deduplicator, DuplicateCandidate
from smartapply.logging_setup import get_logger
from smartapply.offers import ManualOfferInput, RawJob
from smartapply.parsing import clean_description
from smartapply.pipeline.ingest import (
    IngestCollection,
    IngestReport,
    _build_search_audit,
    _KnownJobIndex,
    _normalize_application_url,
    build_source_query_plan,
    collect_round_robin,
    expand_query_for_source,
    split_or_query,
)
from smartapply.scrapers import ManualScraper, get_scraper

logger = get_logger(__name__)


class Ingestor:
    """Turn external job postings into persisted rows."""

    def from_source(
        self,
        source: str,
        query: str,
        location: str | None = None,
        *,
        max_results: int | None = 20,
        split_or: bool = True,
        stop_requested: Callable[[], bool] | None = None,
        **search_kwargs,
    ) -> IngestReport:
        collection = self.collect_source(
            source,
            query,
            location,
            max_results=max_results,
            split_or=split_or,
            stop_requested=stop_requested,
            **search_kwargs,
        )
        return self.persist_collection(collection)

    def collect_source(
        self,
        source: str,
        query: str,
        location: str | None = None,
        *,
        max_results: int | None = 20,
        split_or: bool = True,
        stop_requested: Callable[[], bool] | None = None,
        **search_kwargs,
    ) -> IngestCollection:
        """Collect one source without writing to the database."""
        scraper = get_scraper(source)
        if not scraper.is_available():
            raise RuntimeError(
                f"Source {source!r} is not configured. "
                "Check your .env (SERPAPI_API_KEY, FRANCETRAVAIL_* or APIFY_TOKEN)."
            )
        query_plan = build_source_query_plan(source, query, split_or=split_or)
        queries = list(query_plan.all_queries)
        logger.info(
            "Ingesting from %s: q=%r primary_queries=%d fallback_queries=%d location=%r",
            source,
            query,
            len(query_plan.primary),
            len(query_plan.fallbacks),
            location,
        )
        with session_scope() as s:
            known_external_ids = get_known_external_ids(s, source)
            known_index = _KnownJobIndex.from_jobs(list_known_jobs(s))
        collect_result = collect_round_robin(
            scraper=scraper,
            source=source,
            queries=queries,
            location=location,
            max_results=max_results,
            search_kwargs=search_kwargs,
            known_external_ids=known_external_ids,
            known_index=known_index,
            stop_requested=stop_requested,
            primary_query_count=len(query_plan.primary),
        )
        source_warnings = [
            str(warning)
            for warning in (getattr(scraper, "last_warnings", None) or [])
            if str(warning).strip()
        ]
        return IngestCollection(
            source=source,
            raw_jobs=collect_result.raw_jobs,
            search_audit=_build_search_audit(collect_result.raw_jobs),
            skipped_known_during_collect=collect_result.skipped_known,
            skipped_existing_during_collect=collect_result.skipped_existing,
            hit_raw_seen_cap=collect_result.hit_raw_seen_cap,
            cancelled=collect_result.cancelled,
            warnings=source_warnings,
        )

    def persist_collection(self, collection: IngestCollection) -> IngestReport:
        """Persist one completed collection; callers may serialize this step."""
        return self._persist(
            collection.source,
            collection.raw_jobs,
            search_audit=collection.search_audit,
            collect_skipped_known=collection.skipped_known_during_collect,
            collect_skipped_existing=collection.skipped_existing_during_collect,
            hit_raw_seen_cap=collection.hit_raw_seen_cap,
            cancelled=collection.cancelled,
            warnings=collection.warnings,
        )

    def from_url(self, url: str) -> IngestReport:
        return self._persist("manual", [ManualScraper().from_url(url)])

    def from_manual_offer(self, offer: ManualOfferInput) -> IngestReport:
        return self._persist("manual", [ManualScraper().from_structured(offer)])

    def from_text(
        self,
        text: str,
        *,
        title: str,
        company: str,
        location: str | None = None,
        application_url: str | None = None,
    ) -> IngestReport:
        raw = ManualScraper().from_text(
            text,
            title=title,
            company=company,
            location=location,
            application_url=application_url,
        )
        return self._persist("manual", [raw])

    def _persist(
        self,
        source: str,
        raws: list[RawJob],
        *,
        search_audit: list[dict[str, Any]] | None = None,
        collect_skipped_known: int = 0,
        collect_skipped_existing: int = 0,
        hit_raw_seen_cap: bool = False,
        cancelled: bool = False,
        warnings: list[str] | None = None,
    ) -> IngestReport:
        job_ids: list[int] = []
        inserted = 0
        updated_pending = 0
        skipped_processed = 0
        skipped_existing = 0
        duplicate_review_ids: list[int] = []
        aliases_created = 0
        with session_scope() as s:
            known_jobs = list(list_known_jobs(s))
            dedup_reference_jobs: list[Any] = list(known_jobs)
            for raw in raws:
                existing = get_job_by_external_id(s, raw.external_id)
                existing_status = existing.status if existing is not None else None
                if existing is not None and existing.canonical_job_id is not None:
                    # An already confirmed alias is source history, not a
                    # second candidate. Refresh its source payload in place
                    # and leave the canonical job/application untouched.
                    upsert_job(
                        s,
                        external_id=existing.external_id,
                        title=raw.title,
                        company=raw.company,
                        location=raw.location,
                        contract_type=raw.contract_type,
                        remote_policy=raw.remote_policy,
                        description=raw.description,
                        cleaned_description=clean_description(raw.description),
                        application_url=raw.application_url,
                        apply_options=raw.apply_options,
                        source=raw.source,
                        source_data=raw.source_data,
                        published_date=raw.published_date,
                    )
                    skipped_existing += 1
                    continue
                if existing is None:
                    matching_existing = _matching_known_job(raw, known_jobs)
                    if matching_existing is not None and source == "manual":
                        existing = matching_existing
                        existing_status = existing.status
                    elif source != "manual":
                        exact_url_job = _matching_exact_offer_url(raw, known_jobs)
                        if exact_url_job is not None:
                            root = _canonical_job(exact_url_job, known_jobs)
                            alias = upsert_job(
                                s,
                                external_id=raw.external_id,
                                title=raw.title,
                                company=raw.company,
                                location=raw.location,
                                contract_type=raw.contract_type,
                                remote_policy=raw.remote_policy,
                                description=raw.description,
                                cleaned_description=clean_description(raw.description),
                                application_url=raw.application_url,
                                apply_options=raw.apply_options,
                                source=raw.source,
                                source_data=raw.source_data,
                                published_date=raw.published_date,
                            )
                            alias.canonical_job_id = root.id
                            alias.possible_duplicate_of_id = None
                            alias.duplicate_review_status = JobDuplicateStatus.CONFIRMED
                            alias.duplicate_match_type = "exact_url"
                            alias.duplicate_confidence = 1.0
                            alias.archived_at = alias.archived_at or datetime.now(timezone.utc)
                            alias.status = JobStatus.ARCHIVED
                            aliases_created += 1
                            skipped_existing += 1
                            known_jobs.append(alias)
                            dedup_reference_jobs.append(alias)
                            continue
                # Cross-source fuzzy duplicates must be rejected before the
                # upsert. A probable match is persisted for human review;
                # it must not silently become a second candidate.
                probable = (
                    self._find_probable_duplicate(raw, dedup_reference_jobs)
                    if existing is None
                    else None
                )
                job = upsert_job(
                    s,
                    external_id=(
                        existing.external_id if existing and source == "manual" else raw.external_id
                    ),
                    title=raw.title,
                    company=raw.company,
                    location=raw.location,
                    contract_type=raw.contract_type,
                    remote_policy=raw.remote_policy,
                    description=raw.description,
                    cleaned_description=clean_description(raw.description),
                    application_url=raw.application_url,
                    apply_options=raw.apply_options,
                    source=raw.source,
                    source_data=raw.source_data,
                    published_date=raw.published_date,
                )
                if probable is not None:
                    candidate = _canonical_job(probable.job, known_jobs)
                    job.possible_duplicate_of_id = candidate.id
                    job.canonical_job_id = None
                    job.duplicate_review_status = JobDuplicateStatus.PENDING
                    job.duplicate_match_type = probable.match_type
                    job.duplicate_confidence = probable.confidence
                    job.filtered_at = None
                    job.ranked_at = None
                    job.analyzed_at = None
                    job.shortlisted_at = None
                    job.shortlist_origin = None
                    job.archived_at = None
                    job.status = JobStatus.SCRAPED
                if job.duplicate_review_status == JobDuplicateStatus.PENDING:
                    # Retries reuse the row without recomputing the match.
                    # Keep reporting its unresolved review so callers stop
                    # before analysis, which excludes pending duplicates.
                    duplicate_review_ids.append(int(job.id))
                if source == "manual" and existing_status is not None:
                    # Manual one-shot submissions are explicit user intent:
                    # reuse the existing row, but analyze the freshly pasted
                    # content again before regenerating documents.
                    job.archived_at = None
                    job.analyzed_at = None
                    job.shortlisted_at = None
                    job.status = JobStatus.SCRAPED
                if existing_status is None:
                    inserted += 1
                    job_ids.append(job.id)
                elif existing_status == JobStatus.SCRAPED:
                    updated_pending += 1
                    job_ids.append(job.id)
                elif source == "manual":
                    job_ids.append(job.id)
                if existing_status is None:
                    dedup_reference_jobs.append(job)
        return IngestReport(
            source=source,
            fetched=len(raws),
            persisted=len(job_ids),
            job_ids=job_ids,
            inserted=inserted,
            updated_pending=updated_pending,
            skipped_processed=skipped_processed,
            skipped_existing_during_collect=collect_skipped_existing,
            skipped_existing_during_persist=skipped_existing,
            skipped_known_during_collect=collect_skipped_known,
            hit_raw_seen_cap=hit_raw_seen_cap,
            cancelled=cancelled,
            duplicate_review_ids=duplicate_review_ids,
            aliases_created=aliases_created,
            search_audit=search_audit or [],
            warnings=warnings or [],
        )

    @staticmethod
    def _find_probable_duplicate(
        raw: RawJob,
        known_jobs: list[Any],
    ) -> DuplicateCandidate | None:
        """Return a review candidate without merging or archiving it."""
        return Deduplicator().find_probable_duplicate(raw, known_jobs)


__all__ = [
    "IngestReport",
    "Ingestor",
    "_normalize_application_url",
    "expand_query_for_source",
    "split_or_query",
]


def _matching_known_job(raw: RawJob, known_jobs) -> object | None:  # noqa: ANN001
    """Find the same manual offer without conflating shared result URLs.

    A career-site search URL can be pasted for several different offers. URL
    matching therefore also requires the offer's company and title; otherwise
    a new offer can overwrite the old job row and keep its generated letter.
    """
    if raw.external_id:
        for job in known_jobs:
            if raw.external_id == job.external_id:
                return job

    raw_url = _normalize_application_url(raw.application_url)
    if not raw_url:
        return None
    for job in known_jobs:
        if (
            raw_url == _normalize_application_url(job.application_url)
            and _same_offer_label(raw.company, job.company)
            and _same_offer_label(raw.title, job.title)
        ):
            return job
    return None


def _same_offer_label(left: str | None, right: str | None) -> bool:
    return " ".join((left or "").casefold().split()) == " ".join((right or "").casefold().split())


def _matching_exact_offer_url(raw: RawJob, known_jobs: list[Any]) -> object | None:
    """Match only a direct offer URL, never a shared search/landing page."""
    raw_url = _normalize_application_url(raw.application_url)
    if not raw_url or not _is_direct_offer_url(raw.application_url):
        return None
    matches = [
        job
        for job in known_jobs
        if raw_url == _normalize_application_url(getattr(job, "application_url", None))
    ]
    return min(matches, key=_job_identity_priority) if matches else None


def _is_direct_offer_url(url: str | None) -> bool:
    value = (url or "").strip().lower()
    if not value:
        return False
    from urllib.parse import urlsplit

    path = urlsplit(value if "://" in value else f"https://{value}").path.rstrip("/")
    if "/offres/recherche/detail/" in path:
        return True
    return any(marker in path for marker in ("/jobs/", "/job/", "/vacancies/", "/vacancy/", "/offers/"))


def _canonical_job(job: Any, known_jobs: list[Any]) -> Any:
    """Follow confirmed aliases while keeping malformed legacy chains safe."""
    by_id = {int(item.id): item for item in known_jobs if getattr(item, "id", None) is not None}
    current = job
    seen: set[int] = set()
    while getattr(current, "canonical_job_id", None) is not None:
        current_id = int(current.id)
        if current_id in seen:
            break
        seen.add(current_id)
        next_job = by_id.get(int(current.canonical_job_id))
        if next_job is None:
            break
        current = next_job
    return current


def _job_identity_priority(job: Any) -> tuple[int, int, float, int]:
    """Prefer the row carrying the existing application/workflow history."""
    scraped_at = getattr(job, "scraped_at", None)
    if scraped_at is None:
        timestamp = float("inf")
    else:
        if getattr(scraped_at, "tzinfo", None) is None:
            scraped_at = scraped_at.replace(tzinfo=timezone.utc)
        timestamp = scraped_at.timestamp()
    return (
        0 if getattr(job, "application", None) is not None else 1,
        0 if getattr(job, "analyzed_at", None) is not None else 1,
        timestamp,
        int(getattr(job, "id", 0)),
    )
