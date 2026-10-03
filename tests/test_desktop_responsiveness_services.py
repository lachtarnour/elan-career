"""Unrelated desktop actions remain available while one offer is processed."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from smartapply.concurrency import TaskConflictError, job_operation
from smartapply.database import Application, Job, JobStatus, session_scope, write_session_scope
from smartapply.database.models import JobDuplicateStatus
from smartapply.database.repository import create_or_get_application, upsert_job
from smartapply.desktop.services import DesktopService


def _offer(external_id):
    with write_session_scope() as session:
        job = upsert_job(
            session,
            external_id=external_id,
            title="Engineer",
            company="Acme",
            description="Python et machine learning",
            source="manual",
        )
        session.flush()
        application = create_or_get_application(session, job.id)
        application.cv_pdf_path = "/published/cv.pdf"
        return job.id, application.id


def test_tracking_on_other_offers_and_notes_remain_available_under_lease(isolated_db):
    job_a, app_a = _offer("a")
    job_b, _app_b = _offer("b")
    alias, _alias_app = _offer("alias")
    with write_session_scope() as session:
        job = session.get(Job, alias)
        job.canonical_job_id = job_a
        job.duplicate_review_status = JobDuplicateStatus.CONFIRMED
    entered, release = Event(), Event()

    def processing():
        with job_operation([job_a], operation="generation"):
            entered.set()
            assert release.wait(10)

    service = DesktopService()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(processing)
        assert entered.wait(10)
        try:
            with pytest.raises(TaskConflictError):
                service.archive_job(alias)
            with pytest.raises(TaskConflictError):
                service.mark_job_sent(job_a)
            assert service.set_job_shortlisted(job_a, selected=False)
            assert service.save_application_notes(app_a, "Brouillon conservé")
            assert service.mark_job_sent(job_b)
        finally:
            release.set()
        future.result(timeout=10)
    with session_scope() as session:
        app = session.get(Application, app_a)
        assert app.notes == "Brouillon conservé"
        assert app.status == JobStatus.ANALYZED
        assert session.get(Job, job_b).status == JobStatus.SENT


def test_empty_reservation_cannot_be_marked_sent(isolated_db):
    job_id, app_id = _offer("empty")
    with write_session_scope() as session:
        session.get(Application, app_id).cv_pdf_path = None
    service = DesktopService()
    assert not service.list_jobs()[0].can_send
    assert not service.mark_job_sent(job_id)
    with pytest.raises(ValueError):
        service.update_application(app_id, form_submitted=True)
    assert service.save_application_notes(app_id, "Toujours disponible")


def test_rescuing_a_submitted_dossier_preserves_submission_and_documents(isolated_db, monkeypatch):
    from smartapply.database.repository import mark_analyzed

    service = DesktopService()
    job_id, app_id = _offer("sent-history")
    with write_session_scope() as session:
        mark_analyzed(session, job_id)
    with session_scope() as session:
        analyzed_at = session.get(Job, job_id).analyzed_at
    assert service.mark_job_sent(job_id)
    service.archive_application(app_id)

    def unexpected_pipeline(*args, **kwargs):
        raise AssertionError("A submitted dossier must be reused without generation")

    monkeypatch.setattr("smartapply.pipeline.Pipeline", unexpected_pipeline)
    report = service.rescue_job(job_id)
    assert report["existing"] == 1
    assert report["generated"] == 0
    with session_scope() as session:
        app = session.get(Application, app_id)
        assert app.status == JobStatus.SENT
        assert app.form_submitted_at is not None
        assert app.cv_pdf_path == "/published/cv.pdf"
        assert session.get(Job, job_id).analyzed_at == analyzed_at


def test_manual_offer_is_persisted_without_constructing_providers(isolated_db, monkeypatch):
    def unexpected_provider(*args, **kwargs):
        raise AssertionError("Manual persistence must not construct providers")

    monkeypatch.setattr("smartapply.pipeline.Pipeline", unexpected_provider)
    result = DesktopService().add_manual_offer(
        title="Data Scientist",
        company="Different company",
        location="Paris",
        description="Python et machine learning",
        application_url="https://example.org/jobs/42",
    )
    assert len(result["job_ids"]) == 1
    with session_scope() as session:
        job = session.get(Job, result["job_ids"][0])
        assert job.analyzed_at is None
        assert job.application is None


def test_batch_revalidates_an_offer_archived_while_waiting(isolated_db, monkeypatch):
    from smartapply.database.repository import mark_analyzed
    from smartapply.pipeline.reports import ApplyReport

    job_a, app_a = _offer("first")
    job_b, app_b = _offer("second")
    with write_session_scope() as session:
        # Both offers await generation without a persisted dossier.
        for job_id, app_id in ((job_a, app_a), (job_b, app_b)):
            mark_analyzed(session, job_id)
            session.delete(session.get(Application, app_id))
    calls = []

    class Pipeline:
        def apply_to(self, job_id):
            calls.append(job_id)
            DesktopService().archive_job(job_b)
            return ApplyReport(job_id=job_id, application_id=1)

    monkeypatch.setattr("smartapply.pipeline.Pipeline", Pipeline)
    report = DesktopService().generate_applications([job_a, job_b])
    assert calls == [job_a]
    assert report["generated"] == 1
    assert report["skipped"] == 1


def test_cancelled_shortlist_does_not_construct_provider_or_set_marker(isolated_db, monkeypatch):
    job_id, _app_id = _offer("cancelled")

    def unexpected_pipeline(*args, **kwargs):
        raise AssertionError("A cancelled operation must not start providers")

    monkeypatch.setattr("smartapply.pipeline.Pipeline", unexpected_pipeline)
    report = DesktopService().analyze_and_shortlist([job_id], stop_requested=lambda: True)
    assert report["cancelled"]
    with session_scope() as session:
        assert session.get(Job, job_id).shortlisted_at is None


def test_duplicate_list_does_not_filter_or_write(isolated_db, monkeypatch):
    job_id, _app_id = _offer("pending")
    candidate, _candidate_app = _offer("candidate")
    with write_session_scope() as session:
        job = session.get(Job, job_id)
        job.possible_duplicate_of_id = candidate
        job.duplicate_review_status = JobDuplicateStatus.PENDING

    def unexpected_pipeline(*args, **kwargs):
        raise AssertionError("Opening a list must not start a pipeline")

    monkeypatch.setattr("smartapply.pipeline.Pipeline", unexpected_pipeline)
    assert DesktopService().list_jobs(status=JobStatus.DUPLICATE_REVIEW)[0].id == job_id
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.filtered_at is None
        assert job.archived_at is None


def test_search_filters_the_reference_of_a_new_duplicate_before_returning(isolated_db, monkeypatch):
    from smartapply.pipeline.ingest import IngestCollection, IngestReport

    reference, _reference_app = _offer("reference")
    duplicate, _duplicate_app = _offer("new-duplicate")
    with write_session_scope() as session:
        job = session.get(Job, duplicate)
        job.possible_duplicate_of_id = reference
        job.duplicate_review_status = JobDuplicateStatus.PENDING
    filtered = []

    class Pipeline:
        def collect_source(self, source, *args, **kwargs):
            return IngestCollection(source=source, raw_jobs=[])

        def persist_collection(self, collection):
            return IngestReport(
                source=collection.source, fetched=1, persisted=1, job_ids=[duplicate]
            )

        def filter_pending(self, *, job_ids):
            filtered.extend(job_ids)

    monkeypatch.setattr("smartapply.pipeline.Pipeline", Pipeline)
    monkeypatch.setattr(DesktopService, "source_diagnostics_stale", lambda self: False)
    result = DesktopService().search_jobs(
        query="Engineer",
        location="Paris",
        sources=["serpapi"],
        max_results=10,
        date_posted="week",
    )
    assert not result.errors
    assert filtered == [reference, duplicate]
