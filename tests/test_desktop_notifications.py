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


def test_restore_pending_pair_reports_review_without_changing_context():
    bridge, notifications = _bridge()
    destinations = []
    selected = []
    bridge.navigationRequested.connect(lambda *args: destinations.append(args))
    bridge.selectJob = selected.append
    bridge._bulk_rescue_done(
        {
            "requested": 1,
            "rescued": 1,
            "generated": 0,
            "duplicate_review_ids": [42],
            "warnings": [{"category": "duplicate_review", "job_id": 42, "message": "À vérifier"}],
        }
    )
    assert destinations == []
    assert selected == []
    assert "1 doublon à vérifier" in notifications[0][1]
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
            {"job_id": 2, "issues": ["validation_error:cv_title_not_offer_anchored"]},
            {"job_id": 3, "issues": [raw, "letter_too_long:252"]},
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
    assert "text_review" not in message
    assert raw not in message
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


@pytest.mark.parametrize("method", ["_generation_done", "_regeneration_done"])
def test_single_created_application_counts_distinct_review_points(method):
    bridge, notifications = _bridge()
    report = {
        "job_id": 42,
        "validation_warnings": [
            "text_review_corrected: correction vérifiée",
            "cv_title_not_offer_anchored",
            "letter_too_long:255",
        ],
        "validation_errors": ["validation_error:letter_too_long:255"],
    }
    before = deepcopy(report)
    getattr(bridge, method)(report)
    _, message, kind = notifications[0]
    assert "2 points à vérifier" in message
    assert "text_review" not in message
    assert "letter_too_long" not in message
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
    assert "#99" in message
    assert "Connexion au service indisponible " * 1000 not in message
    assert notifications[0][2] == "warning"


def test_qml_toast_is_bounded_and_dossier_shows_product_review_points():
    """Exercise the actual QML with an inert bridge, without opening a database."""
    script = r"""
import sys
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QObject, QUrl, QElapsedTimer, QEventLoop, QPointF, QRectF
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuick import QQuickItem
from smartapply.desktop.bridge import DesktopBridge
from smartapply.desktop.document_review_presentation import present_document_review

app = QGuiApplication([])

def wait_until(predicate, timeout_ms=2000):
    timer = QElapsedTimer()
    timer.start()
    while not predicate():
        app.processEvents(QEventLoop.AllEvents, 10)
        assert timer.elapsed() < timeout_ms, "QML condition did not become true"

def scene_rect(item):
    return QRectF(item.mapToScene(QPointF(0, 0)), item.size())

def visual_items(item):
    yield item
    for child in item.childItems():
        yield from visual_items(child)

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
toast = window.findChild(QQuickItem, "toast")
title = window.findChild(QQuickItem, "toastTitle")
message = window.findChild(QQuickItem, "toastMessage")
wait_until(lambda: toast.property("opacity") >= 0.99 and toast.property("height") > 0)
viewport = QRectF(0, 0, window.width(), window.height())
assert viewport.contains(scene_rect(toast))
assert title.property("truncated") is True
assert message.property("truncated") is True
bridge._busy = True
bridge._busy_label = "Analyse en cours…"
bridge.busyChanged.emit()
busy = window.findChild(QQuickItem, "busyTaskNotification")
assert busy is not None
wait_until(lambda: busy.property("opacity") >= 0.99)
assert busy.property("visible") is True
assert viewport.contains(scene_rect(busy))
assert not scene_rect(toast).intersects(scene_rect(busy))
raw = "validation_error:letter_too_long:255"
diagnostics = ["text_review_corrected: correction vérifiée", "letter_too_long:255", raw]
bridge._current_job = {
    "id": 42, "title": "Data Scientist", "status": "ready_for_form_submission",
    "application": {
        "id": 7, "warnings": diagnostics,
        "review_points": list(present_document_review(diagnostics)["points"]),
    },
}
component = QQmlComponent(engine, QUrl.fromLocalFile(str(qml / "pages/JobsPage.qml")))
assert not component.isError(), [error.toString() for error in component.errors()]
page = component.create()
assert page is not None, [error.toString() for error in component.errors()]
page.setProperty("width", 1300)
page.setProperty("height", 800)
assert page.property("filterStatus") == "analyzed"
assert page.property("sortKey") == "llm_score"
assert page.property("sortAscending") is False
published_date_header = page.findChild(QObject, "offerPublishedDateHeader")
assert published_date_header is not None
assert published_date_header.property("visible") is False
assert page.findChild(QObject, "offerFilterHeader").property("visible") is False
assert page.findChild(QObject, "offerIaHeader").property("visible") is True
assert page.findChild(QObject, "offerMatchHeader") is None
regenerate = page.findChild(QObject, "offerRegenerateAction")
assert regenerate is not None
wait_until(lambda: regenerate.property("visible") is True)
assert page.property("documentReviewPointCount") == 1
card = page.findChild(QObject, "documentReviewCard")
assert card is not None
assert card.property("visible") is True
title = card.findChild(QObject, "documentReviewCardTitle")
assert title is not None
assert title.property("text") == "CV et lettre"
count = card.findChild(QObject, "documentReviewCardCount")
assert count is not None
assert count.property("text") == "1 point à vérifier"
point = next((item for item in visual_items(card) if item.objectName() == "documentReviewPoint_letter_length"), None)
detail = next((item for item in visual_items(card) if item.objectName() == "documentReviewDetail_letter_length"), None)
assert point is not None and detail is not None
assert "lettre" in point.property("text").lower()
assert "255" in detail.property("text")
public_text = " ".join(
    str(item.property("text")) for item in visual_items(card)
    if item.property("text") is not None
)
assert "validation_error" not in public_text
assert "letter_too_long" not in public_text
assert "text_review_corrected" not in public_text
assert page.findChild(QObject, "documentReviewDetailsToggle") is None
bridge._current_job = {
    "id": 43, "title": "Other role",
    "application": {"id": 8, "warnings": ["text_review_corrected: correction vérifiée"], "review_points": []},
}
bridge.currentJobChanged.emit()
app.processEvents()
assert page.property("documentReviewPointCount") == 0
assert card.property("visible") is False
style = "text_review:style:professional_summary: private stylistic explanation"
bridge._current_job = {
    "id": 44, "title": "Style review",
    "application": {
        "id": 9, "warnings": [style],
        "review_points": list(present_document_review([style])["points"]),
    },
}
bridge.currentJobChanged.emit()
wait_until(lambda: page.property("documentReviewPointCount") == 1)
assert card.property("visible") is True
style_point = next((item for item in visual_items(card)
    if item.objectName() == "documentReviewPoint_style:professional_summary"), None)
assert style_point is not None and "style" in style_point.property("text").lower()
assert "private stylistic explanation" not in style_point.property("text")
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
