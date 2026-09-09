"""Qt Quick bridge between Élan's product UI and its business services."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QObject, QThreadPool, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication

from smartapply.database.models import JobStatus
from smartapply.desktop.services import DesktopService, SearchResult
from smartapply.desktop.workers import TaskWorker

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


def _tone(status: str) -> str:
    if status in {JobStatus.SENT, JobStatus.INTERVIEW}:
        return "success"
    if status in {JobStatus.QUALITY_REJECTED, JobStatus.REJECTED}:
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

    def __init__(self, service: DesktopService | None = None) -> None:
        super().__init__()
        self.service = service or DesktopService()
        self.service.initialize()
        self._thread_pool = QThreadPool.globalInstance()
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
        self._workflow_refresh_timer.setInterval(600)
        self._workflow_refresh_timer.timeout.connect(self._refresh_workflow)
        self._jobs_loading = False
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
        self.refreshDiagnostics()

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
        normalized = self.service.set_shortlist_top_k(value)
        if normalized == self._shortlist_top_k:
            return normalized
        self._shortlist_top_k = normalized
        self.shortlistTopKChanged.emit()
        return normalized

    @Property("QVariantList", notify=jobsChanged)
    def jobs(self) -> list[dict[str, Any]]:
        return self._jobs

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
        if self._companies_saving:
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

        worker.signals.result.connect(saved)
        worker.signals.error.connect(failed)
        worker.signals.finished.connect(finished)
        self._thread_pool.start(worker)

    @Property("QVariantMap", notify=currentJobChanged)
    def currentJob(self) -> dict[str, Any]:  # noqa: N802 - Qt property name
        return self._current_job

    @Property("QVariantMap", notify=profileChanged)
    def profile(self) -> dict[str, Any]:
        return self._profile

    @Property("QVariantMap", notify=diagnosticsChanged)
    def diagnostics(self) -> dict[str, Any]:
        return self._diagnostics

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
        return bool(self._active_worker and self._active_worker.with_cancel)

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
        if not self._jobs_loading:
            self._jobs_loading = True
            self.jobsLoadingChanged.emit()
        self._start_read(
            "jobs",
            self.service.list_jobs,
            search=search,
            status=status or None,
            sort_key=sort_key,
            sort_ascending=sort_ascending,
            on_success=self._jobs_loaded,
            on_finished=self._jobs_read_finished,
        )

    @Slot()
    def refreshJobs(self) -> None:  # noqa: N802
        """Refresh offers without resetting the current page state."""
        self.loadJobs(
            self._job_search,
            self._job_status or JobStatus.SCRAPED,
            self._job_sort_key,
            self._job_sort_ascending,
        )

    def _jobs_loaded(self, rows: list[Any]) -> None:
        self._jobs = _enrich_rows(rows)
        self.jobsChanged.emit()
        visible_ids = {int(row["id"]) for row in self._jobs}
        current_id = self._selected_job_id or int(self._current_job.get("id") or 0)
        if self._jobs and current_id not in visible_ids:
            self.selectJob(int(self._jobs[0]["id"]))
        elif not self._jobs:
            self._current_job = {}
            self.currentJobChanged.emit()

    def _jobs_read_finished(self) -> None:
        if self._jobs_loading:
            self._jobs_loading = False
            self.jobsLoadingChanged.emit()

    @Slot(int)
    def selectJob(self, job_id: int) -> None:  # noqa: N802
        self._selected_job_id = job_id
        if not self._job_detail_loading:
            self._job_detail_loading = True
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
    def refreshDiagnostics(self) -> None:  # noqa: N802
        if self._diagnostics_worker is not None:
            return
        try:
            self._apply_diagnostics(self.service.diagnostics())
        except Exception as exc:
            self._error("Configuration indisponible", exc)
            return

        worker = TaskWorker(self.service.diagnostics, check_sources=True)
        self._diagnostics_worker = worker
        self._workers.add(worker)
        worker.signals.result.connect(self._apply_diagnostics)
        worker.signals.error.connect(self._diagnostics_error)
        worker.signals.finished.connect(lambda: self._diagnostics_finished(worker))
        self._thread_pool.start(worker)

    def _apply_diagnostics(self, snapshot: Any) -> None:
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
        self._diagnostics = diagnostics
        self.diagnosticsChanged.emit()

    def _diagnostics_error(self, message: str, trace: str) -> None:
        logger.error("Source diagnostics failed: %s\n%s", message, trace)
        diagnostics = dict(self._diagnostics)
        sources = []
        for source in diagnostics.get("sources", []):
            item = dict(source)
            if item.get("state") == "checking":
                item.update(
                    ready=False,
                    state="unavailable",
                    message="Vérification impossible.",
                )
            sources.append(item)
        diagnostics["sources"] = sources
        diagnostics["source_ready"] = {
            str(item.get("key")): bool(item.get("ready")) for item in sources
        }
        self._diagnostics = diagnostics
        self.diagnosticsChanged.emit()

    def _diagnostics_finished(self, worker: TaskWorker) -> None:
        self._workers.discard(worker)
        if self._diagnostics_worker is worker:
            self._diagnostics_worker = None

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
        worker = self._active_worker
        if worker is None or not worker.with_cancel:
            return
        worker.cancel()
        self._busy_label = "Annulation en cours…"
        self.busyChanged.emit()

    @Slot(int)
    def processPending(self, top_k: int) -> None:  # noqa: N802
        normalized = self.setAnalysisTopK(top_k)
        self._run(
            "Analyse en cours",
            self.service.process_pending,
            on_success=self._process_done,
            top_k=normalized,
        )

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
            on_success=lambda report: self._analysis_done(job_id, report),
        )

    def _analysis_done(self, job_id: int, report: dict[str, Any]) -> None:
        errors = list(report.get("errors") or [])
        if errors:
            message = self._failure_details(errors)
            self._set_activity("Analyse échouée", message, "danger")
            self._refresh_workflow()
            self.selectJob(job_id)
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
            on_success=self._generation_done,
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
        self._run(
            "Génération des candidatures de la Top sélection",
            self.service.generate_shortlisted_applications,
            on_success=self._bulk_generation_done,
        )

    def _bulk_generation_done(self, report: dict[str, Any]) -> None:
        self._refresh_workflow()
        self.jobSelectionClearRequested.emit()
        generated = int(report.get("generated", 0))
        skipped = int(report.get("skipped", 0))
        failed = int(report.get("failed", 0))
        details = _count_text(generated, "candidature créée", "candidatures créées")
        if skipped:
            details += ", " + _count_text(skipped, "offre ignorée", "offres ignorées")
        if failed:
            details += ", " + _count_text(failed, "échec", "échecs")
        errors = list(report.get("errors") or [])
        if errors:
            details += ". Détails : " + self._failure_details(errors)
        warnings = list(report.get("warnings") or [])
        if warnings:
            details += ". Points à vérifier : " + self._failure_details(warnings)
        kind = (
            "success"
            if generated and not failed and not warnings
            else ("warning" if generated else "danger")
        )
        self._set_activity("Création terminée", details + ".", kind)
        self.toastRequested.emit(
            "Création terminée",
            details + ".",
            kind,
        )

    def _generation_done(self, report: dict[str, Any]) -> None:
        self._refresh_workflow()
        if report.get("existing"):
            message = "Le CV et la lettre sont disponibles dans ce dossier."
            self._set_activity("Candidature existante", message, "neutral")
            self.toastRequested.emit("Candidature existante", message, "neutral")
            self.openApplication(int(report["application_id"]))
            return
        job_id = int(report.get("job_id") or 0)
        warnings = list(report.get("validation_warnings") or [])
        errors = list(report.get("validation_errors") or [])
        issues = [*warnings, *errors]
        message = ""
        kind = "success"
        if issues:
            message = "Points à vérifier : " + " · ".join(str(item) for item in issues[:3])
            kind = "warning"
        self._set_activity("Candidature créée", message, kind)
        self.toastRequested.emit("Candidature créée", message, kind)
        if job_id:
            self.selectJob(job_id)
        self.navigationRequested.emit("jobs", job_id)

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
        try:
            self.service.archive_job(job_id)
            self._current_job = {}
            self.currentJobChanged.emit()
            self._refresh_workflow()
            self.toastRequested.emit("Offre archivée", "", "neutral")
        except Exception as exc:
            self._error("Archivage impossible", exc)

    @Slot(int)
    def markJobSent(self, job_id: int) -> None:  # noqa: N802
        try:
            if not self.service.mark_job_sent(job_id):
                self.toastRequested.emit(
                    "Envoi impossible",
                    "Créez d’abord la candidature.",
                    "warning",
                )
                return
            self._refresh_workflow()
            self.selectJob(job_id)
            self.toastRequested.emit("Offre marquée comme envoyée", "", "success")
        except Exception as exc:
            self._error("Mise à jour impossible", exc)

    @Slot(int)
    def archiveApplication(self, application_id: int) -> None:  # noqa: N802
        try:
            if not self.service.archive_application(application_id):
                self.toastRequested.emit(
                    "Archivage impossible", "Cette candidature n’est plus disponible.", "warning"
                )
                return
            self._current_job = {}
            self.currentJobChanged.emit()
            self._refresh_workflow()
            self.toastRequested.emit("Candidature archivée", "", "neutral")
        except Exception as exc:
            self._error("Archivage impossible", exc)

    @Slot(int, bool)
    def setJobShortlisted(self, job_id: int, selected: bool) -> None:  # noqa: N802
        try:
            if not self.service.set_job_shortlisted(job_id, selected=selected):
                self.toastRequested.emit(
                    "Modification impossible",
                    "Cette offre n’est plus disponible.",
                    "warning",
                )
                return
            self._refresh_workflow()
            self.selectJob(job_id)
            title = "Ajoutée à la Top sélection" if selected else "Retirée de la Top sélection"
            self.toastRequested.emit(title, "", "success" if selected else "neutral")
        except Exception as exc:
            self._error("Top sélection indisponible", exc)

    @Slot(int, bool)
    def resolveDuplicate(self, job_id: int, same_offer: bool) -> None:  # noqa: N802
        """Persist the user's duplicate decision for one pending offer."""
        self._run(
            "Validation du doublon",
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
        self.selectJob(job_id)
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
        try:
            labelled = sum(
                self.service.set_job_shortlisted(job_id, selected=True) for job_id in ids
            )
            self._refresh_workflow()
            self.jobSelectionClearRequested.emit()
            self.toastRequested.emit(
                "Top sélection mise à jour",
                _count_text(labelled, "offre ajoutée", "offres ajoutées") + ".",
                "success" if labelled else "warning",
            )
        except Exception as exc:
            self._error("Top sélection indisponible", exc)

    @Slot(int)
    def rescueJob(self, job_id: int) -> None:  # noqa: N802
        self._run(
            "Désarchivage et création de la candidature",
            self.service.rescue_job,
            job_id,
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
            on_success=self._bulk_rescue_done,
        )

    def _bulk_rescue_done(self, report: dict[str, Any]) -> None:
        self._refresh_workflow()
        self.jobSelectionClearRequested.emit()
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
            message += ", " + _count_text(failed, "échec", "échecs")
        errors = list(report.get("errors") or [])
        if errors:
            message += ". Détails : " + self._failure_details(errors)
        warnings = list(report.get("warnings") or [])
        if warnings:
            message += ". Points à vérifier : " + self._failure_details(warnings)
        kind = "success" if generated or existing else "warning"
        if failed or warnings:
            kind = "warning" if generated or existing else "danger"
        title = "Désarchivage terminé" if not failed else "Préparation incomplète"
        self._set_activity(title, message + ".", kind)
        self.toastRequested.emit(title, message + ".", kind)
        # Leave the archive filter and select the prepared dossier even when
        # the clicked row was an alias of a different source.
        application_ids = list(report.get("application_ids") or [])
        if int(report.get("requested", 0)) == 1 and application_ids:
            self.openApplication(int(application_ids[0]))

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
        self.navigationRequested.emit(f"jobs?status={detail.status}", int(detail.job_id))
        self.selectJob(int(detail.job_id))

    @Slot(int, str, str, bool)
    def updateApplication(  # noqa: N802
        self,
        application_id: int,
        status: str,
        notes: str,
        form_submitted: bool,
    ) -> None:
        try:
            self.service.update_application(
                application_id,
                status=status or None,
                notes=notes,
                form_submitted=form_submitted,
            )
            self._refresh_workflow()
            self._start_read(
                "application_after_update",
                self.service.get_application,
                application_id,
                on_success=self._application_updated_in_jobs,
            )
            self.toastRequested.emit("Suivi enregistré", "", "success")
        except Exception as exc:
            self._error("Mise à jour impossible", exc)

    def _application_updated_in_jobs(self, detail: Any) -> None:
        if detail is not None:
            self.selectJob(int(detail.job_id))

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
        self._run(
            "Création de la candidature",
            self.service.create_manual_application,
            title=title,
            company=company,
            location=location,
            description=description,
            application_url=application_url,
            on_success=self._manual_done,
        )

    def _manual_done(self, report: dict[str, Any]) -> None:
        self._generation_done(report)

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
        label: str,
        fn: Callable[..., Any],
        *args: Any,
        on_success: Callable[[Any], None] | None = None,
        cancellable: bool = False,
        **kwargs: Any,
    ) -> None:
        if self._busy:
            self.toastRequested.emit(
                "Traitement en cours",
                "Attendez la fin de l’opération avant d’en lancer une autre.",
                "warning",
            )
            return
        self._busy = True
        self._busy_label = label
        self.busyChanged.emit()
        self._workflow_refresh_timer.start()
        worker = TaskWorker(
            fn,
            *args,
            with_progress=True,
            with_cancel=cancellable,
            **kwargs,
        )
        self._active_worker = worker
        self._workers.add(worker)
        worker.signals.progress.connect(self._set_progress)
        if on_success:
            worker.signals.result.connect(on_success)
        worker.signals.error.connect(self._task_error)
        worker.signals.finished.connect(lambda: self._task_finished(worker))
        self._thread_pool.start(worker)

    def _start_read(
        self,
        key: str,
        fn: Callable[..., Any],
        *args: Any,
        on_success: Callable[[Any], None],
        on_finished: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """Run a read-only UI operation without blocking or toggling busy state."""
        token = self._read_tokens.get(key, 0) + 1
        self._read_tokens[key] = token
        worker = TaskWorker(fn, *args, **kwargs)
        self._read_workers.add(worker)

        def apply_result(value: Any) -> None:
            if self._read_tokens.get(key) == token:
                on_success(value)

        worker.signals.result.connect(apply_result)
        worker.signals.error.connect(self._task_error)

        def finish() -> None:
            self._read_workers.discard(worker)
            if self._read_tokens.get(key) == token and on_finished:
                on_finished()

        worker.signals.finished.connect(finish)
        self._thread_pool.start(worker)

    @Slot(str)
    def _set_progress(self, label: str) -> None:
        if label == self._busy_label:
            return
        self._busy_label = label
        self.busyChanged.emit()

    @Slot(str, str)
    def _task_error(self, message: str, trace: str) -> None:
        logger.error("Desktop task failed: %s\n%s", message, trace)
        self._set_activity("Échec de l’opération", message, "danger")
        self.toastRequested.emit("Échec de l’opération", message, "danger")

    def _task_finished(self, worker: TaskWorker) -> None:
        self._workers.discard(worker)
        if self._active_worker is worker:
            self._active_worker = None
        if self._workflow_refresh_timer.isActive():
            self._workflow_refresh_timer.stop()
        self._busy = False
        self._busy_label = ""
        self.busyChanged.emit()

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
        self.selectJob(job_id)

    def _refresh_workflow(self) -> None:
        self.refreshDashboard()
        self.refreshShortlist()
        self.loadJobs(
            self._job_search,
            self._job_status,
            self._job_sort_key,
            self._job_sort_ascending,
        )

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
