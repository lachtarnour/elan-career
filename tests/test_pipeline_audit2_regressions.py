"""Repeat the audited interleavings through the app's real mutation services."""

import pytest

from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Application, Job, JobStatus
from smartapply.desktop.services import DesktopService
from smartapply.offers import RawJob
from smartapply.pipeline.ingestor import Ingestor


@pytest.mark.parametrize("reuse_highest_id", [True, False])
def test_cross_source_identity_survives_manual_addition_during_ingest(
    isolated_db, monkeypatch, reuse_highest_id,
):
    raw = RawJob(
        external_id="linkedin:collected-cross-source", title="AI Engineer",
        company="Concurrent Identity Company", source="linkedin", location="Paris",
        contract_type="CDI", description="Build machine learning pipelines using Python.",
        application_url="https://www.linkedin.com/jobs/view/1234567890/",
        source_data={"id": "1234567890"},
    )
    with write_session_scope() as session:
        old = Job(
            external_id="manual:old-unrelated", title="Independent offer",
            source_company="Other company", description="unrelated text", source="manual",
            status=JobStatus.READY_FOR_FORM_SUBMISSION,
        )
        session.add(old)
        session.flush()
        old_id = old.id
        session.add(Application(
            job_id=old_id, status=JobStatus.READY_FOR_FORM_SUBMISSION,
            cv_json={"previous_dossier": True},
        ))
    ingestor = Ingestor()
    original_match = ingestor._find_probable_duplicate
    root_id = None

    def insert_after_matching(raw, known_jobs):
        nonlocal root_id
        result = original_match(raw, known_jobs)
        if root_id is not None:
            return result
        service = DesktopService()
        if reuse_highest_id:
            assert service.delete_ready_offer(old_id).job_ids == (old_id,)
        added = service.add_manual_offer(
            title=raw.title, company=raw.company, location=raw.location,
            description=raw.description, application_url=raw.application_url,
        )
        root_id = added["job_ids"][0]
        with write_session_scope() as session:
            root = session.get(Job, root_id)
            root.status = JobStatus.READY_FOR_FORM_SUBMISSION
            session.add(Application(
                job_id=root_id, status=root.status, cv_json={"already_prepared": True},
            ))
        return result

    monkeypatch.setattr(ingestor, "_find_probable_duplicate", insert_after_matching)
    report = ingestor._persist("linkedin", [raw])
    assert report.inserted == report.persisted == 0
    assert report.aliases_created == 1
    with session_scope() as session:
        collected = session.query(Job).filter_by(external_id=raw.external_id).one()
        root = session.get(Job, root_id)
        assert (root_id == old_id) is reuse_highest_id
        assert collected.canonical_job_id == root_id
        assert collected.status == JobStatus.ARCHIVED
        assert root.application.cv_json == {"already_prepared": True}
    assert DesktopService().pending_analysis_ids() == []


def test_manual_source_update_invalidates_old_batch_screen_before_publication(
    isolated_db, monkeypatch,
):
    from smartapply.llm.schemas import JobScopeScreening
    from smartapply.offers import build_analyzer_input
    from smartapply.pipeline.batch_analysis.coordinator import BatchAnalysisCoordinator
    from smartapply.pipeline.process.scope_screening import save_scope, scope_request, scope_result
    from tests.scope_fixtures import configured_scope_context, set_test_routes
    from tests.test_batch_analysis import FakeTransport, pipeline, seed_jobs

    set_test_routes(monkeypatch, isolated_db, {
        purpose: {
            "provider": "openai", "model": "gpt-5.4-mini",
            "reasoning": "medium", "max_output_tokens": 6000,
        }
        for purpose in ("job_scope_screening", "job_offer_extraction", "job_fit_assessment")
    })
    ids = seed_jobs(screened=False)
    context = configured_scope_context()
    with write_session_scope() as session:
        job = session.get(Job, ids[0])
        job.description = "Old source body represented by the retained screening."
        job.application_url = "https://company.example/jobs/987654321"
        request = scope_request(context, build_analyzer_input(job))
        save_scope(session, job.id, request.cache_key, scope_result(
            JobScopeScreening(mission_screen={
                "mission_relation": "clearly_outside", "mission_evidence": "sufficient",
            }), context=context,
        ))
    instance = pipeline()
    original_candidates = instance.analysis_candidates

    def mutate_after_read(*args, **kwargs):
        captured = original_candidates(*args, **kwargs)
        added = DesktopService().add_manual_offer(
            title=captured[0].title, company=captured[0].source_company,
            location=captured[0].location, application_url=captured[0].application_url,
            description="New source body for machine learning AI engineering with Python.",
        )
        assert added["job_ids"] == ids
        return captured

    monkeypatch.setattr(instance, "analysis_candidates", mutate_after_read)
    transport = FakeTransport()
    with pytest.raises(ValueError, match="sélectionnées ont changé"):
        BatchAnalysisCoordinator(transport=transport, pipeline=instance).start(ids)
    with session_scope() as session:
        job = session.get(Job, ids[0])
        assert job.description.startswith("New source body")
        assert job.archived_at is None
        assert job.status != JobStatus.ARCHIVED
        assert "manual_filter_bypass" in job.score.components
        assert "extraction_screen" not in job.score.components
    assert transport.created == []
    assert transport.upload_attempts == 0
