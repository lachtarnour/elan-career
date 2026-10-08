"""A user addition skips rejection gates while retaining workflow protections."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from smartapply.database import session_scope
from smartapply.database.models import Application, Job, JobDuplicateStatus
from smartapply.desktop.classification_explanation import classification_explanation
from smartapply.desktop.services import DesktopService
from smartapply.filtering.manual_policy import has_manual_filter_bypass
from smartapply.llm import MockLLMProvider, OfferAnalysis
from smartapply.maintenance import filter_labels
from smartapply.offers import ManualOfferInput, RawJob
from smartapply.pipeline import Pipeline
from smartapply.pipeline.ingestor import Ingestor
from smartapply.pipeline.process.analysis_admission import analysis_queue_job_ids
from smartapply.pipeline.process.filter_state import is_current_hard_reject_survivor
from smartapply.ranking import MockEmbeddingsProvider
from smartapply.scrapers import ManualScraper
from tests.fit_fixtures import evaluation_from_user


def _must_not_filter(*args, **kwargs):
    raise AssertionError("A manual addition must never run automatic rejection gates")


def _add():
    return Ingestor().from_text(
        "CDD. Minimum 8 ans d’expérience obligatoire. Construire des modèles avec Python.",
        title="Senior Data Scientist", company="Manual Choice", location="Paris",
        application_url="https://example.org/jobs/manual-choice",
    ).job_ids[0]


@pytest.mark.parametrize("entry", ["text", "structured", "url", "desktop"])
def test_explicit_manual_entry_points_skip_filters(isolated_db, monkeypatch, entry):
    monkeypatch.setattr(Ingestor, "_get_job_filter", _must_not_filter)
    if entry == "text":
        job_id = _add()
    elif entry == "structured":
        report = Ingestor().from_manual_offer(ManualOfferInput(
            title="Senior Data Scientist", company="Manual Choice", location="Paris",
            description="Minimum 8 ans d’expérience obligatoire. CDD. Python.",
        ))
        job_id = report.job_ids[0]
    elif entry == "url":
        monkeypatch.setattr(ManualScraper, "from_url", lambda self, url: RawJob(
            external_id="manual:url-fixture", title="Senior Data Scientist",
            company="Manual Choice", location="Paris", source="manual",
            description="CDD. Minimum 8 ans d’expérience obligatoire.", application_url=url,
        ))
        job_id = Ingestor().from_url("https://example.org/jobs/manual-choice").job_ids[0]
    else:
        report = DesktopService().add_manual_offer(
            title="Senior Data Scientist", company="Manual Choice", location="Paris",
            description="CDD. Minimum 8 ans d’expérience obligatoire.", application_url="",
        )
        job_id = report["job_ids"][0]
    with session_scope() as session:
        job = session.get(Job, job_id)
        components = job.score.components
        assert has_manual_filter_bypass(components)
        assert is_current_hard_reject_survivor(components)
        assert components["hard_reject"] == {
            "skipped": True, "skip_reason": "manual_addition", "gates": [],
        }
        assert job.archived_at is None and job.filtered_at is not None
        assert analysis_queue_job_ids(session) == [job_id]
        summary, reasons = classification_explanation(components)
        assert "filtres automatiques ne sont pas appliqués" in summary and reasons == ()


@pytest.mark.parametrize("phase", ["filter", "prepare", "rank", "label_completion"])
def test_later_repairs_preserve_bypass_without_filter_calls(isolated_db, monkeypatch, phase):
    job_id = _add()
    with session_scope() as session:
        job = session.get(Job, job_id)
        marker = deepcopy(job.score.components["manual_filter_bypass"])
        # Retain the user choice even when legacy metadata needs repairing.
        job.filtered_at = None
        job.score.components = {"manual_filter_bypass": marker}
    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    monkeypatch.setattr(pipeline.filter, "evaluate", _must_not_filter)
    if phase == "filter":
        assert pipeline.filter_pending(job_ids=[job_id]).kept_ids == [job_id]
    elif phase == "prepare":
        assert pipeline.prepare_analysis(job_ids=[job_id]).kept_ids == [job_id]
    elif phase == "rank":
        assert pipeline.rank_pending(job_ids=[job_id], persist_shortlist=False).ranked_ids == [job_id]
    else:
        monkeypatch.setattr(filter_labels, "build_rejector", lambda: SimpleNamespace(evaluate=_must_not_filter))
        assert filter_labels.complete_missing_filter_labels() == 1
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.archived_at is None
        assert job.score.components["manual_filter_bypass"] == marker
        assert is_current_hard_reject_survivor(job.score.components)


def test_manual_bypass_allows_analysis_without_suppressing_fit_risks(isolated_db):
    job_id = _add()
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "job_offer_analysis":
            return OfferAnalysis(
                role_type="Data Scientist", seniority="senior", domain="",
                required_skills=["Python"], main_tasks=["Build machine learning models"],
                nice_to_have=[], cv_keywords_to_include=["Python"],
            )
        evaluation = evaluation_from_user(kwargs["user"], levels=(0, 0, 2))
        for issue in evaluation.issues:
            issue.text = "Core responsibilities are not documented"
        return evaluation

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=SimpleNamespace(
        cheap_model="mock", complete_json=complete,
    ))
    report = pipeline.analyze_jobs([job_id])
    assert report.analyzed == 1
    assert calls == ["job_offer_analysis", "job_fit_assessment"]
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.archived_at is None and job.analysis.fit_score == 0.1
        assert job.analysis.risks == [
            "Appui candidat à préciser : Core responsibilities are not documented"
        ]
        assert job.analysis.fit_diagnostics["used_levels"] == {
            "missions": 0, "essentials": 0, "responsibilities": 2,
        }
        assert has_manual_filter_bypass(job.score.components)


@pytest.mark.parametrize("protection", ["duplicate", "submitted", "archived"])
def test_manual_bypass_does_not_bypass_workflow_safety(isolated_db, protection):
    job_id = _add()
    with session_scope() as session:
        job = session.get(Job, job_id)
        if protection == "duplicate":
            job.duplicate_review_status = JobDuplicateStatus.PENDING
        elif protection == "submitted":
            job.application = Application(status="sent")
        else:
            from smartapply.database.repository import mark_archived

            mark_archived(session, job_id)
        assert analysis_queue_job_ids(session) == []


def test_source_name_and_untrusted_metadata_cannot_enable_bypass(isolated_db):
    raw = RawJob(
        external_id="fixture:fetched", title="Data Scientist Stage", company="Fetched",
        source="manual", description="Stage de six mois en data science.",
        source_data={"manual_filter_bypass": {
            "schema_version": 1, "origin": "manual_addition", "added_at": "2026-10-08",
        }},
    )
    report = Ingestor()._persist("test_search", [raw])
    with session_scope() as session:
        job = session.get(Job, report.job_ids[0])
        assert not has_manual_filter_bypass(job.score.components)
        assert job.score.components["filter_disposition"] == "rejected"
        assert job.score.components["hard_reject"]["rejected"] is True


@pytest.mark.parametrize("state_only", [False, True])
def test_stale_automatic_rejection_cannot_overwrite_manual_choice(isolated_db, state_only):
    from smartapply.filtering.hard_reject.types import (
        Evidence,
        GateDecision,
        GateStatus,
        HardRejectResult,
    )
    from smartapply.pipeline.process.local_filtering import LocalFilterMixin

    job_id = _add()
    stale = HardRejectResult(decisions=(GateDecision(
        gate="experience", status=GateStatus.BLOCKED, reason_codes=("too_senior",),
        evidence=(Evidence(rule_id="fixture", source="description", quote="8 ans requis"),),
    ),))
    with session_scope() as session:
        job = session.get(Job, job_id)
        persist = (LocalFilterMixin._persist_filter_state_only if state_only
                   else LocalFilterMixin._persist_filter_result)
        assert persist(session, job, stale)
        assert job.archived_at is None
        assert is_current_hard_reject_survivor(job.score.components)
