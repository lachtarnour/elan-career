"""Evidence, active classification and stored progress must agree everywhere."""

import copy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.models import Application, GeneratedDocument, Job, JobAnalysis, JobScore
from smartapply.database.repository import mark_analyzed, set_score
from smartapply.desktop.services import DesktopService
from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.jobsearch.offer_state import (
    FILTER_HISTORY_KEY,
    completed_offer_expression,
    offer_state,
)
from smartapply.maintenance.filter_labels import complete_missing_filter_labels
from smartapply.maintenance.offer_states import reconcile_offer_states
from smartapply.pipeline.process.analysis_admission import analysis_queue_job_ids
from smartapply.pipeline.process.filter_state import is_current_hard_reject_survivor


def _job(session, key="offer"):
    job = Job(external_id=key, title="ML Engineer", company="Fixture", source="manual",
              description="Python ML", status="filtered", filtered_at=datetime.now(timezone.utc))
    job.score = JobScore(components={
        "filter_disposition": "pending_relevance",
        "hard_reject": HardRejectResult(decisions=()).audit_payload(),
        "independent_audit": {"keep": True},
    })
    session.add(job)
    session.flush()
    return job


@pytest.mark.parametrize("evidence,expected", [
    ("analysis", "analyzed"), ("timestamp", "analyzed"),
    ("generated", "ready_for_form_submission"), ("document", "ready_for_form_submission"),
    ("submitted", "sent"), ("sent", "sent"), ("interview", "interview"),
    ("legacy_ready", "ready_for_form_submission"), ("legacy_analyzed", "analyzed"),
])
def test_completed_evidence_overrides_stale_labels_and_new_queue(isolated_db, evidence, expected):
    with session_scope() as session:
        job = _job(session)
        if evidence == "analysis":
            job.analysis = JobAnalysis(fit_score=0.8)
        elif evidence == "timestamp":
            job.analyzed_at = datetime.now(timezone.utc)
        elif evidence.startswith("legacy_"):
            job.status = expected
        else:
            job.application = Application(status="analyzed")
            if evidence == "generated":
                job.application.cv_pdf_path = "/published/cv.pdf"
            elif evidence == "document":
                job.application.documents = [GeneratedDocument(doc_type="cv_json", content="saved")]
            elif evidence == "submitted":
                job.application.form_submitted_at = datetime.now(timezone.utc)
            else:
                job.application.status = evidence
        session.flush()
        state = offer_state(job)
        assert state.status == expected and state.completed
        assert not state.filter_disposition and not state.awaits_classification
        assert session.scalar(select(Job.id).where(completed_offer_expression())) == job.id
        assert analysis_queue_job_ids(session) == []
        job_id = job.id
    service = DesktopService()
    assert not service.list_jobs(status="scraped")
    row = service.get_job(job_id)
    assert row.status == expected and not row.filter_disposition and not row.classification_summary


@pytest.mark.parametrize("cv_json", [None, {}])
def test_empty_reservation_does_not_fabricate_completed_work(isolated_db, cv_json):
    with session_scope() as session:
        job = _job(session)
        job.application = Application(status="analyzed", cv_json=cv_json,
                                      updated_at=datetime.now(timezone.utc) - timedelta(hours=1))
        session.flush()
        state = offer_state(job)
        assert not state.completed and not state.analyzed and state.awaits_classification
        assert session.scalar(select(Job.id).where(completed_offer_expression())) is None
    assert len(DesktopService().list_jobs(status="scraped", filter_disposition="pending_relevance")) == 1


def test_repair_is_idempotent_preserves_paid_results_and_keeps_filter_proof(isolated_db):
    with session_scope() as session:
        job = _job(session)
        job.analysis = JobAnalysis(fit_score=0.8, raw_response={"paid": "preserve"},
                                   created_at=datetime(2020, 1, 1))
        job.application = Application(status="sent", form_submitted_at=datetime(2021, 1, 1),
                                      notes="Keep", cv_json={"paid": "preserve"})
        job.application.documents = [GeneratedDocument(doc_type="cv", content="Keep")]
        session.flush()
        job_id = job.id
        originals = {name: copy.deepcopy({c.name: getattr(row, c.name) for c in row.__table__.columns})
                     for name, row in (("analysis", job.analysis), ("application", job.application),
                                       ("document", job.application.documents[0]))}
        components = copy.deepcopy(job.score.components)
        score_date = job.score.computed_at
    changes = reconcile_offer_states()
    assert len(changes) == 1 and reconcile_offer_states() == []
    assert complete_missing_filter_labels() == 0
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.status == "sent" and job.analyzed_at == datetime(2020, 1, 1)
        assert "filter_disposition" not in job.score.components
        assert job.score.components[FILTER_HISTORY_KEY]["disposition"] == "pending_relevance"
        assert {k: v for k, v in job.score.components.items() if k != FILTER_HISTORY_KEY} == {
            k: v for k, v in components.items() if k != "filter_disposition"}
        assert is_current_hard_reject_survivor(job.score.components)
        assert job.score.computed_at == score_date.replace(tzinfo=None)
        for name, row in (("analysis", job.analysis), ("application", job.application),
                          ("document", job.application.documents[0])):
            actual = {c.name: getattr(row, c.name) for c in row.__table__.columns}
            expected = originals[name]
            expected = {k: v.replace(tzinfo=None) if isinstance(v, datetime) else v for k, v in expected.items()}
            assert actual == expected


def test_new_analysis_retires_routing_and_filter_refresh_cannot_restore_it(isolated_db):
    with session_scope() as session:
        job = _job(session)
        job.analysis = JobAnalysis(fit_score=0.8)
        mark_analyzed(session, job.id)
        assert "filter_disposition" not in job.score.components
        set_score(session, job.id, components={"filter_disposition": "pending_relevance",
                                             "hard_reject": HardRejectResult(decisions=()).audit_payload()})
        assert "filter_disposition" not in job.score.components


def test_pending_duplicates_cannot_make_a_completed_offer_pending_again(isolated_db):
    with session_scope() as session:
        reference = _job(session, "reference")
        reference.analysis = JobAnalysis(fit_score=0.8)
        recent = _job(session, "recent")
        recent.duplicate_review_status = "pending"
        recent.possible_duplicate_of_id = reference.id
        recent_id = recent.id
    service = DesktopService()
    assert [row.id for row in service.list_jobs(
        status="duplicate_review", only_pending_duplicates=True,
    )] == [recent_id]
    assert [row.id for row in service.list_jobs(status="duplicate_review")] == [recent_id]
    assert not service.get_job(reference.id).filter_disposition


@pytest.mark.parametrize("application_status", ["archived", "interview"])
def test_tracking_state_prevents_sending_a_generated_dossier_again(isolated_db, application_status):
    with session_scope() as session:
        job = _job(session)
        job.application = Application(status=application_status, cv_pdf_path="/saved/cv.pdf")
        job_id = job.id
    row = DesktopService().get_job(job_id)
    assert not row.can_send and not row.can_generate and not row.filter_disposition
