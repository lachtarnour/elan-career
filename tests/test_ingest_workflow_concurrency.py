"""Search persists independent results while other workflows own an offer."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from smartapply.concurrency import TaskConflictError, job_operation
from smartapply.database import session_scope
from smartapply.database.models import Job, JobStatus
from smartapply.offers import RawJob
from smartapply.pipeline.ingest import IngestCollection
from smartapply.pipeline.ingestor import Ingestor
from smartapply.scrapers.apify_recovery import ApifyRecoveryJournal


def _known_offer():
    with session_scope() as session:
        job = Job(
            external_id="linkedin:busy", title="AI Engineer", source_company="Acme",
            description="Build Python models.", source="linkedin", location="Paris",
            application_url="https://acme.example/jobs/123", source_data={"original": True},
        )
        session.add(job)
        session.flush()
        alias = Job(
            external_id="manual:alias", title=job.title, source_company=job.source_company,
            description=job.description, source="manual", canonical_job_id=job.id,
            status=JobStatus.ARCHIVED,
        )
        session.add(alias)
        session.flush()
        return job.id, alias.id


def _raw(external_id="linkedin:busy"):
    return RawJob(
        external_id=external_id, title="AI Engineer", company="Acme",
        description="Build Python models.", source="linkedin", location="Paris",
        application_url="https://acme.example/jobs/123", source_data={"new": True},
    )


def _independent_raw(external_id, company):
    return RawJob(
        external_id=external_id, title="Data Scientist", company=company,
        description="Forecast inventory demand and optimize transport routes.", source="linkedin",
    )


@contextmanager
def _busy(job_id):
    entered, release = Event(), Event()

    def run():
        with job_operation([job_id], operation="création du CV et de la lettre"):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(run)
        try:
            assert entered.wait(5)
            yield
        finally:
            release.set()
            worker.result(timeout=5)


@pytest.mark.parametrize("reserve_alias", [False, True])
def test_busy_offer_does_not_drop_other_source_results(isolated_db, reserve_alias):
    job_id, alias_id = _known_offer()
    warnings = ["Existing source warning"]
    collection = IngestCollection(
        source="linkedin",
        raw_jobs=[
            _independent_raw("linkedin:before", "Forecast Company"), _raw(),
            _independent_raw("linkedin:after", "Transport Company"),
        ],
        warnings=warnings,
    )

    with _busy(alias_id if reserve_alias else job_id):
        report = Ingestor().persist_collection(collection)

    assert report.fetched == 3 and report.persisted == 2 and report.inserted == 2
    assert report.skipped_busy == 1
    assert warnings == ["Existing source warning"]
    assert len(report.warnings) == 2
    assert "linkedin:busy" in report.warnings[1]
    assert "données existantes sont conservées" in report.warnings[1]
    with session_scope() as session:
        known = session.get(Job, job_id)
        assert known.description == "Build Python models."
        assert known.source_data == {"original": True}
        assert session.query(Job).count() == 4
        assert {session.get(Job, value).external_id for value in report.job_ids} == {
            "linkedin:before", "linkedin:after",
        }


def test_busy_duplicate_keeps_paid_dataset_for_persistence_replay(isolated_db):
    job_id, _alias_id = _known_offer()
    journal = ApifyRecoveryJournal(isolated_db / "pending-runs.json")
    journal.record_started(
        payload={"keywords": "AI"}, run_id="paid-run", dataset_id="paid-dataset",
        actor_status="SUCCEEDED",
    )
    collection = IngestCollection(
        source="linkedin",
        raw_jobs=[_raw("linkedin:collected-copy"), _independent_raw("linkedin:other", "Other")],
        completed_source_run_ids=["paid-run"], source_run_journal_path=str(journal.path),
    )

    with _busy(job_id):
        report = Ingestor().persist_collection(collection)

    assert report.skipped_busy == 1 and report.persisted == 1
    assert [item.run_id for item in journal.list_pending()] == ["paid-run"]
    assert any("journal de récupération Apify est conservé" in value for value in report.warnings)
    with session_scope() as session:
        assert session.query(Job).filter_by(external_id="linkedin:collected-copy").count() == 0

    # Reuse exactly the already collected payload, with no scraper or provider.
    resumed = Ingestor().persist_collection(collection)

    assert resumed.skipped_busy == 0 and journal.list_pending() == []
    with session_scope() as session:
        copy = session.query(Job).filter_by(external_id="linkedin:collected-copy").one()
        assert copy.source_data == {"new": True}
        assert session.get(Job, job_id).source_data == {"original": True}


def test_manual_readdition_of_busy_offer_still_reports_conflict(isolated_db):
    job_id, _alias_id = _known_offer()

    with _busy(job_id), pytest.raises(TaskConflictError):
        Ingestor()._persist("manual", [_raw()], manual_addition=True)

    with session_scope() as session:
        assert session.get(Job, job_id).source_data == {"original": True}


def test_actual_write_conflict_is_not_misreported_as_busy_skip(isolated_db, monkeypatch):
    import smartapply.pipeline.ingestor as module

    def fail_write(*args, **kwargs):
        raise TaskConflictError(999, "unexpected mutation conflict")

    monkeypatch.setattr(module, "upsert_job", fail_write)
    with pytest.raises(TaskConflictError, match="999"):
        Ingestor()._persist("linkedin", [_independent_raw("linkedin:new", "Independent")])

    with session_scope() as session:
        assert session.query(Job).count() == 0
