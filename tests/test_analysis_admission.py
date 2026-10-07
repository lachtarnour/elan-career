"""Analysis admission is exhaustive and independent of numeric ranking."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from smartapply.config import get_settings
from smartapply.database import session_scope
from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun
from smartapply.database.models import (
    Application,
    Job,
    JobAnalysis,
    JobDuplicateStatus,
    JobScore,
    JobStatus,
)
from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.llm import OfferAnalysis
from smartapply.pipeline import Pipeline
from smartapply.pipeline.errors import StaleTaskResultError
from smartapply.pipeline.process.analysis import AnalysisMixin
from smartapply.pipeline.process.analysis_admission import (
    analysis_group_available,
    analysis_queue_job_ids,
    is_analysis_candidate,
    is_current_analysis_candidate,
)
from smartapply.profile import get_profile
from smartapply.ranking import MockEmbeddingsProvider
from tests.fit_fixtures import evaluation_from_user


def _kept_components():
    return {
        "filter_disposition": "pending_relevance",
        "hard_reject": HardRejectResult(decisions=()).audit_payload(),
    }


def _job(external_id: str, **changes) -> Job:
    values = {
        "external_id": external_id,
        "title": "Machine Learning Engineer",
        "company": "Example AI",
        "description": "Build machine learning models with Python.",
        "source": "manual",
        "location": "Paris",
        "filtered_at": datetime.now(timezone.utc),
        "score": JobScore(components=_kept_components()),
    }
    values.update(changes)
    return Job(**values)


def _provider(calls: list, hook=None):
    def complete(**kwargs):
        calls.append((kwargs["purpose"], kwargs["job_id"]))
        if hook:
            hook(kwargs["purpose"])
        if kwargs["purpose"] == "job_offer_analysis":
            return OfferAnalysis(
                role_type="Machine Learning Engineer",
                seniority="",
                domain="",
                required_skills=["Python"],
                main_tasks=["Build machine learning models"],
                nice_to_have=[],
                cv_keywords_to_include=["Python"],
            )
        return evaluation_from_user(kwargs["user"])

    return SimpleNamespace(cheap_model="mock-cheap", complete_json=complete)


def test_unranked_offer_uses_its_own_score_foreign_key(isolated_db):
    with session_scope() as session:
        kept = _job("kept", id=1, score=JobScore(id=2, components=_kept_components()))
        rejected = _job("rejected", id=2, score=JobScore(id=1, components={
            "filter_disposition": "rejected",
            "hard_reject": {"ruleset_version": "hard-reject-v2", "rejected": True},
        }))
        session.add_all([kept, rejected])
    calls = []
    analyzer = AnalysisMixin()
    analyzer.profile, analyzer.settings, analyzer.llm = get_profile(), get_settings(), _provider(calls)

    report = analyzer.analyze_jobs([1, 2])

    assert report.analyzed == 1 and report.skipped_missing == 1
    assert calls == [("job_offer_analysis", 1), ("job_fit_assessment", 1)]
    with session_scope() as session:
        kept, rejected = session.get(Job, 1), session.get(Job, 2)
        assert kept.analysis.fit_score == 0.88
        assert kept.ranked_at is None and kept.score.final_score is None
        assert rejected.analysis is None and rejected.analyzed_at is None


@pytest.mark.parametrize("purpose", ["job_offer_analysis", "job_fit_assessment"])
def test_own_rejection_is_rechecked_between_calls_and_before_publication(isolated_db, purpose):
    with session_scope() as session:
        session.add(_job("offset-score", score=JobScore(id=100, components=_kept_components())))

    def reject(call_purpose):
        if call_purpose == purpose:
            with session_scope() as session:
                session.get(Job, 1).score.components = {
                    "filter_disposition": "rejected",
                    "hard_reject": {"ruleset_version": "hard-reject-v2", "rejected": True},
                }

    calls = []
    analyzer = AnalysisMixin()
    analyzer.profile, analyzer.settings = get_profile(), get_settings()
    analyzer.llm = _provider(calls, reject)
    with pytest.raises(StaleTaskResultError):
        analyzer._analyze_one(job_id=1)
    assert len(calls) == (1 if purpose == "job_offer_analysis" else 2)
    with session_scope() as session:
        assert session.get(Job, 1).analysis is None


def test_global_processing_analyzes_all_survivors_without_embeddings_or_top25(isolated_db):
    with session_scope() as session:
        session.add_all([_job(f"survivor-{index}") for index in range(30)])
    calls = []
    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=_provider(calls))

    def forbidden_ranking(*args, **kwargs):
        raise AssertionError("Analysis must not call the semantic ranking provider")

    pipeline.scorer.rank = forbidden_ranking
    report = pipeline.process_pending(top_k_analyze=1)

    assert report.analyzed == report.kept_after_filter == 30
    assert len(calls) == 60
    with session_scope() as session:
        assert all(job.analyzed_at is not None for job in session.query(Job))
        assert all(job.ranked_at is None and job.score.final_score is None for job in session.query(Job))
    repeated = pipeline.process_pending()
    assert repeated.analyzed == 0 and len(calls) == 60


@pytest.mark.parametrize("blocked", [
    "archived", "pending_duplicate", "alias", "sent", "interview", "application",
    "submitted_application", "analysis_without_timestamp", "rejected", "stale_proof",
])
def test_candidate_queue_preserves_workflow_protections(isolated_db, blocked):
    with session_scope() as session:
        keeper = _job("canonical")
        session.add(keeper)
        session.flush()
        job = _job("protected")
        if blocked == "archived":
            job.archived_at = datetime.now(timezone.utc)
        elif blocked == "pending_duplicate":
            job.duplicate_review_status = JobDuplicateStatus.PENDING
            job.possible_duplicate_of_id = keeper.id
        elif blocked == "alias":
            job.canonical_job_id = keeper.id
        elif blocked in {"sent", "interview"}:
            job.status = blocked
        elif blocked in {"application", "submitted_application"}:
            job.application = Application(
                form_submitted_at=datetime.now(timezone.utc) if blocked == "submitted_application" else None,
            )
        elif blocked == "analysis_without_timestamp":
            job.analysis = JobAnalysis(fit_score=0.9, raw_response={"preserve": True})
        elif blocked == "rejected":
            job.score.components = {
                "filter_disposition": "rejected",
                "hard_reject": {"ruleset_version": "hard-reject-v2", "rejected": True},
            }
        else:
            job.score.components = {"filter_disposition": "pending_relevance"}
        session.add(job)
        session.flush()
        job_id = job.id
        assert not is_analysis_candidate(job)
        if blocked == "submitted_application":
            assert not is_analysis_candidate(job, exclude_applications=False)

    analyzer = AnalysisMixin()
    assert analyzer.analysis_candidates([job_id]) == []


def test_preparation_repairs_legacy_proof_without_touching_completed_history(isolated_db):
    recorded_at = datetime(2020, 1, 1)
    with session_scope() as session:
        job = _job(
            "legacy-history", status=JobStatus.ANALYZED, ranked_at=recorded_at,
            analyzed_at=recorded_at,
            analysis=JobAnalysis(fit_score=0.9, raw_response={"preserve": True}),
            score=JobScore(final_score=0.8, computed_at=recorded_at, components={
                "filter_disposition": "uncertain", "historical_audit": {"preserve": True},
            }),
        )
        session.add(job)
    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=_provider([]))

    prepared = pipeline._processor.prepare_analysis(job_ids=[1], include_analyzed=True)

    assert prepared.kept_ids == [1]
    with session_scope() as session:
        job = session.get(Job, 1)
        assert job.score.components["completed_filter_routing"]["disposition"] == "pending_relevance"
        assert "filter_disposition" not in job.score.components
        assert job.score.components["historical_audit"] == {"preserve": True}
        assert job.score.final_score == 0.8 and job.score.computed_at == recorded_at
        assert job.ranked_at == job.analyzed_at == recorded_at
        assert job.analysis.raw_response == {"preserve": True}
        assert job.status == JobStatus.ANALYZED


@pytest.mark.parametrize("history", ["sent", "submitted", "draft", "pending_review"])
def test_queue_protects_transitive_duplicate_group_history(isolated_db, history):
    with session_scope() as session:
        root = _job("root")
        session.add(root)
        session.flush()
        alias = _job("alias", canonical_job_id=root.id, archived_at=datetime.now(timezone.utc))
        session.add(alias)
        session.flush()
        descendant = _job("descendant", canonical_job_id=alias.id)
        if history == "pending_review":
            descendant.canonical_job_id = None
            descendant.possible_duplicate_of_id = alias.id
            descendant.duplicate_review_status = JobDuplicateStatus.PENDING
        else:
            descendant.application = Application(
                status=JobStatus.SENT if history == "sent" else JobStatus.ANALYZED,
                form_submitted_at=datetime.now(timezone.utc) if history == "submitted" else None,
            )
        session.add(descendant)
        session.flush()
        assert not analysis_group_available(session, root.id)
        assert analysis_group_available(session, root.id, exclude_applications=False) is (history == "draft")

    analyzer = AnalysisMixin()
    assert analyzer.analysis_candidates([1]) == []
    explicit = analyzer.analysis_candidates([1], exclude_applications=False)
    assert [job.id for job in explicit] == ([1] if history == "draft" else [])


@pytest.mark.parametrize("purpose", ["job_offer_analysis", "job_fit_assessment"])
def test_alias_submission_during_analysis_stops_fit_or_publication(isolated_db, purpose):
    with session_scope() as session:
        root = _job("root")
        session.add(root)
        session.flush()
        session.add(_job("alias", canonical_job_id=root.id))

    def submit_alias(call_purpose):
        if call_purpose == purpose:
            with session_scope() as session:
                session.add(Application(job_id=2, form_submitted_at=datetime.now(timezone.utc)))

    calls = []
    analyzer = AnalysisMixin()
    analyzer.profile, analyzer.settings = get_profile(), get_settings()
    analyzer.llm = _provider(calls, submit_alias)
    with pytest.raises(StaleTaskResultError):
        analyzer._analyze_one(job_id=1)
    assert len(calls) == (1 if purpose == "job_offer_analysis" else 2)
    with session_scope() as session:
        assert session.get(Job, 1).analysis is None


@pytest.mark.parametrize("application_status", [JobStatus.ANALYZED, JobStatus.ARCHIVED])
def test_explicit_unsent_dossier_can_be_analyzed_but_default_selection_skips_it(
    isolated_db, application_status,
):
    with session_scope() as session:
        session.add(_job("draft", application=Application(status=application_status)))
    calls = []
    analyzer = AnalysisMixin()
    analyzer.profile, analyzer.settings, analyzer.llm = get_profile(), get_settings(), _provider(calls)

    protected = analyzer.analyze_jobs([1])
    assert protected.analyzed == 0 and protected.skipped_missing == 1 and calls == []
    explicit = analyzer.analyze_jobs([1], exclude_applications=False)
    assert explicit.analyzed == 1 and len(calls) == 2


def test_explicit_rescue_reanalysis_preserves_prior_analysis_when_provider_fails(isolated_db):
    recorded_at = datetime(2020, 1, 1)
    with session_scope() as session:
        session.add(_job(
            "rescue-history", analyzed_at=recorded_at,
            analysis=JobAnalysis(fit_score=0.9, raw_response={"preserve": True}),
            application=Application(status=JobStatus.ARCHIVED),
        ))
    calls = []
    analyzer = AnalysisMixin()
    analyzer.profile, analyzer.settings, analyzer.llm = get_profile(), get_settings(), _provider(calls)
    normal = analyzer.analyze_jobs([1], exclude_applications=False)
    assert normal.already_analyzed == 1 and calls == []

    def unavailable(**kwargs):
        calls.append(kwargs["purpose"])
        raise RuntimeError("Provider unavailable")

    analyzer.llm.complete_json = unavailable
    failed = analyzer.analyze_jobs([1], force_reanalyze=True, exclude_applications=False)
    assert failed.analyzed == 0 and failed.errors[0]["message"] == "Provider unavailable"
    with session_scope() as session:
        job = session.get(Job, 1)
        assert job.analysis.raw_response == {"preserve": True}
        assert job.analysis.fit_score == 0.9 and job.analyzed_at == recorded_at
        assert job.application.status == JobStatus.ARCHIVED


def test_dashboard_and_launch_intent_count_the_same_protected_queue(isolated_db):
    from smartapply.desktop.services import DesktopService
    from smartapply.pipeline.batch_analysis.presentation import analysis_workspace

    with session_scope() as session:
        eligible = _job("eligible-unranked")
        pending_root = _job("pending-root")
        dossier_root = _job("dossier-root")
        session.add_all([eligible, pending_root, dossier_root])
        session.flush()
        session.add_all([
            _job("pending-review", possible_duplicate_of_id=pending_root.id,
                 duplicate_review_status=JobDuplicateStatus.PENDING),
            _job("alias-dossier", canonical_job_id=dossier_root.id,
                 application=Application(form_submitted_at=datetime.now(timezone.utc))),
            _job("rejected", score=JobScore(components={
                "filter_disposition": "rejected",
                "hard_reject": {"ruleset_version": "hard-reject-v2", "rejected": True},
            })),
            _job("unfiltered", filtered_at=None),
        ])
        eligible_id = eligible.id

    service = DesktopService()
    assert service.dashboard().pending == 1
    assert service.pending_analysis_ids() == [eligible_id]
    assert [row["id"] for row in analysis_workspace()["candidates"]] == [eligible_id]


def test_queue_read_does_not_load_offer_bodies_or_analysis_prompts(isolated_db):
    from sqlalchemy import event

    from smartapply.database import get_engine

    with session_scope() as session:
        session.add(_job("queue-read"))
    statements = []

    def capture(_connection, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with session_scope() as session:
            assert analysis_queue_job_ids(session) == [1]
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert statements
    for forbidden in (
        "jobs.description", "jobs.cleaned_description", "job_analyses.raw_response",
        "applications.cv_json", "analysis_batch_items.analyzer_input", "analysis_batch_waves.requests",
    ):
        assert all(forbidden not in statement for statement in statements)


@pytest.mark.parametrize("run_state", ["active", "attention"])
def test_available_queue_excludes_reserved_batch_items_without_blocking_own_result_validation(
    isolated_db, run_state,
):
    from smartapply.desktop.services import DesktopService

    with session_scope() as session:
        reserved, available = _job("reserved"), _job("available")
        session.add_all([reserved, available])
        session.flush()
        run = AnalysisBatchRun(id="test-run", state=run_state, model="mock", profile_data={})
        session.add(run)
        session.flush()
        session.add(AnalysisBatchItem(
            run_id=run.id, job_id=reserved.id, job_snapshot="0" * 64,
            title=reserved.title, company=reserved.company, analyzer_input={},
        ))
        available_id = available.id
    service = DesktopService()
    assert service.dashboard().pending == 1
    assert service.pending_analysis_ids() == [available_id]
    analyzer = AnalysisMixin()
    assert [job.id for job in analyzer.analysis_candidates()] == [available_id]
    with session_scope() as session:
        assert is_current_analysis_candidate(session, session.get(Job, 1))
        session.get(AnalysisBatchRun, "test-run").state = "completed"
    assert service.pending_analysis_ids() == [1, available_id]
