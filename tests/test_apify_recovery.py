"""Crash-safe handoff between Apify collection and local persistence."""

from __future__ import annotations

import json

import pytest
import requests
from sqlalchemy.orm import Session

from smartapply.database import Job, session_scope
from smartapply.offers import RawJob
from smartapply.pipeline.ingest import IngestCollection, collect_round_robin
from smartapply.pipeline.ingestor import Ingestor
from smartapply.scrapers.apify_recovery import (
    ApifyRecoveryJournal,
    ApifyRecoveryJournalError,
    ApifyStartReservedError,
    PendingApifyRun,
)
from smartapply.scrapers.base import ScraperStopError
from smartapply.scrapers.linkedin import LinkedInJobsScraper


def _record_run(journal: ApifyRecoveryJournal, run_id: str = "run-1") -> None:
    journal.record_started(
        payload={
            "keywords": "Data Scientist",
            "location": "France",
            "limit": 10,
        },
        run_id=run_id,
        dataset_id=f"dataset-{run_id}",
        actor_status="SUCCEEDED",
    )
    journal.mark_dataset_ready(
        run_id,
        dataset_id=f"dataset-{run_id}",
        actor_status="SUCCEEDED",
    )


def _raw_job(identifier: str = "job-1") -> RawJob:
    return RawJob(
        external_id=f"linkedin:{identifier}",
        title="Data Scientist",
        company="Acme",
        description="Build machine learning systems.",
        source="linkedin",
    )


def test_start_claim_is_shared_and_converted_to_a_known_run(tmp_path) -> None:
    path = tmp_path / "apify-runs.json"
    first = ApifyRecoveryJournal(path)
    second = ApifyRecoveryJournal(path)
    payload = {"keywords": "ML Engineer", "location": "France", "limit": 10}

    assert first.claim_start(payload) is None
    with pytest.raises(ApifyStartReservedError):
        second.claim_start(payload)

    first.record_started(
        payload=payload,
        run_id="run-shared",
        dataset_id="dataset-shared",
        actor_status="RUNNING",
    )

    assert first.list_start_reservations() == []
    pending = second.claim_start(payload)
    assert pending is not None
    assert (pending.run_id, pending.dataset_id) == ("run-shared", "dataset-shared")


def test_changed_limit_cannot_bypass_an_uncertain_start(tmp_path) -> None:
    journal = ApifyRecoveryJournal(tmp_path / "runs.json")
    payload = {"keywords": "ML Engineer", "location": "France", "limit": 10}
    journal.claim_start(payload)

    with pytest.raises(ApifyStartReservedError):
        journal.claim_start({**payload, "limit": 20, "skipJobId": ["already-seen"]})

    # A deliberate change to geographic scope identifies a different search.
    assert journal.claim_start({**payload, "location": "Paris"}) is None
    journal.record_started(
        payload=payload,
        run_id="known",
        dataset_id="dataset",
        actor_status="RUNNING",
    )
    pending = journal.claim_start({**payload, "limit": 20})
    assert pending.run_id == "known"
    assert pending.request_payload["limit"] == 10


def test_original_version_one_record_without_terminal_fields_still_loads(tmp_path) -> None:
    journal = ApifyRecoveryJournal(tmp_path / "runs.json")
    _record_run(journal)
    document = json.loads(journal.path.read_text())
    for record in document["runs"]:
        record.pop("terminal_status_message", None)
        record.pop("terminal_error_fatal", None)
    journal.path.write_text(json.dumps(document))

    record = journal.list_pending()[0]
    assert record.run_id == "run-1"
    assert record.terminal_error_fatal is None
    assert isinstance(record, PendingApifyRun)


def test_resuming_a_known_pass_does_not_clear_a_different_uncertain_post(tmp_path) -> None:
    journal = ApifyRecoveryJournal(tmp_path / "runs.json")
    payload = {"keywords": "query", "limit": 10}
    journal.claim_start(payload)
    # Existing v1 journals can contain a separate uncertain POST at one limit
    # and a known run at another. Resuming the known run does not resolve the POST.
    journal.record_started(
        payload={**payload, "limit": 20},
        run_id="known",
        dataset_id="dataset",
        actor_status="SUCCEEDED",
    )
    assert journal.claim_start({**payload, "limit": 30}).run_id == "known"
    assert len(journal.list_start_reservations()) == 1
    journal.remove_many(["known"])
    with pytest.raises(ApifyStartReservedError):
        journal.claim_start({**payload, "limit": 30})


