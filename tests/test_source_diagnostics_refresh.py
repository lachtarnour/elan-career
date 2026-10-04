"""Source checks must recover without restarting or losing the user's choices."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QCoreApplication

from smartapply.desktop.bridge import DesktopBridge


def snapshot(state, configured=True):
    health = {
        "configured": configured,
        "ready": state == "available",
        "state": state,
        "message": state,
    }
    return {"source_health": {"linkedin": health}}


@pytest.fixture
def bridge(monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    queued = []
    service = SimpleNamespace(
        initialize=lambda: None,
        analysis_top_k=lambda: 5,
        shortlist_top_k=lambda: 5,
        source_label=lambda key: key,
        diagnostics=lambda **kwargs: snapshot("checking"),
        startup_maintenance_needed=lambda: False,
        source_diagnostics_stale=lambda: False,
        invalidate_diagnostics_cache=lambda: None,
    )
    for method in ("refreshDashboard", "refreshShortlist", "refreshProfile"):
        monkeypatch.setattr(DesktopBridge, method, lambda self: None)
    original = DesktopBridge.refreshDiagnosticsIfStale
    monkeypatch.setattr(DesktopBridge, "refreshDiagnosticsIfStale", lambda self: None)
    instance = DesktopBridge(service)
    instance._read_pool = SimpleNamespace(start=queued.append)
    # Restore the real cached navigation method after avoiding initial real workers.
    instance.refreshDiagnosticsIfStale = original.__get__(instance)
    instance.refreshDiagnostics()
    yield instance, service, queued
    instance._workflow_refresh_timer.stop()
    instance._unsubscribe_resources()
    instance._unsubscribe_database()
    instance.deleteLater()
    app.processEvents()


def finish(bridge, queued, state):
    worker = queued.pop(0)
    worker.signals.result.emit(snapshot(state))
    worker.signals.finished.emit()
    assert not bridge.diagnosticsChecking


def test_cached_navigation_and_explicit_check_do_not_overlap(bridge):
    instance, service, queued = bridge
    assert not hasattr(instance, "_source_refresh_timer")
    instance.refreshDiagnostics()
    assert len(queued) == 1
    finish(instance, queued, "unavailable")
    assert not instance.diagnostics["source_ready"]["linkedin"]

    instance.refreshDiagnosticsIfStale()
    assert queued == []
    service.source_diagnostics_stale = lambda: True
    instance.refreshDiagnosticsIfStale()
    assert instance.diagnosticsChecking
    finish(instance, queued, "available")
    assert instance.diagnostics["source_ready"]["linkedin"]

    instance.refreshDiagnostics()
    assert instance.diagnosticsChecking
    assert instance.diagnostics["source_ready"]["linkedin"]
    finish(instance, queued, "unavailable")
    assert not instance.diagnostics["source_ready"]["linkedin"]


def test_removed_credentials_clear_previous_availability_immediately(bridge):
    instance, service, queued = bridge
    finish(instance, queued, "available")
    service.diagnostics = lambda **kwargs: snapshot("unconfigured", configured=False)
    instance.refreshDiagnostics()
    queued.pop().run()
    assert not instance.diagnosticsChecking
    assert not instance.diagnostics["source_ready"]["linkedin"]
    assert instance.diagnostics["source_health"]["linkedin"]["state"] == "unconfigured"


def test_failed_check_has_consistent_state_and_can_recover(bridge):
    instance, service, queued = bridge
    finish(instance, queued, "available")
    instance.refreshDiagnostics()
    worker = queued.pop()
    worker.signals.error.emit("simulated failure", "")
    worker.signals.finished.emit()
    diagnostics = instance.diagnostics
    assert not diagnostics["source_ready"]["linkedin"]
    assert not diagnostics["source_health"]["linkedin"]["ready"]
    assert diagnostics["sources"][0]["message"] == "Vérification impossible."
    instance.refreshDiagnostics()
    finish(instance, queued, "available")
    assert instance.diagnostics["source_ready"]["linkedin"]


def test_search_qml_preserves_selection_and_refreshes_on_navigation():
    script = r"""
