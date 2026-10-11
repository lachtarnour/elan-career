"""Chrome PDF fallback must release its child process on stop and timeout."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from smartapply.cv import html_renderer
from smartapply.desktop.bridge import DesktopBridge
from smartapply.pipeline.errors import TaskCancelledError

pytestmark = pytest.mark.real_pdf


def _sleeping_chrome(tmp_path: Path, monkeypatch) -> Path:
    pidfile = tmp_path / "chrome.pid"
    executable = tmp_path / "chrome"
    executable.write_text(
        f'#!/bin/sh\nprintf "%s" "$$" > "{pidfile}"\nexec /bin/sleep 30\n',
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setattr(html_renderer, "_weasyprint_html", lambda: None)
    monkeypatch.setattr(html_renderer, "_chrome_executable", lambda: str(executable))
    return pidfile


def _wait_for_pid(pidfile: Path) -> int:
    deadline = time.monotonic() + 3
    while not pidfile.exists():
        assert time.monotonic() < deadline, "Chrome did not start"
        time.sleep(0.01)
    return int(pidfile.read_text())


def _assert_reaped(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_chrome_render_cancellation_reaps_process(tmp_path, monkeypatch):
    pidfile = _sleeping_chrome(tmp_path, monkeypatch)
    stopped = Event()
    target = tmp_path / "output.pdf"
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            html_renderer.html_to_pdf, "<html></html>", target,
            stop_requested=stopped.is_set,
        )
        pid = _wait_for_pid(pidfile)
        stopped.set()
        with pytest.raises(TaskCancelledError):
            result.result(timeout=3)
    _assert_reaped(pid)
    assert not target.exists()
    assert not list(tmp_path.glob("*.html"))
    assert not list(tmp_path.glob("*.pdf"))


def test_chrome_render_timeout_reaps_process_and_preserves_old_pdf(tmp_path, monkeypatch):
    _sleeping_chrome(tmp_path, monkeypatch)
    monkeypatch.setattr(html_renderer, "_CHROME_RENDER_TIMEOUT_SECONDS", 0.25)
    target = tmp_path / "output.pdf"
    target.write_bytes(b"previous pdf")
    real_popen = html_renderer.subprocess.Popen
    children = []

    def capture_process(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(html_renderer.subprocess, "Popen", capture_process)

    with pytest.raises(TimeoutError, match="Chrome PDF rendering timed out"):
        html_renderer.html_to_pdf("<html></html>", target)

    assert len(children) == 1 and children[0].returncode is not None
    _assert_reaped(children[0].pid)
    assert target.read_bytes() == b"previous pdf"
    assert not list(tmp_path.glob("*.html"))
    assert list(tmp_path.glob("*.pdf")) == [target]


def test_desktop_shutdown_cancels_blocked_chrome(tmp_path, monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    for name in (
        "refreshDashboard", "refreshShortlist", "refreshProfile",
        "refreshDiagnosticsIfStale",
    ):
        monkeypatch.setattr(DesktopBridge, name, lambda self: None)
    service = SimpleNamespace(
        initialize=lambda: None, analysis_top_k=lambda: 25,
        shortlist_top_k=lambda: 25, startup_maintenance_needed=lambda: False,
    )
    bridge = DesktopBridge(service)
    pidfile = _sleeping_chrome(tmp_path, monkeypatch)

    def render(*, progress, stop_requested):  # noqa: ARG001
        return html_renderer.html_to_pdf(
            "<html></html>", tmp_path / "output.pdf", stop_requested=stop_requested,
        )

    try:
        bridge._run("PDF render", render, cancellable=True, workflow="generation")
        pid = _wait_for_pid(pidfile)
        bridge.requestShutdown()
        assert bridge.shutdownRequested
        deadline = time.monotonic() + 3
        while not bridge.shutdownReady and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert bridge.shutdownReady
        assert not bridge._workers
        _assert_reaped(pid)
    finally:
        bridge._thread_pool.waitForDone(3_000)
        bridge._unsubscribe_resources()
        bridge._unsubscribe_database()
