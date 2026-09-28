"""Creation notifications summarize outcomes; dossier details retain diagnostics."""

import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from PySide6.QtCore import QObject

from smartapply.desktop.bridge import DesktopBridge


def _bridge():
    bridge = DesktopBridge.__new__(DesktopBridge)
    QObject.__init__(bridge)
    bridge._refresh_workflow = lambda: None
    bridge._set_activity = lambda *args: None
    bridge.selectJob = lambda job_id: None
    bridge.openApplication = lambda app_id: None
    notifications = []
    bridge.toastRequested.connect(lambda *args: notifications.append(args))
    return bridge, notifications


def test_restore_pending_pair_opens_duplicate_review_without_dossier_warning():
    bridge, notifications = _bridge()
    destinations = []
    selected = []
    bridge.navigationRequested.connect(lambda *args: destinations.append(args))
    bridge.selectJob = selected.append
    bridge._bulk_rescue_done({
        "requested": 1,
        "rescued": 1,
        "generated": 0,
        "duplicate_review_ids": [42],
        "warnings": [{"category": "duplicate_review", "job_id": 42, "message": "À vérifier"}],
    })
    assert destinations == [("duplicates", 42)]
    assert selected == [42]
    assert "1 doublon à vérifier" in notifications[0][1]
    assert "dossier avec des points" not in notifications[0][1]
    assert notifications[0][2] == "warning"


@pytest.mark.parametrize("method", ["_bulk_generation_done", "_bulk_rescue_done"])
def test_verified_correction_alone_is_information_not_a_review_point(method):
    bridge, notifications = _bridge()
    report = {
        "generated": 1,
        "warnings": [{"job_id": 42, "issues": ["text_review_corrected: correction vérifiée"]}],
    }
    before = deepcopy(report)

    getattr(bridge, method)(report)

    _, message, kind = notifications[0]
    assert kind == "success"
    assert "Corrections vérifiées" in message
    assert "à vérifier" not in message
    assert "échec" not in message
    assert report == before


@pytest.mark.parametrize("method", ["_bulk_generation_done", "_bulk_rescue_done"])
def test_batch_notification_is_bounded_and_counts_dossiers_not_raw_diagnostics(method):
    bridge, notifications = _bridge()
    raw = "text_review:style:professional_summary:" + "a very long review passage " * 1000
    report = {
        "generated": 7,
        "failed": 0,
        "warnings": [
            {"job_id": 1, "issues": ["text_review_corrected: correction vérifiée"]},
            {"job_id": 2, "issues": [raw, "cv_title_not_offer_anchored"]},
            {"job_id": 3, "issues": [raw]},
            {
                "job_id": 4,
                "issues": ["validation_error:text_review_incomplete: contrôle non terminé"],
            },
        ],
    }
    before = deepcopy(report)

    getattr(bridge, method)(report)

    _, message, kind = notifications[0]
    assert "7 candidatures créées" in message
    assert "3 dossiers avec des points à vérifier" in message
    assert "Détails dans le volet du dossier" in message
    assert "échec" not in message
    assert "text_review" not in message
    assert len(message) < 250
    assert kind == "warning"
    assert report == before


def test_older_joined_diagnostics_still_distinguish_information_and_points():
    bridge, notifications = _bridge()
    bridge._bulk_generation_done(
        {
            "generated": 2,
            "warnings": [
                {"job_id": 1, "message": "text_review_corrected: correction vérifiée"},
                {
                    "job_id": 2,
                    "message": "text_review_corrected: correction vérifiée · cv_title_not_offer_anchored",
                },
            ],
        }
    )
    assert "1 dossier avec des points à vérifier" in notifications[0][1]
    assert "échec" not in notifications[0][1]


def test_single_created_application_keeps_diagnostics_out_of_notification():
    bridge, notifications = _bridge()
    report = {
        "job_id": 42,
        "validation_warnings": [
            "text_review_corrected: correction vérifiée",
            "cv_title_not_offer_anchored",
        ],
        "validation_errors": ["text_review_invalid_correction:letter_too_long:255"],
    }
    before = deepcopy(report)
    bridge._generation_done(report)
    title, message, kind = notifications[0]
    assert title == "Candidature créée"
    assert "Le CV et la lettre sont disponibles" in message
    assert "2 points à vérifier" in message
    assert "text_review" not in message
    assert "bloqu" not in message
    assert kind == "warning"
    assert report == before


