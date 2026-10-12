"""Generation journals participate in the permanent offer deletion boundary."""

import json

import pytest
from sqlalchemy import select, text

from smartapply.config import get_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.generation_runs import (
    GenerationCall,
    GenerationItem,
    GenerationItemContext,
    GenerationRun,
    GenerationWave,
)
from smartapply.database.models import Application, GeneratedDocument, Job, JobStatus, LLMCache
from smartapply.database.repository.offer_deletion import assert_no_active_batch
from smartapply.desktop.offer_deletion import delete_ready_offer


def _ready_offer(external_id):
    with write_session_scope() as session:
        job = Job(
            external_id=external_id,
            title="Data Scientist",
            source_company="Acme",
            description="Python ML",
            source="manual",
            status=JobStatus.READY_FOR_FORM_SUBMISSION,
        )
        app = Application(status=JobStatus.READY_FOR_FORM_SUBMISSION, cv_json={"test": external_id})
        job.application = app
        session.add(job)
        session.flush()
        directory = get_settings().output_dir / str(app.id)
        directory.mkdir(parents=True)
        path = directory / "cv.pdf"
        path.write_bytes(b"pdf")
        app.cv_pdf_path = str(path)
        app.documents.append(GeneratedDocument(doc_type="cv_pdf", path=str(path)))
        return job.id, app.id, directory


def _run(session, state):
    run = GenerationRun(
        id="generation-run",
        mode="batch",
        state=state,
        profile_data={},
        routes={},
        reference_date="2026-10-11",
    )
    session.add(run)
    session.flush()
    return run


def _item(session, job_id, app_id, *, state="completed"):
    item = GenerationItem(
        run_id="generation-run",
        job_id=job_id,
        application_id=app_id,
        job_snapshot="snapshot",
        analysis_snapshot="snapshot",
        title="Data Scientist",
        company="Acme",
        state=state,
    )
    session.add(item)
    session.flush()
    return item


def _call(session, item, *, custom_id):
    call = GenerationCall(
        item_id=item.id,
        position=0,
        purpose="cv_adaptation",
        schema_name="CVSelection",
        provider="openai",
        request_body={"test": custom_id},
        request_key=custom_id,
        state="completed",
        response_data={"selection": custom_id},
        wave_id="generation-wave",
    )
    session.add(call)
    session.flush()
    return call


@pytest.mark.parametrize(
    "run_state", ["active", "attention", "blocked", "paused", "partial", "failed"]
)
@pytest.mark.parametrize("item_state", ["pending", "waiting", "failed"])
def test_unfinished_generation_prevents_deletion(isolated_db, run_state, item_state):
    job_id, app_id, directory = _ready_offer("target")
    with write_session_scope() as session:
        _run(session, run_state)
        _item(session, job_id, app_id, state=item_state)
    with session_scope() as session, pytest.raises(ValueError, match="création"):
        assert_no_active_batch(session, {job_id})
    assert directory.exists()


def test_stale_offer_still_waits_for_outstanding_provider_wave(isolated_db):
    job_id, app_id, _ = _ready_offer("target")
    with write_session_scope() as session:
        _run(session, "active")
        item = _item(session, job_id, app_id, state="stale")
        session.add(GenerationWave(
            id="generation-wave", run_id="generation-run", provider="openai", state="submitted",
        ))
        session.flush()
        _call(session, item, custom_id="target-response")
    with session_scope() as session, pytest.raises(ValueError, match="lot de création"):
        assert_no_active_batch(session, {job_id})


