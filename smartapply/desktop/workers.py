"""Background task primitives that keep the desktop interface responsive."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from threading import Event
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class TaskSignals(QObject):
    result = Signal(object)
    error = Signal(str, str)
    progress = Signal(str)
    finished = Signal()


class TaskWorker(QRunnable):
    def __init__(
        self,
        fn: Callable[..., Any],
        *args: Any,
        with_progress: bool = False,
        with_cancel: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.with_progress = with_progress
        self.with_cancel = with_cancel
        self._cancel_requested = Event()
        self.signals = TaskSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            if self.with_progress:
                self.kwargs["progress"] = self.signals.progress.emit
            if self.with_cancel:
                self.kwargs["stop_requested"] = self._cancel_requested.is_set
            value = self.fn(*self.args, **self.kwargs)
        except Exception as exc:  # pragma: no cover - covered by GUI smoke test
            self.signals.error.emit(str(exc), traceback.format_exc())
        else:
            self.signals.result.emit(value)
        finally:
            self.signals.finished.emit()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    def cancel(self) -> None:
        """Request cooperative cancellation of a cancellable task."""
        self._cancel_requested.set()
