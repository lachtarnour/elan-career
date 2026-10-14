"""Real QML probes for bounded pages and globally preserved work context."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_desktop_qml_responsiveness import _HARNESS, _TEARDOWN

_SCENARIOS = {
    "analysis_candidates": r"""
bridge.analysis_candidates = [{"id": i, "title": "AI Engineer", "company": f"Company {701-i}"}
    for i in range(100, 0, -1)]
bridge.analysis_workspace = {"desktop_view": True, "candidate_ids": list(range(1, 702)),
    "matching_candidate_ids": list(range(1, 702)), "candidate_count": 701,
    "candidates_count": 701, "candidate_page": 1, "candidate_page_size": 100,
    "runs": [], "selected_run": {}, "reserved_job_ids": [701],
    "history_page": 1, "history_page_size": 20, "history_total_count": 0}
analysis = load_page("AnalysisPage.qml")
table = analysis.findChild(QQuickItem, "analysisOfferTable")
view = analysis.findChild(QQuickItem, "analysisCandidateList")
previous = analysis.findChild(QQuickItem, "analysisPreviousPageAction")
next_page = analysis.findChild(QQuickItem, "analysisNextPageAction")
header = analysis.findChild(QQuickItem, "analysisSelectVisible")
assert view.property("count") == 100
assert [row["id"] for row in table.property("sortedOffers").toVariant()] == list(range(100, 0, -1))
assert not previous.property("enabled") and next_page.property("enabled")
invoke(analysis, "setSelected", 1, True)
invoke(analysis, "setSelected", 701, True)
assert analysis.property("selectedIds").toVariant() == [1, 701]
assert not analysis.findChild(QQuickItem, "analysisArchiveSelectionAction").property("enabled")
invoke(next_page, "clicked")
assert bridge.analysis_candidate_pages == [2]
bridge.analysis_workspace = {**bridge.analysis_workspace, "candidate_page": 2}
bridge.analysis_candidates = [{"id": i, "title": "AI Engineer", "company": "Example"}
    for i in range(101, 201)]
bridge.analysisWorkspaceChanged.emit()
bridge.analysisCandidatesChanged.emit()
app.processEvents()
assert view.property("count") == 100 and previous.property("enabled")
assert analysis.property("selectedIds").toVariant() == [1, 701]
bridge.selectAnalysisOffer(701)
table.setProperty("contentY", 120)
detail = analysis.findChild(QQuickItem, "analysisOfferDetailViewport")
detail.setProperty("contentY", 50)
changed, sorted_changed, selected_changed = [], [], []
analysis.candidatesChanged.connect(lambda: changed.append(True))
table.sortedOffersChanged.connect(lambda: sorted_changed.append(True))
analysis.selectedOffersChanged.connect(lambda: selected_changed.append(True))
bridge.analysis_candidate_reads = 0
for i in range(50):
    bridge.analysis_workspace = {**bridge.analysis_workspace, "active_run_count": i % 3}
    bridge.analysisWorkspaceChanged.emit()
    app.processEvents()
assert bridge.analysis_candidate_reads == 0
assert changed == [] and sorted_changed == [] and selected_changed == []
assert table.property("contentY") == 120 and detail.property("contentY") == 50
assert bridge.analysis_offer_id == 701
for query in ("A", "AI", "701"):
    analysis.setProperty("query", query)
assert bridge.analysis_candidate_queries == [] and view.property("count") == 100
assert not next_page.property("enabled")
wait_until(lambda: bridge.analysis_candidate_queries == ["701"])
bridge.analysis_workspace = {**bridge.analysis_workspace, "candidate_page": 1,
    "candidate_count": 1, "matching_candidate_ids": [701]}
bridge.analysis_candidates = [{"id": 701, "title": "AI Engineer", "company": "Example"}]
bridge.analysisWorkspaceChanged.emit()
bridge.analysisCandidatesChanged.emit()
app.processEvents()
assert view.property("count") == 1 and header.property("checked")
assert analysis.property("selectedIds").toVariant() == [1, 701]
assert not previous.property("enabled") and not next_page.property("enabled")
bridge.analysis_workspace = {**bridge.analysis_workspace, "candidate_count": 2,
    "matching_candidate_ids": [2, 701]}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert header.property("partial") and not header.property("checked")
