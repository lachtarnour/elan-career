"""Scoring, advisory diagnostics and workflow guarantees, without paid calls."""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from pydantic import ValidationError
from sqlalchemy import select, text

from smartapply.cli import cli
from smartapply.config import get_settings
from smartapply.database import get_engine, session_scope
from smartapply.database.analysis_batches import AnalysisBatchRun
from smartapply.database.fit_schema import ensure_fit_schema
from smartapply.database.models import Application, GeneratedDocument, Job, LLMCache
from smartapply.database.models import JobAnalysis as SavedAnalysis
from smartapply.database.repository.llm_cache import cache_set
from smartapply.llm import (
    JobFitAssessment,
    JobFitEvaluation,
    LLMError,
    OpenAIProvider,
)
from smartapply.llm.batch import BatchResult, build_batch_request
from smartapply.llm.cache import make_cache_key
from smartapply.llm.fit_context import (
    POLICY_VERSION,
    build_fit_context,
    build_fit_user,
    context_is_current,
    fingerprint,
    resolve,
)
from smartapply.llm.fit_request import check_request_budget, complete_fit
from smartapply.llm.fit_scoring import calculate_fit_score, evaluate_fit
from smartapply.llm.fit_validation import inspect_evaluation
from smartapply.llm.provider import LLMValidationError
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.batch_analysis.results import (
    accept_cache,
    accept_result,
    import_result,
    new_item,
)
from smartapply.pipeline.process.analysis import _assemble_analysis, persist_job_analysis
from smartapply.pipeline.rescore import execute_rescore_plan, plan_summary, prepare_rescore_plan
from smartapply.profile import get_profile
from tests.fit_fixtures import evaluation_from_user
from tests.test_batch_analysis import extraction_offer, offer, seed_jobs


def fixture(*, levels=(4, 3, 3), skills=None, constraints=None, version="evidence_fit_v5"):
    profile = get_profile().model_copy(deep=True)
    context = build_fit_context(profile, get_settings().openai_model_cheap, version=version)
    facts = offer().model_copy(
        update={
            "required_skills": skills or ["Python"],
            "constraints": constraints or [],
        }
    )
    evaluation = evaluation_from_user(build_fit_user(context, facts), levels=levels)
    return context, facts, evaluation


def issue(code, axis="responsibilities", origin="candidate_evidence", refs=None, candidates=None):
    return {
        "code": code,
        "axis": axis,
        "origin": origin,
        "offer_refs": refs or ["main_tasks[0]"],
        "candidate_refs": candidates or [],
        "text": "Question explicite à vérifier",
    }


@pytest.mark.parametrize(
    "levels,codes,expected,raw",
    [
        ((4, 3, 3), [], 0.88, "0.875"),
        ((4, 4, 2), ["substantial_core_gap"], 0.59, "0.9"),
        ((4, 4, 3), ["material_core_question"], 0.79, "0.95"),
        ((2, 4, 4), ["substantial_core_gap"], 0.59, "0.75"),
        ((1, 4, 4), ["distant_core_work"], 0.39, "0.625"),
        ((4, 4, 4), ["confirmed_constraint_conflict"], 0.19, "1"),
        ((4, 4, 4), [], 1.0, "1"),
        ((4, 4, 3), ["limited_question", "offer_clarity"], 0.95, "0.95"),
        (
            (4, 4, 3),
            ["material_core_question", "material_core_question", "confirmed_constraint_conflict"],
            0.19,
            "0.95",
        ),
    ],
)
def test_decimal_score_and_minimum_caps(levels, codes, expected, raw):
    result, diagnostic = calculate_fit_score(*levels, codes)
    assert result == expected and diagnostic["raw_score"] == raw


def test_all_possible_scores_and_strict_levels():
    scores = {
        calculate_fit_score(*levels, [])[0] for levels in itertools.product(range(5), repeat=3)
    }
    assert not any(0.60 <= score <= 0.74 for score in scores)
    assert {score for score in scores if score >= 0.75} == {0.75, 0.80, 0.83, 0.88, 0.93, 0.95, 1.0}
    for value in (True, 2.5, "3", -1, 5):
        with pytest.raises(ValueError):
            calculate_fit_score(value, 4, 4, [])
        context, facts, evaluation = fixture()
        data = evaluation.model_dump()
        data["dimensions"]["missions"]["level"] = value
        with pytest.raises(ValidationError):
            JobFitEvaluation.model_validate(data)