import sys
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QObject, QUrl, QMetaObject, Q_ARG, Qt, QEvent
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuick import QQuickItem, QQuickWindow
from smartapply.desktop.bridge import DesktopBridge

app = QGuiApplication([])
bridge = DesktopBridge.__new__(DesktopBridge)
QObject.__init__(bridge)
for name in ("dashboard", "shortlist", "current_job", "profile", "diagnostics", "activity"):
    setattr(bridge, "_" + name, {})
for name in ("busy", "jobs_loading", "job_detail_loading", "companies_loading", "companies_saving"):
    setattr(bridge, "_" + name, False)
bridge._jobs = []; bridge._companies = []; bridge._busy_label = ""
bridge._active_worker = bridge._diagnostics_worker = None
bridge._analysis_top_k = bridge._shortlist_top_k = 5
bridge._start_read = lambda *args, **kwargs: None
calls = []
bridge._thread_pool = bridge._read_pool = SimpleNamespace(start=calls.append)
bridge._workers = set()
bridge.service = SimpleNamespace(
    job_statuses=lambda: [], list_jobs=lambda: [], source_label=lambda key: key,
    # Navigation must check stale diagnostics regardless of the CI runner uptime.
    source_diagnostics_stale=lambda: True, invalidate_diagnostics_cache=lambda: None,
    diagnostics=lambda **kwargs: {"source_health": {
        "linkedin": {"configured": True, "ready": False, "state": "checking"}
    }},
)
engine = QQmlApplicationEngine()
engine.rootContext().setContextProperty("AppBridge", bridge)
qml = Path(sys.argv[1])
engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
assert engine.rootObjects()
window = engine.rootObjects()[0]
QMetaObject.invokeMethod(window, "refreshRoute", Qt.DirectConnection, Q_ARG("QVariant", "search"))
assert len(calls) == 1, "Search navigation must refresh source status"
worker = calls.pop()
worker.signals.finished.emit()
component = QQmlComponent(engine, QUrl.fromLocalFile(str(qml / "pages/SearchPage.qml")))
assert not component.isError(), [e.toString() for e in component.errors()]
page = component.create()
assert page is not None, [e.toString() for e in component.errors()]
page.setProperty("width", 1300); page.setProperty("height", 800)
page.setParent(engine)
page.setProperty("selectedSourceKeys", ["linkedin"])
def find_item(item, name):
    if item.objectName() == name: return item
    for child in item.childItems():
        found = find_item(child, name)
        if found is not None: return found
    return None
chip = find_item(page, "sourceChip_linkedin")
assert chip is not None
for available in (True, False, True):
    bridge._apply_diagnostics({"source_health": {"linkedin": {
        "configured": True, "ready": available,
        "state": "available" if available else "unavailable", "message": "Quota vérifié"
    }}})
    app.processEvents()
    assert chip.property("enabled") is available
    assert chip.property("checked") is True
    selected = page.property("selectedSourceKeys")
    if hasattr(selected, "toVariant"): selected = selected.toVariant()
    assert selected == ["linkedin"]
button = page.findChild(QObject, "refreshSourcesButton")
assert button.property("enabled")
QMetaObject.invokeMethod(button, "clicked")
app.processEvents()
assert len(calls) == 1
assert not button.property("enabled")
assert bridge.diagnostics["source_ready"]["linkedin"]
bridge._shutdown_ready = True
page.deleteLater(); window.close(); engine.deleteLater()
app.sendPostedEvents(None, QEvent.DeferredDelete)
app.processEvents()
"""
    qml = Path(__file__).resolve().parent.parent / "smartapply/desktop/qml"
    result = subprocess.run(
        [sys.executable, "-c", script, str(qml)],
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
            "QT_QUICK_CONTROLS_STYLE": "Basic",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ReferenceError" not in result.stderr, result.stderr
    assert "TypeError" not in result.stderr, result.stderr
