"""Permanent deletion uses isolated files and DBs, never real dossiers."""

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread

import pytest
from sqlalchemy import func, select, text

from smartapply.concurrency import TaskConflictError, job_operation
from smartapply.config import get_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.analysis_batches import (
    AnalysisBatchItem,
    AnalysisBatchRun,
    AnalysisBatchWave,
)
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.database.models import (
    Application,
    AppSetting,
    DuplicateReviewEvent,
    GeneratedDocument,
    Job,
    JobAnalysis,
    JobScore,
    JobStatus,
    LLMCache,
    LLMUsage,
)
from smartapply.desktop.offer_deletion import delete_analysis_offer, delete_ready_offer
from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.pipeline.output_deletion import (
    DELETION_PREFIX,
    ApplicationOutputDeletion,
    recover_application_deletions,
)


def ready_offer(external_id="target"):
    with write_session_scope() as session:
        job = Job(
            external_id=external_id,
            title="Data Scientist",
            source_company="Acme",
            description="Python ML",
            source="manual",
            status=JobStatus.READY_FOR_FORM_SUBMISSION,
        )
        job.score = JobScore(components={"filter_disposition": "pending"})
        job.analysis = JobAnalysis(
            raw_response={"role_type": "data", "fit_score": 90, "match_reasons": ["ML"]}
        )
        app = Application(
            status=JobStatus.READY_FOR_FORM_SUBMISSION, cv_json={"skills": ["Python"]}
        )
        job.application = app
        session.add(job)
        session.flush()
        directory = get_settings().output_dir / str(app.id)
        directory.mkdir(parents=True)
        (directory / "cv.pdf").write_bytes(b"pdf")
        (directory / "notes.json").write_text("notes")
        app.cv_pdf_path = str(directory / "cv.pdf")
        app.documents.append(GeneratedDocument(doc_type="cv_pdf", path=app.cv_pdf_path))
        ids = job.id, app.id
    return *ids, directory


def count(session, model):
    return session.scalar(select(func.count()).select_from(model))


def analysis_offer(external_id="pending"):
    with write_session_scope() as session:
        job = Job(
            external_id=external_id,
            title="Data Scientist",
            source_company="Acme",
            description="Python ML",
            source="manual",
            status=JobStatus.FILTERED,
            filtered_at=datetime.now(timezone.utc),
        )
        job.score = JobScore(components={
            "filter_disposition": "pending_relevance",
            "hard_reject": HardRejectResult(decisions=()).audit_payload(),
        })
        session.add(job)
        session.flush()
        return job.id


def test_delete_analysis_offer_removes_available_offer(isolated_db):
    job_id = analysis_offer()
    other_id = analysis_offer("other")
    result = delete_analysis_offer(job_id)
    assert result.job_ids == (job_id,)
    assert result.application_ids == ()
    with session_scope() as session:
        assert session.get(Job, job_id) is None
        assert session.get(Job, other_id) is not None


def test_delete_analysis_offer_rejects_linked_application(isolated_db):
    job_id = analysis_offer()
    with write_session_scope() as session:
        session.get(Job, job_id).application = Application(status=JobStatus.ANALYZED)
    with pytest.raises(ValueError, match="À analyser"):
        delete_analysis_offer(job_id)
    with session_scope() as session:
        assert session.get(Job, job_id) is not None


