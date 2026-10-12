"""Desktop facade for Batch analysis, kept separate from offer browsing."""

from smartapply.config import get_llm_settings
from smartapply.database import session_scope
from smartapply.database.session import ensure_startup_maintenance_complete
from smartapply.llm.routes import resolve_route
from smartapply.pipeline.batch_analysis.coordinator import BatchAnalysisCoordinator
from smartapply.pipeline.batch_analysis.presentation import analysis_workspace
from smartapply.pipeline.errors import TaskCancelledError
from smartapply.pipeline.process.analysis_admission import reserved_analysis_job_ids


class AnalysisServiceMixin:
    def analysis_workspace_view(self, **view):
        from smartapply.desktop.workspace_views import analysis_workspace_view

        return analysis_workspace_view(**view)

    def sync_analysis_batches_view(self, **view):
        BatchAnalysisCoordinator().sync()
        return self.analysis_workspace_view(**view)

    def _analysis_result(self, view=None, run_id=None):
        if view is None:
            return analysis_workspace()
        options = dict(view)
        if run_id:
            if not options.get("selected_run_id"):
                options["selected_run_id"] = run_id
            options["monitor_run_ids"] = [*options.get("monitor_run_ids", []), run_id]
        return self.analysis_workspace_view(**options)

    def analysis_workspace(self):
        return analysis_workspace()

    def sync_analysis_batches(self):
        BatchAnalysisCoordinator().sync()
        return analysis_workspace()

    def start_analysis_batch(self, job_ids, *, progress=None, stop_requested=None, view=None):
        ensure_startup_maintenance_complete()
        settings = get_llm_settings()
        for purpose in ("job_scope_screening", "job_offer_extraction", "job_fit_assessment"):
            if resolve_route(settings, purpose).provider not in {"openai", "anthropic"}:
                raise ValueError("Le traitement Batch nécessite OpenAI ou Anthropic.")
        from smartapply.pipeline import Pipeline

        if progress:
            progress("Préparation du lot d’analyse…")
        ids = list(dict.fromkeys(int(job_id) for job_id in job_ids if int(job_id) > 0))
        if not ids:
            raise ValueError("Sélectionnez au moins une offre à analyser.")
        try:
            run_id = BatchAnalysisCoordinator(
                pipeline=Pipeline(stop_requested=stop_requested)
            ).start(ids)
        except TaskCancelledError:
            return self._analysis_result(view)
        workspace = self._analysis_result(view, run_id)
        if run_id:
            workspace["started_run_id"] = run_id
        return workspace

    def start_immediate_analysis(self, job_ids, *, stop_requested=None, view=None):
        ensure_startup_maintenance_complete()
        ids = list(dict.fromkeys(int(job_id) for job_id in job_ids if int(job_id) > 0))
        if not ids:
            raise ValueError("Sélectionnez au moins une offre à analyser.")
        settings = get_llm_settings()
        for purpose in ("job_scope_screening", "job_offer_extraction", "job_fit_assessment"):
            resolve_route(settings, purpose)
        with session_scope() as session:
            if set(ids) & reserved_analysis_job_ids(session):
                raise ValueError(
                    "Une offre sélectionnée est déjà réservée par un Batch en cours. "
                    "Attendez sa fin ou annulez ce Batch avant de lancer l’analyse immédiate."
                )
        from smartapply.pipeline import Pipeline

        pipeline = Pipeline(stop_requested=stop_requested)
        pipeline.prepare_analysis(job_ids=ids)
        report = pipeline.analyze_jobs(ids)
        return {"workspace": self._analysis_result(view), "report": report}

    def retry_analysis_batch(self, run_id, *, view=None):
        ensure_startup_maintenance_complete()
        BatchAnalysisCoordinator().retry(run_id)
        return self._analysis_result(view, run_id)

    def cancel_analysis_batch(self, run_id, *, view=None):
        BatchAnalysisCoordinator().cancel(run_id)
        return self._analysis_result(view, run_id)