@pytest.mark.parametrize("item_state", ["completed", "stale", "transferred"])
def test_deletion_removes_only_target_from_shared_generation_run(isolated_db, item_state):
    job_id, app_id, directory = _ready_offer("target")
    other_id, other_app_id, other_directory = _ready_offer("other")
    with write_session_scope() as session:
        _run(session, "active")
        target = _item(session, job_id, app_id, state=item_state)
        other = _item(session, other_id, other_app_id, state="pending")
        for item in (target, other):
            session.add(GenerationItemContext(
                item_id=item.id, profile_data={}, routes={}, reference_date="2026-10-10",
            ))
        wave = GenerationWave(
            id="generation-wave", run_id="generation-run", provider="openai", state="imported"
        )
        session.add(wave)
        session.flush()
        target_call = _call(session, target, custom_id="target-response")
        other_call = _call(session, other, custom_id="other-response")
        wave.requests = [
            {
                "call_id": target_call.id,
                "custom_id": "target-response",
                "cache_key": "target-response",
                "body": {"offer": "target"},
            },
            {
                "call_id": other_call.id,
                "custom_id": "other-response",
                "cache_key": "other-response",
                "body": {"offer": "other"},
            },
        ]
        wave.consumed_ids = ["target-response", "other-response"]
        session.add_all(
            [
                LLMCache(
                    cache_key="target-response",
                    model="test",
                    purpose="cv_adaptation",
                    response=json.dumps(target_call.response_data),
                ),
                LLMCache(
                    cache_key="other-response",
                    model="test",
                    purpose="cv_adaptation",
                    response=json.dumps(other_call.response_data),
                ),
            ]
        )
        target_item_id, target_call_id, other_item_id, other_call_id = (
            target.id,
            target_call.id,
            other.id,
            other_call.id,
        )
    delete_ready_offer(job_id)
    assert not directory.exists() and other_directory.exists()
    with session_scope() as session:
        assert session.get(Job, job_id) is None
        assert session.get(Application, app_id) is None
        assert session.get(GenerationItem, target_item_id) is None
        assert session.get(GenerationItemContext, target_item_id) is None
        assert session.get(GenerationCall, target_call_id) is None
        assert session.get(GenerationItem, other_item_id) is not None
        assert session.get(GenerationItemContext, other_item_id) is not None
        assert session.get(GenerationCall, other_call_id) is not None
        assert session.get(GenerationRun, "generation-run") is not None
        wave = session.get(GenerationWave, "generation-wave")
        assert wave.consumed_ids == ["other-response"]
        assert [record["call_id"] for record in wave.requests] == [other_call_id]
        assert "target" not in json.dumps(wave.requests)
        assert list(session.scalars(select(LLMCache.cache_key))) == ["other-response"]
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []


@pytest.mark.parametrize(
    "run_state,item_state", [("completed", "completed"), ("cancelled", "failed")]
)
def test_last_offer_deletion_removes_entire_generation_journal(isolated_db, run_state, item_state):
    job_id, app_id, _ = _ready_offer("target")
    with write_session_scope() as session:
        _run(session, run_state)
        item = _item(session, job_id, app_id, state=item_state)
        session.add(
            GenerationWave(
                id="generation-wave", run_id="generation-run", provider="openai", state="imported"
            )
        )
        session.flush()
        _call(session, item, custom_id="target-response")
    delete_ready_offer(job_id)
    with session_scope() as session:
        for model in (GenerationCall, GenerationItem, GenerationWave, GenerationRun):
            assert not list(session.scalars(select(model)))
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []


def test_deletion_erases_both_original_and_transferred_offer_journals(isolated_db):
    from smartapply.pipeline.generation.offer_resume import copy_offer_checkpoint

    job_id, app_id, _ = _ready_offer("transferred-target")
    with write_session_scope() as session:
        _run(session, "paused")
        source = _item(session, job_id, app_id, state="waiting")
        session.add(GenerationWave(
            id="generation-wave", run_id="generation-run", provider="openai", state="imported",
        ))
        session.flush()
        _call(session, source, custom_id="original-response")
        session.add(GenerationRun(
            id="resumed-run", mode="immediate", state="completed", profile_data={}, routes={},
            reference_date="2026-10-11",
        ))
        session.flush()
        target = GenerationItem(
            run_id="resumed-run", job_id=job_id, job_snapshot=source.job_snapshot,
            analysis_snapshot=source.analysis_snapshot, title=source.title,
            company=source.company, state="completed",
        )
        session.add(target)
        session.flush()
        copy_offer_checkpoint(session, source, target)
    delete_ready_offer(job_id)
    with session_scope() as session:
        for model in (GenerationCall, GenerationItemContext, GenerationItem, GenerationWave,
                      GenerationRun):
            assert not list(session.scalars(select(model)))
        assert session.get(Application, app_id) is None
        assert session.execute(text("PRAGMA foreign_key_check")).all() == []