invoke(analysis, "toggleVisible", True)
assert analysis.property("selectedIds").toVariant() == [1, 2, 701]
assert header.property("checked")
invoke(analysis, "toggleVisible", False)
assert analysis.property("selectedIds").toVariant() == [1]
invoke(analysis, "setSelected", 701, True)
bridge.analysis_workspace = {**bridge.analysis_workspace,
    "candidate_ids": list(range(2, 702)), "reserved_job_ids": []}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert analysis.property("selectedIds").toVariant() == [701]
invoke(analysis.findChild(QQuickItem, "analysisCompanyHeader"), "clicked")
assert bridge.analysis_candidate_sorts == [("company", True)]
invoke(analysis.findChild(QQuickItem, "analysisStartBatchAction"), "clicked")
assert bridge.analysis_starts == [[701]]
""",
    "analysis_history": r"""
def run(run_id, sequence):
    return {"id": run_id, "sequence": sequence, "phase": "extraction", "status": "in_progress",
        "status_label": "Traitement en cours", "is_active": True, "total": sequence,
        "created_at": "2026-10-11T10:00:00Z", "items": [{"id": i, "title": "AI Engineer"}
            for i in range(sequence)], "phase_batches": []}
bridge.analysis_workspace = {"desktop_view": True, "candidate_ids": [], "matching_candidate_ids": [],
    "candidate_count": 0, "runs": [{"id": f"run-{i}", "sequence": i} for i in range(41, 21, -1)],
    "selected_run": run("run-41", 41), "active_run_id": "run-41", "active_run_count": 1,
    "reserved_job_ids": list(range(41)), "history_page": 1, "history_page_size": 20,
    "history_total_count": 41}
analysis = load_page("AnalysisPage.qml")
analysis.setProperty("trackingExpanded", True)
app.processEvents()
card = analysis.findChild(QQuickItem, "analysisRunCard")
assert card.property("run")["id"] == "run-41"
assert len(card.property("historyOptions").toVariant()) == 20
history_changes = []
analysis.historyOptionsChanged.connect(lambda: history_changes.append(True))
for i in range(20):
    bridge.analysis_workspace = {**bridge.analysis_workspace,
        "runs": [{**row, "analyzed": i} for row in bridge.analysis_workspace["runs"]],
        "selected_run": {**bridge.analysis_workspace["selected_run"], "analyzed": i}}
    bridge.analysisWorkspaceChanged.emit()
    app.processEvents()
assert history_changes == []
previous = analysis.findChild(QQuickItem, "analysisPreviousHistoryPageAction")
next_page = analysis.findChild(QQuickItem, "analysisNextHistoryPageAction")
assert not previous.property("enabled") and next_page.property("enabled")
invoke(next_page, "clicked")
assert bridge.analysis_history_pages == [2]
bridge.analysis_workspace = {**bridge.analysis_workspace, "history_page": 2,
    "runs": [{"id": f"run-{i}", "sequence": i} for i in range(21, 1, -1)]}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert card.property("run")["id"] == "run-41"
assert len(card.property("historyOptions").toVariant()) == 21
assert card.findChild(QQuickItem, "analysisRunSelector").property("currentValue") == "run-41"
assert previous.property("enabled") and next_page.property("enabled")
assert QMetaObject.invokeMethod(card, "runSelected", Qt.DirectConnection, Q_ARG("QString", "run-3"))
assert bridge.analysis_run_selections == ["run-3"]
assert card.property("run")["id"] == "run-41"
bridge.analysis_workspace = {**bridge.analysis_workspace, "selected_run": run("run-3", 3)}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert card.property("run")["id"] == "run-3" and analysis.property("trackingExpanded")
bridge.analysis_workspace = {**bridge.analysis_workspace,
    "runs": [{"id": f"run-{i}", "sequence": i} for i in range(22, 2, -1)]}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert card.findChild(QQuickItem, "analysisRunSelector").property("currentValue") == "run-3"