def _response(data):  # noqa: ANN001
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(data).encode()
    return response


def _actor_job(identifier: str, level: str = "Associate") -> dict:
    return {
        "id": identifier,
        "title": f"Specialist {identifier}",
        "companyName": f"Company {identifier}",
        "description": "Build ML systems.",
        "experienceLevel": level,
    }


def _collection(scraper, result) -> IngestCollection:  # noqa: ANN001
    return IngestCollection(
        source="linkedin",
        raw_jobs=result.raw_jobs,
        completed_source_run_ids=result.completed_source_run_ids,
        source_run_journal_path=str(scraper.recovery_journal.path),
    )


@pytest.mark.parametrize("crash_at", ["before_commit", "after_commit", "after_ack"])
@pytest.mark.parametrize("resumed_limit", [1, 4])
def test_resume_primary_and_fallback_across_commit_boundaries_without_new_post(
    isolated_db,
    monkeypatch,
    tmp_path,
    crash_at,
    resumed_limit,
) -> None:
    scraper = LinkedInJobsScraper(
        token="fake",
        read_max_attempts=1,
        recovery_path=tmp_path / "runs.json",
    )
    posts = []

    def post(url, **kwargs):  # noqa: ANN001
        posts.append(kwargs["json"])
        index = len(posts)
        return _response(
            {
                "data": {
                    "id": f"run-{index}",
                    "defaultDatasetId": f"dataset-{index}",
                    "status": "SUCCEEDED" if index == 1 else "RUNNING",
                }
            }
        )

    def unavailable_fallback(url, **kwargs):  # noqa: ANN001
        if "/actor-runs/" in url:
            raise requests.ReadTimeout("status unavailable")
        return _response([_actor_job("primary")] if "/dataset-1/" in url else [])

    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.post", post)
    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.get", unavailable_fallback)
    first = collect_round_robin(
        scraper=scraper,
        source="linkedin",
        queries=["query"],
        location="France",
        max_results=3,
        search_kwargs={},
    )
    assert [item["limit"] for item in posts] == [3, 2]
    assert first.completed_source_run_ids == ["run-1"]

    if crash_at == "after_ack":
        Ingestor().persist_collection(_collection(scraper, first))
    elif crash_at == "after_commit":
        with monkeypatch.context() as patch:

            def fail_cleanup(*args, **kwargs):
                raise ApifyRecoveryJournalError("crash before ack")

            patch.setattr(ApifyRecoveryJournal, "remove_many", fail_cleanup)
            report = Ingestor().persist_collection(_collection(scraper, first))
            assert report.persisted == 1
            assert report.warnings

    reads = []

    def resumed_get(url, **kwargs):  # noqa: ANN001
        reads.append(url)
        if "/actor-runs/" in url:
            return _response(
                {
                    "data": {
                        "id": "run-2",
                        "defaultDatasetId": "dataset-2",
                        "status": "SUCCEEDED",
                    }
                }
            )
        if "/dataset-1/" in url:
            return _response([_actor_job("primary")])
        return _response(
            [
                _actor_job("fallback", "Mid-Senior level"),
                _actor_job("fallback-second", "Mid-Senior level"),
            ]
        )

    def forbidden_post(*args, **kwargs):
        pytest.fail("A pending search must resume its runs without a paid POST")

    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.post", forbidden_post)
    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.get", resumed_get)
    resumed = LinkedInJobsScraper(token="fake", recovery_path=scraper.recovery_journal.path)
    # Include a fresh query before the pending one: it must not be repaid first.
    second = collect_round_robin(
        scraper=resumed,
        source="linkedin",
        queries=["fresh-earlier", "query", "fresh-later"],
        location="France",
        max_results=resumed_limit,
        search_kwargs={},
    )
    assert any("actor-runs/run-2" in url for url in reads)
    assert all("fresh" not in job.title for job in second.raw_jobs)
    fallback = next(job for job in second.raw_jobs if job.source_data["id"] == "fallback")
    assert fallback.source_data["_smartapply_search"]["limit"] == 2
    assert fallback.source_data["_smartapply_search"]["experience_pass"] == 2
    assert second.completed_source_run_ids == (
        ["run-2"] if crash_at == "after_ack" else ["run-1", "run-2"]
    )
    assert any("Aucun nouvel Actor" in warning for warning in second.warnings)
    assert len(second.raw_jobs) == (2 if crash_at == "after_ack" else 3)
    Ingestor().persist_collection(_collection(resumed, second))
    assert resumed.recovery_journal.list_pending() == []
    with session_scope() as session:
        assert session.query(Job).count() == 3

    fresh_posts = []

    def fresh_post(url, **kwargs):  # noqa: ANN001
        fresh_posts.append(kwargs["json"])
        return _response(
            {
                "data": {
                    "id": "fresh-run",
                    "defaultDatasetId": "fresh-dataset",
                    "status": "SUCCEEDED",
                }
            }
        )

    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.post", fresh_post)
    monkeypatch.setattr(
        "smartapply.scrapers.linkedin.requests.get",
        lambda *a, **kw: _response([_actor_job("fresh")]),
    )
    fresh = collect_round_robin(
        scraper=resumed,
        source="linkedin",
        queries=["query"],
        location="France",
        max_results=1,
        search_kwargs={},
    )
    assert len(fresh_posts) == 1
    assert fresh.completed_source_run_ids == ["fresh-run"]