def test_declared_skill_alone_supports_essentials_four_without_cap():
    context, facts, evaluation = fixture(levels=(4, 4, 4))
    finding = evaluation.dimensions.essentials.findings[0]
    assert finding.support == "declared_only"
    assert all(
        context["candidate_refs"][ref]["source_type"] == "declared_skill"
        for ref in finding.candidate_refs
    )
    public, diagnostics = evaluate_fit(evaluation, context, facts)
    assert public.fit_score == 1 and diagnostics["applied_caps"] == {}
    assert any(reason.startswith("Déclaré :") for reason in public.match_reasons)


@pytest.mark.parametrize(
    "mutation",
    [
        "offer_ref",
        "candidate_ref",
        "identity",
        "skill_as_work",
        "uncovered",
        "no_positive",
        "central_four",
        "optional_cap",
        "wrong_origin",
        "contradicted_skill",
    ],
)
def test_evidence_warnings_do_not_prevent_a_score(mutation):
    context, facts, evaluation = fixture(levels=(4, 4, 4))
    data = evaluation.model_dump()
    finding = data["dimensions"]["missions"]["findings"][0]
    if mutation == "offer_ref":
        finding["offer_refs"] = ["main_tasks[999]"]
    elif mutation == "candidate_ref":
        finding["candidate_refs"] = ["bullet:absent"]
    elif mutation == "identity":
        finding["candidate_refs"] = ["identity:title"]
    elif mutation == "skill_as_work":
        finding["candidate_refs"] = data["dimensions"]["essentials"]["findings"][0][
            "candidate_refs"
        ]
    elif mutation == "uncovered":
        facts.required_skills.append("SQL")
    elif mutation == "no_positive":
        data["dimensions"]["responsibilities"]["findings"] = []
    elif mutation == "central_four":
        data["issues"] = [issue("material_core_question")]
    elif mutation == "optional_cap":
        data["dimensions"]["responsibilities"]["level"] = 3
        facts.nice_to_have = ["Optional tool"]
        data["issues"] = [issue("material_core_question", refs=["nice_to_have[0]"])]
    elif mutation == "wrong_origin":
        data["issues"] = [issue("offer_clarity", "context")]
    else:
        finding = data["dimensions"]["essentials"]["findings"][0]
        finding["support"] = "contradicted"
    parsed = JobFitEvaluation.model_validate(data)
    assert inspect_evaluation(parsed, context, facts)
    public, diagnostics = evaluate_fit(parsed, context, facts)
    assert 0 <= public.fit_score <= 1
    assert diagnostics["diagnostic_checks"]["status"] == "warnings"
    assert diagnostics["diagnostic_checks"]["mode"] == "advisory"
    assert diagnostics["evaluation"] == data


@pytest.mark.parametrize(
    "requirement",
    [
        "5 ans d’expérience et maîtrise avancée de Python",
        "trois ans et maîtrise avancée de Kubernetes",
    ],
)
def test_years_component_preserves_capability_coverage(requirement):
    context, facts, evaluation = fixture(skills=[requirement])
    data = evaluation.model_dump()
    neutral = {
        "offer_ref": "required_skills[0]",
        "reason": "years_only",
        "scope": "years_component",
        "candidate_refs": [],
    }
    data["neutral_offer_refs"] = [neutral]
    assert evaluate_fit(JobFitEvaluation.model_validate(data), context, facts)[0].fit_score == 0.88
    data["dimensions"]["essentials"]["findings"] = [
        copy.deepcopy(data["dimensions"]["missions"]["findings"][0])
    ]
    assert any(
        "not covered" in warning
        for warning in inspect_evaluation(JobFitEvaluation.model_validate(data), context, facts)
    )
    neutral["scope"] = "whole_entry"
    _, diagnostic = evaluate_fit(JobFitEvaluation.model_validate(data), context, facts)
    assert diagnostic["offer_facts"]["required_skills"] == [requirement]
    assert diagnostic["evaluation"]["neutral_offer_refs"] == [neutral]


