"""Two-state labels cover every offer without rewriting protected history."""

from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from smartapply.database import session_scope
from smartapply.database.models import Application, GeneratedDocument, Job, JobAnalysis, JobScore
from smartapply.filtering.hard_reject.types import (
    Evidence,
    GateDecision,
    GateStatus,
    HardRejectResult,
)
from smartapply.maintenance import filter_labels
from smartapply.pipeline.process.filter_state import MANUAL_KEEP_OVERRIDE_REASON


def _result(*, blocked: bool = False) -> HardRejectResult:
    if not blocked:
        return HardRejectResult(decisions=())
    return HardRejectResult(decisions=(GateDecision(
        gate="contract", status=GateStatus.BLOCKED, reason_codes=("internship",),
        evidence=(Evidence(rule_id="contract_fixture", source="title", quote="Stage"),),
    ),))


class _Rejector:
    def evaluate(self, job) -> HardRejectResult:
        return _result(blocked="Stage" in job.title)


def _offer(session, title="ML Engineer", *, components=None, scored=False) -> Job:
    offer = Job(
        external_id=f"fixture-{title}-{len(session.new)}", title=title,
        company="Fixture", description="Python and machine learning.",
        location="Paris", contract_type="CDI", source="manual",
    )
    session.add(offer)
    if scored:
        offer.score = JobScore(
            components=components, final_score=0.75, semantic_score=0.6,
            computed_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    session.flush()
    return offer


def _row(row) -> dict | None:
    if row is None:
        return None
    values = copy.deepcopy({column.name: getattr(row, column.name)
                            for column in row.__table__.columns})
    return {key: value.replace(tzinfo=timezone.utc).isoformat()
            if isinstance(value, datetime) else value for key, value in values.items()}


@pytest.mark.parametrize("components", [
    None, {}, [], "uncertain", {"filter_disposition": None},
    {"filter_disposition": "uncertain"}, {"filter_disposition": "relevant"},
    {"filter_disposition": []}, {"filter_disposition": {}},
])
def test_absent_obsolete_and_malformed_labels_need_completion(components) -> None:
    assert filter_labels.needs_filter_label(components)


@pytest.mark.parametrize("label", ["pending_relevance", "rejected"])
def test_valid_historical_label_remains_unchanged_without_current_proof(isolated_db, label) -> None:
    with session_scope() as session:
        job = _offer(session, components={"filter_disposition": label}, scored=True)
        before = _row(job.score)
        assert not filter_labels.persist_filter_label(
            session, job, _result(blocked=label != "rejected"), migrate=True,
        )
        assert _row(job.score) == before


def test_metadata_completion_preserves_history_and_independent_rejection(isolated_db) -> None:
    with session_scope() as session:
        components = {
            "filter_disposition": "uncertain", "reasons": ["old uncertain"],
            "rejection_stage": "analysis", "rejection_reasons": ["paid analysis"],
            "rejection_summary": "Existing analysis rejection",
            "semantic": {"value": 0.6}, "manual_shortlist": {"selected": False},
            "independent_audit": {"verified": True},
        }
        job = _offer(session, "Stage AI", components=components, scored=True)
        now = datetime.now(timezone.utc)
        job.status = "sent"
        job.filtered_at = job.ranked_at = job.shortlisted_at = job.analyzed_at = now
        job.archived_at = now
        job.duplicate_review_status = "pending"
        job.possible_duplicate_of_id = _offer(session, "Reference").id
        job.analysis = JobAnalysis(fit_score=8.5, raw_response={"paid": True})
        job.application = Application(status="sent", form_submitted_at=now, notes="Keep")
        job.application.documents = [GeneratedDocument(doc_type="cv", content="Preserve")]
        session.flush()
        before = {"job": _row(job), "score": _row(job.score), "analysis": _row(job.analysis),
                  "application": _row(job.application), "document": _row(job.application.documents[0])}
        assert not filter_labels.persist_filter_label(session, job, _result(blocked=True), migrate=True)
        session.flush()
        assert _row(job) == before["job"]
        assert _row(job.analysis) == before["analysis"]
        assert _row(job.application) == before["application"]
        assert _row(job.application.documents[0]) == before["document"]
        assert {key: value for key, value in _row(job.score).items() if key != "components"} == {
            key: value for key, value in before["score"].items() if key != "components"
        }
        assert _row(job.score) == before["score"]


def test_missing_score_gets_metadata_only_and_attached_relationship(isolated_db) -> None:
    with session_scope() as session:
        job = _offer(session, "Stage AI")
        before = _row(job)
        assert filter_labels.persist_filter_label(session, job, _result(blocked=True))
        assert job.score is not None
        assert job.score.job is job
        session.flush()
        assert job.score.job_id == job.id
        assert _row(job) == before
        assert job.score.components["filter_disposition"] == "rejected"
        assert all(getattr(job.score, column.name) is None for column in JobScore.__table__.columns
                   if column.name.endswith("_score"))


@pytest.mark.parametrize("override", [
    {"reasons": [MANUAL_KEEP_OVERRIDE_REASON]},
    {"manual_keep": True},
    {"manual_rescue": True, "rescued_at": "2025-01-01"},
    {"manual_rescue": {"schema_version": 3, "rescued_at": "2025-01-01"}},
])
def test_explicit_user_keep_survives_completion(isolated_db, override) -> None:
    with session_scope() as session:
        job = _offer(session, "Stage AI", components=override, scored=True)
        filter_labels.persist_filter_label(session, job, _result(blocked=True), migrate=True)
        assert job.score.components["filter_disposition"] == "pending_relevance"
        assert job.score.components["hard_reject"]["rejected"] is True
        assert MANUAL_KEEP_OVERRIDE_REASON in job.score.components["reasons"]


@pytest.mark.parametrize("metadata", [
    {"manual_keep": False}, {"manual_keep": "true"}, {"manual_rescue": {}},
    {"manual_rescue": {"schema_version": 3}}, {"manual_shortlist": {"selected": False}},
])
def test_audit_namespace_without_explicit_keep_does_not_override_filter(isolated_db, metadata) -> None:
    with session_scope() as session:
        job = _offer(session, "Stage AI", components=metadata, scored=True)
        filter_labels.persist_filter_label(session, job, _result(blocked=True))
        assert job.score.components["filter_disposition"] == "rejected"


def test_completion_includes_aliases_archives_missing_scores_and_is_idempotent(isolated_db, monkeypatch):
    monkeypatch.setattr(filter_labels, "build_rejector", _Rejector)
    with session_scope() as session:
        root = _offer(session)
        alias = _offer(session, "Stage alias")
        alias.status = "archived"
        alias.archived_at = datetime.now(timezone.utc)
        alias.canonical_job_id = root.id
        alias.duplicate_review_status = "confirmed"
        old = _offer(session, "Old result", components={"filter_disposition": "uncertain"}, scored=True)
        preserved = _offer(session, "Stage kept old", components={"filter_disposition": "pending_relevance"}, scored=True)
        ids = (root.id, alias.id, old.id, preserved.id)
        before = {job.id: _row(job) for job in (root, alias, old, preserved)}
        old_valid = _row(preserved.score)
    assert filter_labels.complete_missing_filter_labels() == 2
    assert filter_labels.complete_missing_filter_labels() == 0
    with session_scope() as session:
        jobs = [session.get(Job, job_id) for job_id in ids]
        assert {job.id: _row(job) for job in jobs} == before
        assert [job.score.components["filter_disposition"] if job.score else None for job in jobs] == [
            "pending_relevance", None, "pending_relevance", "pending_relevance"
        ]
        assert _row(jobs[-1].score) == old_valid


@pytest.mark.parametrize("components", [
    [], "obsolete", None, {"filter_disposition": []}, {"filter_disposition": {}},
])
def test_completion_query_finds_malformed_legacy_json(isolated_db, monkeypatch, components):
    monkeypatch.setattr(filter_labels, "build_rejector", _Rejector)
    with session_scope() as session:
        job_id = _offer(session, components=components, scored=True).id
    assert filter_labels.complete_missing_filter_labels() == 1
    with session_scope() as session:
        assert session.get(Job, job_id).score.components["filter_disposition"] == "pending_relevance"


@pytest.mark.parametrize("change", ["source", "numeric_score", "new_score", "manual_keep", "delete"])
def test_evaluation_change_rolls_back_entire_batch(isolated_db, monkeypatch, change):
    with session_scope() as session:
        first = _offer(session, "First")
        target = _offer(session, "Target", components={}, scored=change != "new_score")
        first_id, target_id = first.id, target.id

    class MutatingRejector(_Rejector):
        changed = False

        def evaluate(self, job):
            if not self.changed:
                self.changed = True
                with session_scope() as session:
                    target = session.get(Job, target_id)
                    if change == "source":
                        target.description = "Changed source"
                    elif change == "numeric_score":
                        target.score.final_score = 0.99
                    elif change == "new_score":
                        target.score = JobScore(components={"manual_keep": True})
                    elif change == "manual_keep":
                        target.score.components = {"manual_keep": True}
                    else:
                        session.delete(target)
            return super().evaluate(job)

    monkeypatch.setattr(filter_labels, "build_rejector", MutatingRejector)
    with pytest.raises(filter_labels.FilterLabelSnapshotChanged):
        filter_labels.complete_missing_filter_labels()
    with session_scope() as session:
        assert session.get(Job, first_id).score is None
        target = session.get(Job, target_id)
        assert target is None or filter_labels.needs_filter_label(target.score.components)


def test_cancelled_evaluation_does_not_commit_partial_labels(isolated_db, monkeypatch):
    with session_scope() as session:
        ids = [_offer(session, title).id for title in ("First", "Second")]
    stopped = False

    class CancellingRejector(_Rejector):
        def evaluate(self, job):
            nonlocal stopped
            stopped = True
            return super().evaluate(job)

    monkeypatch.setattr(filter_labels, "build_rejector", CancellingRejector)
    assert filter_labels.complete_missing_filter_labels(stop_requested=lambda: stopped) == 0
    with session_scope() as session:
        assert all(session.get(Job, job_id).score is None for job_id in ids)


def test_default_builder_uses_canonical_contract_rules_without_changing_lifecycle(isolated_db):
    with session_scope() as session:
        internship = _offer(session, "Stage Data Scientist")
        internship.contract_type = "stage"
        survivor = _offer(session, "Machine Learning Engineer")
        ids = internship.id, survivor.id
    assert filter_labels.complete_missing_filter_labels() == 2
    with session_scope() as session:
        internship, survivor = (session.get(Job, job_id) for job_id in ids)
        assert internship.score.components["filter_disposition"] == "rejected"
        assert survivor.score.components["filter_disposition"] == "pending_relevance"
        assert all(job.filtered_at is None and job.archived_at is None
                   for job in (internship, survivor))
