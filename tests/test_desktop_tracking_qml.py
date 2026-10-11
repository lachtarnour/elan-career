"""Global tracking survives navigation and uses only bounded bridge snapshots."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_desktop_qml_responsiveness import _HARNESS, _TEARDOWN

_MAIN_HARNESS = _HARNESS[: _HARNESS.index('page = load_page("JobsPage.qml")')].replace(
    'generationWorkspaceError = Property(str, lambda self: "", constant=True)',
    'generationWorkspaceError = Property(str, lambda self: getattr(self, "generation_error", ""), '
    "notify=generationWorkspaceLoadingChanged)",
)
_SETUP = r"""
def tracked_run(run_id, kind, total):
    return {"id": run_id, "kind": kind, "mode": "batch", "sequence": 42, "total": total,
        "status": "in_progress", "status_label": "Traitement en cours", "is_active": True,
        "can_cancel": True, "phase": "letter" if kind == "generation" else "evaluation",
        "phase_label": "Rédaction des textes" if kind == "generation" else "Évaluation",
        "created_at": "2026-10-11T10:00:00Z", "generated": 92 if kind == "generation" else 0,
        "analyzed": 0, "failed": 3 if kind == "generation" else 0,
        "items": [{"id": i, "title": "AI Engineer"} for i in range(total)],
        "phase_batches": [{"phase": phase, "label": phase, "status": "in_progress",
            "status_label": "Traitement en cours", "total": 20, "completed": 10,
            "failed": 0, "batch_count": 10, "batches": [{"number": i,
                "status": "in_progress", "status_label": "Traitement en cours",
                "offers": 2, "total": 2, "completed": 1, "failed": 0} for i in range(10)]}
            for phase in ("cv_selection", "letter", "render") if kind == "generation"]}

gen = tracked_run("persisted-generation", "generation", 98)
ana = tracked_run("persisted-analysis", "analysis", 3)
ana["phase_batches"] = [{"phase": phase, "status": "in_progress", "status_label": "En cours",
    "total": 20, "completed": 10, "failed": 0, "batch_count": 10,
    "batches": [{"number": i, "status": "in_progress", "total": 2, "completed": 1,
        "failed": 0, "offers": 2} for i in range(10)]} for phase in ("screening", "extraction", "evaluation")]
def journal(kind, selected):
    return {"desktop_view": True, "runs": [{"id": f"{kind}-{i}", "sequence": i,
        "created_at": "2026-10-11T10:00:00Z"} for i in range(42, 22, -1)],
        "selected_run": selected, "active_run_id": selected["id"], "active_run_count": 1,
        "history_page": 1, "history_page_size": 20, "history_total_count": 42,
        "reserved_job_ids": [42], "resumable_job_ids": []}
bridge.generation_workspace = journal("generation", gen)
bridge.analysis_workspace = {**journal("analysis", ana), "candidate_ids": [42, 701],
    "matching_candidate_ids": [42, 701], "candidate_count": 2, "candidate_page": 1,
    "candidate_page_size": 100}
bridge.analysis_candidates = [{"id": 42, "title": "AI Engineer"}]
engine.load(QUrl.fromLocalFile(str(qml / "Main.qml")))
assert engine.rootObjects()
window = engine.rootObjects()[0]
window.resize(1400, 850)
def tracking_ready():
    loader = window.findChild(QQuickItem, "trackingLoader")
    return (window.property("currentRoute") == "tracking" and loader is not None
        and loader.property("item") is not None and window.property("pageOffset") == 0)
"""

_SCENARIOS = {
    "available_from_home_without_loading_offers": r"""
assert window.property("currentRoute") == "dashboard"
assert not window.property("jobsLoaded") and not window.property("trackingLoaded")
navigation = window.findChild(QQuickItem, "trackingNavigationAction")
assert navigation is not None and navigation.property("visible")
assert any(action["route"] == "tracking" for action in window.property("actions").toVariant())
assert QMetaObject.invokeMethod(window, "routeIndex", Qt.DirectConnection,
    Q_RETURN_ARG("QVariant"), Q_ARG("QVariant", "tracking")) == 8
