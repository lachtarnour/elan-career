"""Heavy repairs run once per rules version and resume after interruption."""

from threading import Event

from sqlalchemy import delete

from smartapply.database import init_db, session_scope
from smartapply.database.models import AppSetting, Job
from smartapply.database.session import (
    run_startup_maintenance,
    startup_maintenance_pending,
)


def make_pending():
    with session_scope() as session:
        session.execute(delete(AppSetting).where(AppSetting.key == "desktop_startup_maintenance"))


def test_light_initialization_defers_repairs_and_version_skips_future_passes(
    isolated_db, monkeypatch,
):
    make_pending()
    calls = []
    monkeypatch.setattr("smartapply.database.session.backfill_duplicate_reviews",
                        lambda **kwargs: calls.append("duplicates") or 0)
    monkeypatch.setattr("smartapply.database.display_metadata.backfill_display_metadata",
                        lambda **kwargs: calls.append("display") or 0)
    init_db(run_maintenance=False)
    assert startup_maintenance_pending()
    assert calls == []
    assert run_startup_maintenance()
    assert not startup_maintenance_pending()
    assert calls == ["duplicates", "display"]
    assert run_startup_maintenance()
    assert calls == ["duplicates", "display"]


def test_duplicate_repair_resumes_after_a_committed_batch(isolated_db, monkeypatch):
    import smartapply.database.session as module

    make_pending()
    with session_scope() as session:
        session.add_all([
            Job(external_id=f"repair-{index}", title="AI Engineer", source_company="Acme",
                location="Paris", description="Develop Python machine learning systems " * 20,
                source="manual") for index in range(3)
        ])
    cancelled = Event()
    repair = module.backfill_duplicate_reviews
    monkeypatch.setattr(module, "backfill_duplicate_reviews",
                        lambda **kwargs: repair(batch_size=1, **kwargs))
    unsubscribe = module.subscribe_database_changes(cancelled.set)
    try:
        assert not run_startup_maintenance(stop_requested=cancelled.is_set)
    finally:
        unsubscribe()
    assert startup_maintenance_pending()
    assert run_startup_maintenance()
    assert not startup_maintenance_pending()
    with session_scope() as session:
        assert session.query(Job).filter(Job.possible_duplicate_of_id.is_not(None)).count() == 2


def test_interrupted_maintenance_keeps_version_pending_for_resume(isolated_db, monkeypatch):
    make_pending()
    cancelled = Event()
    original = __import__("smartapply.database.session", fromlist=["backfill_shortlisted_at"])
    original_step = original.backfill_shortlisted_at

    def interrupt_after_first_commit():
        result = original_step()
        cancelled.set()
        return result

    monkeypatch.setattr(original, "backfill_shortlisted_at", interrupt_after_first_commit)
    assert not run_startup_maintenance(stop_requested=cancelled.is_set)
    assert startup_maintenance_pending()
    monkeypatch.setattr(original, "backfill_shortlisted_at", original_step)
    assert run_startup_maintenance()
    assert not startup_maintenance_pending()
