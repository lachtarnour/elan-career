"""Read one offer for Analysis without changing the Offers workspace."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class AnalysisOfferController(QObject):
    changed = Signal()

    def __init__(self, bridge, *, serialize):
        super().__init__(bridge)
        self.bridge = bridge
        self.serialize = serialize
        self.selected_id = 0
        self.offer = {}
        self.loading = False

    def select(self, job_id):
        if self.bridge.shutdownRequested or job_id <= 0:
            return
        if job_id == self.selected_id and self.offer:
            return
        self.selected_id = job_id
        self.offer = {}
        self.refresh()

    def refresh(self):
        if not self.selected_id or self.bridge.shutdownRequested:
            return
        # Existing content stays readable during a passive refresh. The shared
        # read scheduler discards stale results and queues only the latest ID.
        self.loading = not bool(self.offer)
        self.changed.emit()
        self.bridge._start_read(
            "analysis_offer", self.bridge.service.get_job, self.selected_id,
            on_success=self._loaded, on_finished=self._finished,
        )

    def _loaded(self, value):
        self.offer = self.serialize(value) if value else {}
        self.changed.emit()

    def _finished(self):
        self.loading = False
        self.changed.emit()