analysis.setProperty("offerView", "lot")
app.processEvents()
assert analysis.findChild(QQuickItem, "analysisCandidateList").property("count") == 3
assert not analysis.findChild(QQuickItem, "analysisCandidatePagination").property("visible")
assert not analysis.findChild(QQuickItem, "analysisOfferTable").property("selectionEnabled")
bridge.analysis_workspace = {**bridge.analysis_workspace, "history_page": 3,
    "runs": [{"id": "run-1", "sequence": 1}]}
bridge.analysisWorkspaceChanged.emit()
app.processEvents()
assert not next_page.property("enabled") and previous.property("enabled")
invoke(previous, "clicked")
assert bridge.analysis_history_pages == [2, 2]
""",
    "generation_history": r"""
def run(run_id, sequence):
    return {"id": run_id, "sequence": sequence, "kind": "generation", "phase": "cv",
        "status": "paused", "status_label": "En pause", "is_active": False, "total": sequence,
        "can_retry": True, "created_at": "2026-10-11T10:00:00Z", "items": [{"id": 42}],
        "phase_batches": []}
bridge.generation_workspace = {"desktop_view": True,
    "runs": [{"id": f"gen-{i}", "sequence": i} for i in range(41, 21, -1)],
    "selected_run": run("gen-41", 41), "active_run_id": "", "active_run_count": 0,
    "reserved_job_ids": [43], "resumable_job_ids": [42],
    "history_page": 1, "history_page_size": 20, "history_total_count": 41}
bridge.generationWorkspaceChanged.emit()
app.processEvents()
tracking_page = load_page("TrackingPage.qml")
tracking = tracking_page.findChild(QQuickItem, "generationTrackingBand")
tracking.setProperty("expanded", True)
app.processEvents()
card = tracking.findChild(QQuickItem, "generationRunCard")
assert card.property("run")["id"] == "gen-41"
history_changes = []
tracking.historyOptionsChanged.connect(lambda: history_changes.append(True))
for i in range(20):
    bridge.generation_workspace = {**bridge.generation_workspace,
        "runs": [{**row, "generated": i} for row in bridge.generation_workspace["runs"]],
        "selected_run": {**bridge.generation_workspace["selected_run"], "generated": i}}
    bridge.generationWorkspaceChanged.emit()
    app.processEvents()
assert history_changes == []
previous = tracking.findChild(QQuickItem, "generationPreviousHistoryPageAction")
next_page = tracking.findChild(QQuickItem, "generationNextHistoryPageAction")
assert not previous.property("enabled") and next_page.property("enabled")
invoke(next_page, "clicked")
assert bridge.generation_history_pages == [2]
bridge.generation_workspace = {**bridge.generation_workspace, "history_page": 2,
    "runs": [{"id": f"gen-{i}", "sequence": i} for i in range(21, 1, -1)]}
bridge.generationWorkspaceChanged.emit()
app.processEvents()
assert card.property("run")["id"] == "gen-41"
assert len(card.property("historyOptions").toVariant()) == 21
assert card.findChild(QQuickItem, "analysisRunSelector").property("currentValue") == "gen-41"
assert page.property("generationReservedIds") == [43]
assert page.property("generationResumableIds") == [42]
assert QMetaObject.invokeMethod(card, "runSelected", Qt.DirectConnection, Q_ARG("QString", "gen-3"))
assert bridge.generation_run_selections == ["gen-3"]
bridge.generation_workspace = {**bridge.generation_workspace, "selected_run": run("gen-3", 3)}
bridge.generationWorkspaceChanged.emit()
app.processEvents()
assert card.property("run")["id"] == "gen-3" and tracking.property("expanded")
bridge.generation_workspace = {**bridge.generation_workspace,
    "runs": [{"id": f"gen-{i}", "sequence": i} for i in range(22, 2, -1)]}
bridge.generationWorkspaceChanged.emit()
app.processEvents()
assert card.findChild(QQuickItem, "analysisRunSelector").property("currentValue") == "gen-3"
bridge.generation_workspace = {**bridge.generation_workspace, "history_page": 3,
    "runs": [{"id": "gen-1", "sequence": 1}]}
bridge.generationWorkspaceChanged.emit()
app.processEvents()
assert not next_page.property("enabled") and previous.property("enabled")
invoke(previous, "clicked")
assert bridge.generation_history_pages == [2, 2]
""",
}


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_bounded_qml_keeps_global_contracts(scenario):
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
