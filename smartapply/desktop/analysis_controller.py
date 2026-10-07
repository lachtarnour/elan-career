"""Background Batch monitoring independent of the Offers page and long tasks."""

from __future__ import annotations

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal

from smartapply.desktop.workers import TaskWorker


class AnalysisController(QObject):
    changed = Signal()
    loadingChanged = Signal()
    errorChanged = Signal()
    operationChanged = Signal()

    def __init__(self, bridge):
        super().__init__(bridge)
        self.bridge = bridge
        self.workspace = {"candidates": [], "runs": [], "active_run_id": ""}
        self.loading = False
        self.error = ""
        self.operation = ""
        self._immediate_worker = None
        self._batch_start_worker = None
        self._cancel_started_run_id = None
        self._pending_read = False
        self._control_busy = False
        self._pending_cancellations = []
        self._monitor_pool = QThreadPool(self)
        self._monitor_pool.setMaxThreadCount(1)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._timer = QTimer(self)
        self._timer.setInterval(120_000)
        self._timer.timeout.connect(self.refresh)
        # Reconcile stored provider IDs even if this page is never opened.
        if hasattr(bridge.service, "sync_analysis_batches"):
            QTimer.singleShot(0, self.refresh)

    def refresh(self, *, sync=True):
        if self.bridge.shutdownRequested:
            return
        if self._control_busy:
            self._pending_read = self._pending_read or not sync
            return
        method = "sync_analysis_batches" if sync else "analysis_workspace"
        if hasattr(self.bridge.service, method):
            self._execute(getattr(self.bridge.service, method), operation="refresh")

    def start(self, ids):
        self._execute(
            self.bridge.service.start_analysis_batch,
            ids,
            operation="start",
            on_result=self._batch_loaded,
            cancellable=True,
        )

    def cancel_batch_start(self):
        if self.operation == "start" and self._batch_start_worker is not None:
            self._batch_start_worker.cancel()

    def start_immediate(self, ids):
        self._execute(
            self.bridge.service.start_immediate_analysis,
            ids,
            operation="immediate",
            on_result=self._immediate_loaded,
            cancellable=True,
        )

    def cancel_immediate(self):
        if self._immediate_worker is not None:
            self._immediate_worker.cancel()

    def retry(self, run_id):
        self._execute(self.bridge.service.retry_analysis_batch, run_id, operation="retry")

    def cancel(self, run_id):
        if self._control_busy:
            if run_id not in self._pending_cancellations:
                self._pending_cancellations.append(run_id)
            return
        self._execute(
            self.bridge.service.cancel_analysis_batch,
            run_id,
            operation="cancel",
            on_result=lambda workspace: self._cancel_loaded(workspace, run_id),
        )

    def stop(self):
        self._timer.stop()
        self._pending_read = False
        self._pending_cancellations.clear()
        self.cancel_batch_start()
        self.cancel_immediate()

    def _execute(self, fn, *args, operation, on_result=None, cancellable=False):
        immediate = operation == "immediate"
        if self.bridge.shutdownRequested or self._control_busy:
            return
        if self._immediate_worker is not None and operation in {"immediate", "start"}:
            return
        if not immediate:
            self._control_busy = True
        self.loading = True
        self.operation = "immediate" if self._immediate_worker is not None else operation
        self.error = ""
        self.loadingChanged.emit()
        self.operationChanged.emit()
        self.errorChanged.emit()
        worker = TaskWorker(fn, *args, with_cancel=cancellable)
        if operation == "immediate":
            self._immediate_worker = worker
        elif operation == "start":
            self._batch_start_worker = worker
        self.bridge._workers.add(worker)
        worker.signals.result.connect(on_result or self._loaded)
        worker.signals.error.connect(self._failed)

        def finished():
            self.bridge._workers.discard(worker)
            if self._immediate_worker is worker:
                self._immediate_worker = None
            if self._batch_start_worker is worker:
                self._batch_start_worker = None
            cancel_run_id, self._cancel_started_run_id = self._cancel_started_run_id, None
            if not immediate:
                self._control_busy = False
            self.loading = self._immediate_worker is not None or self._control_busy
            self.operation = (
                "immediate"
                if self._immediate_worker is not None
                else "refresh"
                if self._control_busy
                else ""
            )
            self.loadingChanged.emit()
            self.operationChanged.emit()
            pending, self._pending_read = self._pending_read, False
            if not cancel_run_id and not self._control_busy and self._pending_cancellations:
                cancel_run_id = self._pending_cancellations.pop(0)
            if cancel_run_id and not self.bridge.shutdownRequested:
                QTimer.singleShot(0, lambda: self.cancel(cancel_run_id))
            if pending and not self.bridge.shutdownRequested:
                QTimer.singleShot(0, lambda: self.refresh(sync=False))
            self.bridge._check_shutdown()

        worker.signals.finished.connect(finished)
        pool = (
            self._monitor_pool
            if not immediate and self._immediate_worker is not None
            else self._pool
        )
        pool.start(worker)

    def _batch_loaded(self, workspace):
        worker = self._batch_start_worker
        if worker is not None and worker.cancel_requested:
            run_id = workspace.get("started_run_id")
            run = next((run for run in workspace.get("runs", []) if run.get("id") == run_id), None)
            if run and run.get("can_cancel"):
                self._cancel_started_run_id = run_id
        self._loaded(workspace)

    def _cancel_loaded(self, workspace, run_id):
        already_cancelling = any(
            run.get("id") == run_id and run.get("status") == "cancelling"
            for run in self.workspace.get("runs", [])
        )
        self._loaded(workspace)
        if (
            not already_cancelling
            and not self.bridge.shutdownRequested
            and not any(run.get("id") == run_id for run in workspace.get("runs", []))
        ):
            self.bridge.toastRequested.emit(
                "Batch annulé",
                "Les offres restantes sont à nouveau disponibles.",
                "info",
            )
            self.bridge._refresh_workflow()

    def _immediate_loaded(self, result):
        self._loaded(result["workspace"])
        report = result["report"]
        analyzed = int(report.analyzed)
        failed = len(report.errors)
        message = (
            f"{analyzed} offre analysée." if analyzed == 1 else f"{analyzed} offres analysées."
        )
        if failed:
            message += f" {failed} en échec."
        cancelled = bool(report.cancelled)
        self.bridge.toastRequested.emit(
            "Analyse immédiate arrêtée" if cancelled else "Analyse immédiate terminée",
            message,
            "warning" if failed or cancelled else "success",
        )
        self.bridge._refresh_workflow()

    def _loaded(self, workspace):
        if self.bridge.shutdownRequested:
            return
        previous = {str(run["id"]): run for run in self.workspace.get("runs", [])}
        remaining_ids = {str(run["id"]) for run in workspace.get("runs", [])}
        for run_id, old in previous.items():
            if run_id not in remaining_ids and old.get("status") == "cancelling":
                self.bridge.toastRequested.emit(
                    "Batch annulé",
                    "Les offres restantes sont à nouveau disponibles.",
                    "info",
                )
                self.bridge._refresh_workflow()
        if workspace != self.workspace:
            self.workspace = workspace
            self.changed.emit()
        if any(run.get("is_active") for run in workspace.get("runs", [])):
            self._timer.start()
        else:
            self._timer.stop()
        for run in workspace.get("runs", []):
            old = previous.get(str(run["id"]))
            if (
                old
                and old.get("is_active")
                and not run.get("is_active")
                and run.get("status") in {"completed", "partial", "failed", "cancelled"}
            ):
                count = int(run.get("analyzed", 0))
                failed = int(run.get("failed", 0))
                message = f"{count} analyses enregistrées."
                if failed:
                    message += f" {failed} offres en échec."
                kind = "warning" if failed or run.get("skipped") else "success"
                self.bridge.toastRequested.emit("Analyse Batch terminée", message, kind)
                self.bridge._refresh_workflow()

    def _failed(self, message, _trace):
        if self.bridge.shutdownRequested:
            return
        self.error = message
        self.errorChanged.emit()
        # Keep checking existing runs after transient connectivity failures.
        if any(run.get("is_active") for run in self.workspace.get("runs", [])):
            self._timer.start()
