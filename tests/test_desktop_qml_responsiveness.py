"""Exercise note drafts, resource guards and closing with an inert Qt bridge."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_HARNESS = r"""
import sys
from pathlib import Path
from PySide6.QtCore import QObject, Property, Signal, Slot, QUrl, QElapsedTimer, QEventLoop, QMetaObject, Q_ARG, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuick import QQuickItem, QQuickWindow

app = QGuiApplication([])
app.setQuitOnLastWindowClosed(False)

class Bridge(QObject):
    currentJobChanged = Signal()
    busyChanged = Signal()
    busyJobIdsChanged = Signal()
    shutdownRequestedChanged = Signal()
    shutdownReadyChanged = Signal()
    jobsChanged = Signal()
    loadingChanged = Signal()
    toastRequested = Signal(str, str, str)
    navigationRequested = Signal(str, int)
    jobSelectionClearRequested = Signal()
    notesFlushRequested = Signal()
    applicationNotesSaved = Signal(int, int)
    applicationNotesSaveFailed = Signal(int, int, str)

    def __init__(self):
        super().__init__()
        self.job = offer(42, 7)
        self.protected = [99]
        self.closing = self.ready = False
        self.working = True
        self.maintenance_failed = False
        self.queued = self.maintenance_retries = 0
        self.saves = []
        self.routes = []
        self.queries = []
        self.selections = []
        self.rows = [offer(42, 7), offer(43, 8)]
        self.list_loading = self.detail_loading = False

    currentJob = Property("QVariantMap", lambda self: self.job, notify=currentJobChanged)
    busyJobIds = Property("QVariantList", lambda self: self.protected, notify=busyJobIdsChanged)
    shutdownRequested = Property(bool, lambda self: self.closing, notify=shutdownRequestedChanged)
    shutdownReady = Property(bool, lambda self: self.ready, notify=shutdownReadyChanged)
    jobs = Property("QVariantList", lambda self: self.rows, notify=jobsChanged)
    jobsModel = Property(QObject, lambda self: None, constant=True)
    jobStatuses = Property("QVariantList", lambda self: [
        {"value": "scraped", "label": "Nouvelle"}, {"value": "archived", "label": "Archivées"}
    ], constant=True)
    shortlist = Property("QVariantMap", lambda self: {"ready_to_generate": 0}, constant=True)
    busy = Property(bool, lambda self: self.working, notify=busyChanged)
    maintenanceFailed = Property(bool, lambda self: self.maintenance_failed, notify=busyChanged)
    canCancel = Property(bool, lambda self: True, constant=True)
    queuedTaskCount = Property(int, lambda self: self.queued, notify=busyChanged)
    busyLabel = Property(str, lambda self: "Processing A", constant=True)
    jobsLoading = Property(bool, lambda self: self.list_loading, notify=loadingChanged)
    jobDetailLoading = Property(bool, lambda self: self.detail_loading, notify=loadingChanged)
    shortlistTopK = Property(int, lambda self: 5, constant=True)
    analysisTopK = Property(int, lambda self: 5, constant=True)
    duplicateRelevantOnly = Property(bool, lambda self: True, constant=True)
    profile = Property("QVariantMap", lambda self: {}, constant=True)
    dashboard = Property("QVariantMap", lambda self: {}, constant=True)
    activity = Property("QVariantMap", lambda self: {}, constant=True)
    diagnostics = Property("QVariantMap", lambda self: {"source_ready": {}}, constant=True)
    companies = Property("QVariantList", lambda self: [], constant=True)
    companiesLoading = Property(bool, lambda self: False, constant=True)
    companiesSaving = Property(bool, lambda self: False, constant=True)

    @Slot(int, str, int)
    def saveApplicationNotes(self, application_id, text, revision):
        self.saves.append((application_id, text, revision))

    @Slot()
    def retryNotesSave(self):
        self.notesFlushRequested.emit()

    @Slot()
    def requestShutdown(self):
        self.closing = True
        self.shutdownRequestedChanged.emit()
        self.notesFlushRequested.emit()

    @Slot()
    def retryStartupMaintenance(self):
        self.maintenance_retries += 1
        self.maintenance_failed = False
        self.working = True
        self.busyChanged.emit()

    @Slot(str)
    def setActiveRoute(self, route):
        self.routes.append(route)

    @Slot(str, str, str, bool)
    def loadJobs(self, *args):
        self.queries.append(args)

    @Slot(int)
    def selectJob(self, job_id):
        self.selections.append(job_id)
        self.job = offer(job_id, 7 if job_id == 42 else 8)
        self.currentJobChanged.emit()

    @Slot()
    def refreshDiagnosticsIfStale(self):
        pass

    @Slot()
    def refreshDashboard(self):
        pass