def test_delete_offer_documents_results_usage_copies_and_completed_batch(isolated_db):
    job_id, app_id, directory = ready_offer()
    other_id, other_app, other_dir = ready_offer("unrelated")
    with write_session_scope() as session:
        copy = Job(
            external_id="copy",
            title="Copy",
            source_company="Acme",
            description="copy",
            source="manual",
            canonical_job_id=job_id,
            duplicate_review_status="confirmed",
            status="archived",
        )
        candidate = Job(
            external_id="pending",
            title="Candidate",
            source_company="Acme",
            description="maybe",
            source="manual",
            possible_duplicate_of_id=job_id,
            duplicate_review_status="pending",
        )
        session.add_all([copy, candidate])
        session.flush()
        copy_id, candidate_id = copy.id, candidate.id
        session.add_all(
            [
                AnalysisCheckpoint(
                    job_id=job_id, extraction_key="extraction", offer_data={"main_tasks": ["ML"]}
                ),
                LLMUsage(
                    job_id=job_id,
                    job_external_id="target",
                    purpose="analysis",
                    model="test",
                    cache_key="tracked",
                ),
                LLMUsage(job_external_id="target", purpose="old", model="test"),
                LLMUsage(job_id=other_id, purpose="analysis", model="test"),
                LLMCache(
                    cache_key="tracked", model="test", purpose="cv", response='{"old":"result"}'
                ),
                LLMCache(
                    cache_key="legacy",
                    model="test",
                    purpose="job_fit_assessment",
                    response='{"fit_score":90,"match_reasons":["ML"],"risks":[]}',
                ),
                LLMCache(
                    cache_key="unrelated",
                    model="test",
                    purpose="profile",
                    response='{"profile":"keep"}',
                ),
                DuplicateReviewEvent(
                    job_id=candidate_id,
                    reference_id=job_id,
                    policy_version="test",
                    action="link",
                    reason="test",
                    before_state={},
                    after_state={},
                    history_snapshot={},
                ),
                AnalysisBatchRun(id="run", state="completed", model="test", provider="openai", max_completion_tokens=6000, profile_data={}),
            ]
        )
        session.flush()
        first = AnalysisBatchItem(
            run_id="run",
            job_id=job_id,
            job_snapshot="snapshot",
            title="target",
            company="Acme",
            analyzer_input={},
            state="completed",
        )
        second = AnalysisBatchItem(
            run_id="run",
            job_id=other_id,
            job_snapshot="snapshot",
            title="other",
            company="Acme",
            analyzer_input={},
            state="completed",
        )
        session.add_all([first, second])
        session.flush()
        session.add(
            AnalysisBatchWave(
                id="wave",
                run_id="run",
                phase="evaluation",
                state="imported",
                requests=[
                    {
                        "custom_id": "target",
                        "cache_key": "tracked",
                        "item_ids": [first.id],
                        "body": {"target": "description"},
                    },
                    {
                        "custom_id": "other",
                        "cache_key": "other",
                        "item_ids": [second.id],
                        "body": {"keep": "description"},
                    },
                ],
                consumed_ids=["target", "other"],
            )
        )
    result = delete_ready_offer(job_id)
    assert result.job_ids == tuple(sorted((job_id, copy_id))) and result.files_removed
    assert not directory.exists() and other_dir.exists()
    with session_scope() as session:
        assert session.get(Job, job_id) is None and session.get(Job, copy_id) is None
        assert session.get(Application, app_id) is None
        for model in (JobScore, JobAnalysis, Application, GeneratedDocument):
            assert count(session, model) == 1
        for model in (AnalysisCheckpoint, DuplicateReviewEvent):
            assert count(session, model) == 0
        assert count(session, LLMUsage) == 1
        assert [row.cache_key for row in session.scalars(select(LLMCache))] == ["unrelated"]
        assert session.get(Application, other_app) is not None
        pending = session.get(Job, candidate_id)
        assert (
            pending.possible_duplicate_of_id is None and pending.duplicate_review_status == "none"
        )
        wave = session.get(AnalysisBatchWave, "wave")
        assert wave.consumed_ids == ["other"] and len(wave.requests) == 1
        assert "target" not in json.dumps(wave.requests)
        assert not list(
            session.scalars(select(AppSetting).where(AppSetting.key.startswith(DELETION_PREFIX)))
        )
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []


@pytest.mark.parametrize(
    "status,submitted",
    [
        ("sent", False),
        ("interview", False),
        ("ready_for_form_submission", True),
        ("archived", False),
    ],
)
def test_refuse_non_ready_or_already_submitted_dossier(isolated_db, status, submitted):
    from datetime import datetime, timezone

    job_id, app_id, directory = ready_offer()
    with write_session_scope() as session:
        app = session.get(Application, app_id)
        app.status = status
        if submitted:
            app.form_submitted_at = datetime.now(timezone.utc)
    with pytest.raises(ValueError):
        delete_ready_offer(job_id)
    assert directory.exists()
    with session_scope() as session:
        assert session.get(Job, job_id) is not None


def test_refuse_active_batch_without_touching_files(isolated_db):
    job_id, _, directory = ready_offer()
    with write_session_scope() as session:
        session.add(AnalysisBatchRun(id="run", state="active", model="test", provider="openai", max_completion_tokens=6000, profile_data={}))
        session.flush()
        session.add(
            AnalysisBatchItem(
                run_id="run",
                job_id=job_id,
                job_snapshot="test",
                title="test",
                company="Acme",
                analyzer_input={},
            )
        )
    with pytest.raises(ValueError, match="batch"):
        delete_ready_offer(job_id)
    assert directory.exists()


def test_refuse_active_worker(isolated_db):
    job_id, _, directory = ready_offer()
    acquired, release = Event(), Event()

    def worker():
        with job_operation([job_id], operation="génération"):
            acquired.set()
            release.wait(5)

    thread = Thread(target=worker)
    thread.start()
    assert acquired.wait(5)
    try:
        with pytest.raises(TaskConflictError):
            delete_ready_offer(job_id)
        assert directory.exists()
    finally:
        release.set()
        thread.join()


def test_database_failure_restores_entire_directory(isolated_db, monkeypatch):
    job_id, _, directory = ready_offer()

    def fail(session, jobs):
        session.delete(jobs[0])
        session.flush()
        raise RuntimeError("transaction failed")

    monkeypatch.setattr("smartapply.desktop.offer_deletion.erase_offer_rows", fail)
    with pytest.raises(RuntimeError):
        delete_ready_offer(job_id)
    assert (directory / "cv.pdf").read_bytes() == b"pdf"
    with session_scope() as session:
        assert session.get(Job, job_id) is not None
    assert not list(directory.parent.glob(".*.deleting-*"))


