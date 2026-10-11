"""Concurrent insertions must use the live offer's identity and lifecycle."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Application, Job, JobStatus
from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.offers import RawJob
from smartapply.pipeline.ingestor import Ingestor


def _ingestor():
    return Ingestor(job_filter=SimpleNamespace(
        evaluate=lambda _: HardRejectResult(decisions=()),
    ))


def _raw():
    return RawJob(
        external_id="linkedin:collected", title="Collected title", company="Collected Company",
        description="Collected source text", source="linkedin",
    )


def _offer(external_id, status=JobStatus.SCRAPED, **kwargs):
    return Job(
        external_id=external_id, title="User verified title", source_company="Verified Company",
        description="User verified source text", source="manual", status=status, **kwargs,
    )


@pytest.mark.parametrize("protected", [True, False])
def test_reused_highest_id_preserves_protected_offer_or_updates_pending(
    isolated_db, monkeypatch, protected,
):
    with write_session_scope() as session:
        old = _offer("manual:deleted")
        session.add(old)
        session.flush()
        reused_id = old.id

    ingestor = _ingestor()

    def concurrent_replace(raw, known_jobs):
        with write_session_scope() as session:
            session.delete(session.get(Job, reused_id))
            session.flush()
            status = JobStatus.READY_FOR_FORM_SUBMISSION if protected else JobStatus.SCRAPED
            replacement = _offer(raw.external_id, status)
            session.add(replacement)
            session.flush()
            assert replacement.id == reused_id
            if protected:
                session.add(Application(
                    job_id=reused_id, status=status, cv_json={"verified_document": True},
                ))
        return None

    monkeypatch.setattr(ingestor, "_find_probable_duplicate", concurrent_replace)
    report = ingestor._persist("linkedin", [_raw()])

    with session_scope() as session:
        job = session.get(Job, reused_id)
        assert session.query(Job).count() == 1
        assert job.external_id == "linkedin:collected"
        if protected:
            assert job.title == "User verified title"
            assert job.source_company == "Verified Company"
            assert job.description == "User verified source text"
            assert job.source == "manual"
            assert job.status == JobStatus.READY_FOR_FORM_SUBMISSION
            assert job.application.cv_json == {"verified_document": True}
            assert job.application.status == JobStatus.READY_FOR_FORM_SUBMISSION
        else:
            assert job.title == _raw().title
            assert job.source_company == _raw().company
            assert job.description == _raw().description
            assert job.source == "linkedin"
            assert job.status == JobStatus.SCRAPED
            assert job.application is None
    assert report.inserted == 0
    assert report.updated_pending == int(not protected)
    assert report.skipped_processed == int(protected)
    assert report.persisted == int(not protected)
    assert report.job_ids == ([] if protected else [reused_id])


def test_reused_highest_id_does_not_drop_genuinely_new_offer(isolated_db, monkeypatch):
    with write_session_scope() as session:
        old = _offer("manual:deleted")
        session.add(old)
        session.flush()
        reused_id = old.id

    ingestor = _ingestor()
    replaced = False

    def concurrent_replace(raw, known_jobs):
        nonlocal replaced
        if replaced:
            return None
        replaced = True
        with write_session_scope() as session:
            session.delete(session.get(Job, reused_id))
            session.flush()
            replacement = _offer("manual:independent")
            session.add(replacement)
            session.flush()
            assert replacement.id == reused_id
        return None

    monkeypatch.setattr(ingestor, "_find_probable_duplicate", concurrent_replace)
    report = ingestor._persist("linkedin", [_raw()])

    assert report.inserted == report.persisted == 1
    assert report.updated_pending == report.skipped_processed == 0
    with session_scope() as session:
        jobs = {job.external_id: job for job in session.query(Job).all()}
        assert set(jobs) == {"manual:independent", "linkedin:collected"}
        assert jobs["manual:independent"].title == "User verified title"
        assert jobs["linkedin:collected"].title == _raw().title
        assert report.job_ids == [jobs["linkedin:collected"].id]


@pytest.mark.parametrize("mutation", ["replace", "update"])
@pytest.mark.parametrize("identity", ["source_url", "native_id", "strict_copy"])
def test_reused_highest_id_refreshes_cross_source_identity(
    isolated_db, monkeypatch, identity, mutation,
):
    from smartapply.database.session import _write_lock

    raw = _raw().model_copy(update={
        "title": "AI Engineer", "company": "Identity Company", "location": "Paris",
        "contract_type": "CDI", "description": "Build machine learning pipelines with Python. " * 8,
        "source_data": {"id": "213HYVJ"} if identity == "native_id" else {},
        "source": "francetravail" if identity == "native_id" else "linkedin",
        "application_url": (
            "https://www.linkedin.com/jobs/view/1234567890/" if identity == "source_url" else None
        ),
    })
    with write_session_scope() as session:
        old = _offer("manual:deleted")
        session.add(old)
        session.flush()
        reused_id = old.id
    ingestor = _ingestor()
    original_match = ingestor._find_probable_duplicate
    changed = False

    def concurrent_insert(raw, known_jobs):
        nonlocal changed
        assert not _write_lock._is_owned(), "Matching must not occupy the SQLite writer"
        match = original_match(raw, known_jobs)
        if not changed:
            changed = True
            with write_session_scope() as session:
                if mutation == "replace":
                    session.delete(session.get(Job, reused_id))
                    session.flush()
                    root = Job(external_id="manual:existing-dossier")
                    session.add(root)
                else:
                    root = session.get(Job, reused_id)
                root.title = raw.title
                root.source_company = raw.company
                root.description = raw.description
                root.location = raw.location
                root.contract_type = raw.contract_type
                root.source = raw.source if identity == "native_id" else "manual"
                root.source_data = raw.source_data
                root.application_url = raw.application_url
                root.status = JobStatus.READY_FOR_FORM_SUBMISSION
                session.flush()
                assert root.id == reused_id
                session.add(Application(
                    job_id=root.id, status=root.status, cv_json={"verified_document": True},
                ))
        return match

    monkeypatch.setattr(ingestor, "_find_probable_duplicate", concurrent_insert)
    report = ingestor._persist(raw.source, [raw])
    assert report.inserted == report.persisted == 0
    assert report.skipped_existing_during_persist == 1
    with session_scope() as session:
        root = session.get(Job, reused_id)
        assert root.application.cv_json == {"verified_document": True}
        alias = session.query(Job).filter_by(external_id=raw.external_id).one_or_none()
        if identity == "native_id":
            assert alias is None
        else:
            assert alias.canonical_job_id == root.id
            assert alias.status == JobStatus.ARCHIVED
            assert report.aliases_created == 1


def test_ingest_own_writes_do_not_reload_population_between_raws(isolated_db, monkeypatch):
    import smartapply.pipeline.ingestor as module
    from smartapply.database.session import _write_lock

    original_scope = module.session_scope
    reads = 0

    @contextmanager
    def counted_scope():
        nonlocal reads
        assert not _write_lock._is_owned()
        reads += 1
        with original_scope() as session:
            yield session

    monkeypatch.setattr(module, "session_scope", counted_scope)
    raws = [_raw().model_copy(update={
        "external_id": f"linkedin:independent-{index}",
        "title": f"Independent role {index}", "company": f"Independent company {index}",
        "description": f"Different responsibilities for team {index}.",
    }) for index in range(3)]
    report = _ingestor()._persist("linkedin", raws)
    assert report.inserted == 3
    assert reads == 1


def test_ingest_fallback_refreshes_identity_without_revision_trigger(isolated_db, monkeypatch):
    from smartapply.database import get_engine

    with get_engine().begin() as connection:
        for action in ("insert", "update", "delete"):
            connection.exec_driver_sql(f"DROP TRIGGER elan_ingest_jobs_{action}")
        connection.exec_driver_sql("DELETE FROM app_settings WHERE key = 'ingest_job_revision'")
    test_reused_highest_id_refreshes_cross_source_identity(
        isolated_db, monkeypatch, "source_url", "replace",
    )


def test_exact_identity_moved_below_known_max_keeps_protected_offer(isolated_db, monkeypatch):
    import smartapply.pipeline.ingestor as module

    with write_session_scope() as session:
        session.add_all([_offer(_raw().external_id, id=1), _offer("manual:other", id=100)])

    original_scope = module._ingest_write_scope
    replaced = False

    @contextmanager
    def replace_before_write(job_ids):
        nonlocal replaced
        if not replaced:
            replaced = True
            with write_session_scope() as session:
                session.delete(session.get(Job, 1))
                session.flush()
                replacement = _offer(
                    _raw().external_id, JobStatus.READY_FOR_FORM_SUBMISSION, id=2,
                )
                session.add(replacement)
                session.add(Application(
                    job_id=2, status=JobStatus.READY_FOR_FORM_SUBMISSION,
                    cv_json={"verified_document": True},
                ))
        with original_scope(job_ids) as scope:
            yield scope

    monkeypatch.setattr(module, "_ingest_write_scope", replace_before_write)
    report = _ingestor()._persist("linkedin", [_raw()])

    assert report.inserted == report.updated_pending == report.persisted == 0
    assert report.skipped_processed == 1
    with session_scope() as session:
        assert session.get(Job, 1) is None
        assert session.query(Job).count() == 2
        job = session.get(Job, 2)
        assert job.title == "User verified title"
        assert job.application.cv_json == {"verified_document": True}
