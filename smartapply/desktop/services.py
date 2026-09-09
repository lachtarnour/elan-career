"""UI-agnostic application services for the desktop client."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import joinedload, load_only

from smartapply.config import ENV_FILE, get_settings
from smartapply.database import init_db, session_scope
from smartapply.database.models import (
    Application,
    AppSetting,
    GeneratedDocument,
    Job,
    JobAnalysis,
    JobDuplicateStatus,
    JobScore,
    JobStatus,
    ShortlistOrigin,
)
from smartapply.database.repository import (
    application_for_duplicate_group,
    application_ids_for_confirmed_groups,
    canonical_job,
    confirm_duplicate,
    duplicate_group_ids,
    mark_archived,
    pending_duplicate_for_group,
    reject_duplicate,
    rescue_archived_job,
    set_score,
    set_shortlisted,
    update_application_tracking,
)
from smartapply.database.repository import companies as company_repository
from smartapply.desktop.source_health import (
    SourceHealth,
    check_source_health,
    pending_source_health,
)
from smartapply.jobsearch.archive_reasons import archive_reason_labels
from smartapply.jobsearch.status import OFFER_FILTER_STATUSES, STATUS_FLOW, status_label
from smartapply.jobsearch.workflow import APPLICATION_STATUSES, next_action_for
from smartapply.logging_setup import get_logger
from smartapply.pipeline.process.ranking import RankingMixin
from smartapply.profile import get_profile
from smartapply.utils.experience import required_min_years

logger = get_logger(__name__)


def _date_text(value: datetime | None, *, with_time: bool = False) -> str:
    if value is None:
        return "—"
    return value.strftime("%d/%m/%Y · %H:%M" if with_time else "%d/%m/%Y")


def _text_list(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value if str(item).strip())


def _desktop_next_action(status: str, updated_at: datetime | None) -> str:
    if status == JobStatus.READY_FOR_FORM_SUBMISSION:
        return "Vérifier les documents de candidature"
    return next_action_for(status, updated_at)


@dataclass(frozen=True)
class DashboardApplication:
    id: int
    company: str
    title: str
    status: str
    status_label: str
    next_action: str
    updated_at: str


@dataclass(frozen=True)
class DashboardDailyPoint:
    day: str
    label: str
    count: int


@dataclass(frozen=True)
class DashboardSnapshot:
    jobs: int
    pending: int
    ready: int
    review: int
    sent: int
    analyzed: int
    llm_cost_usd: float
    recent: tuple[DashboardApplication, ...] = ()
    sent_by_day: tuple[DashboardDailyPoint, ...] = ()


@dataclass(frozen=True)
class DuplicateReviewDetail:
    id: int
    company: str
    title: str
    location: str
    source: str
    url: str
    application_id: int | None = None
    application_status: str = ""
    application_status_label: str = ""
    application_updated_at: str = ""
    contract: str = ""
    remote: str = ""
    description: str = ""


@dataclass(frozen=True)
class JobRow:
    id: int
    company: str
    title: str
    location: str
    source: str
    status: str
    status_label: str
    filter_disposition: str
    score: float | None
    llm_score: float | None
    contract: str
    experience: str
    shortlisted: bool
    application_id: int | None
    analyzed: bool
    can_generate: bool
    can_send: bool
    scraped_at: str
    related_application_id: int | None = None
    duplicate_review_status: str = JobDuplicateStatus.NONE
    possible_duplicate_of_id: int | None = None
    duplicate_confidence: float | None = None
    duplicate_candidate: DuplicateReviewDetail | None = None


@dataclass(frozen=True)
class JobDetail(JobRow):
    remote: str = ""
    url: str = ""
    description: str = ""
    role_type: str = ""
    domain: str = ""
    seniority: str = ""
    match_reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    archive_reasons: tuple[str, ...] = ()
    application: ApplicationDetail | None = None


@dataclass(frozen=True)
class ShortlistSummary:
    total: int
    ready_to_generate: int


@dataclass(frozen=True)
class ApplicationDetail:
    id: int
    job_id: int
    company: str
    title: str
    status: str
    status_label: str
    next_action: str
    updated_at: str
    location: str = ""
    source: str = ""
    job_url: str = ""
    form_url: str = ""
    notes: str = ""
    form_submitted_at: str = ""
    cv_docx_path: str = ""
    cv_pdf_path: str = ""
    letter_pdf_path: str = ""
    match_reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def output_directory(self) -> str:
        for raw in (
            self.cv_pdf_path,
            self.cv_docx_path,
            self.letter_pdf_path,
        ):
            if raw:
                return str(Path(raw).expanduser().resolve().parent)
        return ""


@dataclass(frozen=True)
class ProfileSnapshot:
    name: str
    title: str
    location: str
    email: str
    summary: str
    target_roles: tuple[str, ...]
    contracts: tuple[str, ...]
    remote_policies: tuple[str, ...]
    accepted_job_languages: tuple[str, ...]
    skill_categories: tuple[tuple[str, tuple[str, ...]], ...]
    experiences: int
    projects: int
    education: int
    profile_dir: str


@dataclass(frozen=True)
class RuntimeDiagnostics:
    database_url: str
    database_path: str
    database_exists: bool
    output_dir: str
    profile_dir: str
    env_file: str
    llm_provider: str
    llm_model: str
    source_ready: dict[str, bool]
    source_health: dict[str, SourceHealth]


@dataclass
class SearchResult:
    reports: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def fetched(self) -> int:
        return sum(int(report.get("fetched", 0)) for report in self.reports)

    @property
    def persisted(self) -> int:
        return sum(int(report.get("persisted", 0)) for report in self.reports)


class DesktopService:
    """Small synchronous facade; the GUI runs slow calls through TaskWorker."""

    READY_STATUSES = {JobStatus.READY_FOR_FORM_SUBMISSION}
    REVIEW_STATUSES = {JobStatus.QUALITY_REJECTED}

    def initialize(self) -> None:
        init_db()

    def list_companies(self) -> list[dict[str, Any]]:
        return company_repository.list_companies()

    def set_company_checked(self, company_id: int, checked: bool) -> None:
        company_repository.set_company_checked(company_id, checked)

    def add_company(self, values: dict[str, Any]) -> int:
        return company_repository.add_company(values)

    def dashboard(self) -> DashboardSnapshot:
        from smartapply.database.models import LLMUsage

        with session_scope() as session:
            jobs = session.scalar(select(func.count()).select_from(Job)) or 0
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .outerjoin(Application, Application.job_id == Job.id)
                    .where(
                        Job.filtered_at.is_not(None),
                        Job.analyzed_at.is_(None),
                        Job.shortlisted_at.is_(None),
                        Job.archived_at.is_(None),
                        or_(
                            Job.duplicate_review_status.is_(None),
                            Job.duplicate_review_status != JobDuplicateStatus.PENDING,
                        ),
                        Application.id.is_(None),
                    )
                )
                or 0
            )
            analyzed = (
                session.scalar(
                    select(func.count())
                    .select_from(Job)
                    .outerjoin(Application, Application.job_id == Job.id)
                    .where(
                        Job.analyzed_at.is_not(None),
                        Job.shortlisted_at.is_(None),
                        Job.archived_at.is_(None),
                        or_(
                            Job.duplicate_review_status.is_(None),
                            Job.duplicate_review_status != JobDuplicateStatus.PENDING,
                        ),
                        Application.id.is_(None),
                    )
                )
                or 0
            )
            ready = (
                session.scalar(
                    select(func.count())
                    .select_from(Application)
                    .where(Application.status.in_(self.READY_STATUSES))
                )
                or 0
            )
            review = (
                session.scalar(
                    select(func.count())
                    .select_from(Application)
                    .where(Application.status.in_(self.REVIEW_STATUSES))
                )
                or 0
            )
            # A confirmed alias can retain an old application row for audit
            # purposes. Dashboard metrics must count the canonical offer only
            # once, otherwise historical duplicate submissions inflate both
            # the total and the daily curve.
            sent_events_by_group: dict[int, datetime] = {}
            sent_applications = (
                session.execute(
                    select(Application)
                    .options(joinedload(Application.job))
                    .where(Application.status == JobStatus.SENT)
                )
                .unique()
                .scalars()
                .all()
            )
            for application in sent_applications:
                event_at = application.form_submitted_at or application.updated_at
                if event_at is None:
                    continue
                if event_at.tzinfo is None:
                    event_at = event_at.replace(tzinfo=timezone.utc)
                canonical = canonical_job(session, application.job_id)
                group_id = int(canonical.id) if canonical is not None else -int(application.id)
                previous = sent_events_by_group.get(group_id)
                if previous is None or event_at < previous:
                    sent_events_by_group[group_id] = event_at
            sent = len(sent_events_by_group)
            sent_counts = Counter()
            for event_at in sent_events_by_group.values():
                sent_counts[event_at.astimezone().date()] += 1
            today = datetime.now().astimezone().date()
            chart_days = [today - timedelta(days=offset) for offset in range(13, -1, -1)]
            sent_by_day = tuple(
                DashboardDailyPoint(
                    day=day.isoformat(),
                    label=day.strftime("%d/%m"),
                    count=sent_counts.get(day, 0),
                )
                for day in chart_days
            )
            cost = session.scalar(select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0))) or 0.0
            latest = (
                session.execute(
                    select(Application)
                    .options(joinedload(Application.job))
                    .order_by(Application.updated_at.desc())
                    .limit(8)
                )
                .scalars()
                .all()
            )
            recent = tuple(
                DashboardApplication(
                    id=app.id,
                    company=app.job.company if app.job else "Entreprise inconnue",
                    title=app.job.title if app.job else "Offre supprimée",
                    status=app.status,
                    status_label=self._desktop_status_label(app.status),
                    next_action=_desktop_next_action(app.status, app.updated_at),
                    updated_at=_date_text(app.updated_at, with_time=True),
                )
                for app in latest
            )
        return DashboardSnapshot(
            jobs=int(jobs),
            pending=int(pending),
            ready=int(ready),
            review=int(review),
            sent=int(sent),
            analyzed=int(analyzed),
            llm_cost_usd=float(cost),
            recent=recent,
            sent_by_day=sent_by_day,
        )

    def list_jobs(
        self,
        *,
        search: str = "",
        status: str | None = None,
        sort_key: str = "score",
        sort_ascending: bool = False,
        limit: int = 500,
    ) -> list[JobRow]:
        stmt = (
            select(Job)
            .outerjoin(JobScore, JobScore.job_id == Job.id)
            .outerjoin(JobAnalysis, JobAnalysis.job_id == Job.id)
            .outerjoin(Application, Application.job_id == Job.id)
            .options(
                load_only(
                    Job.id,
                    Job.title,
                    Job.company,
                    Job.location,
                    Job.contract_type,
                    Job.remote_policy,
                    Job.description,
                    Job.cleaned_description,
                    Job.source,
                    Job.scraped_at,
                    Job.status,
                    Job.archived_at,
                    Job.filtered_at,
                    Job.shortlisted_at,
                    Job.analyzed_at,
                    Job.duplicate_review_status,
                    Job.possible_duplicate_of_id,
                    Job.duplicate_confidence,
                ),
                joinedload(Job.score).load_only(
                    JobScore.final_score,
                    JobScore.components,
                ),
                joinedload(Job.analysis).load_only(JobAnalysis.fit_score, JobAnalysis.seniority),
                joinedload(Job.application),
            )
        )
        if status == JobStatus.DUPLICATE_REVIEW:
            # The final archived-only pairs are removed after loading the
            # pending rows below. A pair with one active offer still needs a
            # human decision so that the active version is not lost.
            stmt = stmt.where(Job.duplicate_review_status == JobDuplicateStatus.PENDING)
        else:
            # Probable duplicates stay out of every normal queue until the
            # user explicitly decides whether the offers are identical.
            stmt = stmt.where(
                or_(
                    Job.duplicate_review_status.is_(None),
                    Job.duplicate_review_status != JobDuplicateStatus.PENDING,
                )
            )

        if status == JobStatus.SHORTLISTED:
            stmt = stmt.where(
                Job.shortlisted_at.is_not(None),
                Job.analyzed_at.is_not(None),
                Job.archived_at.is_(None),
                Application.id.is_(None),
            )
        elif status == JobStatus.SCRAPED:
            # ``filtered`` is an internal pipeline state. In the UI, Nouvelle
            # means the offer passed local filtering and still awaits analysis.
            stmt = stmt.where(
                Job.filtered_at.is_not(None),
                Job.analyzed_at.is_(None),
                Job.shortlisted_at.is_(None),
                Job.archived_at.is_(None),
                Application.id.is_(None),
            )
        elif status == JobStatus.ANALYZED:
            # Keep analyzed separate from both Top sélection and applications.
            stmt = stmt.where(
                Job.analyzed_at.is_not(None),
                Job.shortlisted_at.is_(None),
                Job.archived_at.is_(None),
                Application.id.is_(None),
            )
        elif status in APPLICATION_STATUSES:
            # Once a dossier exists, its tracking state is stored on
            # Application. Keep archived rows available through the explicit
            # archive filter, but do not let them leak into active tracking
            # queues when the offer itself was archived manually.
            if status == JobStatus.ARCHIVED:
                stmt = stmt.where(
                    or_(
                        Application.status == JobStatus.ARCHIVED,
                        Job.archived_at.is_not(None),
                        Job.status == JobStatus.ARCHIVED,
                    )
                )
            else:
                stmt = stmt.where(
                    Application.status == status,
                    Job.archived_at.is_(None),
                )
        elif status and status != JobStatus.DUPLICATE_REVIEW:
            stmt = stmt.where(Job.status == status)
        needle = search.strip()
        if needle:
            term = f"%{needle}%"
            stmt = stmt.where(
                or_(
                    Job.title.ilike(term),
                    Job.company.ilike(term),
                    Job.location.ilike(term),
                    Job.source.ilike(term),
                )
            )
        sort_columns = {
            "score": JobScore.final_score,
            "llm_score": JobAnalysis.fit_score,
            "company": Job.company,
            "title": Job.title,
            # Experience is displayed from the same seniority signal when it
            # is available; exact numeric extraction remains a derived text
            # value and is not suitable for a portable SQL expression.
            "experience": JobAnalysis.seniority,
            "location": Job.location,
            "contract": Job.contract_type,
            "status": func.coalesce(Application.status, Job.status),
        }
        sort_column = sort_columns.get(sort_key, JobScore.final_score)
        ordering = sort_column.asc() if sort_ascending else sort_column.desc()
        stmt = stmt.order_by(ordering.nulls_last(), Job.id.asc()).limit(limit)
        with session_scope() as session:
            jobs = session.execute(stmt).scalars().all()
            if status == JobStatus.DUPLICATE_REVIEW:
                jobs = [
                    job
                    for job in jobs
                    if not self._duplicate_pair_is_fully_archived(session, job)
                ]
            grouped_ids = application_ids_for_confirmed_groups(session, [job.id for job in jobs])
            return [
                self._job_row(job, related_application_id=grouped_ids.get(job.id))
                for job in jobs
            ]

    @staticmethod
    def _duplicate_pair_is_fully_archived(session, job: Job) -> bool:
        """Return whether both offers in a pending pair are archived.

        Archived-only pairs have no actionable offer and therefore do not
        need a human decision. If either side is still active, the pair must
        remain in the review queue.
        """
        if job.possible_duplicate_of_id is None:
            return False
        candidate = session.get(Job, int(job.possible_duplicate_of_id))
        if candidate is None:
            return False
        return all(
            bool(offer.archived_at or offer.status == JobStatus.ARCHIVED)
            for offer in (job, candidate)
        )

    def get_job(self, job_id: int) -> JobDetail | None:
        with session_scope() as session:
            job = (
                session.execute(
                    select(Job)
                    .options(
                        joinedload(Job.score),
                        joinedload(Job.analysis),
                        joinedload(Job.application).joinedload(Application.documents),
                    )
                    .where(Job.id == job_id)
                )
                .unique()
                .scalar_one_or_none()
            )
            if job is None:
                return None
            base = replace(
                self._job_row(
                    job,
                    related_application_id=application_ids_for_confirmed_groups(
                        session, [job.id]
                    ).get(job.id),
                ),
                duplicate_candidate=self._duplicate_candidate_detail(session, job),
            )
            base_values = asdict(base)
            base_values["duplicate_candidate"] = base.duplicate_candidate
            analysis = job.analysis
            components = job.score.components if job.score and job.score.components else {}
            # Older records can have an archived application while the offer
            # itself was never marked archived.  The list already exposes
            # those rows as « Archivée » via the application status; keep the
            # detail panel consistent so its archive reason is not hidden.
            archived = bool(
                job.archived_at
                or job.status == JobStatus.ARCHIVED
                or (job.application and job.application.status == JobStatus.ARCHIVED)
            )
            raw_archive_reasons = (
                components.get("rejection_reasons")
                or (components.get("reasons") if archived else [])
                or []
            )
            if archived and job.canonical_job_id is not None:
                reference = canonical_job(session, job.id)
                raw_archive_reasons = [
                    f"duplicate_reference:{reference.title} — {reference.company} (n° {reference.id})"
                    if reference is not None
                    else f"duplicate_of:{job.canonical_job_id}"
                ]
            return JobDetail(
                **base_values,
                remote=job.remote_policy or "",
                url=job.application_url or "",
                description=job.cleaned_description or job.description or "",
                role_type=analysis.role_type or "" if analysis else "",
                domain=analysis.domain or "" if analysis else "",
                seniority=analysis.seniority or "" if analysis else "",
                match_reasons=_text_list(analysis.match_reasons if analysis else None),
                risks=_text_list(analysis.risks if analysis else None),
                archive_reasons=archive_reason_labels(
                    raw_archive_reasons,
                    stage=str(components.get("rejection_stage") or ""),
                    archived=archived,
                ),
                application=(
                    self._application_detail_from(job.application, analysis=analysis)
                    if job.application
                    else None
                ),
            )

    @staticmethod
    def _application_detail_from(
        app: Application,
        *,
        analysis: JobAnalysis | None = None,
    ) -> ApplicationDetail:
        """Build application tracking data while the owning offer is loaded."""
        if app.job is None:
            raise ValueError("Application is not linked to a job")
        analysis = analysis or app.job.analysis
        docs: dict[str, GeneratedDocument] = {}
        for doc in sorted(app.documents, key=lambda item: item.id):
            docs[doc.doc_type] = doc
        letter_pdf = docs.get("motivation_letter_pdf")
        return ApplicationDetail(
            id=app.id,
            job_id=app.job_id,
            company=app.job.company,
            title=app.job.title,
            status=app.status,
            status_label=DesktopService._desktop_status_label(app.status),
            next_action=_desktop_next_action(app.status, app.updated_at),
            updated_at=_date_text(app.updated_at, with_time=True),
            location=app.job.location or "",
            source=app.job.source,
            job_url=app.job.application_url or "",
            form_url=app.form_submission_url or app.job.application_url or "",
            notes=app.notes or "",
            form_submitted_at=_date_text(app.form_submitted_at, with_time=True)
            if app.form_submitted_at
            else "",
            cv_docx_path=app.cv_docx_path or "",
            cv_pdf_path=app.cv_pdf_path or "",
            letter_pdf_path=letter_pdf.path or "" if letter_pdf else "",
            match_reasons=_text_list(analysis.match_reasons if analysis else None),
            risks=_text_list(analysis.risks if analysis else None),
            warnings=_text_list(app.validation_warnings),
        )

    @staticmethod
    def _job_row(job: Job, *, related_application_id: int | None = None) -> JobRow:
        years = required_min_years(job.cleaned_description or job.description)
        seniority = job.analysis.seniority if job.analysis else ""
        components = job.score.components if job.score and job.score.components else {}
        filter_disposition = str(components.get("filter_disposition") or "")
        experience = f"{years}+ ans" if years is not None else (seniority or "—")
        raw_fit_score = job.analysis.fit_score if job.analysis else None
        try:
            llm_score = float(raw_fit_score) if raw_fit_score is not None else None
        except (TypeError, ValueError):
            llm_score = None
        archived = bool(job.archived_at or job.status == JobStatus.ARCHIVED)
        application = job.application
        if application is not None:
            related_application_id = None
        analyzed = job.analyzed_at is not None
        duplicate_review_status = job.duplicate_review_status or JobDuplicateStatus.NONE
        duplicate_review_pending = duplicate_review_status == JobDuplicateStatus.PENDING
        if duplicate_review_pending:
            display_status = JobStatus.DUPLICATE_REVIEW
        elif archived:
            display_status = JobStatus.ARCHIVED
        elif application is not None:
            # Application tracking remains the canonical display state once
            # a dossier exists. The analyzed filter itself excludes these
            # rows, so it cannot accidentally broaden that queue.
            display_status = application.status
        elif job.shortlisted_at is not None and analyzed:
            display_status = JobStatus.SHORTLISTED
        elif analyzed:
            display_status = JobStatus.ANALYZED
        else:
            # FILTERED remains persisted for pipeline routing but is never a
            # user-facing status. It is represented as Nouvelle here.
            display_status = JobStatus.SCRAPED
        return JobRow(
            id=job.id,
            company=job.company,
            title=job.title,
            location=job.location or "",
            source=job.source,
            status=display_status,
            status_label=DesktopService._desktop_status_label(display_status),
            filter_disposition=filter_disposition,
            score=(job.score.final_score if job.score else None),
            llm_score=llm_score,
            contract=job.contract_type or "",
            experience=experience,
            shortlisted=bool(job.shortlisted_at and analyzed and not archived),
            application_id=application.id if application else None,
            related_application_id=related_application_id,
            analyzed=analyzed,
            can_generate=bool(
                not application
                and related_application_id is None
                and analyzed
                and not archived
                and not duplicate_review_pending
            ),
            can_send=bool(
                application
                and not archived
                and not duplicate_review_pending
                and application.status != JobStatus.SENT
                and application.form_submitted_at is None
            ),
            scraped_at=_date_text(job.scraped_at),
            duplicate_review_status=duplicate_review_status,
            possible_duplicate_of_id=job.possible_duplicate_of_id,
            duplicate_confidence=job.duplicate_confidence,
        )

    @staticmethod
    def _duplicate_candidate_detail(
        session,
        job: Job,
    ) -> DuplicateReviewDetail | None:
        if (
            job.duplicate_review_status != JobDuplicateStatus.PENDING
            or job.possible_duplicate_of_id is None
        ):
            return None
        candidate = session.get(Job, int(job.possible_duplicate_of_id))
        if candidate is None:
            return None
        application = application_for_duplicate_group(session, candidate.id)
        return DuplicateReviewDetail(
            id=int(candidate.id),
            company=candidate.company,
            title=candidate.title,
            location=candidate.location or "",
            source=candidate.source,
            url=candidate.application_url or "",
            application_id=int(application.id) if application is not None else None,
            application_status=application.status if application is not None else "",
            application_status_label=(
                DesktopService._desktop_status_label(application.status)
                if application is not None
                else ""
            ),
            application_updated_at=(
                _date_text(application.updated_at, with_time=True)
                if application is not None
                else ""
            ),
            contract=candidate.contract_type or "",
            remote=candidate.remote_policy or "",
            description=candidate.cleaned_description or candidate.description or "",
        )

    def shortlist_summary(self) -> ShortlistSummary:
        from smartapply.pipeline.apply.persistence import reservation_is_stale

        with session_scope() as session:
            jobs = (
                session.execute(
                    select(Job)
                    .options(joinedload(Job.application).joinedload(Application.documents))
                    .where(
                        Job.shortlisted_at.is_not(None),
                        Job.analyzed_at.is_not(None),
                        Job.archived_at.is_(None),
                        or_(
                            Job.duplicate_review_status.is_(None),
                            Job.duplicate_review_status != JobDuplicateStatus.PENDING,
                        ),
                    )
                )
                .unique()
                .scalars()
                .all()
            )
            grouped_ids = application_ids_for_confirmed_groups(session, [job.id for job in jobs])
            ready = sum(
                job.id not in grouped_ids
                or (job.application is not None and reservation_is_stale(job.application))
                for job in jobs
            )
            return ShortlistSummary(total=len(jobs), ready_to_generate=ready)

    def shortlisted_generation_ids(self) -> list[int]:
        from smartapply.pipeline.apply.persistence import reservation_is_stale

        with session_scope() as session:
            jobs = (
                session.execute(
                    select(Job)
                    .options(joinedload(Job.application).joinedload(Application.documents))
                    .where(
                        Job.shortlisted_at.is_not(None),
                        Job.analyzed_at.is_not(None),
                        Job.archived_at.is_(None),
                        or_(
                            Job.duplicate_review_status.is_(None),
                            Job.duplicate_review_status != JobDuplicateStatus.PENDING,
                        ),
                    )
                    .order_by(Job.shortlisted_at.asc(), Job.id.asc())
                )
                .unique()
                .scalars()
                .all()
            )
            grouped_ids = application_ids_for_confirmed_groups(session, [job.id for job in jobs])
            return [
                int(job.id)
                for job in jobs
                if job.id not in grouped_ids
                or (job.application is not None and reservation_is_stale(job.application))
            ]

    def set_job_shortlisted(self, job_id: int, *, selected: bool) -> bool:
        """Persist a manual Top-selection decision across application runs."""
        if selected:
            with session_scope() as session:
                job = session.get(Job, job_id)
                if (
                    job is None
                    or job.archived_at is not None
                    or job.duplicate_review_status == JobDuplicateStatus.PENDING
                ):
                    return False
                needs_analysis = job.analyzed_at is None

            if needs_analysis:
                from smartapply.pipeline import Pipeline

                report = Pipeline().analyze_jobs([job_id])
                if report.analyzed == 0 and report.already_analyzed == 0:
                    details = getattr(report, "errors", []) or []
                    message = (
                        str(details[0].get("message", ""))
                        if details and isinstance(details[0], dict)
                        else ""
                    )
                    raise RuntimeError(
                        "L’analyse automatique de l’offre a échoué."
                        + (f" {message}" if message else "")
                    )

        with session_scope() as session:
            job = session.get(Job, job_id)
            if (
                job is None
                or job.archived_at is not None
                or job.duplicate_review_status == JobDuplicateStatus.PENDING
            ):
                return False
            updated = set_shortlisted(
                session,
                job_id,
                selected=selected,
                origin=ShortlistOrigin.MANUAL,
            )
            if updated is None:
                return False
            components = (
                dict(job.score.components) if job.score is not None and job.score.components else {}
            )
            components["manual_shortlist"] = {
                "selected": selected,
                "updated_at": datetime.now().astimezone().isoformat(),
            }
            set_score(session, job_id, components=components)
            return True

    def resolve_duplicate(
        self,
        job_id: int,
        *,
        same_offer: bool,
        progress=None,
    ) -> bool:
        """Resolve a probable duplicate without deleting either source row."""
        if progress:
            progress("Enregistrement de la décision sur le doublon…")
        with session_scope() as session:
            resolved = (
                confirm_duplicate(session, job_id)
                if same_offer
                else reject_duplicate(session, job_id)
            )
            return resolved is not None

    def get_application(self, application_id: int) -> ApplicationDetail | None:
        with session_scope() as session:
            app = (
                session.execute(
                    select(Application)
                    .options(
                        joinedload(Application.job),
                        joinedload(Application.job).joinedload(Job.analysis),
                        joinedload(Application.documents),
                    )
                    .where(Application.id == application_id)
                )
                .unique()
                .scalar_one_or_none()
            )
            if app is None or app.job is None:
                return None
            return self._application_detail_from(app)

    def application_output_directory(self, application_id: int) -> Path | None:
        """Return the actual artifact directory for one persisted application.

        Persisted document paths are the source of truth. This matters when the
        configured output directory has changed since the application was
        generated.
        """
        with session_scope() as session:
            app = (
                session.execute(
                    select(Application)
                    .options(joinedload(Application.documents))
                    .where(Application.id == application_id)
                )
                .unique()
                .scalar_one_or_none()
            )
            if app is None:
                return None

            for raw_path in (
                app.cv_pdf_path,
                app.cv_docx_path,
                *(doc.path for doc in app.documents),
            ):
                if raw_path:
                    return Path(raw_path).expanduser().resolve().parent

        from smartapply.pipeline.output_paths import application_output_dir

        return (
            application_output_dir(
                get_settings().output_dir,
                application_id,
            )
            .expanduser()
            .resolve()
        )

    def search_jobs(
        self,
        *,
        query: str,
        location: str,
        sources: list[str],
        max_results: int,
        date_posted: str,
        serpapi_hl: str = "en,fr",
        progress=None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> SearchResult:
        from smartapply.pipeline import Pipeline
        from smartapply.pipeline.pipeline import freshness_kwargs

        if not location.strip():
            raise ValueError("Une localisation est requise pour lancer la recherche.")
        requested_sources = list(dict.fromkeys(sources))
        result = SearchResult()
        if not requested_sources:
            return result

        pipeline = Pipeline()

        def collect(source: str):
            if progress:
                progress(f"Recherche sur {self.source_label(source)}…")
            source_kwargs = freshness_kwargs(
                source,
                date_posted=date_posted,
                serpapi_hl=serpapi_hl,
            )
            if source == "serpapi":
                # The explicit app field is authoritative; do not retain the
                # historical SERPAPI_GL=fr country bias for London/Canada/etc.
                source_kwargs["use_configured_country_bias"] = False
            return pipeline.collect_source(
                source,
                query,
                location or None,
                max_results=max_results,
                stop_requested=stop_requested,
                **source_kwargs,
            )

        collections = {}
        with ThreadPoolExecutor(
            max_workers=min(4, len(requested_sources)),
            thread_name_prefix="elan-search",
        ) as executor:
            futures = {executor.submit(collect, source): source for source in requested_sources}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    collection = future.result()
                except Exception as exc:
                    logger.exception("Search collection failed: source=%s", source)
                    result.errors.append(f"{self.source_label(source)} : {exc}")
                    continue
                collections[source] = collection
                result.cancelled = result.cancelled or collection.cancelled
                logger.info(
                    "Search collection done: source=%s raw_jobs=%d warnings=%d cancelled=%s",
                    source,
                    len(collection.raw_jobs),
                    len(collection.warnings),
                    collection.cancelled,
                )

        # SQLite gets a single writer after all network-bound collections.
        persisted_job_ids: set[int] = set()
        for source in requested_sources:
            collection = collections.get(source)
            if collection is None:
                continue
            if progress:
                progress(f"Enregistrement de {self.source_label(source)}…")
            try:
                report = pipeline.persist_collection(collection)
            except Exception as exc:
                logger.exception("Search persistence failed: source=%s", source)
                result.errors.append(f"{self.source_label(source)} : {exc}")
            else:
                result.reports.append(asdict(report))
                persisted_job_ids.update(int(job_id) for job_id in report.job_ids)

        # A search must finish the ingest phase before returning to the UI.
        # Otherwise freshly persisted jobs have no ``filtered_at`` timestamp:
        # they are absent from both the Nouvelle view and the À analyser
        # counter until another pipeline action happens to filter them.
        if persisted_job_ids:
            if progress:
                progress("Filtrage local des nouvelles offres…")
            try:
                pipeline.filter_pending(job_ids=sorted(persisted_job_ids))
            except Exception as exc:
                logger.exception("Search local filtering failed")
                result.errors.append(f"Filtrage local : {exc}")
        logger.info(
            "Search finished: sources=%s fetched=%d persisted=%d errors=%d cancelled=%s",
            ",".join(requested_sources),
            result.fetched,
            result.persisted,
            len(result.errors),
            result.cancelled,
        )
        result.cancelled = result.cancelled or bool(stop_requested and stop_requested())
        return result

    def process_pending(self, *, top_k: int, progress=None) -> dict[str, Any]:
        from smartapply.pipeline import Pipeline

        if progress:
            progress("Classement et analyse des offres…")
        return asdict(Pipeline().process_pending(top_k_analyze=top_k))

    def update_shortlist(self, *, top_k: int, progress=None) -> dict[str, Any]:
        """Apply Top-K directly from persisted scores without rebuilding Pipeline."""
        if progress:
            progress("Mise à jour de la Top sélection…")
        with session_scope() as session:
            candidates = list(
                session.execute(
                    select(Job)
                    .join(JobScore, JobScore.job_id == Job.id)
                    .outerjoin(Application, Application.job_id == Job.id)
                    .options(
                        joinedload(Job.score),
                        joinedload(Job.analysis),
                    )
                    .where(
                        Job.archived_at.is_(None),
                        Job.filtered_at.is_not(None),
                        # The final Top selection is based on the LLM fit
                        # score; pending offers are not eligible yet.
                        Job.analyzed_at.is_not(None),
                        or_(
                            Job.duplicate_review_status.is_(None),
                            Job.duplicate_review_status != JobDuplicateStatus.PENDING,
                        ),
                        JobScore.final_score.is_not(None),
                        Application.id.is_(None),
                    )
                    .order_by(JobScore.final_score.desc(), Job.id.asc())
                )
                .unique()
                .scalars()
                .all()
            )
            shortlist_n = min(max(1, int(top_k)), len(candidates)) if candidates else 0
            selected = RankingMixin._mixed_score_shortlist(candidates, shortlist_n)
            shortlisted = RankingMixin._replace_automatic_shortlist(session, selected)
            return {
                "total": len(candidates),
                "kept_after_filter": len(candidates),
                "duplicates_removed": 0,
                "ranked": len(candidates),
                "shortlisted": len(shortlisted),
                "ranked_ids": [int(job.id) for job in candidates],
                "shortlisted_ids": [int(job.id) for job in shortlisted],
            }

    def analyze_job(self, job_id: int, *, progress=None) -> dict[str, Any]:
        from smartapply.pipeline import Pipeline

        if progress:
            progress("Analyse de l’offre selon votre profil…")
        return asdict(Pipeline().analyze_jobs([job_id]))

    def generate_application(
        self,
        job_id: int,
        *,
        progress=None,
    ) -> dict[str, Any]:
        from smartapply.pipeline import ApplicationAlreadyExistsError, Pipeline

        if progress:
            progress("Création du CV et de la lettre…")
        try:
            report = Pipeline().apply_to(job_id)
        except ApplicationAlreadyExistsError as exc:
            # A stale screen may still offer generation after duplicate review.
            # Open the completed dossier, but keep active reservations protected.
            with session_scope() as session:
                app = session.get(Application, exc.application_id) if exc.application_id else None
                if app is None or not (
                    app.documents or app.cv_json or app.cv_docx_path or app.cv_pdf_path
                ):
                    raise
                return {"job_id": app.job_id, "application_id": app.id, "existing": True}
        return asdict(report)

    def generate_applications(
        self,
        job_ids: list[int],
        *,
        progress=None,
    ) -> dict[str, Any]:
        """Analyze when needed and generate documents for every eligible job."""
        from smartapply.pipeline import ApplicationAlreadyExistsError, Pipeline
        from smartapply.pipeline.apply.persistence import reservation_is_stale

        requested_ids = list(dict.fromkeys(int(job_id) for job_id in job_ids if int(job_id) > 0))
        report: dict[str, Any] = {
            "requested": len(requested_ids),
            "generated": 0,
            "skipped": 0,
            "failed": 0,
            "application_ids": [],
            "errors": [],
            "warnings": [],
            "duplicate_review_ids": [],
        }
        if not requested_ids:
            return report

        with session_scope() as session:
            jobs = (
                session.execute(
                    select(Job)
                    .options(joinedload(Job.application).joinedload(Application.documents))
                    .where(Job.id.in_(requested_ids))
                )
                .unique()
                .scalars()
                .all()
            )
            grouped_ids = application_ids_for_confirmed_groups(session, [job.id for job in jobs])
            state = {
                job.id: {
                    "title": job.title,
                    "company": job.company,
                    "archived": bool(job.archived_at or job.status == JobStatus.ARCHIVED),
                    "analyzed": bool(job.analyzed_at),
                    "application_blocks_generation": bool(
                        (job.application and not reservation_is_stale(job.application))
                        or (job.application is None and job.id in grouped_ids)
                    ),
                    "duplicate_review_pending": (
                        job.duplicate_review_status == JobDuplicateStatus.PENDING
                    ),
                }
                for job in jobs
            }

        pipeline = Pipeline()
        total = len(requested_ids)
        for index, job_id in enumerate(requested_ids, start=1):
            item = state.get(job_id)
            if item is None:
                report["skipped"] += 1
                continue
            if item["duplicate_review_pending"]:
                report["skipped"] += 1
                report["duplicate_review_ids"].append(job_id)
                continue
            if item["archived"] or item["application_blocks_generation"]:
                report["skipped"] += 1
                continue
            try:
                if progress:
                    progress(f"Création de la candidature {index}/{total}…")
                if not item["analyzed"]:
                    analysis = pipeline.analyze_jobs([job_id])
                    if analysis.analyzed == 0 and analysis.already_analyzed == 0:
                        analysis_errors = getattr(analysis, "errors", [])
                        if analysis_errors:
                            report["failed"] += 1
                            message = str(analysis_errors[0].get("message", "Analyse GPT échouée"))
                            report["errors"].append(
                                {"job_id": job_id, "message": f"Analyse : {message}"}
                            )
                        else:
                            report["skipped"] += 1
                        continue
                application = pipeline.apply_to(job_id)
            except ApplicationAlreadyExistsError:
                report["skipped"] += 1
                continue
            except Exception as exc:
                report["failed"] += 1
                report["errors"].append({"job_id": job_id, "message": str(exc)})
                continue
            report["generated"] += 1
            if application.application_id:
                report["application_ids"].append(int(application.application_id))
            issues = [
                *list(getattr(application, "validation_warnings", None) or []),
                *(
                    f"validation_error:{error}"
                    for error in list(getattr(application, "validation_errors", None) or [])
                ),
            ]
            if issues:
                report["warnings"].append(
                    {
                        "job_id": job_id,
                        "title": item["title"],
                        "company": item["company"],
                        "message": " · ".join(str(issue) for issue in issues),
                    }
                )
        return report

    def generate_shortlisted_applications(self, *, progress=None) -> dict[str, Any]:
        """Generate every still-eligible application in the persistent Top selection."""
        job_ids = self.shortlisted_generation_ids()
        return self.generate_applications(job_ids, progress=progress)

    def create_manual_application(
        self,
        *,
        title: str,
        company: str,
        location: str,
        description: str,
        application_url: str,
        progress=None,
    ) -> dict[str, Any]:
        from smartapply.pipeline import Pipeline

        pipeline = Pipeline()
        from smartapply.pipeline import DuplicateReviewRequiredError

        if progress:
            progress("Enregistrement de l’offre…")
        ingest = pipeline.ingest_text(
            description,
            title=title,
            company=company,
            location=location or None,
            application_url=application_url or None,
        )
        if not ingest.job_ids:
            raise RuntimeError("L’offre existe déjà et ne peut pas être régénérée.")
        job_id = int(ingest.job_ids[0])
        if getattr(ingest, "duplicate_review_ids", []):
            with session_scope() as session:
                job = session.get(Job, job_id)
                raise DuplicateReviewRequiredError(
                    job_id,
                    job.possible_duplicate_of_id if job is not None else None,
                )
        if progress:
            progress("Analyse de l’offre…")
        analysis = pipeline.analyze_jobs([job_id])
        if analysis.analyzed == 0 and analysis.already_analyzed == 0:
            analysis_errors = getattr(analysis, "errors", [])
            detail = str(analysis_errors[0].get("message", "")) if analysis_errors else ""
            raise RuntimeError(
                "L’analyse de l’offre n’a pas abouti." + (f" {detail}" if detail else "")
            )
        if progress:
            progress("Création du CV et de la lettre…")
        application = pipeline.apply_to(
            job_id,
            form_url=application_url.strip() or None,
        )
        return asdict(application)

    def update_application(
        self,
        application_id: int,
        *,
        status: str | None = None,
        notes: str | None = None,
        form_submitted: bool = False,
    ) -> ApplicationDetail | None:
        with session_scope() as session:
            update_application_tracking(
                session,
                application_id,
                status=status,
                notes=notes,
                form_submitted=form_submitted,
            )
        return self.get_application(application_id)

    def archive_job(self, job_id: int) -> None:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                return
            components = (
                dict(job.score.components) if job.score is not None and job.score.components else {}
            )
            components.update(
                {
                    "rejection_stage": "manual",
                    "rejection_reasons": ["manual_archive"],
                    "rejection_summary": "manual_archive",
                }
            )
            set_score(session, job_id, components=components)
            mark_archived(session, job_id)

    def archive_application(self, application_id: int) -> bool:
        """Archive an application and its associated offer."""
        with session_scope() as session:
            application = session.execute(
                select(Application)
                .options(joinedload(Application.job))
                .where(Application.id == application_id)
            ).scalar_one_or_none()
            if application is None or application.job is None:
                return False
            application.status = JobStatus.ARCHIVED
            job = application.job
            components = (
                dict(job.score.components) if job.score is not None and job.score.components else {}
            )
            components.update(
                {
                    "rejection_stage": "manual",
                    "rejection_reasons": ["manual_archive"],
                    "rejection_summary": "manual_archive",
                }
            )
            set_score(session, job.id, components=components)
            mark_archived(session, job.id)
            return True

    def mark_job_sent(self, job_id: int) -> bool:
        """Mark a generated application as sent.

        An offer is not a candidature: without generated documents there is
        nothing that can legitimately be marked as sent.
        """
        with session_scope() as session:
            job = session.execute(
                select(Job).options(joinedload(Job.application)).where(Job.id == job_id)
            ).scalar_one_or_none()
            if (
                job is None
                or job.archived_at is not None
                or job.application is None
                or job.duplicate_review_status == JobDuplicateStatus.PENDING
            ):
                return False
            update_application_tracking(
                session,
                job.application.id,
                status=JobStatus.SENT,
                form_submitted=True,
            )
            job.status = JobStatus.SENT
            return True

    def rescue_job(self, job_id: int, *, progress=None) -> dict[str, Any]:
        return self.rescue_jobs([job_id], progress=progress)

    def rescue_jobs(self, job_ids: list[int], *, progress=None) -> dict[str, Any]:
        """Restore selected offers, analyze them and prepare their dossiers."""
        from smartapply.pipeline import Pipeline
        from smartapply.pipeline.apply.persistence import reservation_is_stale

        requested_ids = list(dict.fromkeys(int(job_id) for job_id in job_ids if int(job_id) > 0))
        report: dict[str, Any] = {
            "requested": len(requested_ids),
            "rescued": 0,
            "generated": 0,
            "existing": 0,
            "skipped": 0,
            "failed": 0,
            "job_ids": [],
            "application_ids": [],
            "errors": [],
            "warnings": [],
        }
        handled: set[int] = set()
        pipeline = None
        for index, job_id in enumerate(requested_ids, start=1):
            try:
                with session_scope() as session:
                    job = session.get(Job, job_id)
                    if job is None or not (
                        job.archived_at
                        or job.status == JobStatus.ARCHIVED
                        or (job.application and job.application.status == JobStatus.ARCHIVED)
                    ):
                        report["skipped"] += 1
                        continue
                    if job.duplicate_review_status == JobDuplicateStatus.PENDING:
                        raise ValueError("Vérifiez d’abord le doublon de cette offre.")
                    # A confirmed source alias shares its dossier with the
                    # canonical offer. Resume that dossier without splitting
                    # the group or creating a second application.
                    target = canonical_job(session, job_id)
                    if target is None:
                        raise ValueError("Offre principale introuvable.")
                    canonical_id = int(target.id)
                    if pending_duplicate_for_group(session, canonical_id) is not None:
                        raise ValueError("Vérifiez d’abord le doublon de cette offre.")
                    application = application_for_duplicate_group(session, target.id)
                    if application is not None:
                        target = application.job
                    target_id = int(target.id)
                    if target_id in handled:
                        report["skipped"] += 1
                        continue
                    handled.add(target_id)
                    if target.duplicate_review_status == JobDuplicateStatus.PENDING:
                        raise ValueError("Vérifiez d’abord le doublon de cette offre.")
                    has_documents = bool(
                        application
                        and (
                            application.documents
                            or application.cv_json
                            or application.cv_docx_path
                            or application.cv_pdf_path
                        )
                    )
                    target_archived = bool(
                        target.archived_at
                        or target.status == JobStatus.ARCHIVED
                        or (application and application.status == JobStatus.ARCHIVED)
                    )
                    if has_documents and not target_archived:
                        report["existing"] += 1
                        report["job_ids"].append(target_id)
                        report["application_ids"].append(application.id)
                        continue
                    if application and not has_documents and not reservation_is_stale(application):
                        raise ValueError(
                            "La création de ce dossier est déjà en cours. Réessayez plus tard."
                        )
                    if target_id != canonical_id:
                        # Older groups can own a dossier on an alias. Make
                        # that owner canonical so the pipeline can reuse it.
                        for member_id in duplicate_group_ids(session, canonical_id):
                            member = session.get(Job, member_id)
                            member.canonical_job_id = None if member_id == target_id else target_id
                            if member_id != target_id:
                                mark_archived(session, member_id)
                    if target_archived:
                        rescue_archived_job(
                            session,
                            target_id,
                            justification="Désarchivée depuis l’application macOS",
                        )
                report["rescued"] += 1
                report["job_ids"].append(target_id)
                # Commit restoration before starting network work. A failed
                # generation leaves the offer available for a retry.
                if progress:
                    progress(f"Analyse de l’offre {index}/{len(requested_ids)}…")
                pipeline = pipeline or Pipeline()
                analysis = pipeline.analyze_jobs([target_id])
                if analysis.analyzed == 0 and analysis.already_analyzed == 0:
                    errors = getattr(analysis, "errors", []) or []
                    detail = str(errors[0].get("message", "")) if errors else ""
                    raise RuntimeError(
                        "L’analyse de l’offre n’a pas abouti." + (f" {detail}" if detail else "")
                    )
                if progress:
                    progress(f"Création du CV et de la lettre {index}/{len(requested_ids)}…")
                application_report = pipeline.apply_to(target_id, force_regenerate=has_documents)
                report["generated"] += 1
                if application_report.application_id:
                    report["application_ids"].append(int(application_report.application_id))
                issues = [
                    *(getattr(application_report, "validation_warnings", None) or []),
                    *(getattr(application_report, "validation_errors", None) or []),
                ]
                if issues:
                    report["warnings"].append({"job_id": target_id, "message": " · ".join(issues)})
            except Exception as exc:
                logger.exception("Offer restoration failed: job_id=%s", job_id)
                report["failed"] += 1
                report["errors"].append({"job_id": job_id, "message": str(exc)})
        return report

    def profile(self) -> ProfileSnapshot:
        profile = get_profile()
        settings = get_settings()
        return ProfileSnapshot(
            name=profile.identity.full_name,
            title=profile.identity.title,
            location=profile.identity.location,
            email=str(profile.identity.email),
            summary=profile.identity.summary,
            target_roles=tuple(profile.preferences.target_roles),
            contracts=tuple(profile.preferences.accepted_contract_types),
            remote_policies=tuple(profile.preferences.accepted_remote_policies),
            accepted_job_languages=tuple(profile.preferences.accepted_job_languages),
            skill_categories=tuple(
                (category.name, tuple(category.skills)) for category in profile.skills.categories
            ),
            experiences=len(profile.experiences),
            projects=len(profile.projects),
            education=len(profile.education),
            profile_dir=str(settings.profile_dir.expanduser().resolve()),
        )

    @staticmethod
    def ensure_profile_directory() -> Path:
        """Return the active profile folder, creating it when first opened."""
        path = get_settings().profile_dir.expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def diagnostics(self, *, check_sources: bool = False) -> RuntimeDiagnostics:
        settings = get_settings()
        url = make_url(settings.database_url)
        database_path = ""
        if url.get_backend_name() == "sqlite" and url.database:
            database_path = str(Path(url.database).expanduser().resolve())
        source_health = (
            check_source_health(settings) if check_sources else pending_source_health(settings)
        )
        sources = {key: health.ready for key, health in source_health.items()}
        return RuntimeDiagnostics(
            database_url=url.render_as_string(hide_password=True),
            database_path=database_path,
            database_exists=bool(database_path and Path(database_path).exists()),
            output_dir=str(settings.output_dir.expanduser().resolve()),
            profile_dir=str(settings.profile_dir.expanduser().resolve()),
            env_file=str(ENV_FILE),
            llm_provider=settings.llm_provider,
            llm_model=settings.openai_model_smart,
            source_ready=sources,
            source_health=source_health,
        )

    @staticmethod
    def job_statuses() -> list[tuple[str, str]]:
        # ``filtered`` is an internal pipeline marker. All other workflow
        # states, including application tracking, are visible on the unified
        # Offers page.
        return [
            (row["status"], row["label"])
            for row in STATUS_FLOW
            if row["status"] in OFFER_FILTER_STATUSES
        ]

    @staticmethod
    def _read_top_k_setting(key: str) -> int:
        """Read a Top-K preference, with a one-time-compatible legacy fallback."""
        default = int(get_settings().top_k_ranked)
        with session_scope() as session:
            setting = session.get(AppSetting, key)
            if setting is None:
                # Existing installations used one shared ``top_k_ranked``
                # preference. Keep that value as the initial value for both
                # controls, then let each control persist independently.
                setting = session.get(AppSetting, "top_k_ranked")
            try:
                return max(1, min(100, int(setting.value))) if setting is not None else default
            except (TypeError, ValueError):
                return default

    @staticmethod
    def _write_top_k_setting(key: str, value: int) -> int:
        normalized = max(1, min(100, int(value)))
        with session_scope() as session:
            setting = session.get(AppSetting, key)
            if setting is None:
                session.add(AppSetting(key=key, value=str(normalized)))
            else:
                setting.value = str(normalized)
        return normalized

    @staticmethod
    def analysis_top_k() -> int:
        return DesktopService._read_top_k_setting("top_k_analysis")

    @staticmethod
    def set_analysis_top_k(value: int) -> int:
        return DesktopService._write_top_k_setting("top_k_analysis", value)

    @staticmethod
    def shortlist_top_k() -> int:
        return DesktopService._read_top_k_setting("top_k_shortlist")

    @staticmethod
    def set_shortlist_top_k(value: int) -> int:
        return DesktopService._write_top_k_setting("top_k_shortlist", value)

    @staticmethod
    def source_label(source: str) -> str:
        return {
            "serpapi": "Google Jobs",
            "francetravail": "France Travail",
            "linkedin": "LinkedIn",
            "welcometothejungle": "Welcome to the Jungle",
        }.get(source, source)

    @staticmethod
    def _desktop_status_label(status: str) -> str:
        return status_label(status)
