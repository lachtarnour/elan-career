"""Desktop facade for Batch analysis, kept separate from offer browsing."""

from smartapply.config import get_llm_settings
from smartapply.database.session import ensure_startup_maintenance_complete
from smartapply.llm.routes import resolve_route
from smartapply.pipeline.batch_analysis.coordinator import BatchAnalysisCoordinator
from smartapply.pipeline.batch_analysis.presentation import analysis_workspace
from smartapply.pipeline.errors import TaskCancelledError


class AnalysisServiceMixin:
    def analysis_workspace(self):
        return analysis_workspace()

    def sync_analysis_batches(self):
        BatchAnalysisCoordinator().sync()
        return analysis_workspace()

    def start_analysis_batch(self, job_ids, *, progress=None, stop_requested=None):
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
            return analysis_workspace()
        workspace = analysis_workspace()
        if run_id:
            workspace["started_run_id"] = run_id
        return workspace

    def start_immediate_analysis(self, job_ids, *, stop_requested=None):
        ensure_startup_maintenance_complete()
        settings = get_llm_settings()
        for purpose in ("job_scope_screening", "job_offer_extraction", "job_fit_assessment"):
            resolve_route(settings, purpose)
        ids = list(dict.fromkeys(int(job_id) for job_id in job_ids if int(job_id) > 0))
        if not ids:
            raise ValueError("Sélectionnez au moins une offre à analyser.")
        from smartapply.pipeline import Pipeline

        pipeline = Pipeline(stop_requested=stop_requested)
        pipeline.prepare_analysis(job_ids=ids)
        report = pipeline.analyze_jobs(ids)
        return {"workspace": analysis_workspace(), "report": report}

    def retry_analysis_batch(self, run_id):
        ensure_startup_maintenance_complete()
        BatchAnalysisCoordinator().retry(run_id)
        return analysis_workspace()

    def cancel_analysis_batch(self, run_id):
        BatchAnalysisCoordinator().cancel(run_id)
        return analysis_workspace()
