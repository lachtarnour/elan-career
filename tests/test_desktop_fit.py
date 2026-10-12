"""Ranking and presentation keep technical scores and advisory metadata independent."""

import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import event

from smartapply.database import Job, JobStatus, ShortlistOrigin, session_scope
from smartapply.database.fit_ranking import current_policy_fingerprint
from smartapply.database.repository import (
    mark_analyzed,
    mark_filtered,
    set_analysis,
    set_score,
    set_shortlisted,
    upsert_job,
)
from smartapply.desktop.bridge import _enrich_rows
from smartapply.desktop.services import DesktopService
from smartapply.filtering.hard_reject import HardRejectResult


def _diagnostics(*, warnings=None):
    return {
        "policy_fingerprint": current_policy_fingerprint(),
        "used_decisions": {
            "missions": "strong_fit",
            "essentials": "very_low_support",
            "responsibilities": "strong_fit",
        },
        "used_levels": {"missions": 4, "essentials": 0, "responsibilities": 4},
        "reliability": {
            "offer_information": "Annonce suffisamment documentée.",
            "candidate_information": "Qualification non documentée.",
            "traceability": "Références à vérifier.",
            "business_uncertainties": ["Habilitation à confirmer."],
        },
        "diagnostic_checks": {"warnings": warnings or []},
    }


def _offer(session, external_id, score, diagnostics, *, age=0):
    job = upsert_job(
        session,
        external_id=external_id,
        source="manual",
        company="Example",
        title="Engineer",
        description="Build tools",
        location="Paris",
    )
    session.flush()
    mark_filtered(session, job.id)
    set_score(
        session,
        job.id,
        components={
            "filter_disposition": "pending_relevance",
            "hard_reject": HardRejectResult(decisions=()).audit_payload(),
        },
    )
    analysis = set_analysis(session, job.id, fit_score=score, fit_diagnostics=diagnostics)
    mark_analyzed(session, job.id)
    job.analyzed_at = datetime(2026, 10, 9, 10, tzinfo=timezone.utc) - timedelta(minutes=age)
    analysis.created_at = job.analyzed_at
    session.flush()
    session.expire(job, ["analysis", "score"])
    return job.id


def test_numeric_fit_sort_precedes_pagination_regardless_of_policy(isolated_db):
    with session_scope() as session:
        unknown = _offer(session, "unknown", 0.90, _diagnostics())
        incompatible = _offer(
            session, "incompatible", 0.99, _diagnostics()
        )
        compatible = _offer(session, "compatible", 0.25, _diagnostics(warnings=["bad quote"]))
        historical = _offer(session, "historical", 0.99, None, age=1)
        other_policy = _offer(session, "other-policy", 0.01, {"policy_fingerprint": "other"}, age=2)
    service = DesktopService()
    rows = service.list_jobs(status=JobStatus.ANALYZED, sort_key="llm_score")
    assert [row.id for row in rows] == [incompatible, historical, unknown, compatible, other_policy]
    assert rows[0].llm_score == 0.99
    assert [row.llm_score for row in rows[-2:]] == [0.25, 0.01]
    assert not rows[-1].fit_current
    assert "réévaluer" in rows[-1].fit_status_label
    for page, expected in enumerate([incompatible, historical, unknown, compatible, other_policy]):
        result = service.list_jobs_page(
            status=JobStatus.ANALYZED,
            sort_key="llm_score",
            page=page,
            page_size=1,
        )
        assert result["total"] == 5
        assert result["rows"][0].id == expected
        assert (
            service.job_page_for(
                expected,
                status=JobStatus.ANALYZED,
                sort_key="llm_score",
                page_size=1,
            )
            == page
        )
    ascending = service.list_jobs(
        status=JobStatus.ANALYZED, sort_key="llm_score", sort_ascending=True,
    )
    assert [row.id for row in ascending] == [other_policy, compatible, unknown, incompatible, historical]
    # The threshold applies only to comparable scores, leaving historical records reviewable.
    filtered = service.list_jobs(
        status=JobStatus.ANALYZED,
        min_llm_score=0.8,
        sort_key="llm_score",
    )
    assert [row.id for row in filtered] == [incompatible, historical, unknown, other_policy]
    by_day = service.list_jobs(status=JobStatus.ANALYZED, sort_key="operation_date")
    assert [row.id for row in by_day] == [
        incompatible,
        historical,
        unknown,
        compatible,
        other_policy,
    ]