def offer(job_id, application_id):
    return {
        "id": job_id, "title": "ML Engineer", "company": "Example", "location": "Paris",
        "status": "ready_for_form_submission", "can_send": True,
        "description": "A meaningful responsibility with supporting detail.\n" * 100,
        "application": {"id": application_id, "notes": "persisted", "warnings": ["cv_title_not_offer_anchored"]},
    }

def wait_until(predicate, timeout_ms=2000):
    timer = QElapsedTimer()
    timer.start()
    while not predicate():
        app.processEvents(QEventLoop.AllEvents, 10)
        assert timer.elapsed() < timeout_ms, "Qt condition did not become true"

def load_page(name):
    component = QQmlComponent(engine, QUrl.fromLocalFile(str(qml / "pages" / name)))
    assert not component.isError(), [error.toString() for error in component.errors()]
    page = component.create()
    assert page is not None, [error.toString() for error in component.errors()]
    page.setProperty("width", 1400)
    page.setProperty("height", 850)
    app.processEvents()
    components.append(component)
    pages.append(page)
    return page

def select(job_id, application_id):
    bridge.job = offer(job_id, application_id)
    bridge.currentJobChanged.emit()
    app.processEvents()

def invoke(item, method, *args):
    assert QMetaObject.invokeMethod(item, method, Qt.DirectConnection,
        *(Q_ARG("QVariant", value) for value in args))

bridge = Bridge()
engine = QQmlApplicationEngine()
engine.rootContext().setContextProperty("AppBridge", bridge)
qml = Path(sys.argv[1])
components, pages = [], []
page = load_page("JobsPage.qml")
notes = page.findChild(QQuickItem, "applicationNotes")
assert notes is not None
"""

_SCENARIOS = {
    "late_restore_respects_new_search_and_scroll": r"""
bridge.rows = [offer(42 + i, 7 + i) for i in range(80)]
bridge.jobsChanged.emit()
table = page.findChild(QQuickItem, "offerListViewport")
wait_until(lambda: table.property("contentHeight") > table.property("height"))
table.setProperty("contentY", 180)
page.setProperty("activeView", False)
page.setProperty("activeView", True)
invoke(page, "refreshOffers", 0)
bridge.jobsChanged.emit()
page.findChild(QQuickItem, "offerSearchField").setProperty("text", "new query")
table.setProperty("contentY", 260)
app.processEvents()
assert table.property("contentY") == 260
""",
    "loading_preserves_content_and_blocks_only_pending_detail": r"""
list_overlay = page.findChild(QQuickItem, "offerListLoadingOverlay")
detail_overlay = page.findChild(QQuickItem, "offerDetailLoadingOverlay")
archive = page.findChild(QQuickItem, "offerArchiveApplicationAction")
assert not list_overlay.property("visible") and not detail_overlay.property("visible")
assert archive.property("enabled") and notes.property("enabled")
bridge.detail_loading = bridge.list_loading = True
bridge.loadingChanged.emit()
assert list_overlay.property("visible") and detail_overlay.property("visible")
assert not archive.property("enabled") and not notes.property("enabled")
bridge.detail_loading = bridge.list_loading = False
bridge.loadingChanged.emit()
assert not list_overlay.property("visible") and not detail_overlay.property("visible")
assert archive.property("enabled") and notes.property("enabled")
page.setProperty("activeView", False)
select(43, 8)
page.setProperty("activeView", True)
# A failed reread leaves the other page's result in the bridge. It must not
# expose a dossier whose identity differs from the retained offer selection.
assert page.property("selectedId") == 42
assert not page.findChild(QQuickItem, "offerDetailContent").property("visible")
assert not archive.property("enabled") and not notes.property("enabled")
select(42, 7)
assert page.findChild(QQuickItem, "offerDetailContent").property("visible")
assert archive.property("enabled") and notes.property("enabled")
""",
    "navigation_restores_offers_and_respects_explicit_dossier": r"""
engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
window = engine.rootObjects()[0]
invoke(window, "navigate", "jobs", 0)
wait_until(lambda: window.findChild(QQuickItem, "jobsPage") is not None and bridge.queries)
offers = window.findChild(QQuickItem, "jobsPage")
assert bridge.queries[-1] == ("", "scraped", "filter_disposition", True)
search = offers.findChild(QQuickItem, "offerSearchField")
table = offers.findChild(QQuickItem, "offerListViewport")
viewport = offers.findChild(QQuickItem, "offerDetailViewport")
debounce = offers.findChild(QObject, "offerSearchDebounce")
original_rows = [offer(42 + i, 7 + i) for i in range(80)]
bridge.rows = original_rows
bridge.jobsChanged.emit()
wait_until(lambda: table.property("contentHeight") > table.property("height"))

for route in ("search", "duplicates"):
    search.setProperty("text", "Paris " + route)
    offers.setProperty("sortKey", "published_date")
    offers.setProperty("sortAscending", False)
    invoke(offers, "setSelected", 42, True)
    invoke(offers, "setSelected", 43, True)
    table.setProperty("contentY", 180)
    viewport.setProperty("contentY", 200)
    offers.findChild(QQuickItem, "applicationNotes").setProperty("text", "kept draft")
    invoke(window, "navigate", route, 0)
    wait_until(lambda: window.property("currentRoute") == route)
    assert not debounce.property("running")
    assert not offers.property("activeView")
    if route == "duplicates":
        bridge.rows = [offer(99, 99)]
        bridge.jobsChanged.emit()
        bridge.selectJob(99)
        assert offers.property("displayedJobId") == 42
        assert viewport.property("contentY") == 200
    invoke(window, "navigate", "jobs", 0)
    wait_until(lambda: window.property("currentRoute") == "jobs" and bridge.queries[-1][0] == "Paris " + route)
    assert bridge.queries[-1] == ("Paris " + route, "scraped", "published_date", False)
    bridge.rows = original_rows
    bridge.jobsChanged.emit()
    wait_until(lambda: table.property("contentY") == 180)
    assert offers.property("selectedId") == 42 and bridge.job["id"] == 42
    assert offers.property("selectedCount") == 2
    assert viewport.property("contentY") == 200
    assert offers.findChild(QQuickItem, "applicationNotes").property("text") == "kept draft"

invoke(window, "navigate", "search", 0)
wait_until(lambda: window.property("currentRoute") == "search")
bridge.navigationRequested.emit("jobs?status=archived", 77)
bridge.selectJob(77)
wait_until(lambda: window.property("currentRoute") == "jobs" and bridge.queries[-1][1] == "archived")
assert bridge.job["id"] == 77 and offers.property("selectedId") == 77
assert offers.property("filterStatus") == "archived"
assert bridge.queries[-1] == ("Paris duplicates", "archived", "score", False)
assert offers.property("selectedCount") == 0
""",
    "switch_before_debounce_and_late_results": r"""
notes.setProperty("text", "first draft")
select(43, 8)
assert bridge.saves == [(7, "first draft", 1)]
notes.setProperty("text", "other offer draft")
bridge.notesFlushRequested.emit()
assert bridge.saves[-1] == (8, "other offer draft", 1)
select(42, 7)
assert notes.property("text") == "first draft"
notes.setProperty("text", "latest draft")
bridge.applicationNotesSaved.emit(7, 1)
bridge.currentJobChanged.emit()
assert notes.property("text") == "latest draft"
bridge.notesFlushRequested.emit()
assert bridge.saves[-1] == (7, "latest draft", 2)
bridge.applicationNotesSaveFailed.emit(7, 2, "storage unavailable")
retry = page.findChild(QQuickItem, "applicationNotesRetry")
assert retry.property("visible") is True
assert notes.property("text") == "latest draft"
QMetaObject.invokeMethod(retry, "clicked")
assert bridge.saves[-1] == (7, "latest draft", 2)
bridge.applicationNotesSaved.emit(7, 2)
assert retry.property("visible") is False
assert notes.property("text") == "latest draft"
""",
    "focus_loss_and_idle_save": r"""