@pytest.mark.parametrize("method", ["_bulk_generation_done", "_bulk_rescue_done"])
def test_only_actual_operation_failures_are_called_failures(method):
    bridge, notifications = _bridge()
    getattr(bridge, method)(
        {
            "generated": 2,
            "failed": 1,
            "errors": [{"job_id": 99, "message": "Connexion au service indisponible " * 1000}],
            "warnings": [{"job_id": 42, "issues": ["cv_title_not_offer_anchored"]}],
        }
    )
    message = notifications[0][1]
    assert "1 échec technique" in message
    assert "1 dossier avec des points à vérifier" in message
    assert "Offre #99 : Connexion au service indisponible" in message
    assert "…" in message
    assert "autre(s) échec(s)" not in message
    assert len(message) < 350


def test_qml_toast_is_bounded_and_dossier_retains_full_diagnostics():
    """Exercise the actual QML with an inert bridge, without opening a database."""
    script = r"""
import sys
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QObject, QUrl, QMetaObject
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtTest import QTest
from smartapply.desktop.bridge import DesktopBridge

app = QGuiApplication([])
bridge = DesktopBridge.__new__(DesktopBridge)
QObject.__init__(bridge)
for name in ("dashboard", "shortlist", "current_job", "profile", "diagnostics", "activity"):
    setattr(bridge, "_" + name, {})
for name in ("busy", "jobs_loading", "job_detail_loading", "companies_loading", "companies_saving"):
    setattr(bridge, "_" + name, False)
bridge._jobs = []
bridge._companies = []
bridge._busy_label = ""
bridge._active_worker = None
bridge._shortlist_top_k = bridge._analysis_top_k = 5
bridge._start_read = lambda *args, **kwargs: None
bridge.service = SimpleNamespace(job_statuses=lambda: [], list_jobs=lambda: [])
engine = QQmlApplicationEngine()
engine.rootContext().setContextProperty("AppBridge", bridge)
qml = Path(sys.argv[1])
engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
assert engine.rootObjects(), "Main.qml failed to load"
window = engine.rootObjects()[0]
bridge.toastRequested.emit("Long title " * 100, "Long diagnostic " * 3000, "warning")
QTest.qWait(250)
toast = window.findChild(QObject, "toast")
title = window.findChild(QObject, "toastTitle")
message = window.findChild(QObject, "toastMessage")
assert title.property("maximumLineCount") == 2
assert message.property("maximumLineCount") == 4
assert 0 < toast.property("height") < 200, toast.property("height")
bridge._busy = True
bridge._busy_label = "Analyse en cours…"
bridge.busyChanged.emit()
QTest.qWait(250)
busy = window.findChild(QObject, "busyTaskNotification")
assert busy is not None
assert busy.property("visible") is True
assert abs(window.property("width") - busy.property("x") - busy.property("width") - 28) < 1
assert abs(window.property("height") - busy.property("y") - busy.property("height") - 24) < 1
raw = "text_review_invalid_correction:letter_too_long:255 " + "raw evidence " * 1000
bridge._current_job = {
    "id": 42, "title": "Data Scientist", "status": "ready_for_form_submission",
    "application": {"id": 7, "warnings": ["text_review_corrected: correction vérifiée", raw]},
}
component = QQmlComponent(engine, QUrl.fromLocalFile(str(qml / "pages/JobsPage.qml")))
assert not component.isError(), [error.toString() for error in component.errors()]
page = component.create()
assert page is not None, [error.toString() for error in component.errors()]
page.setProperty("width", 1300)
page.setProperty("height", 800)
QTest.qWait(100)
assert page.property("sortKey") == "filter_disposition"
assert page.property("sortAscending") is True
regenerate = page.findChild(QObject, "offerRegenerateAction")
assert regenerate is not None
assert regenerate.property("visible") is True
assert page.property("documentReviewPointCount") == 1
diagnostics = page.property("documentDiagnostics")
if hasattr(diagnostics, "toVariant"):
    diagnostics = diagnostics.toVariant()
assert diagnostics == ["text_review_corrected: correction vérifiée", raw]
toggle = page.findChild(QObject, "documentReviewDetailsToggle")
assert toggle is not None
assert page.property("documentReviewDetailsExpanded") is False
QMetaObject.invokeMethod(toggle, "clicked")
assert page.property("documentReviewDetailsExpanded") is True
bridge._current_job = {"id": 43, "title": "Other role", "application": {"id": 8, "warnings": ["text_review_corrected: correction vérifiée"]}}
bridge.currentJobChanged.emit()
app.processEvents()
assert page.property("documentReviewPointCount") == 0
assert page.property("documentReviewDetailsExpanded") is False
page.deleteLater()
window.close()
engine.deleteLater()
app.processEvents()
"""
    qml = Path(__file__).resolve().parent.parent / "smartapply/desktop/qml"
    result = subprocess.run(
        [sys.executable, "-c", script, str(qml)],
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
            "QSG_RHI_BACKEND": "software",
            "QT_QUICK_CONTROLS_STYLE": "Basic",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
