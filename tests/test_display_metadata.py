"""Derived display fields remain current without loading offer text into lists."""

from sqlalchemy import event, select, update

from smartapply.database import Job, get_engine, session_scope, write_session_scope
from smartapply.database.display_metadata import backfill_display_metadata
from smartapply.database.repository import upsert_job
from smartapply.desktop.services import DesktopService


def _offer(external_id="metadata", description="Vous avez au moins 3 ans d'expérience."):
    with write_session_scope() as session:
        job = upsert_job(
            session,
            external_id=external_id,
            title="Data Scientist",
            company="Acme",
            description=description,
            source="manual",
        )
        session.flush()
        return job.id


def test_metadata_tracks_description_changes_and_keeps_lists_light(isolated_db):
    job_id = _offer()
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(get_engine(), "before_cursor_execute", record)
    try:
        assert DesktopService().list_jobs()[0].experience == "3+ ans"
        assert DesktopService().dashboard().jobs == 1
    finally:
        event.remove(get_engine(), "before_cursor_execute", record)
    assert all("jobs.description" not in statement for statement in statements)
    assert all("jobs.cleaned_description" not in statement for statement in statements)
    with write_session_scope() as session:
        session.get(Job, job_id).cleaned_description = "Vous avez au moins 2 ans d'expérience."
    assert DesktopService().list_jobs()[0].experience == "2+ ans"


def test_legacy_metadata_fallback_is_read_only_then_backfilled(isolated_db):
    job_id = _offer()
    with write_session_scope() as session:
        session.execute(update(Job).where(Job.id == job_id).values(display_metadata=None))
    assert DesktopService().list_jobs()[0].experience == "3+ ans"
    with session_scope() as session:
        assert session.get(Job, job_id).display_metadata is None
    assert backfill_display_metadata() == 1
    assert backfill_display_metadata() == 0


def test_metadata_backfill_does_not_overwrite_changed_text(isolated_db, monkeypatch):
    import smartapply.database.display_metadata as metadata

    job_id = _offer()
    with write_session_scope() as session:
        session.execute(update(Job).where(Job.id == job_id).values(display_metadata=None))
    original = metadata.build_display_metadata

    def change_during_calculation(description, cleaned_description):
        with write_session_scope() as session:
            session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    description="Vous avez au moins 1 an d'expérience.",
                    display_metadata=None,
                )
            )
        return original(description, cleaned_description)

    monkeypatch.setattr(metadata, "build_display_metadata", change_during_calculation)
    assert backfill_display_metadata() == 0
    monkeypatch.setattr(metadata, "build_display_metadata", original)
    assert backfill_display_metadata() == 1
    with session_scope() as session:
        value = session.scalar(select(Job.display_metadata).where(Job.id == job_id))
        assert value["required_min_years"] == 1


def test_metadata_backfill_stops_between_batches(isolated_db):
    from threading import Event

    stopped = Event()
    for index in range(2):
        _offer(str(index))
    with write_session_scope() as session:
        session.execute(update(Job).values(display_metadata=None))

    def stop_after_write(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE jobs"):
            stopped.set()

    event.listen(get_engine(), "after_cursor_execute", stop_after_write)
    try:
        assert backfill_display_metadata(stopped.is_set, batch_size=1) == 1
    finally:
        event.remove(get_engine(), "after_cursor_execute", stop_after_write)
    assert backfill_display_metadata() == 1
