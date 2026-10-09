"""Restoring an offer keeps its duplicate decision under human control."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.models import Application, DuplicateReviewEvent, Job
from smartapply.database.repository import mark_archived, rescue_archived_job, upsert_job
from smartapply.dedup.review_decisions import plan_pending_review_decisions
from smartapply.maintenance.duplicate_reviews import reconcile_pending_reviews

AT = datetime(2026, 10, 1, tzinfo=timezone.utc)
DESCRIPTION = (
    "Develop production machine learning pipelines in Python, PyTorch and SQL. "
    "Evaluate models, monitor deployments, document experiments and collaborate "
    "with product teams. Build and maintain data validation and model serving. "
) * 3


def _restored_review_pair(session, scenario):
    reference = upsert_job(
        session,
        external_id="serpapi:restored-reference",
        title="Data Scientist",
        company="Acme",
        location="Paris",
        description=DESCRIPTION,
        source="serpapi",
        application_url="https://acme.test/jobs/reference",
        contract_type="CDI",
    )
    restored = upsert_job(
        session,
        external_id="linkedin:restored-review",
        title="Data Scientist",
        company="Acme",
        location="Paris",
        description=DESCRIPTION,
        source="linkedin",
        application_url="https://acme.test/jobs/restored",
        contract_type="CDI",
    )
    reference.published_date = reference.scraped_at = AT
    restored.published_date = restored.scraped_at = AT + timedelta(days=1)
    if scenario == "outside_window":
        reference.published_date = reference.scraped_at = AT - timedelta(days=40)
    elif scenario == "unsupported_fuzzy":
        restored.title = "Infirmier de bloc opératoire"
        restored.description = (
            "Prendre en charge les patients, préparer le matériel chirurgical, "
            "assister les chirurgiens et assurer la surveillance post-opératoire. "
        ) * 3
    restored.possible_duplicate_of_id = reference.id
    restored.duplicate_review_status = "pending"
    restored.duplicate_match_type = "fuzzy"
    restored.duplicate_confidence = 0.99
    mark_archived(session, restored.id, origin="manual")
    rescue_archived_job(session, restored.id, justification="Créer après désarchivage")
    assert restored.score.components["manual_rescue"]["schema_version"] == 3
    assert restored.score.components["manual_rescue"]["rescued_at"]
    session.flush()
    return reference, restored


def _snapshot(job):
    return {
        field: getattr(job, field)
        for field in (
            "possible_duplicate_of_id", "canonical_job_id", "duplicate_review_status",
            "duplicate_match_type", "duplicate_confidence", "status", "archived_at",
            "filtered_at", "analyzed_at", "shortlisted_at",
        )
    }


@pytest.mark.parametrize("scenario", ["strict_copy", "outside_window", "unsupported_fuzzy"])
def test_planning_preserves_explicitly_restored_duplicate_and_its_reference(
    isolated_db, scenario,
):
    with session_scope() as session:
        reference, restored = _restored_review_pair(session, scenario)
        before = [_snapshot(job) for job in (reference, restored)]

        assert plan_pending_review_decisions([reference, restored]) == ()

        assert [_snapshot(job) for job in (reference, restored)] == before
        assert restored.duplicate_review_status == "pending"
        assert restored.canonical_job_id is None
        assert reference.canonical_job_id is None
        assert reference.archived_at is None


@pytest.mark.parametrize("scenario", ["strict_copy", "outside_window", "unsupported_fuzzy"])
@pytest.mark.parametrize("saved_dossier", [False, True])
def test_reconciliation_keeps_restored_duplicate_for_manual_classification(
    isolated_db, scenario, saved_dossier,
):
    with session_scope() as session:
        reference, restored = _restored_review_pair(session, scenario)
        reference_id, restored_id = reference.id, restored.id
        if saved_dossier:
            session.add(Application(
                job_id=restored_id,
                status="archived",
                cv_pdf_path="/preserved/cv.pdf",
                notes="Dossier historique à conserver",
            ))

    with session_scope() as session:
        before = [_snapshot(session.get(Job, job_id)) for job_id in (reference_id, restored_id)]
        components = dict(session.get(Job, restored_id).score.components)

    assert reconcile_pending_reviews() == 0
    assert reconcile_pending_reviews() == 0

    with session_scope() as session:
        reference, restored = (session.get(Job, job_id) for job_id in (reference_id, restored_id))
        assert [_snapshot(job) for job in (reference, restored)] == before
        assert restored.duplicate_review_status == "pending"
        assert restored.possible_duplicate_of_id == reference_id
        assert restored.canonical_job_id is None
        assert restored.archived_at is None
        assert restored.score.components == components
        assert reference.canonical_job_id is None
        assert reference.archived_at is None
        assert session.scalar(select(DuplicateReviewEvent)) is None
        if saved_dossier:
            assert restored.application.status == "archived"
            assert restored.application.cv_pdf_path == "/preserved/cv.pdf"
            assert restored.application.notes == "Dossier historique à conserver"
        else:
            assert restored.application is None
