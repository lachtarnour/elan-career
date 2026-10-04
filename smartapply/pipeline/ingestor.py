"""Phase 1 — scrape and persist raw jobs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from smartapply.concurrency import job_operation
from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Job, JobDuplicateStatus, JobStatus
from smartapply.database.repository import (
    get_known_external_ids,
    list_known_jobs,
    upsert_job,
)
from smartapply.dedup import (
    Deduplicator,
    DuplicateCandidate,
    offers_within_duplicate_group_time_window,
)
from smartapply.dedup.deduplicator import linked_duplicate_components
from smartapply.logging_setup import get_logger
from smartapply.offer_identity import is_direct_offer_url
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
from smartapply.pipeline.ingest.dedupe import _native_offer_key
from smartapply.pipeline.snapshots import application_snapshot_key, job_snapshot_key
from smartapply.scrapers import ManualScraper, get_scraper
from smartapply.scrapers.apify_recovery import (
    ApifyRecoveryJournal,
    ApifyRecoveryJournalError,
)

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
        include_research_cdd: bool = False,
        stop_requested: Callable[[], bool] | None = None,
        **search_kwargs,
    ) -> IngestReport:
        collection = self.collect_source(
            source,
            query,
            location,
            max_results=max_results,
            split_or=split_or,
            include_research_cdd=include_research_cdd,
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
        include_research_cdd: bool = False,
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
        query_plan = build_source_query_plan(
            source,
            query,
            split_or=split_or,
            include_research_cdd=include_research_cdd,
        )
        queries = list(query_plan.all_queries)
        if include_research_cdd and source.strip().lower() == "linkedin":
            # The LinkedIn actor defaults to Full-time via LINKEDIN_CONTRACT_TYPE.
            # Include its Contract/Temporary selectors so CDD-like doctoral
            # offers reach the local filter; ordinary incompatible contracts are
            # still rejected there.
            search_kwargs.setdefault("contract_type", ["F", "T", "C"])
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
            warnings=[*collect_result.warnings, *source_warnings],
            completed_source_run_ids=collect_result.completed_source_run_ids,
            source_run_journal_path=(
                str(scraper.recovery_journal.path)
                if source.strip().lower() == "linkedin"
                and getattr(scraper, "recovery_journal", None) is not None
                else None
            ),
        )

    def persist_collection(self, collection: IngestCollection) -> IngestReport:
        """Persist one completed collection; callers may serialize this step."""
        report = self._persist(
            collection.source,
            collection.raw_jobs,
            search_audit=collection.search_audit,
            collect_skipped_known=collection.skipped_known_during_collect,
            collect_skipped_existing=collection.skipped_existing_during_collect,
            hit_raw_seen_cap=collection.hit_raw_seen_cap,
            cancelled=collection.cancelled,
            warnings=collection.warnings,
            completed_source_run_ids=collection.completed_source_run_ids,
        )
        if collection.completed_source_run_ids and collection.source_run_journal_path:
            try:
                ApifyRecoveryJournal(Path(collection.source_run_journal_path)).remove_many(
                    collection.completed_source_run_ids
                )
            except ApifyRecoveryJournalError as exc:
                report.warnings.append(
                    "Les offres LinkedIn ont été enregistrées, mais le journal de "
                    "récupération Apify n’a pas pu être finalisé. Le prochain passage "
                    f"pourra relire les mêmes datasets : {exc}"
                )
        return report

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
        completed_source_run_ids: list[str] | None = None,
    ) -> IngestReport:
        job_ids: list[int] = []
        inserted = 0
        updated_pending = 0
        skipped_processed = 0
        skipped_existing = 0
        duplicate_review_ids: list[int] = []
        aliases_created = 0
        with session_scope() as session:
            known_jobs = list(session.scalars(
                select(Job).options(selectinload(Job.application))
            ))
            session.expunge_all()
        known_max = max((int(job.id) for job in known_jobs), default=0)
        for raw in raws:
            # Matching may be expensive. It always runs on detached snapshots,
            # never while an SQLite transaction or the writer lock is held.
            while True:
                known_by_external = {job.external_id: job for job in known_jobs}
                existing = known_by_external.get(raw.external_id)
                native_key = _native_offer_key(raw.source, raw.source_data)
                if existing is None and native_key is not None and native_key in (
                    _KnownJobIndex.from_jobs(known_jobs).native_offer_ids
                ):
                    skipped_existing += 1
                    break
                probable = None
                alias_target = None
                if existing is None:
                    manual_match = _matching_known_job(raw, known_jobs) if source == "manual" else None
                    if manual_match is not None:
                        existing = manual_match
                    else:
                        exact = _matching_exact_offer_url(raw, known_jobs)
                        if exact is not None:
                            if source == "manual":
                                probable = DuplicateCandidate(
                                    job=exact, title_score=100.0, description_score=100.0,
                                    confidence=1.0, match_type="exact_url",
                                )
                            else:
                                alias_target = _canonical_job(exact, known_jobs)
                if existing is None and probable is None and alias_target is None:
                    probable = self._find_probable_duplicate(raw, known_jobs)
                candidate = _canonical_job(probable.job, known_jobs) if probable else None
                references = [job for job in (existing, alias_target, candidate) if job is not None]
                components = linked_duplicate_components(known_jobs)
                dependencies = {
                    int(member.id) for job in references
                    for member in components.get(id(job), (job,))
                }
                dependencies.update(int(job.id) for job in references)
                snapshots = {int(job.id): _ingest_snapshot_key(job) for job in known_jobs
                             if int(job.id) in dependencies}
                refreshed = []
                refresh_scope = set()
                retry = False
                with (job_operation(dependencies, operation="enregistrement d’offre"),
                      write_session_scope() as session):
                    current_max = int(session.scalar(select(func.max(Job.id))) or 0)
                    if current_max > known_max:
                        # A manual addition during fetch can be the same offer.
                        # Refresh it and recompute identity outside this scope.
                        refreshed = list(session.scalars(
                            select(Job).where(Job.id > known_max)
                            .options(selectinload(Job.application))
                        ))
                        session.expunge_all()
                        retry = True
                    else:
                        current_refs = list(session.scalars(
                            select(Job).where(Job.id.in_(dependencies))
                            .options(selectinload(Job.application))
                        ))
                        current_by_id = {int(job.id): job for job in current_refs}
                        if (set(current_by_id) != dependencies or any(
                            _ingest_snapshot_key(job) != snapshots[int(job.id)]
                            for job in current_refs
                        )):
                            refreshed = current_refs
                            refresh_scope = dependencies
                            session.expunge_all()
                            retry = True
                        else:
                            current = current_by_id.get(int(existing.id)) if existing else None
                            # Tracking history and published dossiers are never
                            # reopened by adding the same offer manually.
                            if current is not None and current.canonical_job_id is None and (
                                _ingest_history_is_protected(current)
                                or (source != "manual" and (
                                    current.status != JobStatus.SCRAPED
                                    or current.filtered_at is not None
                                    or current.ranked_at is not None
                                    or current.analyzed_at is not None
                                ))
                            ):
                                skipped_processed += 1
                                break
                            old_status = current.status if current is not None else None
                            job = upsert_job(
                                session,
                                external_id=(current.external_id if current is not None
                                             and source == "manual" else raw.external_id),
                                title=raw.title, company=raw.company, location=raw.location,
                                contract_type=raw.contract_type, remote_policy=raw.remote_policy,
                                description=raw.description,
                                cleaned_description=clean_description(raw.description),
                                application_url=raw.application_url, apply_options=raw.apply_options,
                                source=raw.source, source_data=raw.source_data,
                                published_date=raw.published_date,
                            )
                            if current is not None and current.canonical_job_id is not None:
                                skipped_existing += 1
                            elif alias_target is not None:
                                job.canonical_job_id = int(alias_target.id)
                                job.possible_duplicate_of_id = None
                                job.duplicate_review_status = JobDuplicateStatus.CONFIRMED
                                job.duplicate_match_type = "exact_url"
                                job.duplicate_confidence = 1.0
                                job.archived_at = job.archived_at or datetime.now(timezone.utc)
                                job.status = JobStatus.ARCHIVED
                                aliases_created += 1
                                skipped_existing += 1
                            else:
                                if candidate is not None:
                                    job.possible_duplicate_of_id = int(candidate.id)
                                    job.canonical_job_id = None
                                    job.duplicate_review_status = JobDuplicateStatus.PENDING
                                    job.duplicate_match_type = probable.match_type
                                    job.duplicate_confidence = probable.confidence
                                    job.filtered_at = job.ranked_at = job.analyzed_at = None
                                    job.shortlisted_at = job.shortlist_origin = None
                                    job.archived_at = None
                                    job.status = JobStatus.SCRAPED
                                if source == "manual" and old_status is not None:
                                    job.filtered_at = job.ranked_at = job.analyzed_at = None
                                    job.shortlisted_at = job.shortlist_origin = None
                                    job.archived_at = None
                                    job.status = JobStatus.SCRAPED
                                if job.duplicate_review_status == JobDuplicateStatus.PENDING:
                                    duplicate_review_ids.append(int(job.id))
                                if old_status is None:
                                    inserted += 1
                                    job_ids.append(int(job.id))
                                elif old_status == JobStatus.SCRAPED:
                                    updated_pending += 1
                                    job_ids.append(int(job.id))
                                elif source == "manual":
                                    job_ids.append(int(job.id))
                            session.flush()
                            _ = job.application
                            session.expunge_all()
                            refreshed = [job]
                refreshed_ids = {int(job.id) for job in refreshed} | refresh_scope
                known_jobs = [job for job in known_jobs if int(job.id) not in refreshed_ids]
                known_jobs.extend(refreshed)
                known_max = max((int(job.id) for job in known_jobs), default=0)
                if not retry:
                    break

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
            completed_source_run_ids=completed_source_run_ids or [],
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


def _ingest_snapshot_key(job: Job) -> tuple[str, str | None]:
    return job_snapshot_key(job), (
        application_snapshot_key(job.application) if job.application is not None else None
    )


def _ingest_history_is_protected(job: Job) -> bool:
    application = job.application
    return bool(
        job.archived_at is not None
        or job.status in {JobStatus.ARCHIVED, JobStatus.SENT, JobStatus.INTERVIEW, JobStatus.REJECTED}
        or (application is not None and (
            application.form_submitted_at is not None
            or application.status in {JobStatus.SENT, JobStatus.INTERVIEW,
                                      JobStatus.REJECTED, JobStatus.ARCHIVED}
            or application.status != JobStatus.ANALYZED
            or application.cv_json or application.cv_docx_path or application.cv_pdf_path
        ))
    )


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
    components = linked_duplicate_components(known_jobs)
    matches = [
        job
        for job in known_jobs
        if (
            raw_url == _normalize_application_url(job.application_url)
            and _same_offer_label(raw.company, job.company)
            and _same_offer_label(raw.title, job.title)
            and offers_within_duplicate_group_time_window(
                raw,
                job,
                known_jobs,
                components=components,
            )
        )
    ]
    return min(matches, key=_job_identity_priority) if matches else None


def _same_offer_label(left: str | None, right: str | None) -> bool:
    return " ".join((left or "").casefold().split()) == " ".join((right or "").casefold().split())


def _matching_exact_offer_url(raw: RawJob, known_jobs: list[Any]) -> object | None:
    """Match only a direct offer URL, never a shared search/landing page."""
    raw_url = _normalize_application_url(raw.application_url)
    if not raw_url or not _is_direct_offer_url(raw.application_url):
        return None
    components = linked_duplicate_components(known_jobs)
    matches = [
        job
        for job in known_jobs
        if raw_url == _normalize_application_url(getattr(job, "application_url", None))
        and offers_within_duplicate_group_time_window(
            raw,
            job,
            known_jobs,
            components=components,
        )
    ]
    return min(matches, key=_job_identity_priority) if matches else None


def _is_direct_offer_url(url: str | None) -> bool:
    """Compatibility wrapper around the shared direct-offer classifier."""
    return is_direct_offer_url(url)


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