@pytest.mark.parametrize(
    "text_value",
    [
        "5 ans d’expérience",
        "trois ans d’expérience",
        "3 years’ experience",
        "1 à 3 ans d’expérience (stage, alternance ou CDI inclus)",
        "Contrat de 3 ans",
        "bac + 5",
        "Validité de 3 ans",
    ],
)
def test_python_does_not_classify_neutralised_requirement_text(text_value):
    context, facts, evaluation = fixture(constraints=[text_value])
    data = evaluation.model_dump()
    data["dimensions"]["essentials"]["findings"] = data["dimensions"]["essentials"]["findings"][:1]
    data["neutral_offer_refs"] = [
        {
            "offer_ref": "constraints[0]",
            "reason": "years_only",
            "scope": "whole_entry",
            "candidate_refs": [],
        }
    ]
    public, diagnostic = evaluate_fit(JobFitEvaluation.model_validate(data), context, facts)
    assert public.fit_score == 0.88
    assert diagnostic["offer_facts"]["constraints"] == [text_value]
    assert diagnostic["evaluation"]["neutral_offer_refs"] == data["neutral_offer_refs"]


def test_preference_reference_problems_are_reported_without_rejecting_score():
    context, facts, evaluation = fixture(levels=(4, 4, 4), constraints=["CDI"])
    preference = "preference:accepted_contract_types:0"
    data = evaluation.model_dump()
    data["dimensions"]["essentials"]["findings"] = data["dimensions"]["essentials"]["findings"][:1]
    data["neutral_offer_refs"] = [
        {
            "offer_ref": "constraints[0]",
            "reason": "compatible_preference",
            "scope": "whole_entry",
            "candidate_refs": [preference],
        }
    ]
    assert evaluate_fit(JobFitEvaluation.model_validate(data), context, facts)[0].fit_score == 1
    data["neutral_offer_refs"][0]["candidate_refs"] = []
    assert any(
        "compatible_preference requires" in warning
        for warning in inspect_evaluation(JobFitEvaluation.model_validate(data), context, facts)
    )
    assert evaluate_fit(JobFitEvaluation.model_validate(data), context, facts)[0].fit_score == 1
    data["neutral_offer_refs"] = []
    facts.constraints = ["CDD imposé"]
    data["issues"] = [
        issue(
            "confirmed_constraint_conflict",
            "context",
            "preference_alignment",
            ["constraints[0]"],
            [preference],
        )
    ]
    assert evaluate_fit(JobFitEvaluation.model_validate(data), context, facts)[0].fit_score == 0.19
    data["issues"][0]["candidate_refs"] = ["identity:residence"]
    assert inspect_evaluation(JobFitEvaluation.model_validate(data), context, facts)
    data["issues"] = []
    data["neutral_offer_refs"] = [
        {
            "offer_ref": "required_skills[0]",
            "reason": "no_declared_preference",
            "scope": "whole_entry",
            "candidate_refs": [],
        }
    ]
    assert inspect_evaluation(JobFitEvaluation.model_validate(data), context, facts)
    assert evaluate_fit(JobFitEvaluation.model_validate(data), context, facts)[0].fit_score == 1


def test_problem_origins_and_extraction_warnings_are_preserved():
    context, facts, evaluation = fixture()
    facts.offer_risks = ["Client masqué"]
    data = evaluation.model_dump()
    data["issues"] = [
        issue("material_core_question", origin="offer_information"),
        issue("limited_question", origin="candidate_evidence"),
    ]
    data["dimensions"]["responsibilities"]["findings"].append(
        {
            "offer_refs": ["main_tasks[0]"],
            "candidate_refs": [],
            "support": "not_documented",
            "text": "Autonomie non documentée",
        }
    )
    public, diagnostic = evaluate_fit(JobFitEvaluation.model_validate(data), context, facts)
    assert public.fit_score == 0.79 and "Client masqué" in public.risks
    assert any(risk.startswith("Offre à clarifier") for risk in public.risks)
    assert any(risk.startswith("Non documenté") for risk in public.risks)
    assert [item["origin"] for item in diagnostic["evaluation"]["issues"]] == [
        "offer_information",
        "candidate_evidence",
    ]


def test_snapshot_is_immutable_and_editorial_changes_are_ignored():
    profile = get_profile().model_copy(deep=True)
    context = build_fit_context(profile, "mock")
    original = copy.deepcopy(context)
    profile.projects[-1].selection_policy.tier = "disabled"
    profile.style_guide.tone = "Different visual policy"
    assert context_is_current(context, profile)
    profile.skills.categories[0].skills.append("New fact")
    profile.experiences[0].bullets[0].allowed_claims.append("New allowed fact")
    assert context == original and not context_is_current(context, profile)
    for ref in original["candidate_refs"].values():
        assert resolve(original["candidate_facts"], ref) is not None


