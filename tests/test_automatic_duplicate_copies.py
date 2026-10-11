"""Exact copies leave the review queue without losing workflows or source data."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.models import Application, DuplicateReviewEvent, Job, JobAnalysis, JobScore
from smartapply.database.repository import upsert_job
from smartapply.maintenance.duplicate_reviews import (
    DuplicateReviewSnapshotChanged,
    reconcile_pending_reviews,
)
from smartapply.offers import RawJob
from smartapply.pipeline.ingestor import Ingestor

DESCRIPTION = (
    "Vous développerez les modèles de machine learning de notre produit. "
    "Les missions comprennent la préparation des données, les expériences avec "
    "Python et PyTorch, la validation des modèles et leur déploiement. "
    "Vous travaillerez avec les ingénieurs et les utilisateurs sur les métriques "
    "de qualité et les performances du service."
)


def _row_state(row):
    return {
        column.name: value.replace(tzinfo=None) if isinstance(value, datetime) else value
        for column in row.__table__.columns
        for value in (getattr(row, column.name),)
    }


def _copy_pair():
    at = datetime.now(timezone.utc)
    with session_scope() as session:
        reference = upsert_job(
            session, external_id="linkedin:first-copy", title="Machine Learning Engineer",
            company="Acme", location="Paris", contract_type="CDI", description=DESCRIPTION,
            source="linkedin", application_url="https://linkedin.com/jobs/view/1234567890",
            published_date=at - timedelta(days=1),
        )
        session.add(JobAnalysis(job_id=reference.id, fit_score=80, main_tasks=["Machine learning"]))
        reference.status = "analyzed"
        reference.analyzed_at = at
        alias = upsert_job(
            session, external_id="linkedin:second-copy", title=reference.title,
            company="Acme", location="Paris", contract_type="CDI", description=DESCRIPTION,
            source="linkedin", application_url="https://linkedin.com/jobs/view/2345678901",
            published_date=at,
        )
        alias.possible_duplicate_of_id = reference.id
        alias.duplicate_review_status = "pending"
        alias.duplicate_match_type = "fuzzy"
        alias.duplicate_confidence = 0.99
        alias.filtered_at = at
        session.add(JobScore(job_id=alias.id, components={"filter_disposition": "pending_relevance"}))
        return reference.id, alias.id


def test_scored_copy_resolves_automatically_and_preserves_sent_history(isolated_db):
    reference_id, alias_id = _copy_pair()
    with session_scope() as session:
        reference = session.get(Job, reference_id)
        reference.status = "sent"
        app = Application(job_id=reference_id, status="sent", notes="Historique à conserver",
                          form_submitted_at=datetime.now(timezone.utc))
        session.add(app)
        session.flush()
        app_id = app.id
        before = _row_state(app)
        score = session.get(Job, alias_id).score
        score_before = _row_state(score)

    assert reconcile_pending_reviews() == 1
    assert reconcile_pending_reviews() == 0

    with session_scope() as session:
        alias = session.get(Job, alias_id)
        assert (alias.canonical_job_id, alias.status, alias.duplicate_review_status) == (
            reference_id, "archived", "confirmed")
        assert alias.description == DESCRIPTION
        app = session.get(Application, app_id)
        assert _row_state(app) == before
        assert _row_state(alias.score) == score_before
        event = session.scalar(select(DuplicateReviewEvent))
        assert event.reason == "strict_content_copy"
        assert event.before_state["duplicate_review_status"] == "pending"


def test_ingest_different_source_ids_confirms_exact_content_before_analysis(isolated_db):
    reference_id, _ = _copy_pair()
    raw = RawJob(
        external_id="linkedin:third-copy", title="Machine Learning Engineer", company="Acme",
        location="Paris", contract_type="CDI", description=DESCRIPTION, source="linkedin",
        application_url="https://linkedin.com/jobs/view/3456789012",
        published_date=datetime.now(timezone.utc),
    )
    report = Ingestor()._persist("linkedin", [raw])
    assert report.aliases_created == 1
    assert report.persisted == 0
    assert report.duplicate_review_ids == []
    with session_scope() as session:
        alias = session.scalar(select(Job).where(Job.external_id == raw.external_id))
        assert alias.canonical_job_id == reference_id
        assert alias.status == "archived"
        assert alias.score.components["filter_disposition"] in {"pending_relevance", "rejected"}
        assert alias.score is not None
        assert alias.analysis is None and alias.application is None


def test_ingest_prefers_exact_copy_over_an_earlier_fuzzy_variant(isolated_db):
    with session_scope() as session:
        upsert_job(
            session, external_id="linkedin:earlier-variant", title="Machine Learning Engineer",
            company="Acme", location="Paris", contract_type="CDI", source="linkedin",
            description=DESCRIPTION + " Vous encadrerez également dix collaborateurs.",
        )
    reference_id, _ = _copy_pair()
    raw = RawJob(
        external_id="linkedin:exact-new", title="Machine Learning Engineer", company="Acme",
        location="Paris", contract_type="CDI", description=DESCRIPTION, source="linkedin",
        published_date=datetime.now(timezone.utc),
    )
    assert Ingestor()._persist("linkedin", [raw]).aliases_created == 1
    with session_scope() as session:
        job = session.scalar(select(Job).where(Job.external_id == raw.external_id))
        assert job.canonical_job_id == reference_id


def test_ingest_resolves_old_machine_archive_and_keeps_new_active_copy(isolated_db):
    at = datetime.now(timezone.utc)
    with session_scope() as session:
        old = upsert_job(
            session, external_id="linkedin:old-archive", title="Machine Learning Engineer",
            company="Acme", location="Paris", contract_type="CDI", description=DESCRIPTION,
            source="linkedin", published_date=at - timedelta(days=1),
        )
        old.status, old.archived_at = "archived", at - timedelta(hours=1)
        session.add(JobScore(job_id=old.id, components={
            "filter_disposition": "rejected", "rejection_stage": "local_filter",
            "rejection_reasons": ["legacy_score_rejection"],
        }))
        old_id = old.id
    raw = RawJob(
        external_id="linkedin:new-active", title="Machine Learning Engineer", company="Acme",
        location="Paris", contract_type="CDI", description=DESCRIPTION, source="linkedin",
        published_date=at,
    )
    report = Ingestor()._persist("linkedin", [raw])
    assert report.persisted == 1
    assert report.inserted == 1
    assert report.duplicate_review_ids == []
    with session_scope() as session:
        new = session.get(Job, report.job_ids[0])
        assert (new.status, new.archived_at, new.canonical_job_id) == ("scraped", None, None)
        assert new.duplicate_review_status != "pending"
        assert session.get(Job, old_id).canonical_job_id == new.id


@pytest.mark.parametrize("difference", ["seniority", "description", "unknown_location", "date"])
def test_ingest_ambiguous_offer_keeps_human_review(isolated_db, difference):
    _copy_pair()
    raw = RawJob(
        external_id="linkedin:distinct", title="Machine Learning Engineer", company="Acme",
        location="Paris", contract_type="CDI", description=DESCRIPTION, source="linkedin",
        application_url="https://linkedin.com/jobs/view/4567890123",
        published_date=datetime.now(timezone.utc),
    )
    if difference == "seniority":
        raw.title = "Senior Machine Learning Engineer"
    elif difference == "description":
        raw.description += " La gestion d'une équipe de dix personnes est obligatoire."
    elif difference == "unknown_location":
        raw.location = None
    else:
        raw.published_date += timedelta(days=31)
    report = Ingestor()._persist("linkedin", [raw])
    assert report.aliases_created == 0
    assert report.persisted == 1
    with session_scope() as session:
        job = session.get(Job, report.job_ids[0])
        assert job.canonical_job_id is None
        assert job.status == "scraped"


def test_manual_score_choice_during_planning_cancels_confirmation(isolated_db, monkeypatch):
    _, alias_id = _copy_pair()
    from smartapply import concurrency

    real_operation = concurrency.job_operation

    @contextmanager
    def user_keeps_offer(job_ids, *, operation):
        with session_scope() as session:
            session.get(Job, alias_id).score.components = {
                "filter_disposition": "pending_relevance", "manual_keep": True,
            }
        with real_operation(job_ids, operation=operation):
            yield

    monkeypatch.setattr(concurrency, "job_operation", user_keeps_offer)
    with pytest.raises(DuplicateReviewSnapshotChanged, match="changé"):
        reconcile_pending_reviews()
    with session_scope() as session:
        job = session.get(Job, alias_id)
        assert job.duplicate_review_status == "pending"
        assert job.canonical_job_id is None
        assert session.scalar(select(DuplicateReviewEvent)) is None
