"""Explicit repair path for legacy analyzed offers missing local ranking."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from smartapply.database import session_scope
from smartapply.database.models import (
    Application,
    GeneratedDocument,
    Job,
    JobAnalysis,
    JobScore,
    JobStatus,
    ShortlistOrigin,
)
from smartapply.desktop.services import DesktopService
from smartapply.llm import MockLLMProvider
from smartapply.pipeline import Pipeline
from smartapply.ranking import MockEmbeddingsProvider


class _NoCallLLM(MockLLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def complete_json(self, **kwargs):
        self.calls.append(str(kwargs.get("purpose") or ""))
        raise AssertionError("A completed historical analysis must not call the LLM")


def _historical_analyzed_job(
    *,
    external_id: str,
    filtered: bool,
    ranking_state: str,
) -> tuple[int, int, datetime]:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        job = Job(
            external_id=external_id,
            title="Machine Learning Engineer",
            company="Example AI",
            location="Paris",
            contract_type="CDI",
            description="Build machine learning models with Python.",
            source="manual",
            status=JobStatus.ANALYZED,
            filtered_at=now if filtered else None,
            analyzed_at=now,
            analysis=JobAnalysis(
                fit_score=0.82,
                role_type="Machine Learning Engineer",
                raw_response={"historical": True},
                model_used="legacy-model",
            ),
        )
        if ranking_state == "missing_score":
            job.ranked_at = now
            job.score = JobScore(
                final_score=None,
                components={"historical_audit": {"preserve": True}},
            )
        session.add(job)
        session.flush()
        assert job.analysis is not None
        # SQLite returns persisted timezone-aware columns as naive UTC values.
        return int(job.id), int(job.analysis.id), now.replace(tzinfo=None)


@pytest.mark.parametrize(
    ("filtered", "ranking_state"),
    [
        (True, "missing_all"),
        (True, "missing_score"),
        (False, "missing_all"),
    ],
)
def test_explicit_ranking_repairs_analyzed_history_without_reanalysis(
    isolated_db,
    filtered: bool,
    ranking_state: str,
) -> None:
    job_id, analysis_id, analyzed_at = _historical_analyzed_job(
        external_id=f"legacy:{filtered}:{ranking_state}",
        filtered=filtered,
        ranking_state=ranking_state,
    )
    llm = _NoCallLLM()
    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=llm)

    report = pipeline.rank_pending(
        top_k_ranked=1,
        job_ids=[job_id],
        persist_shortlist=False,
    )

    assert report.total == 1
    assert llm.calls == []
    with session_scope() as session:
        repaired = session.get(Job, job_id)
        assert repaired is not None and repaired.analysis is not None
        assert repaired.filtered_at is not None
        assert repaired.ranked_at is not None
        assert repaired.score is not None and repaired.score.final_score is not None
        assert repaired.analyzed_at == analyzed_at
        assert repaired.analysis.id == analysis_id
        assert repaired.analysis.fit_score == 0.82
        assert repaired.status == JobStatus.ANALYZED
        if ranking_state == "missing_score":
            assert repaired.score.components["historical_audit"] == {"preserve": True}


def test_global_ranking_does_not_mutate_analyzed_history(isolated_db) -> None:
    historical_id, analysis_id, analyzed_at = _historical_analyzed_job(
        external_id="legacy:explicit-only",
        filtered=True,
        ranking_state="missing_all",
    )
    llm = _NoCallLLM()
    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=llm)
    fresh = pipeline.ingest_text(
        text="Build Python machine learning models.",
        title="Data Scientist",
        company="Fresh AI",
        location="Paris",
    )

    pipeline.rank_pending(persist_shortlist=False)

    assert llm.calls == []
    with session_scope() as session:
        historical = session.get(Job, historical_id)
        current = session.get(Job, fresh.job_ids[0])
        assert historical is not None and historical.analysis is not None
        assert historical.ranked_at is None
        assert historical.score is None
        assert historical.analyzed_at == analyzed_at
        assert historical.analysis.id == analysis_id
        assert current is not None
        assert current.ranked_at is not None
        assert current.score is not None and current.score.final_score is not None


def test_explicit_repair_of_blocked_legacy_history_changes_only_filter_metadata(
    isolated_db,
) -> None:
    job_id, analysis_id, analyzed_at = _historical_analyzed_job(
        external_id="legacy:protected-blocked",
        filtered=True,
        ranking_state="missing_all",
    )
    ranked_at = datetime(2001, 2, 3, 4, 5, 6)
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.title = "Data Scientist Stage"
        job.description = "Stage de six mois en data science."
        job.ranked_at = ranked_at
        job.shortlisted_at = ranked_at
        job.shortlist_origin = ShortlistOrigin.MANUAL
        job.status = JobStatus.SHORTLISTED
        job.score = JobScore(
            final_score=0.95,
            semantic_score=0.91,
            computed_at=ranked_at,
            components={
                "filter_disposition": "uncertain",
                "semantic": 0.91,
                "final": 0.95,
                "manual_review": {"preserve": True},
            },
        )
        application = Application(
            job_id=job_id,
            status=JobStatus.READY_FOR_FORM_SUBMISSION,
            cv_json={"preserve": True},
        )
        session.add(application)
        session.flush()
        document = GeneratedDocument(
            application_id=application.id,
            doc_type="cv_json",
            content='{"preserve": true}',
        )
        session.add(document)
        session.flush()
        application_id = int(application.id)
        document_id = int(document.id)

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=_NoCallLLM())
    scored_ids: list[int] = []
    rank = pipeline.scorer.rank

    def capture_rank(jobs):
        scored_ids.extend(int(job.id) for job in jobs)
        return rank(jobs)

    pipeline.scorer.rank = capture_rank
    report = pipeline.rank_pending(
        job_ids=[job_id],
        persist_shortlist=False,
    )

    assert scored_ids == []
    assert report.ranked_ids == []
    with session_scope() as session:
        job = session.get(Job, job_id)
        application = session.get(Application, application_id)
        document = session.get(GeneratedDocument, document_id)
        assert job is not None and job.score is not None and job.analysis is not None
        assert job.archived_at is None
        assert job.status == JobStatus.SHORTLISTED
        assert job.filtered_at is not None
        assert job.ranked_at == ranked_at
        assert job.shortlisted_at == ranked_at
        assert job.shortlist_origin == ShortlistOrigin.MANUAL
        assert job.analyzed_at == analyzed_at
        assert job.analysis.id == analysis_id
        assert job.analysis.raw_response == {"historical": True}
        assert job.analysis.model_used == "legacy-model"
        assert job.score.final_score == 0.95
        assert job.score.semantic_score == 0.91
        assert job.score.computed_at == ranked_at
        assert "filter_disposition" not in job.score.components
        assert job.score.components["completed_filter_routing"]["disposition"] == "rejected"
        assert job.score.components["hard_reject"]["rejected"] is True
        assert job.score.components["semantic"] == 0.91
        assert job.score.components["final"] == 0.95
        assert job.score.components["manual_review"] == {"preserve": True}
        assert application is not None and application.cv_json == {"preserve": True}
        assert document is not None and document.content == '{"preserve": true}'


def test_shortlist_action_repairs_history_and_reuses_existing_analysis(
    isolated_db,
    monkeypatch,
) -> None:
    job_id, analysis_id, analyzed_at = _historical_analyzed_job(
        external_id="legacy:shortlist-repair",
        filtered=False,
        ranking_state="missing_all",
    )
    llm = _NoCallLLM()
    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=llm)
    monkeypatch.setattr(
        "smartapply.desktop.services._pipeline",
        lambda stop_requested=None: pipeline,
    )

    report = DesktopService().analyze_and_shortlist([job_id])

    assert report["shortlisted"] == 1
    assert report["errors"] == []
    assert llm.calls == []
    with session_scope() as session:
        repaired = session.get(Job, job_id)
        assert repaired is not None and repaired.analysis is not None
        assert repaired.filtered_at is not None
        assert repaired.ranked_at is not None
        assert repaired.score is not None and repaired.score.final_score is not None
        assert repaired.analyzed_at == analyzed_at
        assert repaired.analysis.id == analysis_id
        assert repaired.shortlisted_at is not None
        assert repaired.status == JobStatus.SHORTLISTED
