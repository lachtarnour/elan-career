"""Automatic duplicate relations preserve offers, lifecycle and applications."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.models import Application, DuplicateReviewEvent, Job, JobScore
from smartapply.database.repository import upsert_job
from smartapply.dedup.review_decisions import can_auto_link_offer, plan_pending_review_decisions
from smartapply.maintenance.duplicate_reviews import (
    DuplicateReviewSnapshotChanged,
    reconcile_pending_reviews,
    restore_automatic_review,
)


def _pair(*, url="https://www.linkedin.com/jobs/view/4464028174"):
    with session_scope() as session:
        reference = upsert_job(session, external_id="serpapi:first", title="Data Scientist",
                               company="Acme", location="Paris", description="Python et ML",
                               source="serpapi", application_url=url, contract_type="CDI")
        alias = upsert_job(session, external_id="linkedin:second", title="Data Scientist",
                           company="Acme", location="Paris", description="Python et ML",
                           source="linkedin", application_url=url, contract_type="CDI")
        alias.possible_duplicate_of_id = reference.id
        alias.duplicate_review_status = "pending"
        alias.duplicate_match_type = "fuzzy"
        alias.duplicate_confidence = 0.99
        return reference.id, alias.id


def test_automatic_confirmation_keeps_sent_dossier_on_reference(isolated_db):
    reference_id, alias_id = _pair()
    with session_scope() as session:
        reference = session.get(Job, reference_id)
        reference.status = "sent"
        app = Application(job_id=reference_id, status="sent",
                          form_submitted_at=datetime.now(timezone.utc), notes="Conserver")
        session.add(app)
        session.flush()
        application_id = app.id
        updated_at = app.updated_at
    assert reconcile_pending_reviews() == 1
    assert reconcile_pending_reviews() == 0
    with session_scope() as session:
        alias = session.get(Job, alias_id)
        assert alias.canonical_job_id == reference_id
        assert alias.status == "archived"
        app = session.get(Application, application_id)
        assert (app.job_id, app.status, app.notes, app.updated_at) == (
            reference_id, "sent", "Conserver", updated_at.replace(tzinfo=None))
        events = list(session.scalars(select(DuplicateReviewEvent)))
        assert len(events) == 1
        assert events[0].action == "confirm"
        assert events[0].before_state["possible_duplicate_of_id"] == reference_id


def test_release_changes_relation_only_and_is_reversible(isolated_db):
    reference_id, alias_id = _pair(url="https://acme.test/jobs/shared")
    with session_scope() as session:
        alias = session.get(Job, alias_id)
        alias.location = "Lyon"
        alias.status = "archived"
        alias.archived_at = datetime.now(timezone.utc)
        alias.shortlisted_at = datetime.now(timezone.utc)
        alias.shortlist_origin = "manual"
        session.add(Application(job_id=alias_id, status="archived", notes="Historique"))
        session.add(JobScore(job_id=alias_id, components={"filter_disposition": "pending_relevance"}))
        session.flush()
        before = {column.name: getattr(alias, column.name) for column in Job.__table__.columns}
    assert reconcile_pending_reviews() == 1
    with session_scope() as session:
        alias = session.get(Job, alias_id)
        for field, value in before.items():
            if field in {"possible_duplicate_of_id", "duplicate_review_status",
                         "duplicate_match_type", "duplicate_confidence"}:
                continue
            if isinstance(value, datetime):
                value = value.replace(tzinfo=None)
            assert getattr(alias, field) == value
        assert alias.status == "archived"
        assert alias.possible_duplicate_of_id is None
        assert alias.application.notes == "Historique"
        assert alias.score is not None
        event = session.scalar(select(DuplicateReviewEvent))
        assert event.reason == "incompatible_location"
        assert restore_automatic_review(session, event.id)
        assert alias.possible_duplicate_of_id == reference_id
        assert alias.duplicate_review_status == "pending"


@pytest.mark.parametrize("field,value", [
    ("title", "Senior Data Scientist"), ("location", "Lyon"),
    ("contract_type", "CDD"), ("remote_policy", "remote"),
])
def test_source_identity_with_metadata_conflict_needs_review(isolated_db, field, value):
    reference_id, alias_id = _pair()
    with session_scope() as session:
        reference, alias = session.get(Job, reference_id), session.get(Job, alias_id)
        reference.remote_policy = "onsite"
        setattr(alias, field, value)
        assert not can_auto_link_offer(alias, reference, [reference, alias])
    assert reconcile_pending_reviews() == 0


def test_used_alias_is_never_automatically_confirmed(isolated_db):
    _, alias_id = _pair()
    with session_scope() as session:
        session.add(Application(job_id=alias_id, status="analyzed", notes="Dossier"))
    assert reconcile_pending_reviews() == 0


def test_score_or_manual_choice_protects_legacy_alias(isolated_db):
    _, alias_id = _pair()
    with session_scope() as session:
        session.add(JobScore(job_id=alias_id, components={"manual_keep": True}))
    assert reconcile_pending_reviews() == 0


def _add_filter_metadata(session, alias_id):
    from smartapply.filtering.factory import build_rejector
    from smartapply.maintenance.filter_labels import persist_filter_label

    alias = session.get(Job, alias_id)
    persist_filter_label(session, alias, build_rejector().evaluate(alias), migrate=True)
    session.flush()
    return alias.score


def test_source_identity_confirms_alias_with_only_filter_label(isolated_db):
    reference_id, alias_id = _pair()
    with session_scope() as session:
        score = _add_filter_metadata(session, alias_id)
        score_id, components, computed_at = score.id, score.components, score.computed_at

    assert reconcile_pending_reviews() == 1
    with session_scope() as session:
        alias = session.get(Job, alias_id)
        assert alias.canonical_job_id == reference_id
        assert alias.duplicate_review_status == "confirmed"
        assert alias.score.id == score_id
        assert alias.score.components == components
        assert alias.score.computed_at == computed_at.replace(tzinfo=None)
        event = session.scalar(select(DuplicateReviewEvent))
        assert event.reason == "source_identity"


@pytest.mark.parametrize("key,value", [
    ("other_audit", 0),
    ("manual_keep", True),
    ("manual_rescue", {"schema_version": 3, "rescued_at": "2026-10-06T12:00:00Z"}),
    ("rejection_stage", "manual"),
    ("other_audit", {"preserve": True}),
])
def test_source_identity_keeps_alias_with_non_filter_history(isolated_db, key, value):
    _, alias_id = _pair()
    with session_scope() as session:
        score = _add_filter_metadata(session, alias_id)
        score.components = {**score.components, key: value}
    assert reconcile_pending_reviews() == 0


def test_source_identity_keeps_alias_with_stale_filter_proof(isolated_db):
    _, alias_id = _pair()
    with session_scope() as session:
        score = _add_filter_metadata(session, alias_id)
        score.components = {
            **score.components,
            "hard_reject": {**score.components["hard_reject"], "ruleset_version": "obsolete"},
        }
    assert reconcile_pending_reviews() == 0


def test_releasing_bad_edge_does_not_invalidate_remaining_chain(isolated_db):
    first_id, middle_id = _pair(url="https://acme.test/jobs/shared")
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        first, middle = session.get(Job, first_id), session.get(Job, middle_id)
        first.published_date = at
        middle.published_date = at + timedelta(days=20)
        middle.location = "Lyon"
        later = upsert_job(session, external_id="linkedin:third", title=middle.title,
                           company=middle.source_company, description=middle.description,
                           location="Lyon", source="linkedin")
        later.published_date = at + timedelta(days=40)
        later.possible_duplicate_of_id = middle_id
        later.duplicate_review_status = "pending"
        decisions = plan_pending_review_decisions([first, middle, later])
        assert [(item.job_id, item.reason) for item in decisions] == [
            (middle_id, "incompatible_location")]
        assert middle.possible_duplicate_of_id == first_id
        assert later.possible_duplicate_of_id == middle_id


def test_undo_release_refuses_a_new_dossier(isolated_db):
    _, alias_id = _pair(url="https://acme.test/jobs/shared")
    with session_scope() as session:
        session.get(Job, alias_id).location = "Lyon"
    assert reconcile_pending_reviews() == 1
    with session_scope() as session:
        event = session.scalar(select(DuplicateReviewEvent))
        session.add(Application(job_id=alias_id, status="analyzed"))
        session.flush()
        with pytest.raises(DuplicateReviewSnapshotChanged, match="historique"):
            restore_automatic_review(session, event.id)


def test_full_group_time_window_cannot_be_bridged(isolated_db):
    reference_id, alias_id = _pair(url="https://acme.test/jobs/shared")
    at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with session_scope() as session:
        reference, alias = session.get(Job, reference_id), session.get(Job, alias_id)
        reference.published_date = at
        alias.published_date = at + timedelta(days=20)
        later = upsert_job(session, external_id="linkedin:third", title=alias.title,
                           company=alias.source_company, description=alias.description,
                           location="Paris", source="linkedin")
        later.published_date = at + timedelta(days=40)
        later.possible_duplicate_of_id = alias_id
        later.duplicate_review_status = "pending"
        decisions = plan_pending_review_decisions([reference, alias, later])
        assert [(item.job_id, item.reason) for item in decisions] == [
            (later.id, "outside_30_day_window")]


def test_undo_refuses_changed_relation(isolated_db):
    _, alias_id = _pair()
    assert reconcile_pending_reviews() == 1
    with session_scope() as session:
        event = session.scalar(select(DuplicateReviewEvent))
        session.get(Job, alias_id).status = "analyzed"
        with pytest.raises(DuplicateReviewSnapshotChanged):
            restore_automatic_review(session, event.id)


def test_undo_refuses_changed_reference_identity(isolated_db):
    reference_id, _ = _pair()
    assert reconcile_pending_reviews() == 1
    with session_scope() as session:
        event = session.scalar(select(DuplicateReviewEvent))
        session.get(Job, reference_id).application_url = "https://linkedin.com/jobs/view/9999999999"
        with pytest.raises(DuplicateReviewSnapshotChanged, match="historique"):
            restore_automatic_review(session, event.id)


@pytest.mark.parametrize("conflict", ["location", "date"])
def test_ingest_refreshes_new_link_on_existing_row_before_confirming(isolated_db, monkeypatch, conflict):
    from smartapply.offers import RawJob
    from smartapply.pipeline import ingestor as module

    at = datetime.now(timezone.utc)
    url = "https://linkedin.com/jobs/view/4464028174"
    with session_scope() as session:
        reference = upsert_job(session, external_id="serpapi:reference", title="Data Scientist",
                               company="Acme", location="Paris", description="Python et ML",
                               source="serpapi", application_url=url, published_date=at)
        existing = upsert_job(session, external_id="serpapi:existing", title="Data Scientist",
                              company="Acme", location="Lyon" if conflict == "location" else "Paris",
                              description="Python et ML", source="serpapi",
                              application_url="https://linkedin.com/jobs/view/8888888888",
                              published_date=at - timedelta(days=40) if conflict == "date" else at)
        reference_id, existing_id = reference.id, existing.id
    real_operation = module.job_operation
    changed = False

    @contextmanager
    def incoming_link(job_ids, *, operation):
        nonlocal changed
        if not changed:
            changed = True
            with session_scope() as session:
                existing = session.get(Job, existing_id)
                existing.canonical_job_id = reference_id
                existing.duplicate_review_status = "confirmed"
        with real_operation(job_ids, operation=operation):
            yield

    monkeypatch.setattr(module, "job_operation", incoming_link)
    raw = RawJob(external_id="linkedin:new", title="Data Scientist", company="Acme",
                 location="Paris", description="Python et ML", source="linkedin",
                 application_url=url, published_date=at)
    report = module.Ingestor()._persist("linkedin", [raw])
    assert report.aliases_created == 0
    assert report.persisted == 1
    with session_scope() as session:
        job = session.get(Job, report.job_ids[0])
        assert job.canonical_job_id is None
        assert job.status == "scraped"


def test_changed_housekeeping_does_not_lose_fetched_rows(isolated_db, monkeypatch):
    from smartapply.maintenance import duplicate_reviews
    from smartapply.offers import RawJob
    from smartapply.pipeline.ingestor import Ingestor

    def busy():
        raise DuplicateReviewSnapshotChanged("Concurrent change")
    monkeypatch.setattr(duplicate_reviews, "reconcile_pending_reviews", busy)
    raw = RawJob(external_id="linkedin:already-fetched", title="Data Scientist",
                 company="Acme", description="Python et ML", source="linkedin")
    assert Ingestor()._persist("linkedin", [raw]).persisted == 1
