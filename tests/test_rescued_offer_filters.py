"""Restoring an offer is a durable, audited choice to keep it active."""

from __future__ import annotations

from copy import deepcopy

import pytest

from smartapply.database import session_scope
from smartapply.database.models import Job, JobScore, JobStatus
from smartapply.database.repository import mark_archived, rescue_archived_job
from smartapply.filtering.hard_reject.types import (
    Evidence,
    GateDecision,
    GateStatus,
    HardRejectDisposition,
    HardRejectResult,
)
from smartapply.llm import MockLLMProvider
from smartapply.llm.schemas import JobScopeScreening
from smartapply.pipeline import Pipeline
from smartapply.pipeline.process.extraction_screening import apply_mission_screen
from smartapply.pipeline.process.filter_state import (
    MANUAL_KEEP_OVERRIDE_REASON,
    current_hard_reject_disposition,
)
from smartapply.pipeline.process.local_filtering import LocalFilterMixin


def _blocked_result() -> HardRejectResult:
    return HardRejectResult(
        decisions=(
            GateDecision(
                gate="experience",
                status=GateStatus.BLOCKED,
                reason_codes=("too_senior",),
                evidence=(
                    Evidence(
                        rule_id="fixture",
                        source="description",
                        quote="8 ans requis",
                    ),
                ),
            ),
        )
    )


def _rescued_job() -> tuple[int, dict]:
    rejection = _blocked_result()
    with session_scope() as session:
        job = Job(
            external_id="rescued:fixture",
            title="Senior Data Scientist",
            source_company="Example",
            description="8 ans requis. Construire des modèles Python.",
            source="test_search",
            score=JobScore(
                components={
                    "filter_disposition": "rejected",
                    "hard_reject": rejection.audit_payload(),
                    "rejection_stage": "local_filter",
                    "rejection_reasons": ["too_senior"],
                    "rejection_summary": "Experience requirement exceeds the profile",
                }
            ),
        )
        session.add(job)
        session.flush()
        mark_archived(session, job.id, origin="automatic_filter")
        rescue_archived_job(session, job.id, justification="Je veux candidater")
        return job.id, deepcopy(job.score.components["manual_rescue"])


def _outside_screen():
    return JobScopeScreening(
        mission_screen={
            "mission_relation": "clearly_outside",
            "mission_evidence": "sufficient",
        }
    ).mission_screen


@pytest.mark.parametrize("state_only", [False, True])
def test_restoration_keeps_offer_and_rejection_evidence_on_filter_retries(
    isolated_db,
    state_only,
):
    job_id, marker = _rescued_job()
    persist = (
        LocalFilterMixin._persist_filter_state_only
        if state_only
        else LocalFilterMixin._persist_filter_result
    )
    for _ in range(2):
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert persist(session, job, _blocked_result()) is True
            assert job.archived_at is None and job.status != JobStatus.ARCHIVED
            components = job.score.components
            assert components["manual_rescue"] == marker
            assert marker["previous_rejection"]["reasons"] == ["too_senior"]
            assert marker["previous_rejection"]["archive_origin"] == "automatic_filter"
            assert marker["previous_hard_reject"] == _blocked_result().audit_payload()
            assert components["hard_reject"] == _blocked_result().audit_payload()
            assert MANUAL_KEEP_OVERRIDE_REASON in components["reasons"]
            assert current_hard_reject_disposition(components) is (
                HardRejectDisposition.PENDING_RELEVANCE
            )


def test_analysis_preparation_retains_restoration_after_metadata_repair(
    isolated_db,
    monkeypatch,
):
    job_id, marker = _rescued_job()
    pipeline = Pipeline(llm=MockLLMProvider())
    monkeypatch.setattr(pipeline.filter, "evaluate", lambda job: _blocked_result())
    for _ in range(2):
        # A later filter-rules upgrade or missing metadata requests a repair.
        with session_scope() as session:
            job = session.get(Job, job_id)
            job.filtered_at = None
        report = pipeline.prepare_analysis(job_ids=[job_id])
        assert report.kept_ids == [job_id] and report.rejected_ids == []
        with session_scope() as session:
            job = session.get(Job, job_id)
            assert job.archived_at is None
            assert job.score.components["manual_rescue"] == marker
            assert job.score.components["hard_reject"]["rejected"] is True


def test_restoration_survives_mission_screen_and_later_filter_repair(isolated_db):
    job_id, marker = _rescued_job()
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert apply_mission_screen(session, job, _outside_screen()) is False
        assert job.score.components["extraction_screen"] == {
            "schema_version": 1,
            "mission_relation": "clearly_outside",
            "mission_evidence": "sufficient",
            "decision": "evaluate_manual_override",
        }
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert LocalFilterMixin._persist_filter_result(session, job, _blocked_result()) is True
        assert job.archived_at is None
        assert job.score.components["manual_rescue"] == marker
        assert job.score.components["extraction_screen"]["decision"] == "evaluate_manual_override"
        assert apply_mission_screen(session, job, _outside_screen()) is False


@pytest.mark.parametrize(
    "marker",
    [
        None,
        True,
        {"schema_version": 2, "rescued_at": "2026-10-08T10:00:00+00:00"},
        {"schema_version": 3},
        {"schema_version": 3, "rescued_at": "  "},
        {"schema_version": 3, "rescued_at": 42},
    ],
)
@pytest.mark.parametrize("gate", ["local_filter", "filter_state", "mission_screen"])
def test_malformed_restoration_marker_cannot_bypass_automatic_rejection(
    isolated_db,
    marker,
    gate,
):
    with session_scope() as session:
        job = Job(
            external_id="invalid:rescue",
            title="Chargé de comptes",
            source_company="Example",
            description="Vendre des abonnements.",
            source="test_search",
            score=JobScore(components={"manual_rescue": marker}),
        )
        session.add(job)
        session.flush()
        if gate == "mission_screen":
            assert apply_mission_screen(session, job, _outside_screen()) is True
            assert job.score.components["extraction_screen"]["decision"] == "archived"
        else:
            persist = (
                LocalFilterMixin._persist_filter_state_only
                if gate == "filter_state"
                else LocalFilterMixin._persist_filter_result
            )
            assert persist(session, job, _blocked_result()) is False
            assert current_hard_reject_disposition(job.score.components) is (
                HardRejectDisposition.REJECTED
            )
        if gate != "filter_state":
            assert job.archived_at is not None and job.status == JobStatus.ARCHIVED