window.setProperty("jobsFilterStatus", "scraped")
reads_before = bridge.analysis_candidate_reads
invoke(navigation, "clicked")
wait_until(tracking_ready)
tracking = window.findChild(QQuickItem, "trackingPage")
assert window.property("trackingLoaded") and not window.property("jobsLoaded")
assert window.property("jobsFilterStatus") == "scraped"
assert bridge.queries == [] and bridge.selections == []
assert bridge.routes == ["dashboard", "tracking"]
invoke(navigation, "clicked")
assert bridge.routes == ["dashboard", "tracking", "tracking"]
assert bridge.analysis_candidate_reads == reads_before
assert tracking.property("visible") and tracking.property("tab") == "generation"
generation = tracking.findChild(QQuickItem, "generationTrackingBand")
assert generation.property("expanded") and generation.property("embedded")
assert not generation.findChild(QQuickItem, "generationTrackingToggle").property("visible")
assert generation.findChild(QQuickItem, "generationRunCard").property("run")["id"] == "persisted-generation"
assert generation.findChild(QQuickItem, "analysisRunTitle").property("text") == "Création de 98 offres"
wait_until(lambda: generation.height() > 240)
assert generation.findChild(QQuickItem, "generationNextHistoryPageAction").property("enabled")
invoke(tracking.findChild(QQuickItem, "trackingAnalysisTab"), "clicked")
app.processEvents()
assert tracking.property("tab") == "analysis"
assert tracking.findChild(QQuickItem, "trackingAnalysisRunCard").property("run")["id"] == "persisted-analysis"
assert bridge.analysis_candidate_reads == reads_before
assert tracking.findChild(QQuickItem, "trackingAnalysisNextHistoryAction").property("enabled")
""",
    "tracking_preserves_tabs_scroll_and_offers_context": r"""
invoke(window, "navigate", "jobs", 0)
wait_until(lambda: window.findChild(QQuickItem, "jobsPage") is not None and bridge.queries)
offers = window.findChild(QQuickItem, "jobsPage")
bridge.rows = [offer(42+i, 7+i) for i in range(80)]
bridge.jobsChanged.emit()
table = offers.findChild(QQuickItem, "offerListViewport")
wait_until(lambda: table.property("contentHeight") > table.property("height"))
invoke(offers, "setSelected", 42, True)
invoke(offers, "setSelected", 43, True)
table.setProperty("contentY", 180)
detail = offers.findChild(QQuickItem, "offerDetailViewport")
detail.setProperty("contentY", 200)
notes = offers.findChild(QQuickItem, "applicationNotes")
notes.setProperty("text", "Draft preserved through global tracking")
invoke(window, "navigate", "tracking", 0)
wait_until(tracking_ready)
tracking = window.findChild(QQuickItem, "trackingPage")
invoke(tracking.findChild(QQuickItem, "trackingAnalysisTab"), "clicked")
card = tracking.findChild(QQuickItem, "trackingAnalysisRunCard")
invoke(card.findChild(QQuickItem, "analysisRunDetailsToggle"), "clicked")
viewport = tracking.findChild(QQuickItem, "trackingAnalysisViewport")
wait_until(lambda: viewport.property("contentHeight") > viewport.property("height"))
viewport.setProperty("contentY", 80)
changed = []
tracking.analysisHistoryOptionsChanged.connect(lambda: changed.append(True))
for i in range(20):
    bridge.analysis_workspace = {**bridge.analysis_workspace,
        "selected_run": {**ana, "analyzed": i}}
    bridge.analysisWorkspaceChanged.emit()
    app.processEvents()
assert changed == [] and viewport.property("contentY") == 80
assert card.property("detailsExpanded")
invoke(window, "navigate", "dashboard", 0)
wait_until(lambda: window.property("currentRoute") == "dashboard")
invoke(window, "navigate", "tracking", 0)
wait_until(lambda: window.property("currentRoute") == "tracking")
assert window.findChild(QQuickItem, "trackingPage") is tracking
assert tracking.property("tab") == "analysis" and card.property("detailsExpanded")
assert viewport.property("contentY") == 80
invoke(window, "navigate", "jobs", 0)
wait_until(lambda: window.property("currentRoute") == "jobs")
assert offers.property("filterStatus") == "analyzed" and offers.property("selectedCount") == 2
assert table.property("contentY") == 180 and detail.property("contentY") == 200
assert notes.property("text") == "Draft preserved through global tracking"
# The global route also remains accessible from Nouvelle without changing its filter.
invoke(window, "navigate", "analysis", 0)
wait_until(lambda: offers.property("filterStatus") == "scraped")
invoke(window, "navigate", "tracking", 0)
wait_until(lambda: window.property("currentRoute") == "tracking")
assert offers.property("filterStatus") == "scraped"
assert window.property("jobsFilterStatus") == "scraped"
assert tracking.property("visible")
""",
    "history_actions_and_shutdown_keep_existing_guards": r"""
