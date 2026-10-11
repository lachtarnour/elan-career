"""Shortlist counters preserve reservation guards without loading document bodies."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from smartapply.database import Application, Job, JobDuplicateStatus, JobStatus, session_scope
from smartapply.database.generation_runs import GenerationCall, GenerationItem, GenerationRun
from smartapply.database.models import GeneratedDocument
from smartapply.database.session import get_engine
from smartapply.desktop.services import DesktopService
from smartapply.pipeline.generation.offer_resume import resumable_generation_job_ids
from smartapply.pipeline.snapshots import _digest, job_snapshot_key
from tests.test_desktop_services import _job, _prepare_analyzed_offer


def _selected_jobs(count):
    ids = [_job(external_id=f"projection:{index}", description="body " * 4000)
           for index in range(count)]
    with session_scope() as session:
        for index, job_id in enumerate(ids):
            _prepare_analyzed_offer(session, job_id)
            job = session.get(Job, job_id)
            job.shortlisted_at = datetime.now(timezone.utc) + timedelta(seconds=index)
            job.source_data = {"raw": "source payload " * 4000}
    return ids


def _item(session, job_id, *, run_id, run_state="paused", created_at=None, item_state="pending"):
    job = session.get(Job, job_id)
    session.add(GenerationRun(
        id=run_id, mode="batch", state=run_state,
        profile_data={"large": "profile " * 4000}, routes={}, reference_date="2026-10-11",
        created_at=created_at or datetime.now(timezone.utc),
    ))
    session.flush()
    item = GenerationItem(
        run_id=run_id, job_id=job_id, title=job.title, company=job.source_company,
        state=item_state, job_snapshot=job_snapshot_key(job),
        analysis_snapshot=_digest({column.name: getattr(job.analysis, column.name)
                                   for column in job.analysis.__table__.columns}),
    )
    session.add(item)
    session.flush()
    return item


def test_shortlist_projection_preserves_stale_documents_group_reserved_and_resume_rules(isolated_db):
    ids = _selected_jobs(12)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    with session_scope() as session:
        for index in range(1, 6):
            application = Application(
                job_id=ids[index], status=JobStatus.ANALYZED,
                created_at=old, updated_at=old if index != 2 else datetime.now(timezone.utc),
                cv_json={"cv": "CV body " * 4000} if index == 3 else {},
                cv_pdf_path="saved.pdf" if index == 5 else None,
                notes="notes " * 4000,
            )
            session.add(application)
            session.flush()
            if index == 4:
                session.add(GeneratedDocument(
                    application_id=application.id, doc_type="letter",
                    content="document " * 4000, extra={"raw": "doc payload " * 4000},
                ))
        session.get(Job, ids[6]).canonical_job_id = ids[3]
        session.get(Job, ids[9]).duplicate_review_status = JobDuplicateStatus.PENDING
        session.get(Job, ids[10]).archived_at = old
        session.get(Job, ids[11]).analyzed_at = None
        _item(session, ids[7], run_id="reserved", run_state="active")
        _item(session, ids[8], run_id="resumable")
    service = DesktopService()
    summary = service.shortlist_summary()
    assert summary.total == 9
    assert summary.ready_to_generate == 3
    assert service.shortlisted_generation_ids() == [ids[0], ids[1], ids[8]]


@pytest.mark.parametrize("cv_json", [None, {}, [], False, 0, "", {"cv": "saved"}, ["saved"], True, 1, "saved"])
def test_shortlist_projection_keeps_python_truth_of_legacy_cv_json(isolated_db, cv_json):
    job_id = _selected_jobs(1)[0]
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    with session_scope() as session:
        session.add(Application(job_id=job_id, cv_json=cv_json, created_at=old, updated_at=old))
    assert DesktopService().shortlist_summary().ready_to_generate == int(not bool(cv_json))
    assert DesktopService().shortlisted_generation_ids() == ([job_id] if not cv_json else [])


def test_summary_reads_only_identity_and_reservation_columns_without_history_profiles(isolated_db):
    job_id = _selected_jobs(1)[0]
    with session_scope() as session:
        app = Application(job_id=job_id, cv_json={"large": "cv " * 4000}, notes="notes " * 4000)
        session.add(app)
        session.flush()
        session.add(GeneratedDocument(
            application_id=app.id, doc_type="cv", content="document " * 4000,
            extra={"large": "extra " * 4000},
        ))
        _item(session, job_id, run_id="completed-history", run_state="completed", item_state="completed")
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(get_engine(), "before_cursor_execute", record)
    try:
        summary = DesktopService().shortlist_summary()
        assert (summary.total, summary.ready_to_generate) == (1, 0)
        assert DesktopService().shortlisted_generation_ids() == []
    finally:
        event.remove(get_engine(), "before_cursor_execute", record)
    assert len(statements) <= 10
    assert all("jobs.description" not in statement for statement in statements)
    assert all("jobs.cleaned_description" not in statement for statement in statements)
    assert all("jobs.source_data" not in statement for statement in statements)
    assert all("applications.notes" not in statement for statement in statements)
    assert all("applications.cv_json" not in statement for statement in statements)
    assert all("applications.validation_warnings" not in statement for statement in statements)
    assert all("generated_documents_1.content" not in statement for statement in statements)
    assert all("generated_documents_1.extra" not in statement for statement in statements)
    assert all("generation_runs.profile_data" not in statement for statement in statements)


def test_scoped_resume_matches_global_and_selects_latest_before_run_state_filter(isolated_db):
    ids = _selected_jobs(4)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    with session_scope() as session:
        for index, job_id in enumerate(ids):
            item = _item(session, job_id, run_id=f"old:{index}", created_at=old)
            if index == 3:
                session.add(GenerationCall(
                    item_id=item.id, position=0, purpose="cv_adaptation", schema_name="CV",
                    provider="mock", request_body={"large": "request " * 4000},
                    request_key="key", state="submitted",
                ))
        _item(session, ids[1], run_id="new:active", run_state="active")
        _item(session, ids[2], run_id="new:terminal", item_state="stale")
    with session_scope() as session:
        global_ids = resumable_generation_job_ids(session)
        assert global_ids == {ids[0], ids[2]}
    loaded_job_ids = []

    def record_item(_session, instance):
        if isinstance(instance, GenerationItem):
            loaded_job_ids.append(instance.job_id)

    event.listen(Session, "loaded_as_persistent", record_item)
    try:
        for selected in [[], ids[:1], ids[1:2], ids[:2], ids[2:], ids]:
            loaded_job_ids.clear()
            with session_scope() as session:
                assert resumable_generation_job_ids(session, selected) == global_ids.intersection(selected)
                assert set(loaded_job_ids).issubset(selected)
    finally:
        event.remove(Session, "loaded_as_persistent", record_item)
    with session_scope() as session:
        # Submitted request bodies and saved profiles remain out of readiness reads.
        statements = []

        def record(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        event.listen(get_engine(), "before_cursor_execute", record)
        try:
            assert resumable_generation_job_ids(session, [ids[0]]) == {ids[0]}
        finally:
            event.remove(get_engine(), "before_cursor_execute", record)
        assert all("generation_runs.profile_data" not in statement for statement in statements)
        assert all("generation_calls.request_body" not in statement for statement in statements)
        assert all("generation_calls.response_data" not in statement for statement in statements)
        assert all("generation_items.result" not in statement for statement in statements)
        assert all("generated_documents_1.content" not in statement for statement in statements)
        assert all("applications_1.notes" not in statement for statement in statements)
