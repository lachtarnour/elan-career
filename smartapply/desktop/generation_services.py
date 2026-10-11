"""Desktop facade for durable application generation runs."""

from smartapply.database.session import ensure_startup_maintenance_complete


class GenerationServiceMixin:
    def generation_workspace_view(self, **view):
        from smartapply.desktop.workspace_views import generation_workspace_view

        return generation_workspace_view(**view)

    def sync_generation_runs_view(self, **view):
        from smartapply.pipeline.generation.coordinator import GenerationCoordinator

        GenerationCoordinator().sync()
        return self.generation_workspace_view(**view)

    def _generation_result(self, view=None, run_id=None):
        if view is None:
            return self.generation_workspace()
        options = dict(view)
        if run_id:
            if not options.get("selected_run_id"):
                options["selected_run_id"] = run_id
            options["monitor_run_ids"] = [*options.get("monitor_run_ids", []), run_id]
        return self.generation_workspace_view(**options)

    def generation_workspace(self):
        from smartapply.pipeline.generation.presentation import generation_workspace

        return generation_workspace()

    def sync_generation_runs(self):
        from smartapply.pipeline.generation.coordinator import GenerationCoordinator

        GenerationCoordinator().sync()
        return self.generation_workspace()

    def _start_generation(self, job_ids, mode, *, progress=None, stop_requested=None, view=None):
        from smartapply.pipeline.generation.coordinator import GenerationCoordinator

        ensure_startup_maintenance_complete()
        ids = list(dict.fromkeys(int(job_id) for job_id in job_ids if int(job_id) > 0))
        if not ids:
            raise ValueError("Sélectionnez au moins une offre analysée à préparer.")
        if progress:
            progress("Préparation des candidatures…")
        run_id = GenerationCoordinator().start(
            ids, mode=mode, stop_requested=stop_requested, progress=progress,
        )
        workspace = self._generation_result(view, run_id)
        if run_id:
            workspace["started_run_id"] = run_id
        return workspace

    def start_generation_batch(self, job_ids, *, progress=None, stop_requested=None, view=None):
        return self._start_generation(
            job_ids, "batch", progress=progress, stop_requested=stop_requested, view=view,
        )

    def start_immediate_generation(self, job_ids, *, progress=None, stop_requested=None, view=None):
        return self._start_generation(
            job_ids, "immediate", progress=progress, stop_requested=stop_requested, view=view,
        )

    def retry_generation_run(self, run_id, *, stop_requested=None, view=None):
        from smartapply.pipeline.generation.coordinator import GenerationCoordinator

        ensure_startup_maintenance_complete()
        GenerationCoordinator().retry(run_id, stop_requested=stop_requested)
        return self._generation_result(view, run_id)

    def cancel_generation_run(self, run_id, *, view=None):
        from smartapply.pipeline.generation.coordinator import GenerationCoordinator

        GenerationCoordinator().cancel(run_id)
        return self._generation_result(view, run_id)

    def abandon_generation_run(self, run_id, *, view=None):
        from smartapply.pipeline.generation.coordinator import GenerationCoordinator

        GenerationCoordinator().abandon(run_id)
        return self._generation_result(view, run_id)