def test_terminal_quota_error_keeps_its_fatal_class_after_crash(
    monkeypatch,
    tmp_path,
) -> None:
    scraper = LinkedInJobsScraper(token="fake", recovery_path=tmp_path / "runs.json")
    monkeypatch.setattr(
        "smartapply.scrapers.linkedin.requests.post",
        lambda *a, **kw: _response(
            {
                "data": {
                    "id": "fatal",
                    "defaultDatasetId": "dataset",
                    "status": "FAILED",
                    "statusMessage": "Account usage quota exceeded",
                },
            }
        ),
    )
    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.get", lambda *a, **kw: _response([]))
    with pytest.raises(ScraperStopError, match="quota exceeded"):
        list(scraper.search("query", max_results=1, experience_level=[]))
    pending = scraper.recovery_journal.list_pending()[0]
    assert pending.terminal_error_fatal is True

    def forbidden_post(*args, **kwargs):
        pytest.fail("A fatal run must not be replaced by a paid POST")

    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.post", forbidden_post)
    resumed = LinkedInJobsScraper(token="fake", recovery_path=scraper.recovery_journal.path)
    with pytest.raises(ScraperStopError, match="quota exceeded"):
        list(resumed.search("query", max_results=2, experience_level=[]))


@pytest.mark.parametrize("metadata_available", [True, False])
def test_legacy_failed_run_refreshes_terminal_metadata_before_recovery(
    monkeypatch,
    tmp_path,
    metadata_available,
) -> None:
    scraper = LinkedInJobsScraper(token="fake", recovery_path=tmp_path / "runs.json")
    payload = scraper.create_search_session(
        "query",
        location="France",
        max_results=1,
        experience_level=[],
    )
    actor_payload = {
        **payload.base_payload,
        "urlParam": [{"key": "f_JT", "value": "F"}, {"key": "f_WT", "value": "1,2,3"}],
        "limit": 1,
    }
    scraper.recovery_journal.record_started(
        payload=actor_payload,
        run_id="legacy",
        dataset_id="dataset",
        actor_status="FAILED",
    )
    scraper.recovery_journal.mark_dataset_ready(
        "legacy",
        dataset_id="dataset",
        actor_status="FAILED",
    )
    document = json.loads(scraper.recovery_journal.path.read_text())
    for record in document["runs"]:
        record.pop("terminal_status_message", None)
        record.pop("terminal_error_fatal", None)
    scraper.recovery_journal.path.write_text(json.dumps(document))
    reads = []

    def get(url, **kwargs):  # noqa: ANN001
        reads.append(url)
        if "/actor-runs/" in url:
            if not metadata_available:
                raise requests.ReadTimeout("legacy metadata inaccessible")
            return _response(
                {
                    "data": {
                        "id": "legacy",
                        "defaultDatasetId": "dataset",
                        "status": "FAILED",
                        "statusMessage": "Account usage quota exceeded",
                    }
                }
            )
        return _response([])

    def forbidden_post(*args, **kwargs):
        pytest.fail("Legacy pending run must not create an Actor")

    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.get", get)
    monkeypatch.setattr("smartapply.scrapers.linkedin.requests.post", forbidden_post)
    message = "quota exceeded" if metadata_available else "cause terminale.*inaccessible"
    with pytest.raises(ScraperStopError, match=message):
        list(scraper.search("query", location="France", max_results=2, experience_level=[]))
    assert reads[0].endswith("actor-runs/legacy")
    assert scraper.recovery_journal.list_pending()