host = QQuickWindow()
host.resize(1400, 850)
page.setParentItem(host.contentItem())
host.show()
notes.forceActiveFocus()
wait_until(lambda: notes.property("activeFocus"))
notes.setProperty("text", "focus draft")
page.forceActiveFocus()
wait_until(lambda: len(bridge.saves) == 1)
assert bridge.saves == [(7, "focus draft", 1)]
bridge.applicationNotesSaved.emit(7, 1)
notes.setProperty("text", "idle draft")
wait_until(lambda: len(bridge.saves) == 2)
assert bridge.saves[-1] == (7, "idle draft", 2)
""",
    "context_and_offer_guards": r"""
viewport = page.findChild(QQuickItem, "offerDetailViewport")
viewport.setProperty("contentY", 200)
page.setProperty("documentReviewDetailsExpanded", True)
bridge.currentJobChanged.emit()
assert viewport.property("contentY") == 200
assert page.property("documentReviewDetailsExpanded") is True
archive = page.findChild(QQuickItem, "offerArchiveApplicationAction")
assert archive.property("enabled") is True
assert notes.property("enabled") is True
bridge.protected = [42]
bridge.busyJobIdsChanged.emit()
assert archive.property("enabled") is False
assert notes.property("enabled") is True
assert page.findChild(QQuickItem, "offerNextAction").property("enabled") is True
select(43, 8)
assert viewport.property("contentY") == 0
assert page.property("documentReviewDetailsExpanded") is False
manual = load_page("ManualPage.qml")
manual.findChild(QQuickItem, "manualTitle").setProperty("text", "AI Engineer")
manual.findChild(QQuickItem, "manualCompany").setProperty("text", "Example")
manual.findChild(QQuickItem, "manualDescription").setProperty("text", "Relevant role description. " * 10)
assert manual.findChild(QQuickItem, "manualCreate").property("enabled") is True
bridge.job = {**offer(42, 7), "status": "duplicate_review", "duplicate_candidate": offer(43, 8)}
bridge.protected = [99]
bridge.busyJobIdsChanged.emit()
bridge.currentJobChanged.emit()
duplicates = load_page("DuplicatesPage.qml")
confirm = duplicates.findChild(QQuickItem, "duplicateConfirmAction")
assert confirm.property("enabled") is True
bridge.protected = [43]
bridge.busyJobIdsChanged.emit()
assert confirm.property("enabled") is False
""",
    "closing_flushes_and_waits_for_confirmation": r"""
notes.setProperty("text", "closing draft")
engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
assert engine.rootObjects()
window = engine.rootObjects()[0]
assert window.close() is False
assert bridge.closing is True
assert bridge.saves == [(7, "closing draft", 1)]
assert window.isVisible()
assert notes.property("enabled") is False
bridge.ready = True
bridge.shutdownReadyChanged.emit()
assert not window.isVisible()
""",
    "maintenance_failure_offers_retry_and_cancellation": r"""
engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
assert engine.rootObjects()
window = engine.rootObjects()[0]
bridge.working = False
bridge.maintenance_failed = True
bridge.queued = 1
bridge.busyChanged.emit()
notification = window.findChild(QQuickItem, "busyTaskNotification")
wait_until(lambda: notification.property("opacity") >= 0.99)
retry = window.findChild(QQuickItem, "maintenanceRetryAction")
assert retry.property("visible") is True
assert window.findChild(QQuickItem, "cancelCurrentTaskAction").property("visible") is True
QMetaObject.invokeMethod(retry, "clicked")
assert bridge.maintenance_retries == 1
assert retry.property("visible") is False
""",
}

_TEARDOWN = r"""
from PySide6.QtCore import QEvent
for created_page in pages:
    created_page.deleteLater()
app.sendPostedEvents(None, QEvent.DeferredDelete)
engine.deleteLater()
app.sendPostedEvents(None, QEvent.DeferredDelete)
app.processEvents()
"""


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_qml_remains_usable_and_preserves_notes(scenario):
    qml = Path(__file__).resolve().parent.parent / "smartapply/desktop/qml"
    result = subprocess.run(
        [sys.executable, "-c", _HARNESS + _SCENARIOS[scenario] + _TEARDOWN, str(qml)],
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
