"""Historical document-review failures remain in the available dossier queue."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject

from smartapply.database import Application, Job, JobStatus, session_scope
from smartapply.database.models import JobDuplicateStatus, ShortlistOrigin
from smartapply.database.repository import create_or_get_application, upsert_job
from smartapply.desktop.bridge import DesktopBridge, _tone
from smartapply.desktop.services import DesktopService
from smartapply.jobsearch.status import STATUS_FLOW, STATUS_LABELS, status_label
from smartapply.jobsearch.workflow import APPLICATION_STATUSES, next_action_for


def _dossier(session, name, status):
    job = upsert_job(
        session,
        external_id=f"compat:{name}",
        title=name,
        company="Doctolib",
        description="Research Scientist",
        source="manual",
    )
    job.analyzed_at = datetime.now(timezone.utc)
    application = create_or_get_application(session, job.id)
    application.status = status
    job.status = status
    return job, application


def test_legacy_review_dossier_is_ready_everywhere_without_rewriting_it(isolated_db):
    warnings = [
        "validation_error:letter_too_long:245",
        "text_review_incomplete: contrôle incomplet",
    ]
    with session_scope() as session:
        job, application = _dossier(session, "Research Scientist", JobStatus.QUALITY_REJECTED)
        application.validation_warnings = warnings
        application.notes = "Notes à conserver"
        application.cv_json = {"professional_summary": "Résumé à conserver"}
        application.cv_pdf_path = "/documents/original.pdf"
        job.shortlisted_at = datetime.now(timezone.utc)
        job.shortlist_origin = ShortlistOrigin.MANUAL
        shortlisted_at = job.shortlisted_at.replace(tzinfo=None)
        job_id, application_id = job.id, application.id
        other, _ = _dossier(session, "Data Scientist", JobStatus.READY_FOR_FORM_SUBMISSION)
        ready_ids = {job_id, other.id}

    service = DesktopService()
    for requested in (JobStatus.READY_FOR_FORM_SUBMISSION, JobStatus.QUALITY_REJECTED):
        rows = service.list_jobs(status=requested)
        assert {row.id for row in rows} == ready_ids
        assert {row.status for row in rows} == {JobStatus.READY_FOR_FORM_SUBMISSION}
        assert {row.status_label for row in rows} == {"Prête"}

    detail = service.get_job(job_id)
    assert detail.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert detail.shortlisted is True
    assert detail.application.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert detail.application.warnings == tuple(warnings)
    assert detail.application.notes == "Notes à conserver"
    assert detail.application.cv_pdf_path == "/documents/original.pdf"
    assert service.get_application(application_id).status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert service.list_jobs(status=JobStatus.SHORTLISTED) == []
    dashboard = service.dashboard()
    assert dashboard.ready == 2
    assert dashboard.review == 0
    assert {item.status for item in dashboard.recent} == {JobStatus.READY_FOR_FORM_SUBMISSION}

    with session_scope() as session:
        app = session.get(Application, application_id)
        job = session.get(Job, job_id)
        assert app.status == JobStatus.QUALITY_REJECTED
        assert app.validation_warnings == warnings
        assert app.cv_json == {"professional_summary": "Résumé à conserver"}
        assert job.status == JobStatus.QUALITY_REJECTED
        assert job.shortlisted_at == shortlisted_at
        assert job.shortlist_origin == ShortlistOrigin.MANUAL


def test_ready_filter_and_counter_keep_archives_duplicates_and_tracking_states_separate(
    isolated_db,
):
    with session_scope() as session:
        ready, _ = _dossier(session, "Available", JobStatus.QUALITY_REJECTED)
        ready_id = ready.id
        archived_ids = set()
        for status in (JobStatus.QUALITY_REJECTED, JobStatus.READY_FOR_FORM_SUBMISSION):
            for marker in ("timestamp", "status", "confirmed-alias"):
                job, _ = _dossier(session, f"{status}-{marker}", status)
                if marker == "timestamp":
                    job.archived_at = datetime.now(timezone.utc)
                else:
                    job.status = JobStatus.ARCHIVED
                if marker == "confirmed-alias":
                    job.canonical_job_id = ready_id
                    job.duplicate_review_status = JobDuplicateStatus.CONFIRMED
                archived_ids.add(job.id)
        pending, _ = _dossier(session, "Pending duplicate", JobStatus.QUALITY_REJECTED)
        pending.duplicate_review_status = JobDuplicateStatus.PENDING
        pending.possible_duplicate_of_id = ready_id
        pending_id = pending.id
        tracked_ids = {}
        for status in (JobStatus.SENT, JobStatus.INTERVIEW, JobStatus.REJECTED):
            job, _ = _dossier(session, status, status)
            tracked_ids[status] = job.id

    service = DesktopService()
    assert [row.id for row in service.list_jobs(status=JobStatus.READY_FOR_FORM_SUBMISSION)] == [
        ready_id
    ]
    assert service.dashboard().ready == 1
    assert {row.id for row in service.list_jobs(status=JobStatus.ARCHIVED)} == archived_ids
    assert [row.id for row in service.list_jobs(status=JobStatus.DUPLICATE_REVIEW)] == [pending_id]
    for status, job_id in tracked_ids.items():
        assert [row.id for row in service.list_jobs(status=status)] == [job_id]
        assert service.get_job(job_id).status == status


def test_review_state_is_not_a_label_or_tracking_option():
    assert JobStatus.QUALITY_REJECTED not in APPLICATION_STATUSES
    assert all(row["status"] != JobStatus.QUALITY_REJECTED for row in STATUS_FLOW)
    assert JobStatus.QUALITY_REJECTED not in dict(DesktopService.job_statuses())
    assert status_label(JobStatus.QUALITY_REJECTED) == "Prête"
    assert STATUS_LABELS[JobStatus.QUALITY_REJECTED] == "Prête"
    assert next_action_for(JobStatus.QUALITY_REJECTED) == next_action_for(
        JobStatus.READY_FOR_FORM_SUBMISSION
    )
    assert _tone(JobStatus.QUALITY_REJECTED) == _tone(JobStatus.READY_FOR_FORM_SUBMISSION)


def test_opening_legacy_dossier_routes_to_accessible_ready_filter():
    bridge = DesktopBridge.__new__(DesktopBridge)
    QObject.__init__(bridge)
    selected, routes = [], []
    bridge.selectJob = selected.append
    bridge.navigationRequested.connect(lambda *args: routes.append(args))
    bridge._application_opened_in_jobs(
        SimpleNamespace(job_id=2622, status=JobStatus.QUALITY_REJECTED)
    )
    assert routes == [(f"jobs?status={JobStatus.READY_FOR_FORM_SUBMISSION}", 2622)]
    assert selected == [2622]


@pytest.mark.parametrize("shortlisted", [False, True])
def test_stale_empty_reservation_reappears_as_regenerable_offer(isolated_db, shortlisted):
    old = datetime.now(timezone.utc) - timedelta(days=1)
    with session_scope() as session:
        job, application = _dossier(session, "Interrupted generation", JobStatus.ANALYZED)
        if shortlisted:
            job.shortlisted_at = old
            job.shortlist_origin = ShortlistOrigin.MANUAL
        application.created_at = application.updated_at = old
        application.notes = "Note historique"
        job_id, application_id = job.id, application.id
        recent, recent_application = _dossier(session, "In progress", JobStatus.ANALYZED)
        recent_id = recent.id
        recent_application.created_at = recent_application.updated_at = datetime.now(timezone.utc)
        sent, sent_application = _dossier(session, "Manually sent", JobStatus.SENT)
        sent_application.created_at = sent_application.updated_at = old
        sent_id = sent.id

    expected_status = JobStatus.SHORTLISTED if shortlisted else JobStatus.ANALYZED
    service = DesktopService()
    rows = service.list_jobs(status=expected_status)
    assert [row.id for row in rows] == [job_id]
    row = rows[0]
    assert row.status == expected_status
    assert row.can_generate is True
    assert row.can_send is False
    assert row.application_id is None
    assert row.related_application_id is None
    assert service.get_job(job_id).application is None
    assert service.get_job(recent_id).can_generate is False
    assert service.get_job(sent_id).status == JobStatus.SENT
    assert service.get_job(sent_id).can_generate is False
    assert service.list_jobs(status=JobStatus.READY_FOR_FORM_SUBMISSION) == []
    assert service.dashboard().analyzed == (0 if shortlisted else 1)
    with session_scope() as session:
        application = session.get(Application, application_id)
        assert application is not None
        assert application.status == JobStatus.ANALYZED
        assert application.cv_json is None
        assert application.notes == "Note historique"
        assert application.updated_at == old.replace(tzinfo=None)