@pytest.mark.parametrize("skipped_known", [0, 10])
def test_completed_empty_or_known_dataset_is_cleared_after_db_commit(
    isolated_db,
    tmp_path,
    skipped_known,
) -> None:
    journal = ApifyRecoveryJournal(tmp_path / "apify-runs.json")
    _record_run(journal)
    collection = IngestCollection(
        source="linkedin",
        raw_jobs=[],
        skipped_known_during_collect=skipped_known,
        completed_source_run_ids=["run-1"],
        source_run_journal_path=str(journal.path),
    )

    report = Ingestor().persist_collection(collection)

    assert report.fetched == 0
    assert report.skipped_known_during_collect == skipped_known
    assert report.completed_source_run_ids == ["run-1"]
    assert journal.list_pending() == []


def test_persistence_failure_keeps_completed_dataset_for_recovery(
    monkeypatch,
    tmp_path,
) -> None:
    journal = ApifyRecoveryJournal(tmp_path / "apify-runs.json")
    _record_run(journal)
    collection = IngestCollection(
        source="linkedin",
        raw_jobs=[],
        completed_source_run_ids=["run-1"],
        source_run_journal_path=str(journal.path),
    )
    ingestor = Ingestor()

    def fail_persistence(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ingestor, "_persist", fail_persistence)

    with pytest.raises(RuntimeError, match="database unavailable"):
        ingestor.persist_collection(collection)

    pending = journal.list_pending()
    assert [(item.run_id, item.state) for item in pending] == [("run-1", "dataset_ready")]


def test_real_commit_failure_rolls_back_job_and_keeps_dataset(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    journal = ApifyRecoveryJournal(tmp_path / "apify-runs.json")
    _record_run(journal)
    collection = IngestCollection(
        source="linkedin",
        raw_jobs=[_raw_job()],
        completed_source_run_ids=["run-1"],
        source_run_journal_path=str(journal.path),
    )
    original_commit = Session.commit

    def fail_commit(self):  # noqa: ANN001
        raise RuntimeError("commit unavailable")

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit unavailable"):
        Ingestor().persist_collection(collection)
    monkeypatch.setattr(Session, "commit", original_commit)

    with session_scope() as session:
        assert session.query(Job).count() == 0
    assert [(item.run_id, item.state) for item in journal.list_pending()] == [
        ("run-1", "dataset_ready")
    ]


def test_journal_cleanup_failure_happens_after_commit_and_warns(
    isolated_db,
    monkeypatch,
    tmp_path,
) -> None:
    journal = ApifyRecoveryJournal(tmp_path / "apify-runs.json")
    _record_run(journal)
    collection = IngestCollection(
        source="linkedin",
        raw_jobs=[_raw_job()],
        completed_source_run_ids=["run-1"],
        source_run_journal_path=str(journal.path),
    )

    def fail_cleanup(self, run_ids):  # noqa: ANN001, ARG001
        raise ApifyRecoveryJournalError("journal read-only")

    monkeypatch.setattr(ApifyRecoveryJournal, "remove_many", fail_cleanup)

    report = Ingestor().persist_collection(collection)

    assert report.persisted == 1
    assert any("journal de récupération Apify" in warning for warning in report.warnings)
    with session_scope() as session:
        assert session.query(Job).filter(Job.external_id == "linkedin:job-1").count() == 1
    assert [(item.run_id, item.state) for item in journal.list_pending()] == [
        ("run-1", "dataset_ready")
    ]
