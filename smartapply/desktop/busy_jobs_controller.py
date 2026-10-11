"""Asynchronous reservation snapshot for QML; business guards remain authoritative."""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal

from smartapply.concurrency import busy_job_ids
from smartapply.desktop.workers import TaskWorker

logger = logging.getLogger(__name__)


class BusyJobsController(QObject):
    changed = Signal()

    def __init__(self, bridge):
        super().__init__(bridge)
        self.bridge = bridge
        self._ids = ()
        self._id_set = frozenset()
        self._revision = 0
        self._worker = None
        self._stopped = False
        self._retry_ms = 0
        # Reservation reads must not queue behind large workspace reads.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._refresh)

    @property
    def ids(self):
        return list(self._ids)

    def contains(self, job_id):
        return job_id in self._id_set

    def invalidate(self):
        if self._stopped or self.bridge.shutdownRequested:
            return
        self._revision += 1
        # Do not restart this timer: a sustained stream must still be read.
        # While a read is active, its completion schedules one fresh snapshot.
        if self._worker is None and (not self._timer.isActive() or self._timer.interval() > 25):
            self._timer.start(25)

    def stop(self):
        self._stopped = True
        self._revision += 1
        self._timer.stop()

    def _refresh(self):
        if self._stopped or self.bridge.shutdownRequested or self._worker is not None:
            return
        revision = self._revision
        worker = TaskWorker(busy_job_ids)
        self._worker = worker
        self.bridge._read_workers.add(worker)

        def result(ids):
            if self._stopped or self.bridge.shutdownRequested:
                return
            observed = set(ids)
            if revision != self._revision:
                # Sustained commits can invalidate every slow read. Publish
                # observed protections conservatively instead of staying empty;
                # only a current snapshot may release an offer in the UI.
                observed.update(self._id_set)
            else:
                self._retry_ms = 0
            snapshot = tuple(sorted(observed))
            if snapshot != self._ids:
                self._ids = snapshot
                self._id_set = frozenset(snapshot)
                self.changed.emit()

        def error(message, trace):
            if self._stopped or self.bridge.shutdownRequested or revision != self._revision:
                return
            # Keep the previous snapshot on a transient failure. Never publish
            # an empty list that could make occupied offers appear available.
            logger.warning("Reservation snapshot failed: %s\n%s", message, trace)
            self._retry_ms = min(10_000, max(500, self._retry_ms * 2))

        def finished():
            self.bridge._read_workers.discard(worker)
            self._worker = None
            if not self._stopped and not self.bridge.shutdownRequested:
                if revision != self._revision:
                    self._timer.start(25)
                elif self._retry_ms:
                    self._timer.start(self._retry_ms)
            self.bridge._check_shutdown()

        worker.signals.result.connect(result)
        worker.signals.error.connect(error)
        worker.signals.finished.connect(finished)
        self._pool.start(worker)
