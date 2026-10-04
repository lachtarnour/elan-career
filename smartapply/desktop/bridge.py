"""Qt Quick bridge between Élan's product UI and its business services."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import Property, QObject, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication

from smartapply.concurrency import busy_job_ids, subscribe_resource_changes
from smartapply.database.models import JobStatus
from smartapply.database.session import subscribe_database_changes
from smartapply.desktop.models import OfferListModel
from smartapply.desktop.services import DesktopService, SearchResult
from smartapply.desktop.workers import TaskWorker
from smartapply.jobsearch.status import display_status

logger = logging.getLogger(__name__)


def _plain(value: Any) -> Any:
    """Return QML-friendly lists and dictionaries."""
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    return value


def _count_text(count: int, singular: str, plural: str) -> str:
    """Format a French quantity without parenthesized plural shortcuts."""
    return f"{count} {singular if count == 1 else plural}"


def _diagnostic_messages(item: Any) -> list[str]:
    """Read structured batch diagnostics, with compatibility for older reports."""
    if isinstance(item, dict):
        issues = item.get("issues")
        if isinstance(issues, (list, tuple)):
            return [str(issue).strip() for issue in issues if str(issue).strip()]
        item = item.get("message") or ""
    return [part.strip() for part in str(item).split(" · ") if part.strip()]


def _is_review_information(message: str) -> bool:
    return message.startswith("text_review_corrected:")


def _batch_review_counts(warnings: list[Any]) -> tuple[int, bool]:
    """Count affected dossiers; a verified correction is useful information."""
    affected = set()
    corrected = False
    for index, item in enumerate(warnings):
        messages = _diagnostic_messages(item)
        corrected = corrected or any(_is_review_information(message) for message in messages)
        if any(not _is_review_information(message) for message in messages):
            key = (
                (item.get("application_id") or item.get("job_id"))
                if isinstance(item, dict)
                else None
            )
            affected.add(("dossier", key) if key else ("entry", index))
    return len(affected), corrected


def _review_notification_suffix(affected: int, corrected: bool, *, batch: bool) -> str:
    if affected:
        quantity = (
            _count_text(
                affected,
                "dossier avec des points à vérifier",
                "dossiers avec des points à vérifier",
            )
            if batch
            else _count_text(affected, "point à vérifier", "points à vérifier")
        )
        return f" {quantity}. Détails dans le volet du dossier."
    if corrected:
        return " Corrections vérifiées."
    return ""


def _technical_failure_preview(errors: list[Any]) -> str:
    """Keep one actionable cause visible even when no dossier could be created."""
    if not errors:
        return ""
    first = errors[0]
    if isinstance(first, dict):
        job_id = first.get("job_id")
        label = f"Offre #{job_id} : " if job_id else ""
        message = str(first.get("message") or "Erreur technique.")
    else:
        label, message = "", str(first)
    preview = " ".join((label + message).split())
    if len(preview) > 140:
        preview = preview[:139].rsplit(" ", 1)[0] + "…"
    elif not preview.endswith((".", "!", "?", "…")):
        preview += "."
    return " " + preview


def _tone(status: str) -> str:
    status = display_status(status)
    if status in {JobStatus.SENT, JobStatus.INTERVIEW}:
        return "success"
    if status == JobStatus.REJECTED:
        return "danger"
    if status == JobStatus.ARCHIVED:
        return "warning"
    if status == JobStatus.DUPLICATE_REVIEW:
        return "warning"
    if status == JobStatus.READY_FOR_FORM_SUBMISSION:
        return "accent"
    if status == JobStatus.SHORTLISTED:
        return "accent"
    return "neutral"


def _filter_disposition_label(disposition: str) -> str:
    return {
        "relevant": "Relevant",
        "uncertain": "Incertain",
        "rejected": "Rejeté",
    }.get(disposition, "—")


def _enrich_rows(rows: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        item = _plain(row)
        item["tone"] = _tone(str(item.get("status", "")))
        if item.get("shortlisted"):
            item["tone"] = "accent"
        score = item.get("score")
        item["score_text"] = "—" if score is None else f"{float(score):.0%}"
        llm_score = item.get("llm_score")
        item["llm_score_text"] = "—" if llm_score is None else f"{float(llm_score):.0%}"
        item["filter_disposition_label"] = _filter_disposition_label(
            str(item.get("filter_disposition") or "")
        )
        company = str(item.get("company", ""))
        item["initial"] = company[:1].upper() or "•"
        result.append(item)
    return result


class DesktopBridge(QObject):
    """Stateful QObject API consumed by the QML product shell."""

    dashboardChanged = Signal()
    shortlistChanged = Signal()
    analysisTopKChanged = Signal()
    shortlistTopKChanged = Signal()
    jobsChanged = Signal()
    duplicateRelevantOnlyChanged = Signal()
    companiesChanged = Signal()
    companiesLoadingChanged = Signal()
    companiesSavingChanged = Signal()
    companyAdded = Signal()
    companySaveFailed = Signal(str)
    currentJobChanged = Signal()
    profileChanged = Signal()
    diagnosticsChanged = Signal()
    busyChanged = Signal()
    jobsLoadingChanged = Signal()
    jobDetailLoadingChanged = Signal()
    activityChanged = Signal()
    toastRequested = Signal(str, str, str)
    navigationRequested = Signal(str, int)
    jobSelectionClearRequested = Signal()
    busyJobIdsChanged = Signal()
    shutdownRequestedChanged = Signal()
    shutdownReadyChanged = Signal()
    notesFlushRequested = Signal()
    applicationNotesSaved = Signal(int, int)
    applicationNotesSaveFailed = Signal(int, int, str)
    _resourceChanges = Signal()
    _databaseChanges = Signal(object)

    def __init__(self, service: DesktopService | None = None) -> None:
        super().__init__()
        self.service = service or DesktopService()
        self.service.initialize()
        self._thread_pool = QThreadPool.globalInstance()
        self._read_pool = QThreadPool(self)
        self._read_pool.setMaxThreadCount(2)
        self._mutation_pool = QThreadPool(self)
        self._mutation_pool.setMaxThreadCount(1)
        self._long_queue = deque()
        self._long_keys: set[str] = set()
        self._reads_active: dict[str, TaskWorker] = {}
        self._reads_pending: dict[str, tuple] = {}
        self._active_route = "dashboard"
        self._maintenance_pending = self.service.startup_maintenance_needed()
        self._maintenance_mutations = []
        self._maintenance_retry = False
        self._maintenance_failed = False
        self._dirty_views: set[str] = set()
        self._shutdown_requested = False
        self._shutdown_ready = False
        self._notes_failures: dict[int, tuple[str, int]] = {}
        self._notes_latest: dict[int, int] = {}
        self._shortlist_intents: dict[int, Event] = {}
        self._shortlist_revisions: dict[int, int] = {}
        self._shortlist_desired: dict[int, bool] = {}
        self._jobs_model = OfferListModel(self)
        self._diagnostics_checked_at = 0.0
        self._resourceChanges.connect(self._resources_changed)
        self._unsubscribe_resources = subscribe_resource_changes(self._resourceChanges.emit)
        self._databaseChanges.connect(self._database_changed)
        self._unsubscribe_database = subscribe_database_changes(
            self._databaseChanges.emit, with_topics=True
        )
        self._workers: set[TaskWorker] = set()
        self._read_workers: set[TaskWorker] = set()
        self._read_tokens: dict[str, int] = {}
        self._active_worker: TaskWorker | None = None
        self._diagnostics_worker: TaskWorker | None = None
        self._dashboard: dict[str, Any] = {}
        self._shortlist: dict[str, Any] = {"total": 0, "ready_to_generate": 0}
        self._analysis_top_k = self.service.analysis_top_k()
        self._shortlist_top_k = self.service.shortlist_top_k()
        self._jobs: list[dict[str, Any]] = []
        self._duplicate_relevant_only = False
        self._companies: list[dict[str, Any]] = []
        self._companies_loading = False
        self._companies_saving = False
        self._current_job: dict[str, Any] = {}
        self._selected_job_id = 0
        self._profile: dict[str, Any] = {}
        self._diagnostics: dict[str, Any] = {}
        self._busy = False
        self._busy_label = ""
        self._workflow_refresh_timer = QTimer(self)
        self._workflow_refresh_timer.setInterval(200)
        self._workflow_refresh_timer.setSingleShot(True)
        self._workflow_refresh_timer.timeout.connect(self._flush_refresh)
        self._jobs_loading = False
        self._displayed_jobs_query: tuple | None = None
        self._job_detail_loading = False
        self._job_search = ""
        self._job_status = JobStatus.SCRAPED
        self._job_sort_key = "score"
        self._job_sort_ascending = False
        self._activity: dict[str, Any] = {
            "title": "",
            "message": "",
            "kind": "neutral",
            "fetched": 0,
            "persisted": 0,
        }
        # Keep launch instant; large offer and application lists are loaded on navigation.
        self.refreshDashboard()
        self.refreshShortlist()
        self.refreshProfile()
        self.refreshDiagnosticsIfStale()
        if self._maintenance_pending:
            self._schedule_maintenance()

    @Property("QVariantMap", notify=dashboardChanged)
    def dashboard(self) -> dict[str, Any]:
        return self._dashboard

    @Property("QVariantMap", notify=shortlistChanged)
    def shortlist(self) -> dict[str, Any]:
        return self._shortlist

    @Property(int, notify=analysisTopKChanged)
    def analysisTopK(self) -> int:  # noqa: N802
        return self._analysis_top_k

    @Slot(int)
    def setAnalysisTopK(self, value: int) -> int:  # noqa: N802
        normalized = max(1, min(100, int(value)))
        if hasattr(self, "_mutation_pool"):
            self._mutate(self.service.set_analysis_top_k, normalized)
        else:
            normalized = self.service.set_analysis_top_k(value)
        if normalized == self._analysis_top_k:
            return normalized
        self._analysis_top_k = normalized
        self.analysisTopKChanged.emit()
        return normalized

    @Property(int, notify=shortlistTopKChanged)
    def shortlistTopK(self) -> int:  # noqa: N802
        return self._shortlist_top_k

    @Slot(int)
    def setShortlistTopK(self, value: int) -> int:  # noqa: N802
        normalized = max(1, min(100, int(value)))
        if hasattr(self, "_mutation_pool"):
            self._mutate(self.service.set_shortlist_top_k, normalized)
        else:
            normalized = self.service.set_shortlist_top_k(value)
        if normalized == self._shortlist_top_k:
            return normalized
        self._shortlist_top_k = normalized
        self.shortlistTopKChanged.emit()
        return normalized

    @Property("QVariantList", notify=jobsChanged)
    def jobs(self) -> list[dict[str, Any]]:
        return self._jobs

    @Property(QObject, constant=True)
    def jobsModel(self):  # noqa: N802
        return getattr(self, "_jobs_model", None)

    @Property("QVariantList", notify=busyJobIdsChanged)
    def busyJobIds(self):  # noqa: N802
        return busy_job_ids()

    @Slot(int, result=bool)
    def isJobBusy(self, job_id):  # noqa: N802
        return job_id in busy_job_ids()

    @Slot()
    def _resources_changed(self):
        self.busyJobIdsChanged.emit()

    @Property(bool, notify=busyChanged)
    def maintenanceFailed(self):  # noqa: N802
        return getattr(self, "_maintenance_failed", False)

    @Slot()
    def retryStartupMaintenance(self):  # noqa: N802
        if not self.shutdownRequested and self._maintenance_pending:
            self._schedule_maintenance()

    @Property(int, notify=busyChanged)
    def queuedTaskCount(self):  # noqa: N802
        return len(getattr(self, "_long_queue", ())) + len(
            getattr(self, "_maintenance_mutations", ())
        )

    @Property(bool, notify=shutdownRequestedChanged)
    def shutdownRequested(self):  # noqa: N802
        return getattr(self, "_shutdown_requested", False)

    @Property(bool, notify=shutdownReadyChanged)
    def shutdownReady(self):  # noqa: N802
        return getattr(self, "_shutdown_ready", False)

    @Slot(str)
    def setActiveRoute(self, route):  # noqa: N802
        next_route = route.split("?", 1)[0]
        if next_route != getattr(self, "_active_route", "") and hasattr(self, "_read_tokens"):
            self._read_tokens["open_application"] = self._read_tokens.get("open_application", 0) + 1
        self._active_route = next_route
        if self._active_route in {"search", "settings"}:
            self.refreshDiagnosticsIfStale()
        if getattr(self, "_dirty_views", None):
            self._workflow_refresh_timer.start()

    @Property(bool, notify=duplicateRelevantOnlyChanged)
    def duplicateRelevantOnly(self) -> bool:  # noqa: N802
        return self._duplicate_relevant_only

    @Slot(bool)
    def setDuplicateRelevantOnly(self, enabled: bool) -> None:  # noqa: N802
        enabled = bool(enabled)
        if enabled == self._duplicate_relevant_only:
            return
        self._duplicate_relevant_only = enabled
        self.duplicateRelevantOnlyChanged.emit()
        if self._job_status == JobStatus.DUPLICATE_REVIEW:
            self.refreshJobs()

    @Property("QVariantList", notify=companiesChanged)
    def companies(self) -> list[dict[str, Any]]:
        return self._companies

    @Property(bool, notify=companiesLoadingChanged)
    def companiesLoading(self) -> bool:  # noqa: N802
        return self._companies_loading

    @Property(bool, notify=companiesSavingChanged)
    def companiesSaving(self) -> bool:  # noqa: N802
        return self._companies_saving

    @Slot()
    def loadCompanies(self) -> None:  # noqa: N802
        if self._companies_saving:
            return
        self._companies_loading = True
        self.companiesLoadingChanged.emit()
        self._start_read(
            "companies",
            self.service.list_companies,
            on_success=self._companies_loaded,
            on_finished=self._companies_load_finished,
        )

    def _companies_loaded(self, rows: list[dict[str, Any]]) -> None:
        getattr(self, "_dirty_views", set()).discard("companies")
        self._companies = rows
        self.companiesChanged.emit()

    def _companies_load_finished(self) -> None:
        self._companies_loading = False
        self.companiesLoadingChanged.emit()

    @Slot(int, bool)
    def setCompanyChecked(self, company_id: int, checked: bool) -> None:  # noqa: N802
        self._save_company(self.service.set_company_checked, company_id, checked)

    @Slot("QVariantMap")
    def addCompany(self, values: dict[str, Any]) -> None:  # noqa: N802
        self._save_company(self.service.add_company, values, created=True)

    def _save_company(
        self, operation: Callable[..., Any], *args: Any, created: bool = False
    ) -> None:
        if self._companies_saving or self.shutdownRequested:
            return
        self._companies_saving = True
        self.companiesSavingChanged.emit()
        # Invalidate an in-flight read so it cannot restore an older check state.
        self._read_tokens["companies"] = self._read_tokens.get("companies", 0) + 1
        self._companies_load_finished()

        def save_and_reload() -> list[dict[str, Any]]:
            operation(*args)
            return self.service.list_companies()

        worker = TaskWorker(save_and_reload)
        self._workers.add(worker)

        def saved(rows: list[dict[str, Any]]) -> None:
            self._companies_loaded(rows)
            if created:
                self.companyAdded.emit()
                self.toastRequested.emit("Entreprise ajoutée", "", "success")

        def failed(message: str, trace: str) -> None:
            self.companySaveFailed.emit(message)
            self._task_error(message, trace)

        def finished() -> None:
            self._workers.discard(worker)
            self._companies_saving = False
            self.companiesSavingChanged.emit()
            self._check_shutdown()

        worker.signals.result.connect(saved)
        worker.signals.error.connect(failed)
        worker.signals.finished.connect(finished)
        self._mutation_pool.start(worker)

    @Property("QVariantMap", notify=currentJobChanged)
    def currentJob(self) -> dict[str, Any]:  # noqa: N802 - Qt property name
        return self._current_job

    @Property("QVariantMap", notify=profileChanged)
    def profile(self) -> dict[str, Any]:
        return self._profile

    @Property("QVariantMap", notify=diagnosticsChanged)
    def diagnostics(self) -> dict[str, Any]:
        return self._diagnostics

    @Property(bool, notify=diagnosticsChanged)
    def diagnosticsChecking(self) -> bool:  # noqa: N802
        return getattr(self, "_diagnostics_worker", None) is not None

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(bool, notify=jobsLoadingChanged)
    def jobsLoading(self) -> bool:  # noqa: N802
        return self._jobs_loading

    @Property(bool, notify=jobDetailLoadingChanged)
    def jobDetailLoading(self) -> bool:  # noqa: N802
        return self._job_detail_loading

    @Property(str, notify=busyChanged)
    def busyLabel(self) -> str:  # noqa: N802
        return self._busy_label

    @Property(bool, notify=busyChanged)
    def canCancel(self) -> bool:  # noqa: N802
        return bool(
            getattr(self, "_long_queue", ())
            or getattr(self, "_maintenance_mutations", ())
            or (self._active_worker and self._active_worker.with_cancel)
        )

    @Property("QVariantMap", notify=activityChanged)
    def activity(self) -> dict[str, Any]:
        return self._activity

    @Property("QVariantList", constant=True)
    def jobStatuses(self) -> list[dict[str, str]]:  # noqa: N802
        return [{"value": value, "label": label} for value, label in self.service.job_statuses()]

    @Slot()
    def refreshAll(self) -> None:  # noqa: N802
        self.refreshDashboard()
        self.refreshShortlist()
        self.loadJobs("", JobStatus.SCRAPED)
        self.refreshProfile()
        self.refreshDiagnostics()

    @Slot()
    def refreshDashboard(self) -> None:  # noqa: N802
        self._start_read(
            "dashboard",
            self.service.dashboard,
            on_success=self._dashboard_loaded,
        )

    def _dashboard_loaded(self, snapshot: Any) -> None:
        dashboard = _plain(snapshot)
        dashboard["recent"] = _enrich_rows(list(snapshot.recent))
        total = max(int(dashboard.get("jobs", 0)), 1)
        dashboard["analysis_progress"] = min(1.0, int(dashboard.get("analyzed", 0)) / total)
        dashboard["ready_progress"] = min(1.0, int(dashboard.get("ready", 0)) / total)
        dashboard["sent_progress"] = min(1.0, int(dashboard.get("sent", 0)) / total)
        self._dashboard = dashboard
        self.dashboardChanged.emit()

    @Slot()
    def refreshShortlist(self) -> None:  # noqa: N802
        self._start_read(
            "shortlist",
            self.service.shortlist_summary,
            on_success=self._shortlist_loaded,
        )

    def _shortlist_loaded(self, summary: Any) -> None:
        self._shortlist = _plain(summary)
        self.shortlistChanged.emit()

    @Slot(str, str, str, bool)
    def loadJobs(
        self,
        search: str = "",
        status: str = "",
        sort_key: str = "score",
        sort_ascending: bool = False,
    ) -> None:  # noqa: N802
        self._job_search = search
        self._job_status = status
        self._job_sort_key = sort_key
        self._job_sort_ascending = sort_ascending
        duplicates_only = (
            self._duplicate_relevant_only if status == JobStatus.DUPLICATE_REVIEW else False
        )
        query = (search, status, sort_key, sort_ascending, duplicates_only)
        loading = query != getattr(self, "_displayed_jobs_query", None)
        if self._jobs_loading != loading:
            self._jobs_loading = loading
            self.jobsLoadingChanged.emit()
        self._start_read(
            "jobs",
            self.service.list_jobs,
            search=search,
            status=status or None,
            sort_key=sort_key,
            sort_ascending=sort_ascending,
            only_relevant_duplicates=duplicates_only,
            on_success=lambda rows: self._jobs_loaded(rows, query),
            on_finished=self._jobs_read_finished,
        )

    @Slot()
    def refreshJobs(self) -> None:  # noqa: N802
        """Refresh offers without resetting the current page state."""
        self.loadJobs(
            self._job_search,
            self._job_status,
            self._job_sort_key,
            self._job_sort_ascending,
        )

    def _jobs_loaded(self, rows: list[Any], query: tuple | None = None) -> None:
        self._displayed_jobs_query = query
        self._jobs = _enrich_rows(rows)
        if getattr(self, "_jobs_model", None) is not None:
            self._jobs_model.replace(self._jobs)
        self.jobsChanged.emit()
        current_id = self._selected_job_id or int(self._current_job.get("id") or 0)
        if self._jobs and not current_id:
            self.selectJob(int(self._jobs[0]["id"]))

    def _jobs_read_finished(self) -> None:
        if self._jobs_loading:
            self._jobs_loading = False
            self.jobsLoadingChanged.emit()

    @Slot(int)
    def selectJob(self, job_id: int) -> None:  # noqa: N802
        self._selected_job_id = job_id
        if hasattr(self, "_read_tokens"):
            self._read_tokens["open_application"] = self._read_tokens.get("open_application", 0) + 1
        self.refreshSelectedJob()

    @Slot()
    def refreshSelectedJob(self) -> None:  # noqa: N802
        """Reread the selected offer without superseding an explicit navigation."""
        job_id = self._selected_job_id
        if not job_id:
            return
        loading = job_id != int(self._current_job.get("id") or 0)
        if self._job_detail_loading != loading:
            self._job_detail_loading = loading
            self.jobDetailLoadingChanged.emit()
        self._start_read(
            "job_detail",
            self.service.get_job,
            job_id,
            on_success=self._job_loaded,
            on_finished=self._job_detail_read_finished,
        )

    def _job_loaded(self, detail: Any) -> None:
        self._current_job = _plain(detail) if detail else {}
        if self._current_job:
            self._current_job["tone"] = _tone(str(self._current_job.get("status", "")))
            score = self._current_job.get("score")
            self._current_job["score_text"] = "—" if score is None else f"{float(score):.0%}"
            llm_score = self._current_job.get("llm_score")
            self._current_job["llm_score_text"] = (
                "—" if llm_score is None else f"{float(llm_score):.0%}"
            )
            application = self._current_job.get("application")
            if isinstance(application, dict):
                application["tone"] = _tone(str(application.get("status", "")))
        self._current_job["shortlist_pending"] = int(self._current_job.get("id") or 0) in getattr(
            self, "_shortlist_intents", {}
        )
        self.currentJobChanged.emit()

    def _job_detail_read_finished(self) -> None:
        if self._job_detail_loading:
            self._job_detail_loading = False
            self.jobDetailLoadingChanged.emit()

    @Slot()
    def refreshProfile(self) -> None:  # noqa: N802
        self._start_read(
            "profile",
            self.service.profile,
            on_success=self._profile_loaded,
        )

    def _profile_loaded(self, value: Any) -> None:
        profile = _plain(value)
        profile["initials"] = (
            "".join(part[0].upper() for part in str(profile.get("name", "")).split()[:2]) or "É"
        )
        profile["skill_categories"] = [
            {"name": name, "skills": skills} for name, skills in profile.get("skill_categories", [])
        ]
        self._profile = profile
        self.profileChanged.emit()

    @Slot()
    def refreshDiagnosticsIfStale(self) -> None:  # noqa: N802
        if (
            time.monotonic() - getattr(self, "_diagnostics_checked_at", 0) >= 300
            or self.service.source_diagnostics_stale()
        ):
            self.refreshDiagnostics()

    @Slot()
    def refreshDiagnostics(self) -> None:  # noqa: N802
        if self.shutdownRequested or self._diagnostics_worker is not None:
            return
        self.service.invalidate_diagnostics_cache()
        worker = TaskWorker(self.service.diagnostics, check_sources=True)
        self._diagnostics_worker = worker
        self.diagnosticsChanged.emit()
        self._workers.add(worker)
        worker.signals.result.connect(self._apply_diagnostics)
        worker.signals.error.connect(self._diagnostics_error)
        worker.signals.finished.connect(lambda: self._diagnostics_finished(worker))
        self._read_pool.start(worker)

    def _apply_diagnostics(self, snapshot: Any) -> None:
        self._diagnostics_checked_at = time.monotonic()
        diagnostics = _plain(snapshot)
        health_by_source = diagnostics.get("source_health", {})
        diagnostics["sources"] = []
        for key, health in health_by_source.items():
            item = dict(health or {})
            item.update(
                {
                    "key": key,
                    "label": self.service.source_label(key),
                }
            )
            diagnostics["sources"].append(item)
        diagnostics["source_ready"] = {
            key: bool(health.get("ready")) for key, health in health_by_source.items()
        }
        self._diagnostics = diagnostics
        self.diagnosticsChanged.emit()

    def _diagnostics_error(self, message: str, trace: str) -> None:
        logger.error("Source diagnostics failed: %s\n%s", message, trace)
        diagnostics = dict(self._diagnostics)
        health_by_source = {}
        for key, source in diagnostics.get("source_health", {}).items():
            item = dict(source)
            if item.get("configured"):
                item.update(
                    ready=False,
                    state="unavailable",
                    message="Vérification impossible.",
                )
            health_by_source[key] = item
        diagnostics["source_health"] = health_by_source
        self._apply_diagnostics(diagnostics)

    def _diagnostics_finished(self, worker: TaskWorker) -> None:
        self._workers.discard(worker)
        if self._diagnostics_worker is worker:
            self._diagnostics_worker = None
            self.diagnosticsChanged.emit()
        self._check_shutdown()

    @Slot(str, str, "QVariantList", int, str)
    def searchJobs(  # noqa: N802
        self,
        query: str,
        location: str,
        sources: list[str],
        max_results: int,
        date_posted: str,
    ) -> None:
        if not query.strip():
            self.toastRequested.emit(
                "Recherche incomplète", "Indiquez au moins un poste recherché.", "warning"
            )
            return
        if not location.strip():
            self.toastRequested.emit(
                "Recherche incomplète",
                "Indiquez la ville, la région ou le pays à utiliser pour cette recherche.",
                "warning",
            )
            return
        if not sources:
            self.toastRequested.emit(
                "Aucune source",
                "Activez au moins une source configurée dans les réglages.",
                "warning",
            )
            return
        self._run(
            "Recherche en cours",
            self.service.search_jobs,
            on_success=self._search_done,
            query=query,
            location=location,
            sources=list(sources),
            max_results=max_results,
            date_posted=date_posted,
            serpapi_hl="en,fr",
            cancellable=True,
        )

    def _search_done(self, report: SearchResult) -> None:
        message = (
            f"{_count_text(report.fetched, 'offre trouvée', 'offres trouvées')}. "
            f"{_count_text(report.persisted, 'nouvelle offre enregistrée', 'nouvelles offres enregistrées')}."
        )
        if report.errors:
            message += " Sources indisponibles : " + " · ".join(report.errors[:3])
        warnings = [
            str(warning)
            for source_report in report.reports
            for warning in (source_report.get("warnings") or [])
            if str(warning).strip()
        ]
        if warnings:
            shown = warnings[:3]
            message += " Avertissements : " + " · ".join(shown)
            if len(warnings) > len(shown):
                message += f" · {len(warnings) - len(shown)} autre(s)"
        title = "Recherche annulée" if report.cancelled else "Recherche terminée"
        kind = "warning" if report.cancelled or report.errors or warnings else "success"
        if report.cancelled:
            message += " Les résultats déjà reçus ont été conservés."
        self._set_activity(title, message, kind, report.fetched, report.persisted)
        self._refresh_workflow()
        self.toastRequested.emit(title, message, kind)

    @Slot()
    def cancelCurrentTask(self) -> None:  # noqa: N802
        for intent in getattr(self, "_shortlist_intents", {}).values():
            intent.set()
        self._shortlist_intents.clear()
        self._long_queue.clear()
        self._maintenance_mutations.clear()
        self._long_keys = {getattr(self, "_active_key", "")} if self._active_worker else set()
        worker = self._active_worker
        if worker is not None and worker.with_cancel:
            worker.cancel()
            self._busy_label = "Annulation en cours…"
        self.busyChanged.emit()
        self._update_shortlist_pending()

    @Slot(int)
    def processPending(self, top_k: int) -> None:  # noqa: N802
        normalized = self.setAnalysisTopK(top_k)

        def schedule(ids=None):
            kwargs = {"top_k": normalized}
            if ids is not None:
                kwargs["job_ids"] = ids
            self._run(
                "Analyse en cours",
                self.service.process_pending,
                on_success=self._process_done,
                cancellable=True,
                **kwargs,
            )

        if hasattr(self.service, "pending_analysis_ids"):
            self._start_read(
                "pending_analysis_intent", self.service.pending_analysis_ids, on_success=schedule
            )
        else:
            schedule()

    @Slot("QVariantList")
    def analyzeSelectedJobs(self, job_ids: list[Any]) -> None:  # noqa: N802
        ids = self._job_ids(job_ids)
        if not ids:
            self.toastRequested.emit(
                "Aucune offre sélectionnée",
                "Sélectionnez au moins une offre à analyser.",
                "warning",
            )
            return
        self._run(
            "Analyse de " + _count_text(len(ids), "offre", "offres"),
            self.service.analyze_selected_jobs,
            ids,
            cancellable=True,
            on_success=self._selected_analysis_done,
        )

    def _selected_analysis_done(self, report: dict[str, Any]) -> None:
        analyzed = int(report.get("analyzed", 0))
        already_analyzed = int(report.get("already_analyzed", 0))
        errors = list(report.get("errors") or [])
        message = _count_text(analyzed, "offre analysée", "offres analysées") + "."
        if already_analyzed:
            message += (
                " "
                + _count_text(
                    already_analyzed,
                    "offre déjà analysée",
                    "offres déjà analysées",
                )
                + "."
            )
        if errors:
            message += " Échecs : " + self._failure_details(errors)
        kind = "warning" if errors and analyzed else ("danger" if errors else "success")
        self._refresh_workflow()
        self._set_activity("Analyse terminée", message, kind)
        self.toastRequested.emit("Analyse terminée", message, kind)

    @Slot(int)
    def updateShortlist(self, top_k: int) -> None:  # noqa: N802
        """Apply Top-K to already ranked offers and refresh the offer table."""
        normalized = self.setShortlistTopK(top_k)
        self._run(
            "Mise à jour de la Top sélection",
            self.service.update_shortlist,
            on_success=self._shortlist_updated,
            top_k=normalized,
        )

    def _shortlist_updated(self, report: dict[str, Any]) -> None:
        count = int(report.get("shortlisted", 0))
        # Update this state before the asynchronous table refresh. Otherwise
        # the generation button can briefly keep the previous Top K count.
        self._shortlist = {"total": count, "ready_to_generate": count}
        self.shortlistChanged.emit()
        self._refresh_workflow()
        message = _count_text(count, "offre dans la Top sélection", "offres dans la Top sélection")
        self._set_activity("Top sélection mise à jour", message, "success")
        self.toastRequested.emit("Top sélection mise à jour", message, "success")

    def _process_done(self, report: dict[str, Any]) -> None:
        analyzed = int(report.get("analyzed", 0))
        errors = list(report.get("analysis_errors") or [])
        message = _count_text(analyzed, "offre analysée", "offres analysées") + "."
        if errors:
            message += " Échecs : " + self._failure_details(errors)
        kind = "warning" if errors and analyzed else ("danger" if errors else "success")
        self._set_activity("Analyse terminée", message, kind)
        self._refresh_workflow()
        self.toastRequested.emit("Analyse terminée", message, kind)

    @Slot(int)
    def analyzeJob(self, job_id: int) -> None:  # noqa: N802
        self._run(
            "Analyse de l’offre",
            self.service.analyze_job,
            job_id,
            cancellable=True,
            on_success=lambda report: self._analysis_done(job_id, report),
        )

    def _analysis_done(self, job_id: int, report: dict[str, Any]) -> None:
        errors = list(report.get("errors") or [])
        if errors:
            message = self._failure_details(errors)
            self._set_activity("Analyse échouée", message, "danger")
            self._refresh_workflow()
            self._refresh_selected(job_id)
            self.toastRequested.emit("Analyse échouée", message, "danger")
            return
        if int(report.get("analyzed", 0)):
            self._job_action_done(job_id, "Analyse terminée", "Offre analysée.")
            return
        if int(report.get("already_analyzed", 0)):
            self._job_action_done(job_id, "Analyse terminée", "Offre déjà analysée.")
            return
        self._set_activity("Analyse impossible", "Offre introuvable ou archivée.", "danger")
        self._refresh_workflow()
        self.toastRequested.emit("Analyse impossible", "Offre introuvable ou archivée.", "danger")

    @Slot(int)
    def generateApplication(self, job_id: int) -> None:  # noqa: N802
        self._run(
            "Création de la candidature",
            self.service.generate_application,
            job_id,
            cancellable=True,
            on_success=self._generation_done,
        )

    @Slot(int)
    def regenerateApplication(self, job_id: int) -> None:  # noqa: N802
        self._run(
            "Régénération du CV et de la lettre",
            self.service.regenerate_application,
            job_id,
            cancellable=True,
            on_success=self._regeneration_done,
        )

    @Slot("QVariantList")
    def generateApplications(self, job_ids: list[Any]) -> None:  # noqa: N802
        ids = self._job_ids(job_ids)
        if not ids:
            self.toastRequested.emit(
                "Aucune offre sélectionnée",
                "Sélectionnez au moins une offre.",
                "warning",
            )
            return
        self._run(
            "Création de " + _count_text(len(ids), "candidature", "candidatures"),
            self.service.generate_applications,
            ids,
            cancellable=True,
            on_success=self._bulk_generation_done,
        )

    @Slot()
    def generateShortlistedApplications(self) -> None:  # noqa: N802
        ready = int(self._shortlist.get("ready_to_generate") or 0)
        if ready <= 0:
            self.toastRequested.emit(
                "Top sélection à jour",
                "Aucune candidature ne reste à créer.",
                "neutral",
            )
            return
        self._start_read(
            "shortlist_generation_intent",
            self.service.shortlisted_generation_ids,
            on_success=self.generateApplications,
        )

    def _bulk_generation_done(self, report: dict[str, Any]) -> None:
        self._refresh_workflow()
        generated = int(report.get("generated", 0))
        skipped = int(report.get("skipped", 0))
        failed = int(report.get("failed", 0))
        details = _count_text(generated, "candidature créée", "candidatures créées")
        if skipped:
            details += ", " + _count_text(skipped, "offre ignorée", "offres ignorées")
        if failed:
            details += ", " + _count_text(failed, "échec technique", "échecs techniques")
        warnings = list(report.get("warnings") or [])
        affected, corrected = _batch_review_counts(warnings)
        details += "."
        if failed:
            details += _technical_failure_preview(list(report.get("errors") or []))
        details += _review_notification_suffix(affected, corrected, batch=True)
        kind = (
            "success"
            if generated and not failed and not affected
            else ("warning" if generated else "danger")
        )
        self._set_activity("Création terminée", details, kind)
        self.toastRequested.emit(
            "Création terminée",
            details,
            kind,
        )

    def _generation_done(self, report: dict[str, Any]) -> None:
        self._refresh_workflow()
        if report.get("existing"):
            message = "Le CV et la lettre sont disponibles dans ce dossier."
            self._set_activity("Candidature existante", message, "neutral")
            self.toastRequested.emit("Candidature existante", message, "neutral")
            return
        job_id = int(report.get("job_id") or 0)
        warnings = list(report.get("validation_warnings") or [])
        errors = list(report.get("validation_errors") or [])
        issues = [
            message for item in [*warnings, *errors] for message in _diagnostic_messages(item)
        ]
        points = {message for message in issues if not _is_review_information(message)}
        corrected = any(_is_review_information(message) for message in issues)
        message = "Le CV et la lettre sont disponibles."
        message += _review_notification_suffix(len(points), corrected, batch=False)
        kind = "warning" if points else "success"
        self._set_activity("Candidature créée", message, kind)
        self.toastRequested.emit("Candidature créée", message, kind)
        if job_id:
            self._refresh_selected(job_id)

    def _regeneration_done(self, report: dict[str, Any]) -> None:
        self._refresh_workflow()
        job_id = int(report.get("job_id") or 0)
        issues = [
            message
            for item in [
                *list(report.get("validation_warnings") or []),
                *list(report.get("validation_errors") or []),
            ]
            for message in _diagnostic_messages(item)
        ]
        review_points = {str(issue) for issue in issues if not _is_review_information(str(issue))}
        corrected = any(_is_review_information(str(issue)) for issue in issues)
        message = "Le CV et la lettre ont été régénérés dans le dossier existant."
        suffix = _review_notification_suffix(len(review_points), corrected, batch=False)
        if suffix:
            message += suffix
        kind = "warning" if review_points else "success"
        self._set_activity("Candidature régénérée", message, kind)
        self.toastRequested.emit("Candidature régénérée", message, kind)
        if job_id:
            self._refresh_selected(job_id)

    @staticmethod
    def _failure_details(errors: list[Any]) -> str:
        details: list[str] = []
        for error in errors[:3]:
            if isinstance(error, dict):
                job_id = error.get("job_id")
                label = str(error.get("title") or (f"offre #{job_id}" if job_id else "offre"))
                message = str(error.get("message") or "erreur inconnue")
                details.append(f"{label} — {message}")
            else:
                details.append(str(error))
        remaining = len(errors) - len(details)
        if remaining > 0:
            details.append(f"{remaining} autre(s) échec(s)")
        return " · ".join(details)

    @Slot(int)
    def archiveJob(self, job_id: int) -> None:  # noqa: N802
        self._mutate(
            self.service.archive_job,
            job_id,
            on_success=lambda _: self._job_action_done(job_id, "Offre archivée", ""),
        )

    @Slot(int)
    def markJobSent(self, job_id: int) -> None:  # noqa: N802
        def saved(ok):
            if ok:
                self._job_action_done(job_id, "Offre marquée comme envoyée", "")
            else:
                self.toastRequested.emit(
                    "Envoi impossible", "Créez d’abord les documents de la candidature.", "warning"
                )

        self._mutate(self.service.mark_job_sent, job_id, on_success=saved)

    @Slot(int)
    def archiveApplication(self, application_id: int) -> None:  # noqa: N802
        self._mutate(
            self.service.archive_application,
            application_id,
            on_success=lambda ok: self._operation_done(
                "Candidature archivée" if ok else "Candidature indisponible", ""
            ),
        )

    def _update_shortlist_pending(self):
        if getattr(self, "_current_job", None):
            self._current_job = dict(self._current_job)
            self._current_job["shortlist_pending"] = (
                int(self._current_job.get("id") or 0) in self._shortlist_intents
            )
            self.currentJobChanged.emit()

    @Slot(int, bool)
    def setJobShortlisted(self, job_id: int, selected: bool) -> None:  # noqa: N802
        if self.shutdownRequested:
            return
        revision = self._shortlist_revisions.get(job_id, 0) + 1
        self._shortlist_revisions[job_id] = revision
        self._shortlist_desired[job_id] = selected
        if not selected:
            intent = self._shortlist_intents.pop(job_id, None)
            if intent is not None:
                intent.set()
                self._update_shortlist_pending()
                return

        def inspected(needs_analysis):
            if revision != self._shortlist_revisions.get(job_id):
                return
            if not selected or not needs_analysis:

                def persist_decision():
                    if revision != self._shortlist_revisions.get(job_id):
                        return True
                    return self.service.set_job_shortlisted(job_id, selected=selected)

                self._mutate(
                    persist_decision,
                    requires_maintenance=True,
                    on_success=lambda ok: self._job_action_done(
                        job_id, "Top sélection mise à jour" if ok else "Modification impossible", ""
                    ),
                )
                return
            if job_id in self._shortlist_intents:
                return
            intent = Event()
            self._shortlist_intents[job_id] = intent
            self._update_shortlist_pending()

            def analyze(*, progress, stop_requested):
                if intent.is_set():
                    return {"shortlisted": 0, "cancelled": True}
                return self.service.analyze_and_shortlist(
                    [job_id],
                    progress=progress,
                    stop_requested=lambda: intent.is_set() or stop_requested(),
                )

            def done(report):
                if self._shortlist_intents.get(job_id) is intent:
                    self._shortlist_intents.pop(job_id, None)
                self._update_shortlist_pending()
                self._refresh_workflow()
                if intent.is_set() and report.get("shortlisted"):

                    def undo_cancelled_intent():
                        if self._shortlist_desired.get(job_id) is False:
                            return self.service.set_job_shortlisted(job_id, selected=False)
                        return True

                    self._mutate(
                        undo_cancelled_intent,
                        on_success=lambda _: self._refresh_workflow(),
                    )
                elif report.get("shortlisted"):
                    self.toastRequested.emit("Ajoutée à la Top sélection", "", "success")
                elif report.get("errors"):
                    self.toastRequested.emit(
                        "Top sélection indisponible",
                        self._failure_details(report["errors"]),
                        "warning",
                    )

            def cleanup():
                if self._shortlist_intents.get(job_id) is intent:
                    self._shortlist_intents.pop(job_id, None)
                    self._update_shortlist_pending()

            self._run(
                "Analyse avant ajout à la Top sélection",
                analyze,
                on_success=done,
                cancellable=True,
                task_key=f"shortlist:{job_id}:{id(intent)}",
                on_finished=cleanup,
            )

        self._mutate(self.service.shortlist_requires_analysis, job_id, on_success=inspected)

    @Slot(int, bool)
    def resolveDuplicate(self, job_id: int, same_offer: bool) -> None:  # noqa: N802
        """Persist the user's duplicate decision for one pending offer."""
        self._mutate(
            self.service.resolve_duplicate,
            job_id,
            same_offer=same_offer,
            on_success=lambda resolved: self._duplicate_resolved(job_id, same_offer, resolved),
        )

    def _duplicate_resolved(self, job_id: int, same_offer: bool, resolved: bool) -> None:
        if not resolved:
            self.toastRequested.emit(
                "Doublon déjà traité",
                "Cette décision n’est plus en attente.",
                "warning",
            )
            return
        self._refresh_workflow()
        self._refresh_selected(job_id)
        self.toastRequested.emit(
            "Offre regroupée" if same_offer else "Offres conservées séparément",
            "La décision a été enregistrée.",
            "success",
        )

    @Slot("QVariantList")
    def labelJobsAsTop(self, job_ids: list[Any]) -> None:  # noqa: N802
        """Add all selected active offers to the persisted Top selection."""
        ids = self._job_ids(job_ids)
        if not ids:
            self.toastRequested.emit(
                "Aucune offre sélectionnée",
                "Sélectionnez au moins une offre active.",
                "warning",
            )
            return
        for job_id in ids:
            self.setJobShortlisted(job_id, True)

    @Slot(int)
    def rescueJob(self, job_id: int) -> None:  # noqa: N802
        self._run(
            "Désarchivage et création de la candidature",
            self.service.rescue_job,
            job_id,
            cancellable=True,
            on_success=self._bulk_rescue_done,
        )

    @Slot("QVariantList")
    def rescueJobs(self, job_ids: list[Any]) -> None:  # noqa: N802
        ids = self._job_ids(job_ids)
        if not ids:
            self.toastRequested.emit(
                "Aucune offre sélectionnée",
                "Sélectionnez au moins une offre archivée.",
                "warning",
            )
            return
        self._run(
            "Désarchivage et création de " + _count_text(len(ids), "candidature", "candidatures"),
            self.service.rescue_jobs,
            ids,
            cancellable=True,
            on_success=self._bulk_rescue_done,
        )

    def _bulk_rescue_done(self, report: dict[str, Any]) -> None:
        self._refresh_workflow()
        generated = int(report.get("generated", 0))
        existing = int(report.get("existing", 0))
        skipped = int(report.get("skipped", 0))
        failed = int(report.get("failed", 0))
        message = _count_text(generated, "candidature créée", "candidatures créées")
        if existing:
            message += ", " + _count_text(
                existing, "dossier déjà disponible", "dossiers déjà disponibles"
            )
        if skipped:
            message += ", " + _count_text(skipped, "offre ignorée", "offres ignorées")
        if failed:
            message += ", " + _count_text(failed, "échec technique", "échecs techniques")
        duplicate_review_ids = list(report.get("duplicate_review_ids") or [])
        if duplicate_review_ids:
            message += ", " + _count_text(
                len(duplicate_review_ids), "doublon à vérifier", "doublons à vérifier"
            )
        warnings = [
            warning
            for warning in (report.get("warnings") or [])
            if not isinstance(warning, dict) or warning.get("category") != "duplicate_review"
        ]
        affected, corrected = _batch_review_counts(warnings)
        message += "."
        if failed:
            message += _technical_failure_preview(list(report.get("errors") or []))
        message += _review_notification_suffix(affected, corrected, batch=True)
        kind = "success" if generated or existing else "warning"
        if failed or affected:
            kind = "warning" if generated or existing else "danger"
        title = "Désarchivage terminé" if not failed else "Préparation incomplète"
        self._set_activity(title, message, kind)
        self.toastRequested.emit(title, message, kind)

    @Slot(int)
    def openApplication(self, application_id: int) -> None:  # noqa: N802
        self._start_read(
            "open_application",
            self.service.get_application,
            application_id,
            on_success=self._application_opened_in_jobs,
        )

    def _application_opened_in_jobs(self, detail: Any) -> None:
        if detail is None:
            self._error("Candidature indisponible", ValueError("Candidature introuvable"))
            return
        self.navigationRequested.emit(
            f"jobs?status={display_status(detail.status)}", int(detail.job_id)
        )
        self.selectJob(int(detail.job_id))

    @Slot(int, str, str, bool)
    def updateApplication(  # noqa: N802
        self,
        application_id: int,
        status: str,
        notes: str,
        form_submitted: bool,
    ) -> None:
        self._mutate(
            self.service.update_application,
            application_id,
            status=status or None,
            notes=notes,
            form_submitted=form_submitted,
            on_success=lambda _: self._operation_done("Suivi enregistré", ""),
        )

    @Slot(int, str, int)
    def saveApplicationNotes(self, application_id: int, notes: str, revision: int) -> None:  # noqa: N802
        self._notes_latest[application_id] = max(
            revision, self._notes_latest.get(application_id, -1)
        )

        def saved(ok):
            if not ok:
                failed("La candidature n’est plus disponible.", "")
                return
            previous = self._notes_failures.get(application_id)
            if previous is None or previous[1] <= revision:
                self._notes_failures.pop(application_id, None)
            self.applicationNotesSaved.emit(application_id, revision)

        def failed(message, trace):
            if revision >= self._notes_latest[application_id]:
                self._notes_failures[application_id] = (notes, revision)
            self.applicationNotesSaveFailed.emit(application_id, revision, message)
            self.toastRequested.emit("Notes non enregistrées", message, "danger")

        def persist_latest():
            if revision < self._notes_latest.get(application_id, revision):
                return True
            return self.service.save_application_notes(application_id, notes)

        self._mutate(persist_latest, on_success=saved, on_error=failed, allow_shutdown=True)

    @Slot()
    def retryNotesSave(self) -> None:  # noqa: N802
        self.notesFlushRequested.emit()
        for application_id, (notes, revision) in tuple(self._notes_failures.items()):
            if revision >= self._notes_latest.get(application_id, revision):
                self.saveApplicationNotes(application_id, notes, revision)
        QTimer.singleShot(0, self._check_shutdown)

    @Slot(str, str, str, str, str)
    def createManualApplication(  # noqa: N802
        self,
        title: str,
        company: str,
        location: str,
        description: str,
        application_url: str,
    ) -> None:
        if not title.strip() or not company.strip() or len(description.strip()) < 40:
            self.toastRequested.emit(
                "Offre incomplète",
                "Ajoutez le poste, l’entreprise et une description suffisamment détaillée.",
                "warning",
            )
            return
        self._mutate(
            self.service.add_manual_offer,
            title=title,
            company=company,
            location=location,
            description=description,
            application_url=application_url,
            on_success=self._manual_done,
        )

    def _manual_done(self, report: dict[str, Any]) -> None:
        self._refresh_workflow()
        ids = [
            int(job_id)
            for job_id in report.get("job_ids", [])
            if int(job_id) not in report.get("duplicate_review_ids", [])
        ]
        self.toastRequested.emit(
            "Offre enregistrée",
            "Traitement programmé."
            if ids
            else "Vérifiez le doublon avant de créer la candidature.",
            "success",
        )
        if ids:
            self.generateApplications(ids)

    @Slot(str, result=bool)
    def copyText(self, text: str) -> bool:  # noqa: N802
        clipboard = QGuiApplication.clipboard()
        if not text or clipboard is None:
            return False
        clipboard.setText(text)
        return True

    @Slot(str)
    def openUrl(self, url: str) -> None:  # noqa: N802
        if url and not QDesktopServices.openUrl(QUrl(url)):
            self.toastRequested.emit("Lien inaccessible", url, "danger")

    @Slot(str)
    def openPath(self, raw_path: str) -> None:  # noqa: N802
        if not raw_path:
            return
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            self.toastRequested.emit("Fichier introuvable", str(path), "danger")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            self.toastRequested.emit("Ouverture impossible", str(path), "danger")

    @Slot()
    def openProfileFolder(self) -> None:  # noqa: N802
        """Open the active profile directory directly in Finder."""
        try:
            self.openPath(str(self.service.ensure_profile_directory()))
        except Exception as exc:
            self._error("Dossier du profil inaccessible", exc)

    @Slot(int)
    def openApplicationFolder(self, application_id: int) -> None:  # noqa: N802
        """Open the folder belonging to the explicitly selected application."""
        if application_id <= 0:
            return
        path = self.service.application_output_directory(application_id)
        if path is None:
            self.toastRequested.emit(
                "Candidature introuvable",
                "Le dossier sélectionné n’existe plus dans la base.",
                "danger",
            )
            return
        self.openPath(str(path))

    def _run(
        self,
        label,
        fn,
        *args,
        on_success=None,
        on_finished=None,
        cancellable=False,
        task_key=None,
        **kwargs,
    ):
        if self.shutdownRequested:
            return
        key = task_key or repr((getattr(fn, "__name__", str(fn)), args, sorted(kwargs.items())))
        if key in self._long_keys:
            if key != "maintenance" and self._maintenance_pending:
                self._schedule_maintenance()
            return
        self._long_keys.add(key)
        item = (key, label, fn, args, kwargs, on_success, on_finished, cancellable)
        if key == "maintenance":
            self._long_queue.appendleft(item)
        else:
            self._long_queue.append(item)
            if self._maintenance_pending:
                self._schedule_maintenance()
        self.busyChanged.emit()
        self._launch_next()

    def _schedule_maintenance(self):
        self._maintenance_failed = False
        self._run(
            "Vérification des anciennes offres",
            self.service.startup_maintenance,
            on_success=self._maintenance_done,
            cancellable=True,
            task_key="maintenance",
        )

    def _maintenance_done(self, complete):
        self._maintenance_pending = not complete
        self._refresh_workflow()
        if complete:
            pending, self._maintenance_mutations = self._maintenance_mutations, []
            for fn, args, kwargs, on_success, on_error in pending:
                self._mutate(fn, *args, on_success=on_success, on_error=on_error, **kwargs)
        elif not self._active_worker.cancel_requested:
            self._maintenance_retry = True

    def _launch_next(self):
        if self._active_worker is not None or self.shutdownRequested or not self._long_queue:
            return
        if self._maintenance_pending and self._long_queue[0][0] != "maintenance":
            return
        key, label, fn, args, kwargs, on_success, on_finished, cancellable = (
            self._long_queue.popleft()
        )
        self._active_key = key
        self._busy = True
        self._busy_label = label
        self.busyChanged.emit()
        worker = TaskWorker(fn, *args, with_progress=True, with_cancel=cancellable, **kwargs)
        self._active_worker = worker
        self._workers.add(worker)
        worker.signals.progress.connect(self._set_progress)
        if on_success:
            worker.signals.result.connect(on_success)
        worker.signals.error.connect(
            lambda message, trace: self._long_task_error(worker, key, message, trace)
        )
        if on_finished:
            worker.signals.finished.connect(on_finished)
        worker.signals.finished.connect(lambda: self._task_finished(worker))
        self._thread_pool.start(worker)

    def _mutate(
        self,
        fn,
        *args,
        on_success=None,
        on_error=None,
        allow_shutdown=False,
        requires_maintenance=False,
        **kwargs,
    ):
        if self.shutdownRequested and not allow_shutdown:
            return
        if self._maintenance_pending and (
            requires_maintenance
            or getattr(fn, "__name__", "")
            in {
                "add_manual_offer",
                "set_job_shortlisted",
                "resolve_duplicate",
                "update_shortlist",
            }
        ):
            self._maintenance_mutations.append((fn, args, kwargs, on_success, on_error))
            self._schedule_maintenance()
            self.toastRequested.emit(
                "Opération en attente",
                "La vérification des anciennes offres doit se terminer.",
                "neutral",
            )
            return
        worker = TaskWorker(fn, *args, **kwargs)
        self._workers.add(worker)
        if on_success:
            worker.signals.result.connect(on_success)
        worker.signals.error.connect(on_error or self._task_error)

        def finished():
            self._workers.discard(worker)
            self._check_shutdown()

        worker.signals.finished.connect(finished)
        self._mutation_pool.start(worker)

    def _start_read(self, key, fn, *args, on_success, on_finished=None, **kwargs):
        """One active read and one newest pending request per view."""
        if self.shutdownRequested:
            return
        token = self._read_tokens.get(key, 0) + 1
        self._read_tokens[key] = token
        request = (token, fn, args, kwargs, on_success, on_finished)
        if key in self._reads_active:
            self._reads_pending[key] = request
        else:
            self._launch_read(key, request)

    def _launch_read(self, key, request):
        token, fn, args, kwargs, on_success, on_finished = request
        worker = TaskWorker(fn, *args, **kwargs)
        self._read_workers.add(worker)
        self._reads_active[key] = worker

        def result(value):
            if self._read_tokens.get(key) == token and not self.shutdownRequested:
                on_success(value)

        def error(message, trace):
            if self._read_tokens.get(key) == token and not self.shutdownRequested:
                self._task_error(message, trace)

        def finished():
            self._read_workers.discard(worker)
            self._reads_active.pop(key, None)
            pending = self._reads_pending.pop(key, None)
            if pending is not None and not self.shutdownRequested:
                self._launch_read(key, pending)
            elif on_finished:
                on_finished()
            self._check_shutdown()

        worker.signals.result.connect(result)
        worker.signals.error.connect(error)
        worker.signals.finished.connect(finished)
        self._read_pool.start(worker)

    @Slot()
    def requestShutdown(self) -> None:  # noqa: N802
        if self.shutdownRequested:
            return
        self._shutdown_requested = True
        self.shutdownRequestedChanged.emit()
        self._workflow_refresh_timer.stop()
        self.cancelCurrentTask()
        self._reads_pending.clear()
        self._maintenance_mutations.clear()
        self.notesFlushRequested.emit()
        QTimer.singleShot(0, self._check_shutdown)

    def _check_shutdown(self):
        if (
            self.shutdownRequested
            and not self.shutdownReady
            and not self._workers
            and not self._read_workers
            and not self._notes_failures
        ):
            self._shutdown_ready = True
            self._unsubscribe_resources()
            self._unsubscribe_database()
            self.shutdownReadyChanged.emit()

    @Slot(str)
    def _set_progress(self, label: str) -> None:
        if label == self._busy_label:
            return
        self._busy_label = label
        self.busyChanged.emit()

    def _long_task_error(self, worker, key, message, trace):
        if worker.cancel_requested:
            self._set_activity("Opération annulée", message, "warning")
            self.toastRequested.emit("Opération annulée", message, "warning")
            return
        if key == "maintenance":
            self._maintenance_failed = True
            self.busyChanged.emit()
        self._task_error(message, trace)

    @Slot(str, str)
    def _task_error(self, message: str, trace: str) -> None:
        logger.error("Desktop task failed: %s\n%s", message, trace)
        self._set_activity("Échec de l’opération", message, "danger")
        self.toastRequested.emit("Échec de l’opération", message, "danger")

    def _task_finished(self, worker):
        self._workers.discard(worker)
        if self._active_worker is worker:
            self._active_worker = None
            self._long_keys.discard(self._active_key)
            self._busy = False
            self._busy_label = ""
            self.busyChanged.emit()
        if self._maintenance_retry and not self.shutdownRequested:
            self._maintenance_retry = False
            self._schedule_maintenance()
        self._launch_next()
        self._check_shutdown()

    def _set_activity(
        self,
        title: str,
        message: str,
        kind: str,
        fetched: int = 0,
        persisted: int = 0,
    ) -> None:
        self._activity = {
            "title": title,
            "message": message,
            "kind": kind,
            "fetched": fetched,
            "persisted": persisted,
        }
        self.activityChanged.emit()

    def _operation_done(self, title: str, message: str) -> None:
        self._set_activity(title, message, "success")
        self._refresh_workflow()
        self.toastRequested.emit(title, message, "success")

    def _job_action_done(self, job_id: int, title: str, message: str) -> None:
        self._operation_done(title, message)
        self._refresh_selected(job_id)

    def _refresh_selected(self, job_id):
        if job_id == getattr(self, "_selected_job_id", 0):
            self.refreshSelectedJob()

    @Slot(object)
    def _database_changed(self, tables):
        if self.shutdownRequested:
            return
        if set(tables) & {
            "jobs",
            "job_scores",
            "job_analyses",
            "applications",
            "generated_documents",
        }:
            self._dirty_views.update({"dashboard", "shortlist", "jobs", "detail"})
        if "llm_usage" in tables:
            self._dirty_views.add("dashboard")
        if "companies" in tables:
            self._dirty_views.add("companies")
        if self._dirty_views:
            self._workflow_refresh_timer.start()

    def _refresh_workflow(self):
        if self.shutdownRequested:
            return
        self._dirty_views.update({"dashboard", "shortlist", "jobs", "detail"})
        self._workflow_refresh_timer.start()

    def _flush_refresh(self):
        if self.shutdownRequested:
            return
        if "shortlist" in self._dirty_views:
            self._dirty_views.discard("shortlist")
            self.refreshShortlist()
        if self._active_route == "companies" and "companies" in self._dirty_views:
            self._dirty_views.discard("companies")
            self.loadCompanies()
        if self._active_route == "dashboard" and "dashboard" in self._dirty_views:
            self._dirty_views.discard("dashboard")
            self.refreshDashboard()
        if self._active_route in {"jobs", "duplicates"}:
            if "jobs" in self._dirty_views:
                self._dirty_views.discard("jobs")
                self.refreshJobs()
            if "detail" in self._dirty_views:
                self._dirty_views.discard("detail")
                if self._selected_job_id:
                    self.refreshSelectedJob()

    @staticmethod
    def _job_ids(values: list[Any]) -> list[int]:
        result: list[int] = []
        seen: set[int] = set()
        for value in values:
            try:
                job_id = int(value)
            except (TypeError, ValueError):
                continue
            if job_id > 0 and job_id not in seen:
                seen.add(job_id)
                result.append(job_id)
        return result

    def _error(self, title: str, exc: Exception) -> None:
        logger.exception("%s", title)
        self.toastRequested.emit(title, str(exc), "danger")