def test_inferred_and_self_reported_realizations_keep_their_status():
    context, facts, evaluation = fixture()
    ref = evaluation.dimensions.missions.findings[0].candidate_refs[0]
    bullet = resolve(context["candidate_facts"], context["candidate_refs"][ref])
    bullet["evidence_level"] = "self_reported"
    assert any(
        reason.startswith("Autodéclaré")
        for reason in evaluate_fit(evaluation, context, facts)[0].match_reasons
    )
    bullet["evidence_level"] = "inferred"
    public, diagnostic = evaluate_fit(evaluation, context, facts)
    assert public.fit_score == 0.88
    assert any("inferred" in warning for warning in diagnostic["diagnostic_checks"]["warnings"])


def test_evidence_warnings_keep_cached_score_without_correction(isolated_db):
    context, facts, evaluation = fixture()
    user = build_fit_user(context, facts)
    invalid = evaluation.model_copy(deep=True)
    invalid.dimensions.missions.findings[0].candidate_refs = ["absent"]
    key = make_cache_key(
        model=context["model"],
        system=context["system_prompt"],
        user=user,
        schema_name="JobFitEvaluation",
        extra={"temperature": 0.2, "schema": JobFitEvaluation.model_json_schema()},
    )
    with session_scope() as session:
        for cache_key in (key, "unrelated"):
            cache_set(
                session,
                cache_key=cache_key,
                model=context["model"],
                response=invalid.model_dump_json(),
                prompt_tokens=1,
                completion_tokens=1,
            )
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        return invalid if len(calls) == 1 else evaluation

    public, diagnostic = complete_fit(SimpleNamespace(complete_json=complete), context, facts)
    assert public.fit_score == 0.88 and len(calls) == 1
    assert diagnostic["diagnostic_checks"]["status"] == "warnings"
    assert diagnostic["evaluation"] == invalid.model_dump(mode="json")
    assert diagnostic["context"]["candidate_facts"] == context["candidate_facts"]
    with session_scope() as session:
        assert {entry.cache_key for entry in session.scalars(select(LLMCache))} == {
            key,
            "unrelated",
        }


def test_unreadable_output_does_not_trigger_automatic_correction(isolated_db):
    context, facts, _ = fixture()
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        raise LLMValidationError("Missing dimensions")

    with pytest.raises(LLMValidationError, match="Missing dimensions"):
        complete_fit(SimpleNamespace(complete_json=complete), context, facts)
    assert len(calls) == 1
    assert "CORRECTION UNIQUE" not in calls[0]["user"]