def test_file_cleanup_failure_is_recoverable_after_commit(isolated_db, monkeypatch):
    job_id, _, directory = ready_offer()
    with monkeypatch.context() as context:
        context.setattr(
            "smartapply.pipeline.output_deletion.shutil.rmtree",
            lambda _: (_ for _ in ()).throw(PermissionError()),
        )
        result = delete_ready_offer(job_id)
    assert not result.files_removed and not directory.exists()
    assert list(directory.parent.glob(".*.deleting-*"))
    recover_application_deletions()
    assert not list(directory.parent.glob(".*.deleting-*"))
    with session_scope() as session:
        assert session.get(Job, job_id) is None
        assert not list(
            session.scalars(select(AppSetting).where(AppSetting.key.startswith(DELETION_PREFIX)))
        )


def test_crash_before_commit_restores_dossier(isolated_db):
    _, app_id, directory = ready_offer()
    plan = ApplicationOutputDeletion({app_id: directory})
    plan.prepare()
    plan.stage()
    assert not directory.exists()
    recover_application_deletions()
    assert (directory / "cv.pdf").read_bytes() == b"pdf"


def test_refuse_symlink_directory(isolated_db):
    job_id, _, directory = ready_offer()
    original = directory.with_name("external")
    directory.rename(original)
    directory.symlink_to(original, target_is_directory=True)
    with pytest.raises(ValueError, match="sûr"):
        delete_ready_offer(job_id)
    assert (original / "cv.pdf").exists()


def test_refuse_shared_document_directory(isolated_db):
    job_id, _, directory = ready_offer()
    _, other_app, _ = ready_offer("other")
    with write_session_scope() as session:
        session.get(Application, other_app).cv_pdf_path = str(directory / "another.pdf")
    with pytest.raises(ValueError, match="autre candidature"):
        delete_ready_offer(job_id)
    assert directory.exists()


def test_ui_requires_confirmation_for_specific_offer_before_deleting():
    page = (Path(__file__).parents[1] / "smartapply/desktop/qml/pages/JobsPage.qml").read_text()
    assert page.index('objectName: "offerArchiveApplicationAction"') < page.index(
        'objectName: "offerDeleteAction"'
    )
    assert "onAccepted: AppBridge.deleteReadyOffer(targetJobId)" in page
    assert "deleteOfferDialog.targetJobId = Number(AppBridge.currentJob.id)" in page
    assert 'confirmText: "Supprimer définitivement"' in page


def test_empty_batch_history_is_removed(isolated_db):
    job_id, _, _ = ready_offer()
    with write_session_scope() as session:
        session.add(AnalysisBatchRun(id="only", state="completed", model="test", provider="openai", max_completion_tokens=6000, profile_data={}))
        session.flush()
        item = AnalysisBatchItem(
            run_id="only",
            job_id=job_id,
            job_snapshot="test",
            title="test",
            company="Acme",
            analyzer_input={},
        )
        session.add(item)
        session.flush()
        session.add(
            AnalysisBatchWave(
                id="only-wave",
                run_id="only",
                phase="extraction",
                state="imported",
                requests=[
                    {"custom_id": "one", "cache_key": "one", "item_ids": [item.id], "body": {}},
                ],
            )
        )
    delete_ready_offer(job_id)
    with session_scope() as session:
        for model in (AnalysisBatchRun, AnalysisBatchItem, AnalysisBatchWave):
            assert count(session, model) == 0


def test_old_managed_rendering_folders_are_removed_without_following_links(isolated_db):
    job_id, app_id, directory = ready_offer()
    for kind in ("staging", "backup"):
        stale = directory.with_name(f".{app_id}.{kind}-" + "a" * 32)
        stale.mkdir()
        (stale / "old.json").write_text("private old dossier")
    external = directory.parent / "unrelated"
    external.mkdir()
    (external / "keep").write_text("other")
    (directory / "external-link").symlink_to(external, target_is_directory=True)
    assert delete_ready_offer(job_id).files_removed
    assert list(directory.parent.iterdir()) == [external]
    assert (external / "keep").read_text() == "other"


def test_sent_confirmed_copy_blocks_entire_deletion(isolated_db):
    job_id, _, directory = ready_offer()
    copy_id, copy_app, copy_dir = ready_offer("copy")
    with write_session_scope() as session:
        session.get(Job, copy_id).canonical_job_id = job_id
        session.get(Application, copy_app).status = "sent"
    with pytest.raises(ValueError, match="non envoyés"):
        delete_ready_offer(job_id)
    assert directory.exists() and copy_dir.exists()


def test_recovery_does_not_restore_old_files_into_a_reused_application_id(isolated_db, monkeypatch):
    job_id, app_id, directory = ready_offer()
    with monkeypatch.context() as context:
        context.setattr(
            "smartapply.pipeline.output_deletion.shutil.rmtree",
            lambda _: (_ for _ in ()).throw(PermissionError()),
        )
        assert not delete_ready_offer(job_id).files_removed
    _, new_app_id, new_directory = ready_offer("new")
    assert new_app_id == app_id and new_directory == directory
    (new_directory / "cv.pdf").write_bytes(b"new dossier")
    recover_application_deletions()
    assert (new_directory / "cv.pdf").read_bytes() == b"new dossier"
    assert not list(directory.parent.glob(".*.deleting-*"))