invoke(window, "navigate", "tracking", 0)
wait_until(tracking_ready)
tracking = window.findChild(QQuickItem, "trackingPage")
generation = tracking.findChild(QQuickItem, "generationTrackingBand")
card = generation.findChild(QQuickItem, "generationRunCard")
invoke(generation.findChild(QQuickItem, "generationNextHistoryPageAction"), "clicked")
assert bridge.generation_history_pages == [2]
assert QMetaObject.invokeMethod(card, "runSelected", Qt.DirectConnection, Q_ARG("QString", "generation-30"))
assert bridge.generation_run_selections == ["generation-30"]
bridge.generation_workspace = {**bridge.generation_workspace, "selected_run": {**gen,
    "status": "paused", "is_active": False, "can_cancel": False, "can_retry": True}}
bridge.generationWorkspaceChanged.emit()
app.processEvents()
retry = card.findChild(QQuickItem, "analysisRetryAction")
assert retry.property("enabled")
invoke(retry, "clicked")
assert bridge.generation_retries == ["persisted-generation"]
bridge.generation_loading = True
bridge.generationWorkspaceLoadingChanged.emit()
assert not retry.property("enabled")
bridge.generation_loading = False
bridge.generationWorkspaceLoadingChanged.emit()
invoke(tracking.findChild(QQuickItem, "trackingAnalysisTab"), "clicked")
invoke(tracking.findChild(QQuickItem, "trackingAnalysisNextHistoryAction"), "clicked")
assert bridge.analysis_history_pages == [2]
analysis_card = tracking.findChild(QQuickItem, "trackingAnalysisRunCard")
assert QMetaObject.invokeMethod(analysis_card, "runSelected", Qt.DirectConnection, Q_ARG("QString", "analysis-30"))
assert bridge.analysis_run_selections == ["analysis-30"]
invoke(analysis_card.findChild(QQuickItem, "analysisCancelAction"), "clicked")
confirm = tracking.findChild(QObject, "trackingAnalysisCancelConfirm")
wait_until(lambda: confirm.property("opened"))
assert bridge.analysis_cancellations == []
assert QMetaObject.invokeMethod(confirm, "accepted", Qt.DirectConnection)
assert bridge.analysis_cancellations == ["persisted-analysis"]
bridge.closing = True
bridge.shutdownRequestedChanged.emit()
assert not tracking.findChild(QQuickItem, "trackingAnalysisRefreshAction").property("enabled")
assert not analysis_card.findChild(QQuickItem, "analysisCancelAction").property("enabled")
assert not generation.findChild(QQuickItem, "generationRefreshAction").property("enabled")
assert not retry.property("enabled")
assert not generation.findChild(QQuickItem, "generationNextHistoryPageAction").property("enabled")
assert not tracking.findChild(QQuickItem, "trackingAnalysisNextHistoryAction").property("enabled")
""",
    "initial_tab_waits_for_journals_then_keeps_user_choice": r"""
# Cold startup snapshots can arrive separately; the first empty snapshot must not
# prevent choosing the only active analysis once both journals have been read.
analysis_journal = bridge.analysis_workspace
generation_journal = {**bridge.generation_workspace, "active_run_count": 0,
    "active_run_id": "", "selected_run": {}, "runs": [], "history_total_count": 0}
bridge.analysis_workspace = {"runs": [], "active_run_id": ""}
bridge.generation_workspace = {"runs": [], "active_run_id": ""}
invoke(window, "navigate", "tracking", 0)
wait_until(tracking_ready)
tracking = window.findChild(QQuickItem, "trackingPage")
assert not tracking.property("initialTabSelected")
bridge.generation_workspace = generation_journal
bridge.generationWorkspaceChanged.emit()
assert not tracking.property("initialTabSelected")
bridge.analysis_workspace = analysis_journal
bridge.analysisWorkspaceChanged.emit()
assert tracking.property("initialTabSelected") and tracking.property("tab") == "analysis"
bridge.generation_workspace = journal("generation", gen)
bridge.generationWorkspaceChanged.emit()
assert tracking.property("tab") == "analysis"
invoke(tracking.findChild(QQuickItem, "trackingGenerationTab"), "clicked")
bridge.analysisWorkspaceChanged.emit()
assert tracking.property("tab") == "generation"
bridge.generation_error = "Synchronisation temporairement indisponible"
bridge.generationWorkspaceLoadingChanged.emit()
assert tracking.findChild(QQuickItem, "generationTrackingStatus").property("text") == "Dernier état enregistré"
assert tracking.findChild(QQuickItem, "generationRunCard").property("run")["id"] == "persisted-generation"
""",
}


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_global_tracking_page_keeps_workspaces_independent(scenario):
    qml = Path(__file__).resolve().parent.parent / "smartapply/desktop/qml"
    result = subprocess.run(
        [sys.executable, "-c", _MAIN_HARNESS + _SETUP + _SCENARIOS[scenario] + _TEARDOWN, str(qml)],
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
