"""Provider results must respect decisions made while their computation runs."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from smartapply.config import get_settings
from smartapply.database import session_scope
from smartapply.database.models import Application, Job, JobAnalysis, JobScore, JobStatus
from smartapply.llm import MotivationLetter, OfferAnalysis
from smartapply.pipeline.applier import Applier
from smartapply.pipeline.errors import StaleTaskResultError, TaskCancelledError
from smartapply.pipeline.process.analysis import AnalysisMixin
from smartapply.pipeline.processor import Processor
from smartapply.profile import get_profile
from smartapply.ranking import MockEmbeddingsProvider


def offer_result():
    return OfferAnalysis(role_type="AI Engineer", seniority="", domain="",
                         main_tasks=["Build models"], required_skills=["Python"],
                         nice_to_have=[], cv_keywords_to_include=["Python"])


def insert_job(*, analyzed=False):
    with session_scope() as session:
        job = Job(external_id="concurrent", title="AI Engineer", company="Acme",
                  description="Build Python models.", source="manual", location="Paris")
        if analyzed:
            job.analyzed_at = datetime.now(timezone.utc)
            job.status = JobStatus.ANALYZED
            job.analysis = JobAnalysis(raw_response={"main_tasks": ["Build Python models"]})
        session.add(job)
        session.flush()
        return job.id


def prepare_analysis_candidate(job_id: int) -> None:
    with session_scope() as session:
        job = session.get(Job, job_id)
        now = datetime.now(timezone.utc)
        job.filtered_at = now
        job.ranked_at = now
        session.add(JobScore(
            job_id=job_id,
            final_score=0.7,
            components={
                "filter_disposition": "pending_relevance",
                "hard_reject": {
                    "ruleset_version": "hard-reject-v2",
                    "rejected": False,
                    "gates": [],
                },
            },
        ))


@pytest.mark.parametrize("change", ["description", "archive", "sent"])
def test_changed_offer_does_not_consume_fit_call_or_publish_analysis(isolated_db, change):
    job_id = insert_job()
    prepare_analysis_candidate(job_id)
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        with session_scope() as session:
            job = session.get(Job, job_id)
            if change == "description":
                job.description = "A different role"
            else:
                job.status = JobStatus.ARCHIVED if change == "archive" else JobStatus.SENT
                if change == "archive":
                    job.archived_at = datetime.now(timezone.utc)
        return offer_result()

    instance = AnalysisMixin()
    instance.profile, instance.settings = get_profile(), get_settings()
    instance.llm = SimpleNamespace(cheap_model="mock", complete_json=complete)
    with pytest.raises(StaleTaskResultError):
        instance._analyze_one(job_id=job_id)
    assert calls == ["job_offer_analysis"]
    with session_scope() as session:
        assert session.get(Job, job_id).analysis is None
        assert session.get(Job, job_id).analyzed_at is None


def test_direct_analysis_refuses_unfiltered_offer_without_provider_call(isolated_db):
    job_id = insert_job()
    calls = []
    instance = AnalysisMixin()
    instance.profile, instance.settings = get_profile(), get_settings()
    instance.llm = SimpleNamespace(
        cheap_model="mock",
        complete_json=lambda **kwargs: calls.append(kwargs["purpose"]),
    )

    assert instance._analyze_one(job_id=job_id) is False
    assert calls == []


def test_cancelled_extraction_does_not_start_fit_call(isolated_db):
    job_id = insert_job()
    prepare_analysis_candidate(job_id)
    cancelled, calls = Event(), []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        cancelled.set()
        return offer_result()

    instance = AnalysisMixin()
    instance.profile, instance.settings = get_profile(), get_settings()
    instance.llm = SimpleNamespace(cheap_model="mock", complete_json=complete)
    instance.stop_requested = cancelled.is_set
    report = instance.analyze_jobs([job_id])
    assert report.cancelled and report.analyzed == 0 and report.errors == []
    assert calls == ["job_offer_analysis"]


def test_ranking_does_not_reactivate_offer_archived_during_embedding(isolated_db):
    from smartapply.dedup import Deduplicator
    from smartapply.filtering import JobFilter, ruleset_from_preferences
    from smartapply.ranking import JobScorer

    job_id = insert_job()
    profile = get_profile()
    scorer = JobScorer(profile, embeddings=MockEmbeddingsProvider())
    rank = scorer.rank

    def archive_then_rank(jobs):
        with session_scope() as session:
            job = session.get(Job, job_id)
            job.status, job.archived_at = JobStatus.ARCHIVED, datetime.now(timezone.utc)
        return rank(jobs)

    scorer.rank = archive_then_rank
    processor = Processor(profile=profile, deduplicator=Deduplicator(),
                          job_filter=JobFilter(ruleset_from_preferences(profile.preferences)),
                          scorer=scorer, llm=None)
    result = processor.rank_pending(job_ids=[job_id], persist_shortlist=False)
    assert result.ranked_ids == []
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.ARCHIVED
        assert job.score is None and job.ranked_at is None


def test_ranking_does_not_overwrite_rejection_persisted_during_scoring(isolated_db):
    from smartapply.dedup import Deduplicator
    from smartapply.filtering import JobFilter, ruleset_from_preferences
    from smartapply.ranking import JobScorer

    job_id = insert_job()
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.filtered_at = datetime.now(timezone.utc)
        job.score = JobScore(
            components={
                "filter_disposition": "pending_relevance",
                "hard_reject": {
                    "ruleset_version": "hard-reject-v2",
                    "rejected": False,
                    "gates": [],
                },
            },
        )

    profile = get_profile()
    scorer = JobScorer(profile, embeddings=MockEmbeddingsProvider())
    rank = scorer.rank

    def reject_then_return_scores(jobs):
        result = rank(jobs)
        with session_scope() as session:
            score = session.get(JobScore, job_id)
            assert score is not None
            score.components = {
                "filter_disposition": "rejected",
                "hard_reject": {
                    "ruleset_version": "hard-reject-v2",
                    "rejected": True,
                    "gates": [],
                },
            }
        return result

    scorer.rank = reject_then_return_scores
    processor = Processor(
        profile=profile,
        deduplicator=Deduplicator(),
        job_filter=JobFilter(ruleset_from_preferences(profile.preferences)),
        scorer=scorer,
        llm=None,
    )

    report = processor.rank_pending(job_ids=[job_id], persist_shortlist=False)

    assert report.ranked_ids == []
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None and job.score is not None
        assert job.ranked_at is None
        assert job.score.final_score is None
        assert job.score.components["filter_disposition"] == "rejected"


def test_analysis_ranking_keeps_the_captured_offer_scope(isolated_db):
    from smartapply.database.models import JobScore
    from smartapply.dedup import Deduplicator
    from smartapply.filtering import JobFilter, ruleset_from_preferences
    from smartapply.ranking import JobScorer

    selected_id = insert_job()
    with session_scope() as session:
        other = Job(external_id="outside-scope", title="AI Engineer", company="Elsewhere",
                    description="Build Python models", source="manual", status=JobStatus.FILTERED,
                    filtered_at=datetime.now(timezone.utc), ranked_at=datetime.now(timezone.utc))
        other.score = JobScore(final_score=1.0)
        session.add(other)
    profile = get_profile()
    processor = Processor(profile=profile, deduplicator=Deduplicator(),
                          job_filter=JobFilter(ruleset_from_preferences(profile.preferences)),
                          scorer=JobScorer(profile, embeddings=MockEmbeddingsProvider()), llm=None)
    report = processor.rank_pending(job_ids=[selected_id], top_k_ranked=1, persist_shortlist=False)
    assert report.ranked_ids == report.shortlisted_ids == [selected_id]


@pytest.mark.parametrize("change", ["sent", "description", "notes", "cancel"])
def test_publication_revalidates_state_and_preserves_old_files(isolated_db, change):
    job_id = insert_job(analyzed=True)
    with session_scope() as session:
        application = Application(job_id=job_id, cv_docx_path="old", cv_json={"old": True},
                                  status=JobStatus.READY_FOR_FORM_SUBMISSION)
        session.add(application)
        session.flush()
        application_id = application.id
    final = get_settings().output_dir / str(application_id)
    final.mkdir(parents=True)
    previous = final / "previous.docx"
    previous.write_bytes(b"previous complete dossier")
    cancelled = Event()

    def render_all(**kwargs):
        report = kwargs["report"]
        path = Path(kwargs["output_dir"]) / "replacement.docx"
        path.write_bytes(b"replacement")
        report.docx_path = str(path)
        with session_scope() as session:
            job, app = session.get(Job, job_id), session.get(Application, application_id)
            if change == "sent":
                app.status, app.form_submitted_at = JobStatus.SENT, datetime.now(timezone.utc)
                job.status = JobStatus.SENT
            elif change == "description":
                job.description = "Changed after rendering"
            elif change == "notes":
                app.notes = "Note edited during generation"
            else:
                cancelled.set()

    renderer = SimpleNamespace(render_all=render_all, add_document_rows=lambda *args: None)
    applier = Applier(profile=get_profile(), llm=None, adapter=None, validator=None,
                      renderer=renderer, stop_requested=cancelled.is_set)
    adapted = SimpleNamespace(model_dump=lambda: {"new": True})
    applier._generate_draft = lambda **kwargs: (
        adapted, MotivationLetter(subject="Application", body="Motivation"),
    )
    applier._validate_letter = lambda *args: None
    applier._validate_cv_offer_alignment = lambda *args: None
    if change == "notes":
        report = applier.apply(job_id, force_regenerate=True)
        assert Path(report.docx_path).read_bytes() == b"replacement"
        with session_scope() as session:
            assert session.get(Application, application_id).notes == "Note edited during generation"
    else:
        expected = TaskCancelledError if change == "cancel" else StaleTaskResultError
        with pytest.raises(expected):
            applier.apply(job_id, force_regenerate=True)
        assert previous.read_bytes() == b"previous complete dossier"
        with session_scope() as session:
            assert session.get(Application, application_id).cv_json == {"old": True}
    assert not list(final.parent.glob(f".{application_id}.*"))


def test_other_offer_can_be_archived_during_fetch_comparison(isolated_db):
    from smartapply.concurrency import job_operation
    from smartapply.database import write_session_scope
    from smartapply.offers import RawJob
    from smartapply.pipeline.ingestor import Ingestor

    other_id = insert_job()
    entered, release = Event(), Event()
    ingestor = Ingestor()
    compare = ingestor._find_probable_duplicate

    def paused_compare(raw, known):
        entered.set()
        assert release.wait(5)
        return compare(raw, known)

    ingestor._find_probable_duplicate = paused_compare
    raw = RawJob(external_id="fetched", title="Data Scientist", company="Different",
                 description="Forecast demand", source="linkedin")

    def archive_other():
        with (job_operation([other_id], operation="archivage"),
              write_session_scope() as session):
            job = session.get(Job, other_id)
            job.status, job.archived_at = JobStatus.ARCHIVED, datetime.now(timezone.utc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fetch = pool.submit(ingestor._persist, "linkedin", [raw])
        try:
            assert entered.wait(5)
            # Completion while comparison remains suspended proves there is
            # no SQLite/writer lock held around the matching calculation.
            pool.submit(archive_other).result(timeout=5)
        finally:
            release.set()
        report = fetch.result(timeout=5)
    assert report.inserted == 1
    with session_scope() as session:
        assert session.get(Job, other_id).status == JobStatus.ARCHIVED


def test_partial_ingest_keeps_apify_journal_until_idempotent_resume(isolated_db, monkeypatch):
    import smartapply.pipeline.ingestor as module
    from smartapply.offers import RawJob
    from smartapply.pipeline.ingest import IngestCollection
    from smartapply.pipeline.ingestor import Ingestor
    from smartapply.scrapers.apify_recovery import ApifyRecoveryJournal

    journal = ApifyRecoveryJournal(isolated_db / "pending-runs.json")
    journal.record_started(payload={"keywords": "AI"}, run_id="run-1",
                           dataset_id="dataset-1", actor_status="SUCCEEDED")
    raws = [RawJob(external_id=f"batch-{index}", title="AI Engineer",
                   company=f"Company {index}", description="Build models", source="linkedin")
            for index in range(2)]
    collection = IngestCollection(source="linkedin", raw_jobs=raws,
                                 completed_source_run_ids=["run-1"],
                                 source_run_journal_path=str(journal.path))
    upsert = module.upsert_job

    def fail_second(session, **fields):
        if fields["external_id"] == "batch-1":
            raise RuntimeError("simulated commit failure")
        return upsert(session, **fields)

    monkeypatch.setattr(module, "upsert_job", fail_second)
    with pytest.raises(RuntimeError):
        Ingestor().persist_collection(collection)
    assert len(journal.list_pending()) == 1
    with session_scope() as session:
        assert session.query(Job).count() == 1
    monkeypatch.setattr(module, "upsert_job", upsert)
    report = Ingestor().persist_collection(collection)
    assert report.inserted == 1
    assert journal.list_pending() == []
    with session_scope() as session:
        assert session.query(Job).count() == 2


@pytest.mark.parametrize("state", ["archived", "sent", "published"])
def test_manual_readdition_preserves_tracking_history_and_documents(isolated_db, state):
    from smartapply.offers import ManualOfferInput
    from smartapply.pipeline.ingestor import Ingestor

    ingestor = Ingestor()
    offer = ManualOfferInput(title="AI Engineer", company="Acme", description="Build Python models",
                             application_url="https://acme.example/jobs/123")
    job_id = ingestor.from_manual_offer(offer).job_ids[0]
    with session_scope() as session:
        job = session.get(Job, job_id)
        if state == "archived":
            job.status, job.archived_at = JobStatus.ARCHIVED, datetime.now(timezone.utc)
        else:
            application = Application(job_id=job_id, notes="Preserved note",
                                      cv_json={"old": True}, cv_docx_path="old.docx")
            application.status = JobStatus.SENT if state == "sent" else JobStatus.READY_FOR_FORM_SUBMISSION
            if state == "sent":
                application.form_submitted_at = datetime.now(timezone.utc)
            session.add(application)
    changed_offer = replace(offer, description="A replacement description")
    result = ingestor.from_manual_offer(changed_offer)
    assert result.job_ids == [] and result.skipped_processed == 1
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.description == offer.description
        if state == "archived":
            assert job.archived_at is not None
        else:
            assert job.application.cv_json == {"old": True}
            assert job.application.notes == "Preserved note"


def test_cancelled_empty_reservation_preserves_notes_and_can_be_retried(isolated_db):
    from smartapply.pipeline.apply.persistence import (
        ApplicationPersistenceMixin,
        reservation_is_stale,
    )
    from smartapply.pipeline.reports import ApplyReport

    job_id = insert_job(analyzed=True)
    persistence = ApplicationPersistenceMixin()
    first = ApplyReport(job_id=job_id, application_id=None)
    assert persistence._reserve_application_id(first)
    with session_scope() as session:
        session.get(Application, first.application_id).notes = "Note written during generation"
    persistence._release_application_reservation(first)
    with session_scope() as session:
        application = session.get(Application, first.application_id)
        assert application.notes == "Note written during generation"
        assert reservation_is_stale(application)
    retry = ApplyReport(job_id=job_id, application_id=None)
    assert persistence._reserve_application_id(retry)
    assert retry.application_id == first.application_id
