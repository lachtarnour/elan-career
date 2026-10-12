"""Journal progress cannot repeatedly serialize the selected offer body."""

from copy import deepcopy

from tests.test_analysis_workspace import make_bridge as make_bridge
from tests.test_analysis_workspace import workspace


def test_progress_and_unchanged_preview_do_not_reread_or_notify(make_bridge):
    bridge = make_bridge()
    reads, changes = [], []
    bridge.service.get_job = lambda job_id: reads.append(job_id) or {"id": job_id, "description": "body"}
    bridge.selectAnalysisOffer(9)
    bridge._read_pool.finish_next()
    bridge.analysisOfferChanged.connect(lambda: changes.append(deepcopy(bridge.analysisOffer)))
    for count in range(20):
        progress = workspace(active=True)
        progress["runs"][0]["analyzed"] = count
        bridge._analysis_controller._loaded(progress)
    assert reads == [9] and not bridge._read_pool.pending and changes == []
    bridge._analysis_offer_controller.refresh()
    bridge._read_pool.finish_next()
    assert reads == [9, 9] and changes == []


def test_actual_offer_change_refreshes_analysis_preview_preserving_offers_context(make_bridge):
    bridge = make_bridge()
    bridge._active_route = "analysis"
    bridge._current_job = {"id": 42, "application": {"notes": "unsaved"}}
    original = deepcopy(bridge.currentJob)
    body = ["old"]
    bridge.service.get_job = lambda job_id: {"id": job_id, "description": body[0]}
    bridge.selectAnalysisOffer(9)
    bridge._read_pool.finish_next()
    body[0] = "new"
    bridge._database_changed({"job_analyses"})
    assert "analysis_offer" in bridge._dirty_views
    bridge._workflow_refresh_timer.stop()
    bridge._flush_refresh()
    bridge._read_pool.finish_next()
    assert bridge.analysisOffer == {"id": 9, "description": "new"}
    assert bridge.currentJob == original
    assert "analysis_offer" not in bridge._dirty_views


def test_checkpoint_only_commit_invalidates_and_schedules_analysis_queue(make_bridge):
    bridge = make_bridge()
    initial = bridge._analysis_controller._candidate_revision
    bridge._database_changed({"analysis_checkpoints"})
    assert bridge._analysis_controller._candidate_revision == initial + 1
    assert bridge._analysis_controller._candidates_dirty
    assert "analysis" in bridge._dirty_views
    assert bridge._workflow_refresh_timer.isActive()
