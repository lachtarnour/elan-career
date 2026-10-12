"""Generation monitoring independent of offer selection and other long tasks."""

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal

from smartapply.desktop.workers import TaskWorker
from smartapply.desktop.workspace_view_state import WorkspaceViewState


class GenerationController(QObject, WorkspaceViewState):
    changed = Signal()
    loadingChanged = Signal()
    errorChanged = Signal()
    operationChanged = Signal()

    def __init__(self, bridge):
        super().__init__(bridge)
        self.bridge = bridge
        self._init_view("generation")
        self.workspace = {"runs": [], "active_run_id": ""}
        self.loading = False
        self.error = ""
        self.operation = ""
        self._command_worker = None
        self._command_is_batch = False
        self._monitor_worker = None
        self._pending_read = False
        self._pending_cancellations = []
        self._pending_abandons = []
        self._notified_runs = set()
        self._command_revision = 0
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._monitor_pool = QThreadPool(self)
        self._monitor_pool.setMaxThreadCount(1)
        self._timer = QTimer(self)
        self._timer.setInterval(120_000)
        self._timer.timeout.connect(self.refresh)
        if hasattr(bridge.service, "sync_generation_runs"):
            self._startup_sync_pending = True
            QTimer.singleShot(0, lambda: self.refresh(sync=False))

    def refresh(self, *, sync=True):
        if self.bridge.shutdownRequested:
            return
        if self._monitor_worker is not None:
            self._pending_read = True
            return
        # An immediate worker advances its own durable run. Its monitor only
        # reads progress, so a refresh cannot execute a phase concurrently.
        method = (
            "sync_generation_runs"
            if sync and self._command_worker is None
            else "generation_workspace"
        )
        if hasattr(self.bridge.service, method + "_view"):
            method += "_view"
        if hasattr(self.bridge.service, method):
            self._execute(
                getattr(self.bridge.service, method), operation="refresh", monitor=True,
                syncing=method.startswith("sync_"),
            )

    def start(self, ids, *, immediate=False):
        method = "start_immediate_generation" if immediate else "start_generation_batch"
        self._execute(
            getattr(self.bridge.service, method), ids,
            operation="immediate" if immediate else "start", cancellable=True,
        )

    def retry(self, run_id):
        self._execute(
            self.bridge.service.retry_generation_run, run_id,
            operation="retry", cancellable=True,
        )

    def cancel(self, run_id):
        if self._monitor_worker is not None:
            if run_id not in self._pending_cancellations:
                self._pending_cancellations.append(run_id)
            return
        self._execute(
            self.bridge.service.cancel_generation_run, run_id,
            operation="cancel", monitor=True,
        )

    def cancel_current(self):
        if self._command_worker is not None:
            self._command_worker.cancel()

    def abandon(self, run_id):
        if self._command_worker is not None:
            return
        if self._monitor_worker is not None:
            if run_id not in self._pending_abandons:
                self._pending_abandons.append(run_id)
            return
        self._execute(
            self.bridge.service.abandon_generation_run, run_id,
            operation="abandon", monitor=True,
        )

    def stop(self):
        self._timer.stop()
        self._startup_sync_pending = False
        self._pending_read = False
        # Keep remote Batch work alive on close, including a submission that
        # is still being journalled. The bridge waits for worker completion.
        if not self._command_is_batch:
            self.cancel_current()

    def _state_changed(self):
        self.loading = self._command_worker is not None or self._monitor_worker is not None
        if self._command_worker is None:
            self.operation = "refresh" if self._monitor_worker is not None else ""
        self.loadingChanged.emit()
        self.operationChanged.emit()
        self._update_timer()

    def _update_timer(self):
        if self.bridge.shutdownRequested:
            self._timer.stop()
            return
        if self._command_worker is not None:
            interval = 1_000
        elif self.workspace.get("polling_run_count", any(run.get("is_active") for run in self.workspace.get("runs", []))):
            interval = 120_000
        else:
            self._timer.stop()
            return
        # Passive reads must not postpone the next provider sync indefinitely.
        # Changing a running QTimer interval also restarts its deadline.
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)
        if not self._timer.isActive():
            self._timer.start()

    def _execute(self, fn, *args, operation, cancellable=False, monitor=False, syncing=False):
        if self.bridge.shutdownRequested and operation not in {"cancel", "abandon"}:
            return
        if (self._monitor_worker if monitor else self._command_worker) is not None:
            return
        # Starting a run while a provider sync owns a phase would race its
        # state transitions. Read-only monitoring is allowed during commands.
        if not monitor and self._monitor_worker is not None:
            return
        if operation in {"start", "immediate"} and hasattr(self.bridge.service, "generation_workspace_view"):
            self._view["selected_run_id"] = ""
            self._view_revision += 1
        kwargs = {}
        if hasattr(self.bridge.service, "generation_workspace_view"):
            options = self._view_options()
            kwargs = options if operation == "refresh" else {"view": options}
        worker = TaskWorker(fn, *args, with_cancel=cancellable, **kwargs)
        command_revision = self._command_revision
        if monitor:
            self._monitor_worker = worker
        else:
            self._command_is_batch = operation == "start" or (
                operation == "retry" and any(
                    run.get("id") == args[0] and run.get("mode") == "batch"
                    for run in self._observed_runs(self.workspace)
                )
            )
            self._command_worker = worker
            self.operation = operation
        if operation != "refresh" or syncing:
            self.error = ""
        self.errorChanged.emit()
        self._state_changed()
        self.bridge._workers.add(worker)

        def result(workspace):
            if not monitor:
                self._command_revision += 1
            elif operation == "refresh" and command_revision != self._command_revision:
                # A read that started before command completion must not
                # replace its newer persisted final state with old progress.
                self._pending_read = True
                return
            self._loaded(workspace)
            if operation == "abandon":
                self.bridge.toastRequested.emit(
                    "Offres libérées", "Les étapes enregistrées ont été conservées.", "info",
                )
                self.bridge._refresh_workflow()
            started = workspace.get("started_run_id") or (args[0] if operation == "retry" else None)
            if not monitor and started:
                run = next((r for r in self._observed_runs(workspace) if r.get("id") == started), None)
                if run:
                    self._notify_finished(run)
                    if worker.cancel_requested and run.get("can_cancel"):
                        self._pending_cancellations.append(started)

        def finished():
            self.bridge._workers.discard(worker)
            if self._command_worker is worker:
                self._command_worker = None
                self._command_is_batch = False
            if self._monitor_worker is worker:
                self._monitor_worker = None
            self._state_changed()
            # Finalize controls explicitly requested before closing. Register
            # the next worker before checking readiness, without a timer gap.
            if self._monitor_worker is None and self._pending_cancellations:
                run_id = self._pending_cancellations.pop(0)
                self.cancel(run_id)
            elif self._monitor_worker is None and self._pending_abandons:
                run_id = self._pending_abandons.pop(0)
                self.abandon(run_id)
            elif not self.bridge.shutdownRequested:
                if self._pending_read or not monitor:
                    self._pending_read = False
                    QTimer.singleShot(0, lambda: self.refresh(sync=False))
                elif self._startup_sync_pending:
                    self._startup_sync_pending = False
                    QTimer.singleShot(0, self.refresh)
            self.bridge._check_shutdown()

        def failed(message, trace):
            self._failed(message, trace)
            if syncing:
                self._pending_read = True

        worker.signals.result.connect(result)
        worker.signals.error.connect(failed)
        worker.signals.finished.connect(finished)
        (self._monitor_pool if monitor else self._pool).start(worker)

    def _notify_finished(self, run):
        run_id = str(run.get("id") or "")
        notification = (run_id, run.get("status"), run.get("generated"), run.get("failed"))
        if (
            not run_id or notification in self._notified_runs or run.get("is_active")
            or run.get("status") not in {"completed", "partial", "failed", "cancelled", "interrupted", "paused"}
        ):
            return
        self._notified_runs.add(notification)
        generated = int(run.get("generated", 0))
        failed = int(run.get("failed", 0))
        message = f"{generated} candidature créée." if generated == 1 else f"{generated} candidatures créées."
        if failed:
            message += f" {failed} en échec."
        self.bridge.toastRequested.emit(
            "Création arrêtée" if run.get("status") in {"cancelled", "interrupted", "paused"} else "Création terminée",
            message, "success" if run.get("status") == "completed" and not failed else "warning",
        )
        self.bridge._refresh_workflow()

    def _loaded(self, workspace):
        if self.bridge.shutdownRequested:
            return
        workspace = self._accept_view(workspace)
        if workspace is None:
            return
        previous = {str(run["id"]): run for run in self._observed_runs(self.workspace)}
        if workspace != self.workspace:
            self.workspace = workspace
            self.changed.emit()
        for run in self._observed_runs(workspace):
            old = previous.get(str(run["id"]))
            if old and old.get("is_active") and not run.get("is_active"):
                self._notify_finished(run)
        self._update_timer()

    def _failed(self, message, _trace):
        if not self.bridge.shutdownRequested:
            self.error = message
            self.errorChanged.emit()
            self._update_timer()
