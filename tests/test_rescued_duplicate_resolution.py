"""Resolving a restored duplicate clears only its temporary review routing."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from smartapply.database import session_scope
from smartapply.database.models import Application, Job, JobScore
from smartapply.database.repository import upsert_job
from smartapply.database.repository.duplicates import confirm_duplicate, reject_duplicate
from smartapply.jobsearch.offer_state import has_manual_rescue

AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _pair(session, *, archived=False):
    offers = [upsert_job(
        session, external_id=key, title="ML Engineer", company="Acme",
        description="Python ML", source="fixture",
    ) for key in ("reference", "restored", "unrelated")]
    reference, restored, unrelated = offers
    reference.published_date = AT
    restored.published_date = AT + timedelta(days=1)
    restored.possible_duplicate_of_id = reference.id
    restored.duplicate_review_status = "pending"
    restored.duplicate_match_type = "fuzzy"
    restored.duplicate_confidence = 0.87
    if archived:
        restored.status = "archived"
        restored.archived_at = AT
    for job in offers:
        job.score = JobScore(components={
            "manual_rescue": {
                "schema_version": 3,
                "rescued_at": AT.isoformat(),
                "justification": f"Choix conservé pour {job.external_id}",
                "previous_rejection": {"stage": "manual", "reasons": ["manual_archive"]},
                "previous_duplicate": {"canonical_job_id": reference.id},
                "duplicate_review_job_id": unrelated.id if job is unrelated else restored.id,
            },
            "independent_audit": {"must_survive": job.external_id},
        }, computed_at=AT)
    session.flush()
    return offers


@pytest.mark.parametrize("resolve", [confirm_duplicate, reject_duplicate])
@pytest.mark.parametrize("archived", [False, True])
def test_resolution_clears_both_review_routes_without_losing_rescue_or_other_fields(
    isolated_db, resolve, archived,
):
    with session_scope() as session:
        reference, restored, unrelated = _pair(session, archived=archived)
        ids = [job.id for job in (reference, restored, unrelated)]
        before = {job.id: deepcopy(job.score.components) for job in (reference, restored, unrelated)}

        assert resolve(session, restored.id) is restored

        for job in (reference, restored):
            expected = deepcopy(before[job.id])
            expected["manual_rescue"].pop("duplicate_review_job_id")
            assert job.score.components == expected
            assert has_manual_rescue(job.score.components)
            assert job.score.computed_at == AT
        assert unrelated.score.components == before[unrelated.id]

    with session_scope() as session:
        for job_id in ids[:2]:
            job = session.get(Job, job_id)
            assert "duplicate_review_job_id" not in job.score.components["manual_rescue"]
            assert has_manual_rescue(job.score.components)
        assert session.get(Job, ids[2]).score.components == before[ids[2]]


def test_failed_confirmation_preserves_all_restoration_routes(isolated_db):
    with session_scope() as session:
        reference, restored, unrelated = _pair(session)
        for job in (reference, restored):
            job.application = Application(status="ready_for_form_submission", cv_pdf_path=f"/{job.id}/cv.pdf")
        session.flush()
        before = {job.id: deepcopy(job.score.components) for job in (reference, restored, unrelated)}

        with pytest.raises(ValueError, match="plusieurs dossiers"):
            confirm_duplicate(session, restored.id)

        for job in (reference, restored, unrelated):
            assert job.score.components == before[job.id]
        assert restored.duplicate_review_status == "pending"
        assert restored.possible_duplicate_of_id == reference.id


@pytest.mark.parametrize("resolve", [confirm_duplicate, reject_duplicate])
def test_unavailable_resolution_does_not_clear_an_existing_route(isolated_db, resolve):
    with session_scope() as session:
        reference, restored, unrelated = _pair(session)
        restored.duplicate_review_status = "rejected"
        before = {job.id: deepcopy(job.score.components) for job in (reference, restored, unrelated)}

        assert resolve(session, restored.id) is None

        for job in (reference, restored, unrelated):
            assert job.score.components == before[job.id]
