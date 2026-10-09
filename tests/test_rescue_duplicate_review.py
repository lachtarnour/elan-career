"""Restored duplicate sources require a decision before any paid processing."""

from datetime import datetime, timezone

import pytest

from smartapply.database import session_scope
from smartapply.database.models import Application, Job, JobAnalysis, JobScore, JobStatus
from smartapply.desktop.services import DesktopService


def _job(session, key, *, application_only=False):
    job = Job(external_id=key, title="ML Engineer", company="Fixture", source="fixture",
              description="Python ML", status="analyzed" if application_only else "archived",
              archived_at=None if application_only else datetime.now(timezone.utc))
    job.analysis = JobAnalysis(fit_score=0.8, raw_response={"preserved": key})
    job.score = JobScore(components={"filter_disposition": "rejected",
                                    "rejection_stage": "local_filter"})
    job.application = Application(status="archived", cv_pdf_path=f"/{key}/cv.pdf",
                                  notes=f"Notes {key}", cv_json={"preserved": key})
    session.add(job)
    session.flush()
    return job


@pytest.mark.parametrize("selected_reference", [False, True])
@pytest.mark.parametrize("application_only", [False, True])
def test_rescue_pending_pair_with_archived_documents_stops_before_pipeline(
    isolated_db, monkeypatch, selected_reference, application_only,
):
    with session_scope() as session:
        reference = _job(session, "reference", application_only=application_only)
        pending = _job(session, "pending", application_only=application_only)
        pending.possible_duplicate_of_id = reference.id
        pending.duplicate_review_status = "pending"
        reference_id, pending_id = reference.id, pending.id
        originals = {job.id: (job.application.id, job.application.cv_json,
                              job.application.cv_pdf_path, job.application.notes,
                              job.analysis.raw_response) for job in (reference, pending)}
    monkeypatch.setattr("smartapply.desktop.services._pipeline",
                        lambda *args: pytest.fail("No extraction, fit or generation"))
    service = DesktopService()
    report = service.rescue_job(reference_id if selected_reference else pending_id)
    assert report["generated"] == report["failed"] == 0
    assert report["rescued"] == 1 and report["duplicate_review_ids"] == [pending_id]
    assert service.get_job(pending_id).status == "duplicate_review"
    assert service.get_job(pending_id).filter_disposition != "rejected"
    assert service.get_job(pending_id).duplicate_candidate.id == reference_id
    assert [row.id for row in service.list_jobs(status="duplicate_review",
                                               only_pending_duplicates=True)] == [pending_id]
    assert pending_id not in {row.id for row in service.list_jobs(status="archived")}
    assert service.get_job(reference_id).status == "duplicate_review"
    assert reference_id not in {row.id for row in service.list_jobs(status="archived")}
    assert not service.get_job(reference_id).can_generate
    with session_scope() as session:
        for job_id, previous in originals.items():
            job = session.get(Job, job_id)
            assert job.archived_at is None and job.status != JobStatus.ARCHIVED
            assert job.score.components["manual_rescue"]["schema_version"] == 3
            assert job.application.status == "archived"
            assert (job.application.id, job.application.cv_json, job.application.cv_pdf_path,
                    job.application.notes, job.analysis.raw_response) == previous
        assert session.query(Application).count() == 2


def test_rescue_confirmed_root_opens_associated_review_without_moving_documents(isolated_db, monkeypatch):
    with session_scope() as session:
        reference = _job(session, "reference", application_only=True)
        alias = _job(session, "alias")
        alias.canonical_job_id = reference.id
        alias.duplicate_review_status = "confirmed"
        reference_id, alias_id = reference.id, alias.id
        application_ids = reference.application.id, alias.application.id
    monkeypatch.setattr("smartapply.desktop.services._pipeline",
                        lambda *args: pytest.fail("No generation before review"))
    service = DesktopService()
    report = service.rescue_jobs([reference_id, alias_id])
    assert report["rescued"] == report["skipped"] == 1
    assert report["generated"] == report["failed"] == 0
    assert report["duplicate_review_ids"] == [alias_id]
    assert service.get_job(alias_id).status == "duplicate_review"
    assert service.get_job(reference_id).status == "duplicate_review"
    assert reference_id not in {row.id for row in service.list_jobs(status="archived")}
    with session_scope() as session:
        assert session.get(Job, alias_id).canonical_job_id is None
        assert session.get(Job, alias_id).possible_duplicate_of_id == reference_id
        previous_relation = session.get(Job, alias_id).score.components["manual_rescue"]["previous_duplicate"]
        assert previous_relation["canonical_job_id"] == reference_id
        assert previous_relation["duplicate_review_status"] == "confirmed"
        assert (session.get(Job, reference_id).application.id,
                session.get(Job, alias_id).application.id) == application_ids


def test_existing_pending_review_takes_priority_over_confirmed_alias(isolated_db, monkeypatch):
    with session_scope() as session:
        reference = _job(session, "reference")
        alias = _job(session, "alias")
        alias.canonical_job_id = reference.id
        alias.duplicate_review_status = "confirmed"
        pending = _job(session, "pending")
        pending.possible_duplicate_of_id = reference.id
        pending.duplicate_review_status = "pending"
        reference_id, alias_id, pending_id = reference.id, alias.id, pending.id
    monkeypatch.setattr("smartapply.desktop.services._pipeline",
                        lambda *args: pytest.fail("No generation before review"))
    report = DesktopService().rescue_job(alias_id)
    assert report["duplicate_review_ids"] == [pending_id]
    assert report["generated"] == report["failed"] == 0
    with session_scope() as session:
        assert session.get(Job, alias_id).canonical_job_id == reference_id
        assert session.get(Job, alias_id).duplicate_review_status == "confirmed"


def test_rescue_archived_reference_protects_an_already_active_pending_pair(isolated_db, monkeypatch):
    from smartapply.dedup.review_decisions import plan_pending_review_decisions

    with session_scope() as session:
        reference = _job(session, "reference")
        pending = Job(external_id="pending", title=reference.title, company=reference.company,
                      source="fixture", description="Unrelated original text", status="scraped",
                      duplicate_review_status="pending", duplicate_match_type="fuzzy",
                      possible_duplicate_of_id=reference.id)
        session.add(pending)
        session.flush()
        reference_id, pending_id = reference.id, pending.id
    monkeypatch.setattr("smartapply.desktop.services._pipeline",
                        lambda *args: pytest.fail("No processing before review"))
    report = DesktopService().rescue_job(reference_id)
    assert report["duplicate_review_ids"] == [pending_id]
    with session_scope() as session:
        assert session.get(Job, pending_id).score.components["manual_rescue"]["schema_version"] == 3
        assert plan_pending_review_decisions(session.query(Job).all()) == ()