def test_shortlist_ranks_scores_without_warnings_and_ignores_old_indices(isolated_db):
    with session_scope() as session:
        compatible = _offer(session, "direct", 0.25, _diagnostics(warnings=["invalid reference"]))
        unknown = _offer(session, "verify", 0.95, _diagnostics())
        incompatible = _offer(session, "conflict", 0.99, _diagnostics())
        historical = _offer(session, "history", 1.0, None)
        assert set_shortlisted(session, historical, selected=True, origin=ShortlistOrigin.MANUAL)
    report = DesktopService().update_shortlist(top_k=1)
    assert report["ranked_ids"] == [incompatible, unknown, compatible]
    assert report["shortlisted_ids"] == [incompatible]
    with session_scope() as session:
        assert session.get(Job, historical).shortlisted_at is not None
        for job_id, score in [
            (compatible, 0.25),
            (unknown, 0.95),
            (incompatible, 0.99),
            (historical, 1.0),
        ]:
            assert session.get(Job, job_id).analysis.fit_score == score
    # All current scores remain eligible, without altering manual historical selections.
    all_groups = DesktopService().update_shortlist(top_k=3)
    assert all_groups["shortlisted_ids"] == [incompatible, unknown, compatible]


def test_malformed_reliability_does_not_hide_score(isolated_db):
    diagnostics = _diagnostics(warnings=["Auxiliary schema invalid", "Citation invalid"])
    diagnostics["reliability"] = ["invalid"]
    with session_scope() as session:
        job_id = _offer(session, "malformed-aux", 0.70, diagnostics)
    service = DesktopService()
    detail = service.get_job(job_id)
    assert detail.llm_score == 0.70
    assert detail.fit_current
    assert [item["level"] for item in detail.fit_axes] == [4, 0, 4]
    assert detail.fit_warnings == ("Auxiliary schema invalid", "Citation invalid")
    row = _enrich_rows([detail])[0]
    assert row["llm_score_text"] == "70%"
    with session_scope() as session:
        assert session.get(Job, job_id).analysis.fit_diagnostics == diagnostics
        assert session.get(Job, job_id).analysis.fit_score == 0.70


def test_descriptive_reliability_is_preserved(isolated_db):
    diagnostics = _diagnostics(warnings=["Reference missing"])
    with session_scope() as session:
        job_id = _offer(session, "unknown-condition", 1.0, diagnostics)
    detail = DesktopService().get_job(job_id)
    assert detail.llm_score == 1.0
    assert detail.fit_reliability[-1] == {
        "label": "À confirmer",
        "text": "Habilitation à confirmer.",
    }


def test_legacy_admissibility_has_no_effect_on_selection_or_presentation(isolated_db):
    diagnostics = _diagnostics()
    diagnostics["admissibility"] = {
        "mandatory_conditions": {"status": "incompatible", "findings": []},
        "candidate_preferences": {"status": "incompatible", "findings": []},
    }
    with session_scope() as session:
        best = _offer(session, "legacy-block", 0.99, diagnostics)
        _offer(session, "lower-score", 0.25, _diagnostics())
    service = DesktopService()
    assert service.update_shortlist(top_k=1)["shortlisted_ids"] == [best]
    detail = service.get_job(best)
    row = _enrich_rows([detail])[0]
    assert detail.llm_score == 0.99
    assert not {"fit_admissibility", "candidacy_status", "candidacy_label"} & row.keys()
    with session_scope() as session:
        assert session.get(Job, best).analysis.fit_diagnostics == diagnostics


def test_offer_list_projects_fit_metadata_without_loading_prompt_payloads(isolated_db):
    from smartapply.database.session import get_engine

    diagnostics = _diagnostics()
    diagnostics["context"] = {"candidate_facts": "large private context" * 10000}
    diagnostics["original_response"] = "large model response" * 10000
    with session_scope() as session:
        job_id = _offer(session, "light-list", 0.70, diagnostics)
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", observe)
    try:
        rows = DesktopService().list_jobs(status=JobStatus.ANALYZED)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert rows[0].id == job_id
    assert rows[0].llm_score == 0.70 and rows[0].fit_current
    assert not any(
        re.search(r"job_analyses(?:_\d+)?\.fit_diagnostics\s+AS\s+", sql) for sql in statements
    )
    assert not any("admissibility" in sql for sql in statements)


def test_end_to_end_top_jobs_use_current_policy_and_score(isolated_db):
    from smartapply.pipeline import Pipeline

    with session_scope() as session:
        _offer(session, "e2e-direct", 0.25, _diagnostics(warnings=["missing citation"]))
        best = _offer(session, "e2e-best", 0.99, _diagnostics())
        historical = _offer(session, "e2e-historical", 1.0, None)
    pipeline = Pipeline.__new__(Pipeline)
    pipeline.process_pending = lambda: SimpleNamespace(analyzed=0)
    applied = []

    def apply(job_id):
        applied.append(job_id)
        return SimpleNamespace(job_id=job_id)

    pipeline.apply_to = apply
    report = pipeline.run_end_to_end(sources=[], top_k_apply=1)
    assert applied == [best]
    assert report["applications"] == [{"job_id": best}]
    with session_scope() as session:
        assert session.get(Job, historical).analysis.fit_score == 1.0
