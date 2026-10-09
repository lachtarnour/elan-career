"""The extraction screen skips fit only for clearly distant, described missions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from smartapply.config import get_settings
from smartapply.database import session_scope
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.database.models import Job, JobScore, JobStatus
from smartapply.database.repository import mark_archived, rescue_archived_job
from smartapply.database.scope_checkpoints import ScopeCheckpoint
from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.filtering.manual_policy import manual_filter_bypass_result
from smartapply.llm import OfferExtractionOutput, OpenAIProvider
from smartapply.llm.prompts import job_analysis, job_scope_screening
from smartapply.llm.schemas import JobScopeScreening
from smartapply.pipeline.process.analysis import AnalysisMixin
from smartapply.profile import get_profile
from tests.fit_fixtures import evaluation_from_user

BODY = (
    "Gérer les commandes des clients et vendre les abonnements de notre logiciel IA. "
    "Organiser des démonstrations commerciales et suivre les renouvellements."
)


def _offer():
    return OfferExtractionOutput(
        role_type="Chargé de comptes",
        seniority="unknown",
        domain="Logiciel",
        main_tasks=["Vendre des abonnements", "Suivre les renouvellements"],
        required_skills=[],
        nice_to_have=[],
        cv_keywords_to_include=[],
    )


def _screen(*, relation="clearly_outside", evidence="sufficient"):
    return JobScopeScreening(
        mission_screen={
            "mission_relation": relation,
            "mission_evidence": evidence,
        }
    ).mission_screen


def _job(external_id: str, *, components: dict | None = None) -> Job:
    return Job(
        external_id=external_id,
        title="Chargé de comptes pour logiciel IA",
        company="Example Software",
        description=BODY,
        source="test_search",
        filtered_at=datetime.now(timezone.utc),
        score=JobScore(
            components=components
            or {
                "filter_disposition": "pending_relevance",
                "hard_reject": HardRejectResult(decisions=()).audit_payload(),
            }
        ),
    )


def _analyzer(provider):
    analyzer = AnalysisMixin()
    analyzer.profile = get_profile()
    analyzer.settings = get_settings()
    analyzer.llm = provider
    return analyzer


def test_extraction_prompt_defines_the_conservative_gate() -> None:
    prompt = job_scope_screening.build_system_prompt()
    assert "mission_screen" not in job_analysis.build_system_prompt()
    assert "mission_screen" in prompt
    assert "clearly_outside" in prompt
    assert "source_quote" not in prompt
    assert "adjacent" in prompt
    assert "job title" in prompt.lower()


@pytest.mark.parametrize(
    ("relation", "evidence"),
    [
        ("direct", "sufficient"),
        ("direct", "insufficient"),
        ("direct", "contradictory"),
        ("adjacent", "sufficient"),
        ("adjacent", "insufficient"),
        ("adjacent", "contradictory"),
        ("unknown", "sufficient"),
        ("unknown", "insufficient"),
        ("unknown", "contradictory"),
        ("clearly_outside", "insufficient"),
        ("clearly_outside", "contradictory"),
    ],
)
def test_screen_keeps_non_decisive_or_unknown_offers(
    isolated_db,
    relation,
    evidence,
) -> None:
    from smartapply.pipeline.process.extraction_screening import apply_mission_screen

    with session_scope() as session:
        job = _job("screen:keep")
        session.add(job)
        session.flush()
        skipped_fit = apply_mission_screen(
            session, job, _screen(relation=relation, evidence=evidence)
        )
        assert skipped_fit is False
        assert job.archived_at is None
        assert job.status != JobStatus.ARCHIVED
        assert job.score.components["extraction_screen"]["decision"] != "archived"


def test_decisive_screen_archives_with_auditable_reason(isolated_db) -> None:
    from smartapply.pipeline.process.extraction_screening import apply_mission_screen

    screen = _screen()
    assert set(screen.model_dump()) == {"mission_relation", "mission_evidence"}
    with session_scope() as session:
        job = _job("screen:archive")
        session.add(job)
        session.flush()
        skipped_fit = apply_mission_screen(session, job, screen)
        assert skipped_fit is True
        assert job.archived_at is not None
        assert job.status == JobStatus.ARCHIVED
        components = job.score.components
        assert components["rejection_stage"] == "extraction_screening"
        assert components["archive_origin"] == "mission_screen"
        assert "mission_clearly_outside" in components["rejection_reasons"]
        assert components["extraction_screen"]["mission_relation"] == "clearly_outside"
        assert components["extraction_screen"]["mission_evidence"] == "sufficient"
        assert "source_quote" not in components["extraction_screen"]
        assert "quote_verified" not in components["extraction_screen"]


@pytest.mark.parametrize("override", ["manual_addition", "manual_rescue"])
def test_user_chosen_offer_bypasses_extraction_screen(isolated_db, override) -> None:
    from smartapply.pipeline.process.extraction_screening import apply_mission_screen

    with session_scope() as session:
        job = _job(f"screen:{override}")
        session.add(job)
        session.flush()
        if override == "manual_addition":
            components = dict(job.score.components)
            components["manual_filter_bypass"] = {
                "schema_version": 1,
                "origin": "manual_addition",
                "added_at": "2026-10-08T10:00:00+00:00",
            }
            job.score.components = components
        else:
            mark_archived(session, job.id)
            rescue_archived_job(session, job.id, justification="Choix utilisateur")
        skipped_fit = apply_mission_screen(session, job, _screen())
        assert skipped_fit is False
        assert job.archived_at is None
        assert job.status != JobStatus.ARCHIVED


def test_immediate_screen_stops_before_extraction_and_keeps_verdict(isolated_db) -> None:
    with session_scope() as session:
        job = _job("screen:immediate-reject")
        session.add(job)
        session.flush()
        job_id = job.id

    calls: list[str] = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        assert kwargs["purpose"] == "job_scope_screening"
        assert kwargs["schema"] is JobScopeScreening
        return JobScopeScreening(mission_screen=_screen())

    analyzer = _analyzer(
        SimpleNamespace(name="openai", cheap_model="mock-cheap", complete_json=complete)
    )
    analyzer.analyze_jobs([job_id])

    assert calls == ["job_scope_screening"]
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.archived_at is not None
        assert job.analysis is None and job.analyzed_at is None
        assert session.get(AnalysisCheckpoint, job_id) is None
        checkpoint = session.scalar(select(ScopeCheckpoint).where(ScopeCheckpoint.job_id == job_id))
        assert checkpoint.result_data["mission_screen"]["mission_relation"] == "clearly_outside"


def test_immediate_kept_offer_passes_only_offer_facts_to_fit(isolated_db) -> None:
    with session_scope() as session:
        job = _job("screen:immediate-keep")
        session.add(job)
        session.flush()
        job_id = job.id

    calls: list[str] = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "job_scope_screening":
            return JobScopeScreening(mission_screen=_screen(relation="adjacent"))
        if kwargs["purpose"] == "job_offer_extraction":
            return OfferExtractionOutput.model_validate(
                _offer().model_dump(exclude={"mission_screen"})
            )
        assert kwargs["purpose"] == "job_fit_assessment"
        assert "mission_screen" not in kwargs["user"]
        return evaluation_from_user(kwargs["user"], levels=(3, 3, 3))

    analyzer = _analyzer(
        SimpleNamespace(name="openai", cheap_model="mock-cheap", complete_json=complete)
    )
    report = analyzer.analyze_jobs([job_id])

    assert report.analyzed == 1
    assert calls == ["job_scope_screening", "job_offer_extraction", "job_fit_assessment"]
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.analysis is not None
        assert job.analysis.fit_score is not None
        assert "mission_screen" not in job.analysis.raw_response
        assert "mission_screen" not in job.analysis.fit_diagnostics["request_user"]
        assert job.score.components["extraction_screen"]["decision"] != "archived"


@pytest.mark.parametrize(
    "screen",
    [
        {"mission_relation": "sales", "mission_evidence": "sufficient"},
        {"mission_relation": "clearly_outside"},
    ],
)
def test_immediate_invalid_screen_still_runs_fit(isolated_db, monkeypatch, screen) -> None:
    with session_scope() as session:
        job = _job("screen:invalid-immediate")
        session.add(job)
        session.flush()
        job_id = job.id

    response_data = {"mission_screen": screen}
    from tests.scope_fixtures import set_test_routes

    set_test_routes(
        monkeypatch,
        isolated_db,
        {
            purpose: {
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "reasoning": "medium",
                "max_output_tokens": 6000,
            }
            for purpose in ("job_scope_screening", "job_offer_extraction", "job_fit_assessment")
        },
    )
    provider = OpenAIProvider(model="gpt-5.4-mini")
    calls = []

    def api(**kwargs):
        name = kwargs["response_format"]["json_schema"]["name"]
        calls.append(name)
        content = (
            json.dumps(response_data)
            if name == "JobScopeScreening"
            else _offer().model_dump_json(exclude={"mission_screen"})
            if name == "OfferExtractionOutput"
            else evaluation_from_user(kwargs["messages"][1]["content"]).model_dump_json()
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=content, refusal=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(provider, "_call_openai", api)
    report = _analyzer(provider).analyze_jobs([job_id])
    assert report.analyzed == 1 and report.screened_out == 0
    assert calls == ["JobScopeScreening", "OfferExtractionOutput", "JobFitDecisionEvaluation"]
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.analysis is not None and job.archived_at is None


@pytest.mark.parametrize("failing_step", ["set_score", "mark_archived"])
def test_immediate_screen_write_error_runs_fit_without_archiving(
    isolated_db,
    monkeypatch,
    failing_step,
) -> None:
    from smartapply.pipeline.process import extraction_screening

    with session_scope() as session:
        job = _job(f"screen:error:{failing_step}")
        session.add(job)
        session.flush()
        job_id = job.id

    original = getattr(extraction_screening, failing_step)

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("screen audit write interrupted")

    monkeypatch.setattr(extraction_screening, failing_step, fail_after_write)
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "job_scope_screening":
            return JobScopeScreening(mission_screen=_screen())
        if kwargs["purpose"] == "job_offer_extraction":
            return OfferExtractionOutput.model_validate(
                _offer().model_dump(exclude={"mission_screen"})
            )
        return evaluation_from_user(kwargs["user"], levels=(3, 3, 3))

    report = _analyzer(
        SimpleNamespace(name="openai", cheap_model="mock-cheap", complete_json=complete)
    ).analyze_jobs([job_id])
    assert report.analyzed == 1 and report.screened_out == 0
    assert calls == ["job_scope_screening", "job_offer_extraction", "job_fit_assessment"]
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.analysis is not None and job.archived_at is None
        assert "rejection_stage" not in job.score.components


@pytest.mark.parametrize("override", ["manual_addition", "manual_rescue"])
def test_immediate_user_choice_runs_fit_even_when_screen_is_decisive(
    isolated_db,
    override,
) -> None:
    components = {
        "filter_disposition": "pending_relevance",
        "hard_reject": HardRejectResult(decisions=()).audit_payload(),
    }
    if override == "manual_addition":
        components["hard_reject"] = manual_filter_bypass_result().audit_payload()
        components["manual_filter_bypass"] = {
            "schema_version": 1,
            "origin": "manual_addition",
            "added_at": "2026-10-08T10:00:00+00:00",
        }
    else:
        components["manual_rescue"] = {
            "schema_version": 3,
            "rescued_at": "2026-10-08T10:00:00+00:00",
            "justification": "Choix utilisateur",
        }

    with session_scope() as session:
        job = _job(f"screen:immediate-{override}", components=components)
        session.add(job)
        session.flush()
        job_id = job.id

    calls: list[str] = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        if kwargs["purpose"] == "job_scope_screening":
            return JobScopeScreening(mission_screen=_screen())
        if kwargs["purpose"] == "job_offer_extraction":
            return OfferExtractionOutput.model_validate(
                _offer().model_dump(exclude={"mission_screen"})
            )
        return evaluation_from_user(kwargs["user"], levels=(2, 2, 2))

    analyzer = _analyzer(
        SimpleNamespace(name="openai", cheap_model="mock-cheap", complete_json=complete)
    )
    report = analyzer.analyze_jobs([job_id])

    assert report.analyzed == 1
    assert calls == ["job_scope_screening", "job_offer_extraction", "job_fit_assessment"]
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job.archived_at is None
        assert job.analysis is not None
        assert job.score.components["extraction_screen"]["decision"] == "evaluate_manual_override"