@pytest.mark.parametrize("finish,refusal", [("length", None), ("stop", "Refused")])
def test_provider_flags_with_usable_levels_are_observations(
    isolated_db, monkeypatch, finish, refusal
):
    context, facts, evaluation = fixture()
    provider = OpenAIProvider()
    calls = []

    def api(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=finish,
                    message=SimpleNamespace(content=evaluation.model_dump_json(), refusal=refusal),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(provider, "_call_openai", api)
    public, diagnostics = complete_fit(provider, context, facts)
    assert public.fit_score == 0.88 and len(calls) == 1
    assert any(
        "usable levels retained" in warning
        for warning in diagnostics["diagnostic_checks"]["warnings"]
    )
    with session_scope() as session:
        assert len(list(session.scalars(select(LLMCache)))) == 1


def test_complete_request_budget_includes_schema_and_output_reserve():
    context, facts, _ = fixture()
    user = build_fit_user(context, facts)
    budget = check_request_budget(context, user)
    assert budget["input_token_upper_bound"] > len((context["system_prompt"] + user).encode())
    assert budget["output_reserved"] == 12000
    context["configured_context_window"] = budget["input_token_upper_bound"] + 11999
    with pytest.raises(LLMError, match="no factual content was truncated"):
        check_request_budget(context, user)
    context["model"] = "unknown-alias"
    context["configured_context_window"] = None
    with pytest.raises(LLMError, match="Unknown model"):
        check_request_budget(context, user)


def seeded_analysis(*, trace=True):
    job_id = seed_jobs()[0]
    context, facts, _ = fixture()
    public = JobFitAssessment(
        fit_score=0.72, match_reasons=["Ancien point positif"], risks=["Ancienne réserve"]
    )
    with session_scope() as session:
        job = session.get(Job, job_id)
        provenance = {"analyzer_input": asdict(build_analyzer_input(job))}
        persist_job_analysis(
            session,
            job,
            _assemble_analysis(facts, public),
            context["model"],
            fit_diagnostics={"policy_version": "legacy", "extraction_provenance": provenance}
            if trace
            else None,
        )
        job.application = Application(
            status="ready_for_form_submission",
            notes="Notes manuelles",
            cv_json={"manual": True},
            cv_pdf_path="existing.pdf",
            ready_at=job.analyzed_at,
            documents=[GeneratedDocument(doc_type="cv", content="Document conservé")],
        )
    return job_id


def column_values(record):
    return {
        column.name: copy.deepcopy(getattr(record, column.name))
        for column in record.__table__.columns
    }


def snapshot(job_id):
    with session_scope() as session:
        job = session.get(Job, job_id)
        return {
            "job": column_values(job),
            "score": column_values(job.score),
            "analysis": column_values(job.analysis),
            "application": column_values(job.application),
            "documents": [column_values(doc) for doc in job.application.documents],
        }


@pytest.mark.parametrize("fail", [False, True])
def test_rescore_fit_only_atomic_writer_preserves_workflow(isolated_db, fail):
    job_id = seeded_analysis()
    before = snapshot(job_id)
    plan = prepare_rescore_plan(job_ids=[job_id])
    assert plan_summary(plan)["evaluation_requests"] == 1
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        if fail:
            raise LLMError("Unavailable")
        return evaluation_from_user(kwargs["user"])

    provider = SimpleNamespace(complete_json=complete)
    with pytest.raises(ValueError, match="Explicit approval"):
        execute_rescore_plan(plan, provider)
    assert calls == []
    result = execute_rescore_plan(plan, provider, approved=True)
    after = snapshot(job_id)
    assert calls == ["job_fit_assessment"]
    if fail:
        assert result[0]["status"] == "failed_or_stale" and after == before
    else:
        assert result[0]["status"] == "rescored"
        for section in ("job", "score", "application", "documents"):
            assert after[section] == before[section]
        for key in before["analysis"].keys() - {
            "fit_score",
            "match_reasons",
            "risks",
            "fit_diagnostics",
            "raw_response",
        }:
            assert after["analysis"][key] == before["analysis"][key]
        raw = after["analysis"]["raw_response"]
        for key in ("fit_score", "match_reasons", "risks"):
            assert raw[key] == after["analysis"][key]
        assert raw.keys() == before["analysis"]["raw_response"].keys()
        assert "Ancienne réserve" not in after["analysis"]["risks"]
        assert after["analysis"]["fit_diagnostics"]["policy_version"] == POLICY_VERSION


@pytest.mark.parametrize("change", ["sent", "offer", "profile", "policy", "plan"])
def test_frozen_plan_skips_changes_and_never_adds_new_offers(isolated_db, change):
    job_id = seeded_analysis()
    plan = prepare_rescore_plan(all_active=True)
    with session_scope() as session:
        session.add(
            Job(
                external_id="new-after-plan",
                title="New",
                company="New",
                description="New offer",
                source="manual",
            )
        )
        job = session.get(Job, job_id)
        if change == "sent":
            job.status = "sent"
        elif change == "offer":
            job.description += " New condition"
    profile = get_profile().model_copy(deep=True)
    if change == "profile":
        profile.skills.categories[0].skills.append("Changed")
    elif change == "policy":
        plan["context"]["policy"]["caps"]["material_core_question"] = "0.78"
        plan["plan_fingerprint"] = fingerprint(
            {k: v for k, v in plan.items() if k != "plan_fingerprint"}
        )
    elif change == "plan":
        plan["entries"][0]["job_id"] = 999
    calls = []
    provider = SimpleNamespace(complete_json=lambda **kwargs: calls.append(kwargs))
    if change == "plan":
        with pytest.raises(ValueError, match="modified"):
            execute_rescore_plan(plan, provider, approved=True)
    else:
        result = execute_rescore_plan(plan, provider, approved=True, profile=profile)
        assert len(result) == 1 and result[0]["status"] == "failed_or_stale"
    assert calls == []


def test_additional_extractions_must_be_announced(isolated_db):
    job_id = seeded_analysis(trace=False)
    plan = prepare_rescore_plan(job_ids=[job_id])
    assert plan["entries"][0]["operation"] == "needs_extraction"
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        return (
            extraction_offer()
            if kwargs["purpose"] == "job_offer_analysis"
            else evaluation_from_user(kwargs["user"])
        )

    provider = SimpleNamespace(complete_json=complete)
    assert execute_rescore_plan(plan, provider, approved=True)[0]["status"] == "needs_extraction"
    assert calls == []
    approved_plan = prepare_rescore_plan(job_ids=[job_id], include_extractions=True)
    assert plan_summary(approved_plan)["extraction_requests"] == 1
    before = snapshot(job_id)
    assert execute_rescore_plan(approved_plan, provider, approved=True)[0]["status"] == "rescored"
    assert calls == ["job_offer_analysis", "job_fit_assessment"]
    assert snapshot(job_id)["job"] == before["job"]


def test_incompatible_history_is_reported_before_any_call(isolated_db):
    job_id = seeded_analysis()
    with session_scope() as session:
        session.get(Job, job_id).analysis.raw_response = "old incompatible text"
    plan = prepare_rescore_plan(job_ids=[job_id], include_extractions=True)
    assert plan["entries"][0]["operation"] == "unresolved"
    assert plan_summary(plan)["evaluation_requests"] == 0


def test_cli_dry_run_and_diagnostics_and_schema_migration_do_not_recalculate(isolated_db, tmp_path):
    job_id = seeded_analysis()
    before = snapshot(job_id)
    runner = CliRunner()
    destination = tmp_path / "private-plan.json"
    result = runner.invoke(
        cli, ["rescore", "--dry-run", "--all-active", "--plan", str(destination)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["evaluation_requests"] == 1
    assert destination.stat().st_mode & 0o777 == 0o600
    assert snapshot(job_id) == before
    result = runner.invoke(cli, ["fit-diagnostics", "--job-id", str(job_id)])
    assert result.exit_code == 0 and json.loads(result.output)["policy_version"] == "legacy"
    with get_engine().begin() as connection:
        connection.execute(text("ALTER TABLE job_analyses DROP COLUMN fit_diagnostics"))
        connection.execute(text("ALTER TABLE analysis_batch_runs DROP COLUMN fit_context"))
    assert len(ensure_fit_schema()) == 2
    assert ensure_fit_schema() == []
    # The trace column was removed in this synthetic migration setup, never the score.
    assert snapshot(job_id)["analysis"]["fit_score"] == before["analysis"]["fit_score"]


def batch_fixture(session, job_id, *, legacy=False):
    context, facts, evaluation = fixture()
    run = AnalysisBatchRun(
        id="fit-test-run",
        state="active",
        model=context["model"],
        profile_data=get_profile().model_dump(mode="json"),
        fit_context=None if legacy else context,
    )
    session.add(run)
    session.flush()
    item = new_item(run.id, session.get(Job, job_id))
    item.offer_data = facts.model_dump(mode="json")
    item.state = "evaluation_submitted"
    session.add(item)
    session.flush()
    schema = JobFitAssessment if legacy else JobFitEvaluation
    request = build_batch_request(
        custom_id=f"evaluation-{item.id}",
        system=context["system_prompt"],
        user=build_fit_user(context, facts),
        schema=schema,
        model=context["model"],
        purpose="job_fit_assessment",
        max_completion_tokens=12000,
    )
    return run, item, request, facts, evaluation


@pytest.mark.parametrize("path", ["batch", "cache"])
@pytest.mark.parametrize("warning", [False, True])
def test_immediate_batch_cache_share_diagnostic_calculation_and_conversion(
    isolated_db, path, warning
):
    job_id = seed_jobs()[0]
    with session_scope() as session:
        run, item, request, facts, evaluation = batch_fixture(session, job_id)
        if warning:
            evaluation.dimensions.missions.findings[0].candidate_refs.append("unknown_ref")
        expected, _ = complete_fit(
            SimpleNamespace(complete_json=lambda **kwargs: evaluation), run.fit_context, facts
        )
        assert request.body["messages"][0]["content"] == run.fit_context["system_prompt"]
        assert request.body["messages"][1]["content"] == build_fit_user(run.fit_context, facts)
        if path == "batch":
            accept_result(session, item, run, "evaluation", evaluation, request_body=request.body)
        else:
            cache_set(
                session,
                cache_key=request.cache_key,
                model=run.model,
                response=evaluation.model_dump_json(),
                prompt_tokens=1,
                completion_tokens=1,
            )
            assert accept_cache(session, item, run, "evaluation", request)
        session.flush()
        job = session.get(Job, job_id)
        assert job.analysis is not None and item.state == "completed"
        assert {
            key: getattr(job.analysis, key) for key in type(expected).model_fields
        } == expected.model_dump()
        assert job.analysis.fit_diagnostics["evaluation"] == evaluation.model_dump(mode="json")
        assert bool(job.analysis.fit_diagnostics["diagnostic_checks"]["warnings"]) == warning
        assert set(job.analysis.raw_response) == set(
            _assemble_analysis(facts, expected).model_dump()
        )


def test_legacy_batch_uses_recorded_schema_without_invented_levels(isolated_db):
    job_id = seed_jobs()[0]
    with session_scope() as session:
        run, item, request, _, _ = batch_fixture(session, job_id, legacy=True)
        public = JobFitAssessment(fit_score=0.67, match_reasons=["Ancien contexte"], risks=[])
        import_result(
            session,
            run,
            "evaluation",
            {"body": request.body, "cache_key": request.cache_key, "item_ids": [item.id]},
            BatchResult(custom_id=request.custom_id, content=public.model_dump_json()),
        )
        assert item.state == "completed"
        saved = session.get(Job, job_id).analysis
        assert saved.fit_score == 0.67
        assert saved.fit_diagnostics["policy_version"] == "legacy_llm_fit"
        assert "evaluation" not in saved.fit_diagnostics


def test_missing_batch_schema_with_identifiable_levels_is_nonblocking(isolated_db):
    job_id = seed_jobs()[0]
    with session_scope() as session:
        run, item, request, _, evaluation = batch_fixture(session, job_id)
        body = copy.deepcopy(request.body)
        del body["response_format"]
        import_result(
            session,
            run,
            "evaluation",
            {"body": body, "cache_key": request.cache_key, "item_ids": [item.id]},
            BatchResult(custom_id=request.custom_id, content=evaluation.model_dump_json()),
        )
        assert item.state == "completed"
        saved = session.get(Job, job_id).analysis
        assert saved.fit_score == 0.88
        assert any(
            "schema unavailable" in warning
            for warning in saved.fit_diagnostics["diagnostic_checks"]["warnings"]
        )


def test_grouped_batch_cannot_copy_response_to_different_offer(isolated_db):
    ids = seed_jobs(2)
    with session_scope() as session:
        run, item, request, facts, evaluation = batch_fixture(session, ids[0])
        other = new_item(run.id, session.get(Job, ids[1]))
        other.offer_data = facts.model_copy(
            update={"required_skills": ["Python", "Uncovered"]}
        ).model_dump(mode="json")
        other.state = "evaluation_submitted"
        session.add(other)
        session.flush()
        import_result(
            session,
            run,
            "evaluation",
            {"body": request.body, "cache_key": request.cache_key, "item_ids": [item.id, other.id]},
            BatchResult(custom_id=request.custom_id, content=evaluation.model_dump_json()),
        )
        assert item.state == other.state == "evaluation_failed"
        assert "autre extraction" in item.error
        assert all(session.get(Job, job_id).analysis is None for job_id in ids)


def test_default_selection_includes_ready_and_canonical_but_excludes_history(isolated_db):
    canonical_id = seeded_analysis()
    excluded_ids = []
    with session_scope() as session:
        canonical = session.get(Job, canonical_id)
        canonical.duplicate_review_status = "confirmed"
        analysis_data = {
            key: value
            for key, value in column_values(canonical.analysis).items()
            if key not in {"id", "job_id"}
        }
        for index, status in enumerate(
            (
                "sent",
                "interview",
                "rejected",
                "quality_rejected",
                "archived",
                "closed",
                "withdrawn",
                "removed",
            )
        ):
            job = Job(
                external_id=f"excluded-{index}",
                title="History",
                company="Example",
                source="manual",
                description="Old job",
                status=status,
                analysis=SavedAnalysis(**copy.deepcopy(analysis_data)),
            )
            session.add(job)
            session.flush()
            excluded_ids.append(job.id)
        secondary = Job(
            external_id="secondary",
            title="Duplicate",
            company="Example",
            source="manual",
            description="Duplicate offer",
            canonical_job_id=canonical_id,
            duplicate_review_status="confirmed",
            analysis=SavedAnalysis(**copy.deepcopy(analysis_data)),
        )
        pending = Job(
            external_id="pending",
            title="Review pending",
            company="Example",
            source="manual",
            description="Review pending offer",
            duplicate_review_status="pending",
            analysis=SavedAnalysis(**copy.deepcopy(analysis_data)),
        )
        submitted = Job(
            external_id="submitted",
            title="Submitted",
            company="Example",
            source="manual",
            description="Submitted offer",
            analysis=SavedAnalysis(**copy.deepcopy(analysis_data)),
            application=Application(status="sent"),
        )
        session.add_all([secondary, pending, submitted])
        session.flush()
        excluded_ids.extend([secondary.id, pending.id, submitted.id])
    plan = prepare_rescore_plan(all_active=True)
    operations = {entry["job_id"]: entry["operation"] for entry in plan["entries"]}
    assert operations[canonical_id] == "evaluate_fit"
    assert all(operations[job_id] == "excluded" for job_id in excluded_ids)


def test_policy_version_changes_are_visible_and_do_not_recalculate_history(
    isolated_db, monkeypatch
):
    from smartapply.database import init_db
    from smartapply.llm.fit_context import POLICY

    job_id = seeded_analysis()
    before = snapshot(job_id)["analysis"]
    context = build_fit_context(get_profile(), get_settings().openai_model_cheap)
    monkeypatch.setitem(POLICY, "calculation_version", "test_changed_calculation")
    monkeypatch.setitem(POLICY, "caps", {**POLICY["caps"], "material_core_question": "0.78"})
    assert not context_is_current(context, get_profile())
    changed = build_fit_context(get_profile(), get_settings().openai_model_cheap)
    assert changed["policy_fingerprint"] != context["policy_fingerprint"]
    init_db(run_maintenance=False)
    assert snapshot(job_id)["analysis"] == before


def test_alternatives_and_peripheral_questions_do_not_introduce_code_penalties():
    context, facts, evaluation = fixture(levels=(4, 4, 4), skills=["Python ou Julia"])
    data = evaluation.model_dump()
    data["issues"] = [issue("limited_question", "context", "offer_information", ["domain"])]
    public, diagnostic = evaluate_fit(JobFitEvaluation.model_validate(data), context, facts)
    # The simulated interpretation chooses Python. Python validates one requirement;
    # it does not split alternatives or infer missing Julia from their names.
    assert public.fit_score == 1.0 and diagnostic["applied_caps"] == {}


def test_factual_profile_edit_during_immediate_request_prevents_publication(isolated_db):
    from tests.test_two_stage_analysis import extraction_offer_result, insert_job, runner

    profile = get_profile().model_copy(deep=True)
    job_id = insert_job()

    def complete(**kwargs):
        if kwargs["purpose"] == "job_offer_analysis":
            return extraction_offer_result()
        evaluation = evaluation_from_user(kwargs["user"])
        profile.experiences[0].bullets[0].text += " Changed after submission"
        return evaluation

    instance = runner(SimpleNamespace(complete_json=complete, cheap_model="mock"), profile)
    from smartapply.pipeline.errors import StaleTaskResultError

    with pytest.raises(StaleTaskResultError):
        instance._analyze_one(job_id=job_id)
    with session_scope() as session:
        assert session.get(Job, job_id).analysis is None


def test_profile_file_change_during_extraction_stops_before_fit(isolated_db, monkeypatch):
    from smartapply.pipeline.errors import StaleTaskResultError
    from tests.test_two_stage_analysis import extraction_offer_result, insert_job, runner

    changed = get_profile().model_copy(deep=True)
    changed.skills.categories[0].skills.append("New canonical fact")
    calls = []

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        monkeypatch.setattr("smartapply.pipeline.process.analysis.get_profile", lambda: changed)
        return extraction_offer_result()

    job_id = insert_job()
    instance = runner(SimpleNamespace(complete_json=complete, cheap_model="mock"))
    instance._uses_global_profile = True
    with pytest.raises(StaleTaskResultError):
        instance._analyze_one(job_id=job_id)
    assert calls == ["job_offer_analysis"]
    with session_scope() as session:
        assert session.get(Job, job_id).analysis is None
