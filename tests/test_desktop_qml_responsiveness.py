"""Exercise note drafts, resource guards and closing with an inert Qt bridge."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_HARNESS = r"""
import os
import sys
from pathlib import Path
from PySide6.QtCore import QObject, Property, Signal, Slot, QUrl, QElapsedTimer, QEventLoop, QMetaObject, Q_ARG, Q_RETURN_ARG, Qt, QPointF
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
from PySide6.QtQuick import QQuickItem, QQuickWindow
from smartapply.pipeline.ingest.role_families import default_search_query

app = QGuiApplication([])
def visual_item(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual_item(child, name)
        if found is not None:
            return found
    return None


app.setQuitOnLastWindowClosed(False)

class Bridge(QObject):
    currentJobChanged = Signal()
    busyChanged = Signal()
    busyJobIdsChanged = Signal()
    shutdownRequestedChanged = Signal()
    shutdownReadyChanged = Signal()
    jobsChanged = Signal()
    minimumScoreChanged = Signal()
    allMatchingJobsReady = Signal("QVariantList")
    duplicatePendingOnlyChanged = Signal()
    duplicateResolutionFinished = Signal(int, bool)
    loadingChanged = Signal()
    toastRequested = Signal(str, str, str)
    navigationRequested = Signal(str, int)
    jobSelectionClearRequested = Signal()
    notesFlushRequested = Signal()
    applicationNotesSaved = Signal(int, int)
    applicationNotesSaveFailed = Signal(int, int, str)
    analysisOfferChanged = Signal()
    analysisArchivingChanged = Signal()
    analysisOffersArchived = Signal("QVariantList")
    analysisWorkspaceChanged = Signal()
    analysisWorkspaceLoadingChanged = Signal()
    analysisWorkspaceOperationChanged = Signal()
    profileChanged = Signal()

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
        self.duplicate_decisions = []
        self.rows = [offer(42, 7), offer(43, 8)]
        self.min_score = -1
        self.matching_rows = self.rows
        self.page_index = 0
        self.total = len(self.rows)
        self.list_loading = self.detail_loading = False
        self.analysis_offer = {}
        self.analysis_offer_id = 0
        self.analysis_archiving = False
        self.analysis_archives = []
        self.analysis_workspace = {"candidates": [], "runs": [], "active_run_id": ""}
        self.analysis_loading = False
        self.analysis_operation = ""
        self.analysis_error = ""
        self.profile_data = {}
        self.analysis_refreshes = 0
        self.analysis_starts = []
        self.analysis_immediate = []
        self.analysis_immediate_cancellations = 0
        self.analysis_start_cancellations = 0
        self.analysis_retries = []
        self.analysis_cancellations = []

    currentJob = Property("QVariantMap", lambda self: self.job, notify=currentJobChanged)
    busyJobIds = Property("QVariantList", lambda self: self.protected, notify=busyJobIdsChanged)
    shutdownRequested = Property(bool, lambda self: self.closing, notify=shutdownRequestedChanged)
    shutdownReady = Property(bool, lambda self: self.ready, notify=shutdownReadyChanged)
    jobs = Property("QVariantList", lambda self: self.rows, notify=jobsChanged)
    minimumLlmScorePercent = Property(int, lambda self: self.min_score, notify=minimumScoreChanged)
    jobsPage = Property(int, lambda self: self.page_index, notify=jobsChanged)
    jobsTotal = Property(int, lambda self: self.total, notify=jobsChanged)
    jobsPageSize = Property(int, lambda self: 2, constant=True)
    selectionLoading = Property(bool, lambda self: False, constant=True)
    jobsModel = Property(QObject, lambda self: None, constant=True)
    jobStatuses = Property("QVariantList", lambda self: [
        {"value": "analyzed", "label": "Analysée"},
        {"value": "ready_for_form_submission", "label": "Prête"},
        {"value": "sent", "label": "Envoyée"},
        {"value": "archived", "label": "Archivées"}
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
    duplicatePendingOnly = Property(bool, lambda self: True, notify=duplicatePendingOnlyChanged)
    profile = Property("QVariantMap", lambda self: self.profile_data, notify=profileChanged)
    defaultSearchQuery = Property(str, lambda self: default_search_query(), constant=True)
    dashboard = Property("QVariantMap", lambda self: {}, constant=True)
    activity = Property("QVariantMap", lambda self: {}, constant=True)
    diagnostics = Property("QVariantMap", lambda self: {"source_ready": {}}, constant=True)
    companies = Property("QVariantList", lambda self: [], constant=True)
    companiesLoading = Property(bool, lambda self: False, constant=True)
    companiesSaving = Property(bool, lambda self: False, constant=True)
    analysisOffer = Property("QVariantMap", lambda self: self.analysis_offer, notify=analysisOfferChanged)
    analysisOfferId = Property(int, lambda self: self.analysis_offer_id, notify=analysisOfferChanged)
    analysisOfferLoading = Property(bool, lambda self: False, notify=analysisOfferChanged)
    analysisArchiving = Property(bool, lambda self: self.analysis_archiving, notify=analysisArchivingChanged)
    analysisWorkspace = Property("QVariantMap", lambda self: self.analysis_workspace, notify=analysisWorkspaceChanged)
    analysisWorkspaceLoading = Property(bool, lambda self: self.analysis_loading, notify=analysisWorkspaceLoadingChanged)
    analysisWorkspaceOperation = Property(str, lambda self: self.analysis_operation, notify=analysisWorkspaceOperationChanged)
    analysisWorkspaceError = Property(str, lambda self: self.analysis_error, notify=analysisWorkspaceLoadingChanged)

    @Slot(int)
    def selectAnalysisOffer(self, job_id):
        self.analysis_offer_id = job_id
        self.analysis_offer = {**offer(job_id, 0), "application": {}}
        self.analysisOfferChanged.emit()

    @Slot("QVariantList")
    def archiveAnalysisOffers(self, job_ids):
        self.analysis_archives.append(job_ids)
        self.analysis_archiving = True
        self.analysisArchivingChanged.emit()

    @Slot()
    def refreshAnalysisWorkspace(self):
        self.analysis_refreshes += 1

    @Slot("QVariantList")
    def startAnalysisBatch(self, job_ids):
        self.analysis_starts.append(job_ids)

    @Slot("QVariantList")
    def startImmediateAnalysis(self, job_ids):
        self.analysis_immediate.append(job_ids)

    @Slot()
    def cancelImmediateAnalysis(self):
        self.analysis_immediate_cancellations += 1

    @Slot()
    def cancelBatchStart(self):
        self.analysis_start_cancellations += 1

    @Slot(str)
    def retryAnalysisBatch(self, run_id):
        self.analysis_retries.append(run_id)

    @Slot(str)
    def cancelAnalysisBatch(self, run_id):
        self.analysis_cancellations.append(run_id)

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
    @Slot(str, str, str, bool, str)
    @Slot(str, str, str, bool, str, int)
    @Slot(str, str, str, bool, str, int, str)
    @Slot(str, str, str, bool, str, int, str, str)
    def loadJobs(self, *args):
        self.queries.append(args)

    @Slot(int)
    def setMinimumLlmScorePercent(self, value):
        self.min_score = value
        self.minimumScoreChanged.emit()

    @Slot()
    def selectAllMatchingJobs(self):
        self.allMatchingJobsReady.emit(self.matching_rows)

    @Slot(int)
    def selectJob(self, job_id):
        self.selections.append(job_id)
        self.job = next((row for row in self.rows if row["id"] == job_id),
            offer(job_id, 7 if job_id == 42 else 8)) if job_id else {}
        self.currentJobChanged.emit()

    @Slot(int, bool)
    def resolveDuplicate(self, job_id, same_offer):
        self.duplicate_decisions.append((job_id, same_offer))

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
    "profile_sections_follow_live_records": r"""
bridge.profile_data = {
    "name": "Profil de test", "initials": "PT", "title": "Ingénieure", "location": "Paris",
    "experience_items": [
        {"id": "exp_live", "title": "Premier poste", "company": "Première entreprise",
         "start_date": "2023-04", "end_date": "Present"}
    ],
    "project_items": [{"id": "project_live", "title": "Premier projet"}],
}
bridge.profileChanged.emit()
profile = load_page("ProfilePage.qml")
def visual_child(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual_child(child, name)
        if found is not None:
            return found
experience_title = visual_child(profile, "profileExperienceTitle_exp_live")
experience_dates = visual_child(profile, "profileExperienceDates_exp_live")
project_title = visual_child(profile, "profileProjectTitle_project_live")
assert experience_title.property("text") == "Premier poste"
assert experience_dates.property("text") == "04/2023 – Aujourd’hui"
assert project_title.property("text") == "Premier projet"
evidence = visual_child(profile, "profileEvidenceRow")
preferences = visual_child(profile, "profilePreferencesSection")
skills = visual_child(profile, "profileSkillsSection")
assert preferences.mapToItem(profile, QPointF()).y() < skills.mapToItem(profile, QPointF()).y() < evidence.mapToItem(profile, QPointF()).y()
assert project_title.mapToItem(profile, QPointF()).x() < experience_title.mapToItem(profile, QPointF()).x()
bridge.profile_data = {
    **bridge.profile_data,
    "experience_items": [
        {"id": "exp_live", "title": "Poste modifié", "company": "Autre entreprise",
         "start_date": "05/2022", "end_date": "2024-06"}
    ],
    "project_items": [{"id": "project_live", "title": "Projet modifié"}],
}
bridge.profileChanged.emit()
wait_until(lambda:
    visual_child(profile, "profileExperienceTitle_exp_live").property("text") == "Poste modifié"
    and visual_child(profile, "profileExperienceDates_exp_live").property("text") == "05/2022 – 06/2024"
    and visual_child(profile, "profileProjectTitle_project_live").property("text") == "Projet modifié")
""",
    "mission_relation_filter_is_visible_only_for_analyzed_offers": r"""
host = QQuickWindow()
host.resize(1400, 850)
page.setParentItem(host.contentItem())
host.show()
control = page.findChild(QQuickItem, "offerMissionRelationFilter")
status = page.findChild(QQuickItem, "offerStatusFilter")
wait_until(lambda: control is not None and control.property("visible") and control.width() > 0)
assert [(row["value"], row["label"]) for row in control.property("model")] == [
    ("", "Toutes les relations"),
    ("direct", "Directe"),
    ("adjacent", "Adjacente"),
    ("clearly_outside", "Hors cible"),
    ("unknown", "Inconnue"),
]
assert control.parentItem() == status.parentItem()
assert control.x() >= status.x() + status.width()
invoke(page, "setSelected", 42, True)
page.setProperty("savedListScrollY", 180)
page.setProperty("restoreListScroll", True)
control.setProperty("currentIndex", 1)
assert QMetaObject.invokeMethod(control, "activated", Qt.DirectConnection, Q_ARG("int", 1))
app.processEvents()
assert page.property("missionRelation") == "direct"
assert page.property("selectedCount") == 0
assert page.property("savedListScrollY") == 0
assert not page.property("restoreListScroll")
assert bridge.queries[-1] == ("", "analyzed", "llm_score", False, "", 0, "", "direct")
assert bridge.selections[-1] == 0

invoke(page, "sortBy", "title")
assert bridge.queries[-1] == ("", "analyzed", "title", True, "", 0, "", "direct")
page.setProperty("requestedFilter", "archived")
invoke(page, "refreshOffers", 0)
assert not control.property("visible")
assert bridge.queries[-1] == ("", "archived", "llm_score", False, "", 0, "")
page.setProperty("requestedFilter", "analyzed")
invoke(page, "refreshOffers", 0)
assert control.property("visible") and control.property("currentIndex") == 1
assert bridge.queries[-1] == ("", "analyzed", "llm_score", False, "", 0, "", "direct")
control.setProperty("currentIndex", 0)
assert QMetaObject.invokeMethod(control, "activated", Qt.DirectConnection, Q_ARG("int", 0))
app.processEvents()
assert bridge.queries[-1] == ("", "analyzed", "llm_score", False, "", 0)
host.hide()
page.setParentItem(None)
host.deleteLater()
""",
    "archive_origin_filter_persists_and_resets_selection_on_change": r"""
host = QQuickWindow()
host.resize(1400, 850)
page.setParentItem(host.contentItem())
host.show()
control = page.findChild(QQuickItem, "offerArchiveOriginFilter")
status = page.findChild(QQuickItem, "offerStatusFilter")
assert control is not None and not control.property("visible")
assert [(row["value"], row["label"]) for row in control.property("model")] == [
    ("", "Tous les archivages"),
    ("automatic_filter", "Filtre automatique"),
    ("mission_screen", "Missions hors cible"),
    ("manual", "Archivage manuel"),
    ("confirmed_duplicate", "Doublon confirmé"),
]
page.setProperty("requestedFilter", "archived")
invoke(page, "refreshOffers", 0)
wait_until(lambda: control.property("visible") and control.width() > 0)
app.processEvents()
assert bridge.queries[-1] == ("", "archived", "llm_score", False, "", 0, "")
assert control.parentItem() == status.parentItem()
assert abs(control.y() - status.y()) < 1
assert control.x() >= status.x() + status.width()

bridge.page_index = 1
bridge.total = 5
bridge.rows = [{**offer(42, 0), "status": "archived"},
    {**offer(43, 0), "status": "archived"}]
bridge.jobsChanged.emit()
invoke(page, "setSelected", 42, True)
assert page.property("selectedCount") == 1
page.setProperty("savedListScrollY", 180)
page.setProperty("restoreListScroll", True)
control.setProperty("currentIndex", 2)
assert QMetaObject.invokeMethod(control, "activated", Qt.DirectConnection, Q_ARG("int", 2))
app.processEvents()
assert page.property("archiveOrigin") == "mission_screen"
assert page.property("selectedCount") == 0
assert page.property("savedListScrollY") == 0
assert not page.property("restoreListScroll")
assert bridge.queries[-1] == ("", "archived", "llm_score", False, "", 0, "mission_screen")
assert bridge.selections[-1] == 0

bridge.page_index = 0
bridge.jobsChanged.emit()
search = page.findChild(QQuickItem, "offerSearchField")
search.setProperty("text", "Paris")
wait_until(lambda: bridge.queries[-1][0] == "Paris")
assert bridge.queries[-1] == ("Paris", "archived", "llm_score", False, "", 0, "mission_screen")
invoke(page, "sortBy", "published_date")
assert bridge.queries[-1] == ("Paris", "archived", "published_date", False, "", 0, "mission_screen")
invoke(page.findChild(QQuickItem, "offerNextPageAction"), "clicked")
assert bridge.queries[-1] == ("Paris", "archived", "published_date", False, "", 1, "mission_screen")
page.setProperty("activeView", False)
page.setProperty("activeView", True)
invoke(page, "refreshOffers", 0)
assert page.property("archiveOrigin") == "mission_screen"
assert bridge.queries[-1][-1] == "mission_screen"

page.setProperty("requestedFilter", "ready_for_form_submission")
invoke(page, "refreshOffers", 0)
assert not control.property("visible")
assert bridge.queries[-1] == ("Paris", "ready_for_form_submission", "llm_score", False, "", 0)
page.setProperty("requestedFilter", "archived")
invoke(page, "refreshOffers", 0)
assert control.property("visible")
assert control.property("currentIndex") == 2
assert bridge.queries[-1] == ("Paris", "archived", "llm_score", False, "", 0, "mission_screen")
host.hide()
page.setParentItem(None)
host.deleteLater()
""",
    "archived_offer_shows_supported_sub_label_without_changing_status": r"""
host = QQuickWindow()
host.resize(1400, 850)
page.setParentItem(host.contentItem())
host.show()
def visual_item(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual_item(child, name)
        if found is not None:
            return found
    return None
bridge.rows = [{**offer(42, 0), "status": "archived", "archive_sub_label": "Missions hors cible", "application": {}}]
bridge.job = bridge.rows[0]
bridge.jobsChanged.emit()
bridge.currentJobChanged.emit()
page.setProperty("filterStatus", "archived")
page.setProperty("displayedJobId", 42)
wait_until(lambda: visual_item(page, "offerArchiveSubLabel-42") is not None)
label = visual_item(page, "offerArchiveSubLabel-42")
assert label.property("visible") and label.property("text") == "Missions hors cible"
detail_label = page.findChild(QQuickItem, "offerDetailArchiveSubLabel")
assert detail_label.property("visible") and detail_label.property("text") == "Archivée · Missions hors cible"
bridge.rows = [{**bridge.rows[0], "archive_sub_label": ""}]
bridge.job = bridge.rows[0]
bridge.jobsChanged.emit()
bridge.currentJobChanged.emit()
app.processEvents()
assert not visual_item(page, "offerArchiveSubLabel-42").property("visible")
assert not detail_label.property("visible")
host.hide()
page.setParentItem(None)
host.deleteLater()
""",
    "search_default_covers_all_families_and_preserves_custom_text": r"""
from smartapply.pipeline.ingest.role_families import ROLE_FAMILIES, matching_role_families
search = load_page("SearchPage.qml")
field = search.findChild(QQuickItem, "searchQuery")
assert field.property("text") == bridge.defaultSearchQuery
assert {family.key for family in matching_role_families(field.property("text").split(" OR "))} == {family.key for family in ROLE_FAMILIES}
field.setProperty("text", "IA Engineer OR NLP Engineer")
bridge.busyChanged.emit()
bridge.currentJobChanged.emit()
app.processEvents()
assert field.property("text") == "IA Engineer OR NLP Engineer"
if os.environ.get("ELAN_SEARCH_SCREENSHOT"):
    field.setProperty("text", bridge.defaultSearchQuery)
    window = QQuickWindow()
    window.resize(1400, 850)
    search.setParentItem(window.contentItem())
    window.show()
    app.processEvents()
    window.grabWindow().save(os.environ["ELAN_SEARCH_SCREENSHOT"])
""",
    "minimum_ai_score_slider_persists_until_none": r"""
slider = page.findChild(QQuickItem, "offerMinimumLlmScoreSlider")
clear = page.findChild(QQuickItem, "offerMinimumLlmScoreClear")
assert slider and slider.property("visible")
assert slider.property("from") == 0 and slider.property("to") == 100
assert slider.property("stepSize") == 1 and slider.property("value") == 0
assert slider.property("background").height() == 3
assert not clear.property("visible")
invoke(page, "setMinLlmScorePercent", 70)
app.processEvents()
assert bridge.min_score == 70 and slider.property("value") == 70
assert clear.property("visible")
assert bridge.queries[-1] == ("", "analyzed", "llm_score", False, "", 0)
invoke(page, "sortBy", "operation_date")
assert bridge.min_score == 70 and slider.property("value") == 70
page.setProperty("requestedFilter", "archived")
invoke(page, "refreshOffers", 0)
assert not slider.property("visible") and bridge.min_score == 70
page.setProperty("requestedFilter", "analyzed")
invoke(page, "refreshOffers", 0)
assert slider.property("visible") and slider.property("value") == 70
invoke(clear, "clicked")
app.processEvents()
assert bridge.min_score == -1 and slider.property("value") == 0
assert not clear.property("visible")
""",
    "lifecycle_dates_follow_the_filter_and_keep_columns_aligned": r"""
host = QQuickWindow()
host.resize(1400, 850)
page.setParentItem(host.contentItem())
host.show()
def visual_item(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual_item(child, name)
        if found is not None:
            return found
    return None
bridge.rows = [{**row, "operation_date": "03/10/2026"}
               for row in bridge.rows]
bridge.jobsChanged.emit()
date_header = page.findChild(QQuickItem, "offerLifecycleDateHeader")
ia_header = page.findChild(QQuickItem, "offerIaHeader")
wait_until(lambda: visual_item(page, "offerLifecycleDate-42") is not None)
assert date_header and visual_item(page, "offerLifecycleDate-42")
for status, value in [
    ("analyzed", "03/10/2026"),
    ("ready_for_form_submission", "06/10/2026"),
    ("sent", "07/10/2026"),
]:
    bridge.rows = [{**row, "operation_date": value} for row in bridge.rows]
    bridge.jobsChanged.emit()
    page.setProperty("filterStatus", status)
    page.setProperty("sortKey", "llm_score")
    app.processEvents()
    date_cell = visual_item(page, "offerLifecycleDate-42")
    assert date_cell is not None
    assert date_header.property("visible") and date_cell.property("visible"), (
        status, page.property("filterStatus"),
        date_header.property("visible"), date_cell.property("visible"),
    )
    assert date_header.property("title") == "Operation date"
    assert date_header.property("sortKey") == "operation_date"
    assert date_cell.property("text") == value
    for width in (1400, 1100):
        page.setProperty("width", width)
        app.processEvents()
        assert date_header.x() >= ia_header.x() + ia_header.width()
        assert date_header.x() + date_header.width() <= date_header.parentItem().width() + 1
        assert abs(date_header.x() - date_cell.x()) <= 1
        assert visual_item(page, "offerTitle-42").width() > 0
    assert QMetaObject.invokeMethod(date_header, "clicked", Qt.DirectConnection)
    assert page.property("sortKey") == "operation_date" and page.property("sortAscending") is False
page.setProperty("filterStatus", "archived")
app.processEvents()
assert not date_header.property("visible") and not date_cell.property("visible")
host.hide()
page.setParentItem(None)
host.deleteLater()
""",
    "analysis_multiple_runs_keep_one_timeline_per_selected_run": r"""
def run(run_id, sequence, count, batches):
    return {"id": run_id, "sequence": sequence, "phase": "extraction",
        "status": "in_progress", "status_label": "Traitement en cours", "is_active": True,
        "total": count, "analyzed": 0, "failed": 0, "skipped": 0,
        "created_at": "2026-10-06T10:00:00Z", "updated_at": "2026-10-06T10:00:00Z",
        "items": [{"id": i, "title": "AI Engineer", "company": "Example"}
            for i in range(sequence * 10, sequence * 10 + count)],
        "phase_batches": [{"phase": "extraction", "status": "in_progress",
            "status_label": "Traitement en cours", "total": sum(b["total"] for b in batches),
            "completed": sum(b["completed"] for b in batches), "failed": 0,
            "batch_count": len(batches), "batches": batches},
            {"phase": "evaluation", "status": "waiting", "status_label": "En attente",
             "total": 0, "completed": 0, "failed": 0, "batch_count": 0, "batches": []}]}
first = run("run-a", 1, 3, [
    {"number": 1, "status": "completed", "status_label": "Terminé", "offers": 1,
     "total": 1, "completed": 1, "failed": 0},
    {"number": 2, "status": "in_progress", "status_label": "Traitement en cours",
     "offers": 2, "total": 2, "completed": 0, "failed": 0}])
second = run("run-b", 2, 1, [
    {"number": 1, "status": "in_progress", "status_label": "Traitement en cours",
     "offers": 1, "total": 1, "completed": 0, "failed": 0}])
bridge.analysis_workspace = {"candidates": [{"id": 99, "title": "AI Engineer"}],
    "runs": [second, first], "active_run_id": "run-b", "active_run_count": 2}
analysis = load_page("AnalysisPage.qml")
assert analysis.findChild(QQuickItem, "analysisRunSelector").property("visible")
assert analysis.findChild(QQuickItem, "analysisRunTitle").property("text") == "Analyse de 1 offre"
assert len(visual_item(analysis, "analysisExtractionBatches").property("batches")) == 1
analysis.setProperty("selectedRunId", "run-a")
app.processEvents()
assert analysis.findChild(QQuickItem, "analysisRunTitle").property("text") == "Analyse de 3 offres"
assert len(visual_item(analysis, "analysisExtractionBatches").property("batches")) == 2
# The table stays usable at supported sizes, including with Batch details open.
card = analysis.findChild(QQuickItem, "analysisRunCard")
table = analysis.findChild(QQuickItem, "analysisOfferTable")
details = analysis.findChild(QQuickItem, "analysisRunDetails")
assert analysis.findChild(QQuickItem, "analysisRefreshAction") is not None
assert analysis.findChild(QQuickItem, "analysisViewOffer_99") is None
assert analysis.findChild(QQuickItem, "analysisQueuePanel").property("surfaceColor") is None
assert analysis.findChild(QQuickItem, "analysisDetailPanel").property("surfaceColor") is None
assert analysis.findChild(QQuickItem, "analysisTableSurface") is None
assert not details.property("visible")
assert card.property("surfaceColor").alpha() == 0
assert card.property("strokeColor").alpha() == 0
assert card.property("padding") == 0
assert analysis.findChild(QQuickItem, "analysisTrackingBand").property("color") is None
assert "Parcours" not in analysis.findChild(QQuickItem, "analysisRunSummary").property("text")
assert analysis.findChild(QQuickItem, "analysisLotTab") is None
host = QQuickWindow()
analysis.setParentItem(host.contentItem())
host.show()
for width, height in [(1100, 720), (1320, 740), (1700, 820), (2400, 820)]:
    host.resize(width, height)
    analysis.setProperty("width", width)
    analysis.setProperty("height", height)
    reading = analysis.findChild(QQuickItem, "analysisReadingWorkspace")
    queue = analysis.findChild(QQuickItem, "analysisQueuePanel")
    detail = analysis.findChild(QQuickItem, "analysisDetailPanel")
    preview = analysis.findChild(QQuickItem, "analysisOfferDetail")
    tracking = analysis.findChild(QQuickItem, "analysisTrackingBand")
    wait_until(lambda: abs(reading.width() - (width - 20)) < 1 and table.width() > 480)
    assert abs(reading.mapToItem(analysis, QPointF()).x() - 10) < 1
    assert abs(queue.width() - detail.width()) < 20, (width, queue.width(), detail.width())
    assert abs(table.width() - queue.width()) < 2
    assert preview.mapToItem(analysis, QPointF()).x() > table.mapToItem(analysis, QPointF()).x()
    assert tracking.mapToItem(analysis, QPointF()).y() >= reading.y() + reading.height()
    assert tracking.height() >= 150
    assert table.width() >= 480
    wait_until(lambda: table.height() >= 230)
    assert table.height() >= 230, (width, height, table.height(), reading.height(), tracking.height())
    invoke(analysis.findChild(QQuickItem, "analysisToggleDetails"), "clicked")
    app.processEvents()
    assert details.property("visible")
    assert table.height() >= 160
    assert analysis.findChild(QQuickItem, "analysisCancelAction").property("visible") == bool(first.get("can_cancel"))
    invoke(analysis.findChild(QQuickItem, "analysisToggleDetails"), "clicked")
    app.processEvents()
# Passive updates preserve the disclosure state; changing runs closes it.
invoke(analysis.findChild(QQuickItem, "analysisToggleDetails"), "clicked")
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert details.property("visible")
analysis.setProperty("selectedRunId", "run-b")
app.processEvents()
assert not details.property("visible")
analysis.setProperty("selectedRunId", "run-a")
app.processEvents()
assert analysis.findChild(QQuickItem, "analysisCandidateList").property("count") == 1
assert analysis.findChild(QQuickItem, "analysisAvailableTab") is None
assert not analysis.findChild(QQuickItem, "analysisStartBatchAction").property("enabled")
invoke(analysis, "setSelected", 99, True)
assert analysis.findChild(QQuickItem, "analysisStartBatchAction").property("enabled")
invoke(analysis.findChild(QQuickItem, "analysisStartBatchAction"), "clicked")
assert bridge.analysis_starts == [[99]]
""",
    "analysis_selection_is_exhaustive_and_keeps_offers_independent": r"""
bridge.analysis_workspace = {"candidates": [
    {"id": i, "title": "AI Engineer", "company": "Example", "location": "Paris"}
    for i in range(1, 702)
], "runs": [], "active_run_id": ""}
analysis = load_page("AnalysisPage.qml")
view = analysis.findChild(QQuickItem, "analysisCandidateList")
assert view.property("count") == 701
assert not bridge.analysis_starts
assert bridge.queries == [] and bridge.selections == []
assert analysis.findChild(QQuickItem, "analysisCompanyHeader") is not None
assert analysis.findChild(QQuickItem, "analysisTitleHeader") is not None
assert not analysis.findChild(QQuickItem, "analysisStartBatchAction").property("enabled")
assert not analysis.findChild(QQuickItem, "analysisStartImmediateAction").property("enabled")
invoke(analysis, "setSelected", 1, True)
invoke(analysis, "setSelected", 701, True)
invoke(analysis.findChild(QQuickItem, "analysisStartBatchAction"), "clicked")
assert bridge.analysis_starts == [[1, 701]]
current_offer = dict(bridge.job)
table = analysis.findChild(QQuickItem, "analysisOfferTable")
assert QMetaObject.invokeMethod(table, "offerOpened", Qt.DirectConnection,
    Q_ARG("int", 701), Q_ARG("QString", "scraped"))
assert bridge.analysis_offer_id == 701
assert bridge.job == current_offer
assert analysis.property("selectedIds").toVariant() == [1, 701]
assert analysis.findChild(QQuickItem, "analysisOfferTitle").property("text") == "ML Engineer"
assert analysis.findChild(QQuickItem, "analysisOfferDescription").property("description").startswith("A meaningful")
viewport = analysis.findChild(QQuickItem, "analysisOfferDetailViewport")
viewport.setProperty("contentY", 50)
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert viewport.property("contentY") == 50
invoke(analysis.findChild(QQuickItem, "analysisStartImmediateAction"), "clicked")
app.processEvents()
assert bridge.analysis_immediate == []
bridge.analysis_operation = "immediate"
bridge.analysis_loading = True
bridge.analysisWorkspaceOperationChanged.emit()
bridge.analysisWorkspaceLoadingChanged.emit()
app.processEvents()
assert analysis.findChild(QQuickItem, "analysisCancelImmediateAction").property("visible")
invoke(analysis.findChild(QQuickItem, "analysisCancelImmediateAction"), "clicked")
assert bridge.analysis_immediate_cancellations == 1
bridge.analysis_operation = ""
bridge.analysis_loading = False
bridge.analysisWorkspaceOperationChanged.emit()
bridge.analysisWorkspaceLoadingChanged.emit()
search = analysis.findChild(QQuickItem, "analysisSearchField")
search_control = analysis.findChild(QQuickItem, "analysisExpandableSearch")
assert not search_control.property("expanded")
invoke(analysis.findChild(QQuickItem, "analysisSearchToggle"), "clicked")
assert search_control.property("expanded")
assert search.property("enabled")
search.setProperty("text", "701")
app.processEvents()
assert view.property("count") == 1
assert len(analysis.property("selectedIds").toVariant()) == 2
bridge.analysis_workspace = {**bridge.analysis_workspace,
    "candidates": bridge.analysis_workspace["candidates"][1:]}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert analysis.property("selectedIds").toVariant() == [701]
assert search.property("text") == "701"
invoke(search_control, "close", True)
app.processEvents()
assert not search_control.property("expanded")
assert analysis.property("query") == ""
assert view.property("count") == 700
assert bridge.queries == [] and bridge.selections == []
""",
    "analysis_tracks_both_phases_recovery_and_errors": r"""
run = {"id": "run-1", "phase": "evaluation", "phase_label": "Étape 2/2 · Évaluation",
    "status": "in_progress", "status_label": "Traitement en cours", "total": 100,
    "completed": 20, "failed": 3, "analyzed": 20, "created_at": "2026-10-06T10:00:00Z",
    "updated_at": "2026-10-06T12:00:00Z", "expires_at": "2026-10-07T10:00:00Z",
    "is_active": True, "can_retry": False, "can_cancel": True,
    "phase_batches": [{"phase": "extraction", "status": "completed", "total": 100, "completed": 97, "failed": 3},
        {"phase": "evaluation", "status": "in_progress", "total": 97, "completed": 20, "failed": 0}]}
bridge.analysis_workspace = {"candidates": [], "runs": [run], "active_run_id": "run-1"}
analysis = load_page("AnalysisPage.qml")
assert analysis.findChild(QQuickItem, "analysisRunTitle").property("text") == "Analyse de 100 offres"
assert visual_item(analysis, "analysisPhaseMarker_1").property("finished")
assert visual_item(analysis, "analysisPhaseMarker_2").property("active")
assert visual_item(analysis, "analysisPhaseGlow_2").property("visible")
assert not visual_item(analysis, "analysisPhaseMarker_3").property("active")
assert visual_item(analysis, "analysisPhaseStatus_3").property("text") == "En attente"
assert visual_item(analysis, "analysisPhaseStatus_1").property("text") == "Terminé"
assert visual_item(analysis, "analysisPhaseStatus_2").property("text") == "En cours"
assert visual_item(analysis, "analysisPhaseCounts_1").property("text") == "97 / 100 demandes réussies · 3 en échec"
assert visual_item(analysis, "analysisPhaseCounts_2").property("text") == "20 / 97 demandes traitées"
assert not analysis.findChild(QQuickItem, "analysisRetryAction").property("visible")
assert analysis.findChild(QQuickItem, "analysisCancelAction").property("enabled")
run.update(status="attention", status_label="Reprise nécessaire", is_active=True,
    can_retry=True, can_cancel=False, skipped=2, last_error="Connexion interrompue <b>à reprendre</b>",
    errors=[{"job_id": 42, "title": "ML Engineer", "message": "Le profil a changé."}])
bridge.analysis_workspace = {**bridge.analysis_workspace, "runs": [run]}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert "L’envoi doit être vérifié" in analysis.findChild(QQuickItem, "analysisRunError").property("text")
assert not visual_item(analysis, "analysisPhaseGlow_2").property("visible")
assert visual_item(analysis, "analysisPhaseStatus_2").property("text") == "Reprise nécessaire"
assert "2 ignorées" in analysis.findChild(QQuickItem, "analysisRunResult").property("text")
assert analysis.findChild(QQuickItem, "analysisFailedOffers").property("count") == 1
run.update(status="blocked", status_label="Connexion à rétablir", is_active=False,
    can_retry=True, can_cancel=True, last_error="Permission api.files.write requise")
bridge.analysis_workspace = {**bridge.analysis_workspace, "runs": [run]}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert visual_item(analysis, "analysisPhaseStatus_2").property("text") == "Connexion à rétablir"
assert visual_item(analysis, "analysisPhaseStatus_3").property("text") == "En attente"
assert not visual_item(analysis, "analysisPhaseGlow_2").property("visible")
assert analysis.findChild(QQuickItem, "analysisRetryAction").property("text") == "Reprendre"
assert analysis.findChild(QQuickItem, "analysisQueuePanel").property("visible")
assert analysis.findChild(QQuickItem, "analysisRunError").property("text").find("api.files.write") < 0
assert analysis.findChild(QQuickItem, "analysisCancelAction").property("enabled")
invoke(analysis.findChild(QQuickItem, "analysisRetryAction"), "clicked")
assert bridge.analysis_retries == ["run-1"]
bridge.analysis_loading = True
bridge.analysis_error = "Réseau indisponible"
bridge.analysisWorkspaceLoadingChanged.emit()
assert not analysis.findChild(QQuickItem, "analysisRefreshAction").property("enabled")
assert not analysis.findChild(QQuickItem, "analysisRetryAction").property("enabled")
assert analysis.findChild(QQuickItem, "analysisWorkspaceError").property("text") == "Réseau indisponible"
bridge.analysis_error = "Configuration IA — Évaluation de l’adéquation : modèle manquant (LLM_JOB_FIT_ASSESSMENT_MODEL)."
bridge.analysisWorkspaceLoadingChanged.emit()
app.processEvents()
assert "LLM_JOB_FIT_ASSESSMENT_MODEL" in analysis.findChild(QQuickItem, "analysisWorkspaceError").property("text")
assert analysis.findChild(QQuickItem, "analysisRunTitle").property("text") == "Analyse de 100 offres"
run.update(status="completed", status_label="Terminé", phase="done", is_active=False,
    phase_batches=[run["phase_batches"][0],
        {"phase": "evaluation", "status": "completed", "total": 97, "completed": 97, "failed": 0}])
bridge.analysis_workspace = {**bridge.analysis_workspace, "runs": [run]}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert visual_item(analysis, "analysisPhaseMarker_3").property("finished")
assert not visual_item(analysis, "analysisPhaseGlow_2").property("visible")
assert not analysis.findChild(QQuickItem, "analysisRunError").property("visible")
run.update(status="cancelled", status_label="Annulé", last_error="Permission api.files.write requise")
bridge.analysis_workspace = {**bridge.analysis_workspace, "runs": [run]}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert not analysis.findChild(QQuickItem, "analysisRunError").property("visible")
""",
    "analysis_launch_is_visible_before_the_first_run_is_saved": r"""
bridge.analysis_workspace = {"candidates": [
    {"id": 42, "title": "AI Engineer", "company": "Example", "location": "Paris"}
], "runs": [], "active_run_id": ""}
analysis = load_page("AnalysisPage.qml")
bridge.analysis_operation = "start"
bridge.analysis_loading = True
bridge.analysisWorkspaceOperationChanged.emit()
bridge.analysisWorkspaceLoadingChanged.emit()
app.processEvents()
assert analysis.findChild(QQuickItem, "analysisLaunchCard").property("visible")
assert not analysis.findChild(QQuickItem, "analysisStartBatchAction").property("enabled")
invoke(analysis.findChild(QQuickItem, "analysisCancelLaunchAction"), "clicked")
assert bridge.analysis_start_cancellations == 1
assert analysis.findChild(QQuickItem, "analysisQueuePanel").property("visible")
bridge.analysis_workspace = {"candidates": [], "runs": [{
    "id": "run-1", "phase": "extraction", "status": "in_progress",
    "status_label": "Traitement en cours", "total": 1, "is_active": True,
    "phase_batches": [{"phase": "extraction", "status": "in_progress", "total": 1}]
}], "active_run_id": "run-1"}
bridge.analysis_loading = False
bridge.analysis_operation = ""
bridge.analysisWorkspaceChanged.emit()
bridge.analysisWorkspaceLoadingChanged.emit()
bridge.analysisWorkspaceOperationChanged.emit()
app.processEvents()
assert not analysis.findChild(QQuickItem, "analysisLaunchCard").property("visible")
assert analysis.findChild(QQuickItem, "analysisRunCard").property("visible")
assert analysis.findChild(QQuickItem, "analysisQueuePanel").property("visible")
""",
    "analysis_navigation_follows_duplicates_and_moves_launch_actions": r"""
engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
window = engine.rootObjects()[0]
actions = window.property("actions").toVariant()
routes = [action["route"] for action in actions]
assert routes.index("analysis") == routes.index("duplicates") + 1
invoke(window, "navigate", "analysis", 0)
def analysis_page_ready():
    item = window.findChild(QQuickItem, "analysisPage")
    # An asynchronous Loader exposes the root object before all bindings and
    # children are initialized. Wait for the workspace property and list too.
    return (item is not None and item.property("selectedIds") is not None
        and item.findChild(QQuickItem, "analysisCandidateList") is not None)
wait_until(analysis_page_ready)
assert bridge.analysis_refreshes == 1
assert bridge.queries == []
analysis = window.findChild(QQuickItem, "analysisPage")
assert analysis.property("selectedIds").toVariant() == []
assert not bridge.analysis_starts
routes_from_offers = []
page.navigateRequested.connect(routes_from_offers.append)
bridge.job = {**offer(42, 0), "application": {}, "status": "filtered", "can_generate": False}
bridge.currentJobChanged.emit()
action = page.findChild(QQuickItem, "offerPrimaryAction")
assert action.property("text") == "Ouvrir l’analyse"
invoke(action, "clicked")
assert routes_from_offers == ["analysis"]
search = load_page("SearchPage.qml")
assert search.findChild(QQuickItem, "searchOpenAnalysisAction") is None
assert search.findChild(QQuickItem, "searchQuery") is not None
""",
    "duplicate_pairs_remain_separate_and_keep_refresh_state": r"""
def pair(job_id):
    return {**offer(job_id, 7), "source": "serpapi", "status": "duplicate_review",
        "duplicate_candidate": offer(43, 8), "possible_duplicate_of_id": 43}
bridge.rows = [pair(42), pair(44)] + [
    {**offer(job_id, 7), "possible_duplicate_of_id": 43} for job_id in range(100, 140)
]
bridge.job = bridge.rows[0]
bridge.currentJobChanged.emit()
bridge.jobsChanged.emit()
duplicates = load_page("DuplicatesPage.qml")
window = QQuickWindow()
window.resize(1400, 850)
duplicates.setParentItem(window.contentItem())
window.show()
app.processEvents()
comparison = duplicates.findChild(QQuickItem, "duplicateComparisonPanel")
assert comparison is not None and comparison.property("surfaceColor") is None
assert duplicates.findChild(QQuickItem, "duplicateListPanel").property("surfaceColor") is None
recent = duplicates.findChild(QQuickItem, "duplicateRecentOfferCard")
known = duplicates.findChild(QQuickItem, "duplicateKnownOfferCard")
assert recent is not None and known is not None
assert abs(recent.mapToItem(comparison, QPointF()).y() - known.mapToItem(comparison, QPointF()).y()) <= 1
assert recent.mapToItem(comparison, QPointF()).x() < known.mapToItem(comparison, QPointF()).x()
compare_content = duplicates.findChild(QQuickItem, "duplicateComparisonContent")
confirm = duplicates.findChild(QQuickItem, "duplicateConfirmAction")
distinct = duplicates.findChild(QQuickItem, "duplicateDistinctAction")
view = duplicates.findChild(QQuickItem, "duplicatePairList")
recent_description = duplicates.findChild(QQuickItem, "duplicateRecentDescriptionScroll")
known_description = duplicates.findChild(QQuickItem, "duplicateKnownDescriptionScroll")
assert compare_content.property("y") == 0
assert 500 < recent.height() <= 600 and abs(recent.height() - known.height()) <= 1
assert recent.mapToItem(comparison, QPointF()).x() >= 19
assert recent.mapToItem(comparison, QPointF()).y() >= 19
assert known.mapToItem(comparison, QPointF()).x() - (recent.mapToItem(comparison, QPointF()).x() + recent.width()) >= 51
assert compare_content.width() - (known.mapToItem(comparison, QPointF()).x() + known.width()) >= 19
assert confirm.mapToItem(comparison, QPointF()).y() - (recent.mapToItem(comparison, QPointF()).y() + recent.height()) >= 35
assert abs(confirm.mapToItem(duplicates, QPointF()).y() + confirm.height()
    - view.mapToItem(duplicates, QPointF()).y() - view.height()) <= 1
assert abs(confirm.mapToItem(comparison, QPointF()).y() - distinct.mapToItem(comparison, QPointF()).y()) <= 1
assert abs(confirm.width() - distinct.width()) <= 1
window.resize(1100, 500)
duplicates.setProperty("width", 1100)
duplicates.setProperty("height", 500)
app.processEvents()
assert compare_content.property("y") == 0
assert abs(confirm.width() - distinct.width()) <= 1
assert abs(recent.height() - known.height()) <= 1
assert recent_description.property("contentHeight") > recent_description.height()
assert known_description.property("contentHeight") > known_description.height()
button_y = confirm.mapToItem(comparison, QPointF()).y()
recent_description.setProperty("contentY", 50)
app.processEvents()
assert abs(confirm.mapToItem(comparison, QPointF()).y() - button_y) <= 1
assert abs(confirm.mapToItem(duplicates, QPointF()).y() + confirm.height()
    - view.mapToItem(duplicates, QPointF()).y() - view.height()) <= 1
window.resize(1400, 850)
duplicates.setProperty("width", 1400)
duplicates.setProperty("height", 850)
app.processEvents()
if os.environ.get("ELAN_DUPLICATES_SCREENSHOT"):
    window.grabWindow().save(os.environ["ELAN_DUPLICATES_SCREENSHOT"])
def visual_child(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual_child(child, name)
        if found is not None:
            return found
view = duplicates.findChild(QQuickItem, "duplicatePairList")
assert view.property("count") == 42
assert duplicates.property("selectedId") == 42
assert duplicates.findChild(QQuickItem, "groupDuplicateCopiesSwitch") is None
active_filter = duplicates.findChild(QQuickItem, "pendingDuplicatesOnlySwitch")
assert active_filter.property("text") == "Au moins une offre active"
action = visual_child(duplicates, "duplicatePairAction_44")
assert action is not None
QMetaObject.invokeMethod(action, "clicked")
assert bridge.selections[-1] == 44
confirm = duplicates.findChild(QQuickItem, "duplicateConfirmAction")
QMetaObject.invokeMethod(confirm, "clicked")
assert bridge.duplicate_decisions == [(44, True)]
view.setProperty("contentY", 120)
bridge.rows = [bridge.rows[1], bridge.rows[0], *bridge.rows[2:]]
bridge.jobsChanged.emit()
app.processEvents()
assert duplicates.property("selectedId") == 44
assert view.property("contentY") == 120
duplicates.setProperty("query", "44")
app.processEvents()
assert view.property("count") == 1
assert duplicates.findChild(QQuickItem, "duplicateRelationCount").property("text") == "1 / 42"
duplicates.setProperty("query", "")
app.processEvents()
assert view.property("count") == 42
assert duplicates.property("selectedId") == 44
assert bridge.duplicate_decisions == [(44, True)]
""",
    "duplicate_decisions_advance_through_filtered_pairs_after_refresh": r"""
def pair(job_id, company="Visible"):
    return {**offer(job_id, 7), "company": company, "status": "duplicate_review",
        "duplicate_candidate": offer(43, 8), "possible_duplicate_of_id": 43}
bridge.rows = [pair(44), pair(42), pair(45), pair(46), pair(99, "Hidden")]
bridge.job = bridge.rows[0]
bridge.currentJobChanged.emit()
bridge.jobsChanged.emit()
duplicates = load_page("DuplicatesPage.qml")
duplicates.setProperty("query", "Visible")
confirm = duplicates.findChild(QQuickItem, "duplicateConfirmAction")
distinct = duplicates.findChild(QQuickItem, "duplicateDistinctAction")
QMetaObject.invokeMethod(confirm, "clicked")
assert bridge.duplicate_decisions == [(44, True)]
assert not confirm.property("enabled") and not distinct.property("enabled")
bridge.duplicateResolutionFinished.emit(44, True)
app.processEvents()
assert bridge.selections == [], "Wait until the resolved pair leaves the refreshed list"
bridge.rows = bridge.rows[1:]
bridge.jobsChanged.emit()
wait_until(lambda: bridge.selections == [42])
assert duplicates.property("query") == "Visible"
QMetaObject.invokeMethod(distinct, "clicked")
# A database notification may arrive before the worker's confirmation.
bridge.rows = [row for row in bridge.rows if row["id"] not in (42, 45)]
bridge.jobsChanged.emit()
app.processEvents()
assert bridge.selections == [42]
bridge.duplicateResolutionFinished.emit(42, True)
wait_until(lambda: bridge.selections == [42, 46])
assert bridge.duplicate_decisions == [(44, True), (42, False)]
QMetaObject.invokeMethod(distinct, "clicked")
bridge.duplicateResolutionFinished.emit(46, True)
bridge.rows = [pair(99, "Hidden")]
bridge.jobsChanged.emit()
wait_until(lambda: bridge.selections == [42, 46, 0])
assert not duplicates.property("hasSelection")
assert duplicates.property("visibleRelationCount") == 0
""",
    "duplicate_advancement_respects_errors_manual_navigation_and_end_of_list": r"""
def pair(job_id):
    return {**offer(job_id, 7), "status": "duplicate_review", "duplicate_candidate": offer(43, 8)}
bridge.rows = [pair(42), pair(44), pair(45)]
bridge.job = bridge.rows[1]
bridge.currentJobChanged.emit()
bridge.jobsChanged.emit()
duplicates = load_page("DuplicatesPage.qml")
confirm = duplicates.findChild(QQuickItem, "duplicateConfirmAction")
QMetaObject.invokeMethod(confirm, "clicked")
bridge.duplicateResolutionFinished.emit(44, False)
app.processEvents()
assert bridge.selections == [] and confirm.property("enabled")
QMetaObject.invokeMethod(confirm, "clicked")
bridge.selectJob(45)
bridge.duplicateResolutionFinished.emit(44, True)
bridge.rows = [pair(42), pair(45)]
bridge.jobsChanged.emit()
app.processEvents()
assert bridge.selections == [45]
QMetaObject.invokeMethod(confirm, "clicked")
bridge.duplicateResolutionFinished.emit(45, True)
bridge.rows = [pair(42)]
bridge.jobsChanged.emit()
wait_until(lambda: bridge.selections == [45, 42])
# Leaving the page while persistence is running must not steal selection.
QMetaObject.invokeMethod(confirm, "clicked")
duplicates.setVisible(False)
bridge.duplicateResolutionFinished.emit(42, True)
bridge.rows = []
bridge.jobsChanged.emit()
app.processEvents()
assert bridge.selections == [45, 42]
""",
    "classification_card_displays_recorded_reason_as_plain_text": r"""
card = page.findChild(QQuickItem, "offerClassificationCard")
assert not card.property("visible")
bridge.job["classification_summary"] = "Aucune incompatibilité explicite n’a été prouvée."
bridge.job["classification_reasons"] = ["Contrat à vérifier", "Métier à vérifier"]
bridge.currentJobChanged.emit()
app.processEvents()
assert card.property("visible")
summary = page.findChild(QQuickItem, "offerClassificationSummary")
reasons = page.findChild(QQuickItem, "offerClassificationReasons")
assert summary.property("text") == bridge.job["classification_summary"]
assert reasons.property("text") == "• Contrat à vérifier\n• Métier à vérifier"
assert reasons.property("visible")
bridge.job["classification_reasons"] = []
bridge.currentJobChanged.emit()
app.processEvents()
assert not reasons.property("visible") and card.property("visible")
""",
    "new_offers_are_confined_to_analysis": r"""
control = page.findChild(QQuickItem, "offerDispositionFilter")
assert control is not None and not control.property("visible")
assert page.property("filterStatus") == "analyzed"
status_control = page.findChild(QQuickItem, "offerStatusFilter")
assert "scraped" not in [row["value"] for row in status_control.property("model")]
assert "duplicate_review" not in [row["value"] for row in status_control.property("model")]
invoke(page.findChild(QQuickItem, "offerRefreshAction"), "clicked")
assert bridge.queries[-1] == ("", "analyzed", "llm_score", False, "", 0)
search = page.findChild(QQuickItem, "offerSearchField")
search.setProperty("text", "Paris")
wait_until(lambda: bridge.queries[-1][0] == "Paris")
assert bridge.queries[-1][-2] == ""
invoke(page, "sortBy", "published_date")
assert bridge.queries[-1] == ("Paris", "analyzed", "published_date", False, "", 0)
page.setProperty("activeView", False)
page.setProperty("activeView", True)
invoke(page, "refreshOffers", 0)
assert bridge.queries[-1][-2] == ""
page.setProperty("requestedFilter", "archived")
invoke(page, "refreshOffers", 0)
assert not control.property("visible")
assert bridge.queries[-1] == ("Paris", "archived", "llm_score", False, "", 0, "")
page.setProperty("requestedFilter", "ready_for_form_submission")
invoke(page, "refreshOffers", 0)
assert bridge.queries[-1] == ("Paris", "ready_for_form_submission", "llm_score", False, "", 0)
page.setProperty("requestedFilter", "scraped")
invoke(page, "refreshOffers", 0)
assert not control.property("visible")
assert bridge.queries[-1] == ("Paris", "analyzed", "llm_score", False, "", 0)
""",
    "offer_pages_keep_global_and_page_selection_distinct": r"""
bridge.matching_rows = [offer(100 + i, 0) for i in range(5)]
bridge.rows = bridge.matching_rows[:2]
bridge.total = 5
bridge.jobsChanged.emit()
assert page.property("selectedCount") == 0
invoke(page.findChild(QQuickItem, "offerSelectPageAction"), "clicked")
assert page.property("selectedCount") == 2
invoke(page.findChild(QQuickItem, "offerNextPageAction"), "clicked")
assert bridge.queries[-1] == ("", "analyzed", "llm_score", False, "", 1)
bridge.page_index = 1
bridge.rows = bridge.matching_rows[2:4]
bridge.jobsChanged.emit()
invoke(page.findChild(QQuickItem, "offerSelectPageAction"), "clicked")
assert page.property("selectedCount") == 4
assert set(page.property("selectedIds").toVariant()) == {100, 101, 102, 103}
invoke(page, "selectAllFiltered")
assert page.property("selectedCount") == 5
assert page.property("allFilteredSelected") is True
invoke(page.findChild(QQuickItem, "offerSelectPageAction"), "clicked")
assert set(page.property("selectedIds").toVariant()) == {100, 101, 104}
assert page.property("allFilteredSelected") is False
""",
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
assert bridge.queries[-1] == ("", "analyzed", "llm_score", False, "", 0)
search = offers.findChild(QQuickItem, "offerSearchField")
table = offers.findChild(QQuickItem, "offerListViewport")
viewport = offers.findChild(QQuickItem, "offerDetailViewport")
debounce = offers.findChild(QObject, "offerSearchDebounce")
original_rows = [offer(42 + i, 7 + i) for i in range(80)]
bridge.rows = original_rows
bridge.jobsChanged.emit()
wait_until(lambda: table.property("contentHeight") > table.property("height"))

for route in ("analysis", "search", "duplicates"):
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
    assert bridge.queries[-1] == ("Paris " + route, "analyzed", "published_date", False, "", 0)
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
assert bridge.queries[-1] == ("Paris duplicates", "archived", "llm_score", False, "", 0, "")
assert offers.property("selectedCount") == 0
invoke(window, "navigate", "jobs?status=scraped", 0)
wait_until(lambda: window.property("currentRoute") == "analysis")
invoke(window, "navigate", "jobs?status=duplicate_review", 0)
wait_until(lambda: window.property("currentRoute") == "duplicates")
assert bridge.queries[-1][1] == "duplicate_review"
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

_SCENARIOS["analysis_numeric_experience_sort_without_state_column"] = r"""
bridge.analysis_workspace = {"candidates": [
    {"id": 1, "title": "AI Engineer", "company": "A", "experience": "10+ ans", "state_label": "À évaluer"},
    {"id": 2, "title": "AI Engineer", "company": "B", "experience": "2+ ans", "state_label": "À extraire"},
], "runs": []}
analysis = load_page("AnalysisPage.qml")
table = analysis.findChild(QQuickItem, "analysisOfferTable")
assert table.findChild(QQuickItem, "analysisStateHeader") is None
table.setProperty("sortKey", "experience")
table.setProperty("ascending", True)
app.processEvents()
assert [row["id"] for row in table.property("sortedOffers").toVariant()] == [2, 1]
table.setProperty("ascending", False)
app.processEvents()
assert [row["id"] for row in table.property("sortedOffers").toVariant()] == [1, 2]
"""

_SCENARIOS["analysis_archiving_confirms_exact_selection_and_protects_active_work"] = r"""
bridge.protected = []
bridge.analysis_workspace = {"candidates": [
    {"id": i, "title": "AI Engineer", "company": "Example"} for i in range(1, 5)
], "runs": []}
analysis = load_page("AnalysisPage.qml")
host = QQuickWindow()
analysis.setParentItem(host.contentItem())
host.show()
bulk = analysis.findChild(QQuickItem, "analysisArchiveSelectionAction")
single = analysis.findChild(QQuickItem, "analysisArchiveOfferAction")
confirm = analysis.findChild(QObject, "analysisArchiveConfirm")
assert not bulk.property("enabled")
invoke(analysis, "setSelected", 1, True)
invoke(analysis, "setSelected", 2, True)
assert bulk.property("enabled") and bulk.property("text") == "Archiver (2)"
invoke(bulk, "clicked")
assert confirm.property("ids").toVariant() == [1, 2]
assert bridge.analysis_archives == []
invoke(confirm, "reject")
assert analysis.property("selectedIds").toVariant() == [1, 2]
invoke(bulk, "clicked")
# Confirmation retains the exact requested IDs, not a subsequently changed selection.
invoke(analysis, "setSelected", 3, True)
invoke(confirm, "accept")
assert bridge.analysis_archives == [[1, 2]]
assert not bulk.property("enabled") and not single.property("enabled")
assert analysis.property("selectedIds").toVariant() == [1, 2, 3]
bridge.analysis_archiving = False
bridge.analysisArchivingChanged.emit()
bridge.analysisOffersArchived.emit([1, 2])
assert analysis.property("selectedIds").toVariant() == [3]
bridge.analysis_workspace["candidates"] = bridge.analysis_workspace["candidates"][2:]
bridge.analysisWorkspaceChanged.emit()
bridge.selectAnalysisOffer(4)
bridge.analysis_offer["url"] = "https://example.org/jobs/4"
bridge.analysisOfferChanged.emit()
app.processEvents()
assert single.property("enabled")
invoke(single, "clicked")
assert confirm.property("ids").toVariant() == [4]
invoke(confirm, "accept")
assert bridge.analysis_archives == [[1, 2], [4]]
bridge.analysis_archiving = False
bridge.analysisArchivingChanged.emit()
bridge.analysisOffersArchived.emit([4])
assert analysis.property("selectedIds").toVariant() == [3]
assert bridge.queries == [] and bridge.selections == []

bridge.protected = [4]
bridge.busyJobIdsChanged.emit()
assert not single.property("enabled")
bridge.protected = []
bridge.busyJobIdsChanged.emit()
assert single.property("enabled")
bridge.analysis_workspace["runs"] = [{"id": "blocked-run", "status": "blocked",
    "is_active": False, "can_cancel": True, "items": [{"id": 4}]}]
bridge.analysisWorkspaceChanged.emit()
assert not single.property("enabled")
bridge.analysis_workspace["runs"] = []
bridge.analysisWorkspaceChanged.emit()
assert single.property("enabled")

toolbar = analysis.findChild(QQuickItem, "analysisToolbar")
table = analysis.findChild(QQuickItem, "analysisOfferTable")
source = analysis.findChild(QQuickItem, "analysisOfferSourceAction")
for width, height in [(1100, 720), (1320, 820), (1480, 920), (1800, 1000), (2524, 1000)]:
    host.resize(width, height)
    analysis.setProperty("width", width)
    analysis.setProperty("height", height)
    app.processEvents()
    ordered_controls = [analysis.findChild(QQuickItem, name) for name in (
        "analysisStartImmediateAction", "analysisStartBatchAction",
        "analysisArchiveSelectionAction", "analysisRefreshAction", "analysisExpandableSearch",
    )]
    positions = [control.mapToItem(toolbar, QPointF()) for control in ordered_controls]
    for before, after in zip(positions, positions[1:]):
        assert before.y() < after.y() - 1 or (
            abs(before.y() - after.y()) <= 1 and before.x() < after.x()
        ), (width, [(point.x(), point.y()) for point in positions])
    for name in ("analysisArchiveSelectionAction", "analysisStartImmediateAction", "analysisStartBatchAction"):
        button = analysis.findChild(QQuickItem, name)
        position = button.mapToItem(toolbar, QPointF())
        assert position.x() >= 0 and position.x() + button.width() <= toolbar.width() + 1, (
            width, name, position.x(), toolbar.width(),
        )
        assert button.mapToItem(analysis, QPointF()).y() + button.height() <= table.mapToItem(analysis, QPointF()).y()
    assert single.mapToItem(analysis, QPointF()).x() > source.mapToItem(analysis, QPointF()).x()
bridge.analysis_offer["status"] = "archived"
bridge.analysisOfferChanged.emit()
assert not single.property("enabled") and single.property("text") == "Archivée"
bridge.closing = True
bridge.shutdownRequestedChanged.emit()
assert not bulk.property("enabled")
"""

_TEARDOWN = r"""
from PySide6.QtCore import QEvent
for created_page in pages:
    created_page.deleteLater()
app.sendPostedEvents(None, QEvent.DeferredDelete)
engine.deleteLater()
app.sendPostedEvents(None, QEvent.DeferredDelete)
app.processEvents()
"""


_SCENARIOS["technical_score_survives_advisory_warnings_and_unknown_admissibility"] = r"""
card = page.findChild(QQuickItem, "offerFitEvaluation")
assert not card.property("visible")
bridge.job.update({
    "llm_score": 0.70, "fit_current": True, "fit_status_label": "Évaluation actuelle",
    "candidacy_label": "Candidature à vérifier",
    "fit_axes": [
        {"axis": "missions", "label": "Missions", "decision_label": "Fort", "level": 4},
        {"axis": "essentials", "label": "Essentiels", "decision_label": "Très faible", "level": 0},
        {"axis": "responsibilities", "label": "Responsabilités", "decision_label": "Fort", "level": 4},
    ],
    "fit_admissibility": [
        {"dimension": "mandatory_conditions", "label": "Conditions obligatoires", "status": "unknown",
         "status_label": "À confirmer", "findings": ["Habilitation non documentée."]},
        {"dimension": "candidate_preferences", "label": "Préférences du candidat", "status": "",
         "status_label": "Information indisponible", "findings": []},
    ],
    "fit_reliability": [{"label": "Traçabilité", "text": "Références problématiques."}],
    "fit_warnings": ["Citation invalide", "Références absentes"],
})
bridge.currentJobChanged.emit()
app.processEvents()
assert card.property("visible")
score = page.findChild(QQuickItem, "offerTechnicalScore")
assert score.property("text") == "70 / 100"
method = page.findChild(QQuickItem, "offerFitMethod")
assert "50 / 30 / 20" in method.property("text")
axis = visual_item(card, "offerFitAxis_essentials")
assert axis is not None and axis.property("text") == "Très faible · 0 / 4"
mandatory = visual_item(card, "offerAdmissibility_mandatory_conditions")
assert mandatory.property("text") == "Conditions obligatoires · À confirmer"
warning_action = page.findChild(QQuickItem, "offerFitWarningsAction")
assert warning_action.property("visible")
card.setProperty("warningsExpanded", True)
app.processEvents()
warnings = page.findChild(QQuickItem, "offerFitWarnings")
assert warnings.property("visible") and "Citation invalide" in warnings.property("text")
assert score.property("text") == "70 / 100"
bridge.job.update({"fit_warnings": [], "fit_current": False,
                   "fit_status_label": "Évaluation historique · à réévaluer"})
bridge.currentJobChanged.emit()
app.processEvents()
assert score.property("text") == "70 / 100"
assert "historique" in page.findChild(QQuickItem, "offerFitStatus").property("text")
assert "non comparable" in method.property("text")
assert "réévaluation explicite" in method.property("text")
assert "50 / 30 / 20" not in method.property("text")
assert visual_item(card, "offerFitAxis_essentials") is None
# The Analysis offer detail exposes the same score and dimensions after completion.
analysis_component = QQmlComponent(engine, QUrl.fromLocalFile(str(qml / "components" / "AnalysisOfferDetail.qml")))
assert not analysis_component.isError(), [error.toString() for error in analysis_component.errors()]
analysis_detail = analysis_component.create()
assert analysis_detail is not None
analysis_detail.setProperty("width", 600)
analysis_detail.setProperty("height", 850)
analysis_detail.setProperty("offer", bridge.job)
components.append(analysis_component)
pages.append(analysis_detail)
app.processEvents()
analysis_card = analysis_detail.findChild(QQuickItem, "analysisOfferFitEvaluation")
assert analysis_card.property("visible")
assert analysis_detail.findChild(QQuickItem, "offerTechnicalScore").property("text") == "70 / 100"
assert "réévaluation explicite" in analysis_detail.findChild(QQuickItem, "offerFitMethod").property("text")
assert visual_item(analysis_card, "offerFitAxis_essentials") is None
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


def test_analysis_qml_uses_real_bridge_with_isolated_runtime():
    """Exercise Qt property/slot wiring using the real service and local journal."""
    qml = Path(__file__).resolve().parent.parent / "smartapply/desktop/qml"
    script = r'''
import os
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

runtime = tempfile.TemporaryDirectory(prefix="elan-analysis-qml-")
runtime_path = Path(runtime.name)
os.environ.update(ELAN_HOME=str(runtime_path), ELAN_ENV_FILE=str(runtime_path / ".env"),
    DATABASE_URL=f"sqlite:///{runtime_path / 'test.db'}", OUTPUT_DIR=str(runtime_path / "output"),
    CACHE_DIR=str(runtime_path / "cache"), PROFILE_DIR=str(Path("tests/fixtures/profile").resolve()),
    LLM_PROVIDER="mock", OPENAI_API_KEY="", APIFY_TOKEN="",
    SERPAPI_API_KEY="", FRANCETRAVAIL_CLIENT_ID="", FRANCETRAVAIL_CLIENT_SECRET="", WTTJ_COOKIE="")
def deny_network(*args, **kwargs):
    raise AssertionError("The Qt wiring smoke test must not use the network")
socket.create_connection = deny_network
socket.socket.connect = deny_network

from PySide6.QtCore import Q_ARG, QElapsedTimer, QEvent, QEventLoop, QMetaObject, QUrl, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from smartapply.database import init_db, session_scope
from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun, AnalysisBatchWave
from smartapply.database.models import Job, JobScore, JobStatus
from smartapply.desktop.bridge import DesktopBridge
from smartapply.desktop.services import DesktopService
from smartapply.filtering.hard_reject.types import HardRejectResult

app = QGuiApplication([])
def visual_item(item, name):
    if item.objectName() == name:
        return item
    for child in item.childItems():
        found = visual_item(child, name)
        if found is not None:
            return found
    return None


init_db()
now = datetime.now(timezone.utc)
with session_scope() as session:
    jobs = [Job(external_id=f"qml-mock-{i}", title="Machine Learning Engineer", source_company="Example AI",
        location="Paris", description="Build machine learning systems using Python.", source="manual",
        status=JobStatus.FILTERED, filtered_at=now,
        score=JobScore(components={"filter_disposition": "pending_relevance",
            "hard_reject": HardRejectResult(decisions=()).audit_payload()})) for i in range(31)]
    session.add_all(jobs)
    session.flush()
    session.add(AnalysisBatchRun(id="mock-run", state="active", model="mock", provider="openai", max_completion_tokens=6000, profile_data={}, scope_context={"provider": "anthropic", "model": "claude-haiku-5-5", "effort": "high", "max_completion_tokens": 4096}))
    session.flush()
    for job in jobs[:2]:
        session.add(AnalysisBatchItem(run_id="mock-run", job_id=job.id, job_snapshot="mock-snapshot",
            title=job.title, company=job.source_company, analyzer_input={}, state="evaluation_pending"))
    session.add_all([
        AnalysisBatchWave(id="mock-screening", run_id="mock-run", phase="screening", state="imported",
            provider_batch_id="scope-1", provider_data={"status": "completed", "total": 2, "completed": 2, "failed": 0}),
        AnalysisBatchWave(id="mock-extraction", run_id="mock-run", phase="extraction", state="imported",
            provider_batch_id="provider-extraction",
            provider_data={"status": "completed", "total": 2, "completed": 2, "failed": 0}),
        AnalysisBatchWave(id="mock-evaluation", run_id="mock-run", phase="evaluation", state="in_progress",
            provider_batch_id="provider-evaluation",
            provider_data={"status": "in_progress", "total": 2, "completed": 1, "failed": 0}),
    ])

service = DesktopService()
# The progress fixture is local; the actual controller, worker, presentation and
# DesktopBridge still run. A remote coordinator cannot touch this fake journal.
service.sync_analysis_batches = service.analysis_workspace
bridge = DesktopBridge(service)
engine = QQmlApplicationEngine()
warnings = []
engine.warnings.connect(lambda errors: warnings.extend(error.toString() for error in errors))
engine.rootContext().setContextProperty("AppBridge", bridge)
engine.load(QUrl.fromLocalFile(str(Path(sys.argv[1]) / "Main.qml")))
assert engine.rootObjects(), warnings
window = engine.rootObjects()[0]
window.resize(1320, 820)

def wait_until(predicate):
    timer = QElapsedTimer()
    timer.start()
    while not predicate():
        app.processEvents(QEventLoop.AllEvents, 10)
        assert timer.elapsed() < 8000, "Real bridge condition did not become ready"

assert QMetaObject.invokeMethod(window, "navigate", Qt.DirectConnection,
    Q_ARG("QVariant", "analysis"), Q_ARG("QVariant", 0))
def ready():
    page = window.findChild(QQuickItem, "analysisPage")
    view = page.findChild(QQuickItem, "analysisCandidateList") if page is not None else None
    counts = visual_item(page, "analysisPhaseCounts_3") if page is not None else None
    return (page is not None and page.property("selectedIds") is not None
        and view is not None and view.property("count") == 29
        and counts is not None and counts.property("text") == "1 / 2 demandes traitées"
        and not bridge.analysisWorkspaceLoading and len(bridge.analysisWorkspace["candidates"]) == 29)
wait_until(ready)
analysis = window.findChild(QQuickItem, "analysisPage")
assert analysis.findChild(QQuickItem, "analysisCandidateList").property("count") == 29
assert len(bridge.analysisWorkspace["runs"][0]["items"]) == 2
assert analysis.findChild(QQuickItem, "analysisCompanyHeader") is not None
assert not analysis.findChild(QQuickItem, "analysisStartBatchAction").property("enabled")
assert analysis.findChild(QQuickItem, "analysisLotTab") is None
assert analysis.findChild(QQuickItem, "analysisCandidateList").property("count") == 29
assert not analysis.findChild(QQuickItem, "analysisStartBatchAction").property("enabled")
first_available = bridge.analysisWorkspace["candidates"][0]["id"]
analysis.setProperty("selectedOffers", {str(first_available): True})
app.processEvents()
assert analysis.findChild(QQuickItem, "analysisStartBatchAction").property("enabled")
assert analysis.findChild(QQuickItem, "analysisStartImmediateAction").property("enabled")
assert visual_item(analysis, "analysisExtractionBatches").property("batches")[0]["completed"] == 2
assert visual_item(analysis, "analysisEvaluationBatches").property("batches")[0]["completed"] == 1
assert visual_item(analysis, "analysisPhaseCounts_2").property("text") == "2 / 2 demandes réussies"
assert visual_item(analysis, "analysisPhaseCounts_1").property("text") == "2 / 2 demandes réussies"
assert visual_item(analysis, "analysisPhaseCounts_3").property("text") == "1 / 2 demandes traitées"
assert analysis.findChild(QQuickItem, "analysisCancelAction").property("enabled")
wait_until(lambda: bool(bridge.analysisOffer.get("description")))
assert bridge.analysisOfferId == analysis.findChild(QQuickItem, "analysisOfferTable").property("sortedOffers").toVariant()[0]["id"]
assert bridge.currentJob == {} and bridge._jobs == []
assert analysis.findChild(QQuickItem, "analysisRunTitle").property("text") == "Analyse de 2 offres"
assert not [warning for warning in warnings if "TypeError" in warning or "ReferenceError" in warning], warnings
if os.environ.get("ELAN_ANALYSIS_SMOKE_SCREENSHOT"):
    wait_until(lambda: window.property("pageOffset") == 0)
    window.grabWindow().save(os.environ["ELAN_ANALYSIS_SMOKE_SCREENSHOT"])
if os.environ.get("ELAN_ANALYSIS_DETAILS_SCREENSHOT"):
    QMetaObject.invokeMethod(analysis.findChild(QQuickItem, "analysisToggleDetails"), "clicked")
    app.processEvents()
    window.grabWindow().save(os.environ["ELAN_ANALYSIS_DETAILS_SCREENSHOT"])
bridge.requestShutdown()
wait_until(lambda: bridge.shutdownReady)
engine.deleteLater()
app.sendPostedEvents(None, QEvent.DeferredDelete)
app.processEvents()
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(qml)],
        cwd=qml.parents[2],
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QUICK_BACKEND": "software",
            "QSG_RHI_BACKEND": "software",
            "QT_QUICK_CONTROLS_STYLE": "Basic",
        },
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
