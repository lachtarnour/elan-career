"""Scoring, advisory diagnostics and workflow guarantees, without paid calls."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from sqlalchemy import text

from smartapply.cli import cli
from smartapply.config import get_llm_settings
from smartapply.database import get_engine, session_scope
from smartapply.database.analysis_batches import AnalysisBatchRun
from smartapply.database.fit_schema import ensure_fit_schema
from smartapply.database.models import Application, GeneratedDocument, Job
from smartapply.database.models import JobAnalysis as SavedAnalysis
from smartapply.database.repository.llm_cache import cache_set
from smartapply.llm import (
    JobFitAssessment,
    JobFitDecisionEvaluation,
    LLMError,
)
from smartapply.llm.batch import BatchResult, build_batch_request
from smartapply.llm.fit_context import (
    POLICY,
    build_fit_context,
    build_fit_user,
    context_is_current,
    fingerprint,
)
from smartapply.llm.fit_request import check_request_budget, complete_fit
from smartapply.llm.fit_scoring import evaluate_fit
from smartapply.llm_policy import resolve_route
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
from tests.scope_fixtures import configured_scope_context
from tests.test_batch_analysis import extraction_offer, offer, seed_jobs


@pytest.fixture(autouse=True)
def explicit_fit_routes(monkeypatch, tmp_path):
    from tests.scope_fixtures import set_test_routes

    set_test_routes(
        monkeypatch,
        tmp_path,
        {
            purpose: {
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "reasoning": "medium",
                "max_output_tokens": tokens,
            }
            for purpose, tokens in (
                ("job_scope_screening", 6000),
                ("job_offer_extraction", 6000),
                ("job_fit_assessment", 12000),
            )
        },
    )


def fixture(*, levels=(4, 3, 3), skills=None, constraints=None):
    profile = get_profile().model_copy(deep=True)
    context = build_fit_context(
        profile, resolve_route(get_llm_settings(), "job_fit_assessment").model
    )
    facts = offer().model_copy(
        update={
            "required_skills": skills or ["Python"],
            "constraints": constraints or [],
        }
    )
    evaluation = evaluation_from_user(build_fit_user(context, facts), levels=levels)
    return context, facts, evaluation


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
            fit_diagnostics={"evaluation": "historical", "extraction_provenance": provenance}
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

    provider = SimpleNamespace(name="openai", complete_json=complete)
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
        assert after["analysis"]["fit_diagnostics"]["policy_name"] == POLICY["name"]


@pytest.mark.parametrize("change", ["sent", "offer", "profile", "policy", "plan"])
def test_frozen_plan_skips_changes_and_never_adds_new_offers(isolated_db, change):
    job_id = seeded_analysis()
    plan = prepare_rescore_plan(all_active=True)
    with session_scope() as session:
        session.add(
            Job(
                external_id="new-after-plan",
                title="New",
                source_company="New",
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
        plan["context"]["policy"]["weights"] = [40, 40, 20]
        plan["plan_fingerprint"] = fingerprint(
            {k: v for k, v in plan.items() if k != "plan_fingerprint"}
        )
    elif change == "plan":
        plan["entries"][0]["job_id"] = 999
    calls = []
    provider = SimpleNamespace(name="openai", complete_json=lambda **kwargs: calls.append(kwargs))
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
            if kwargs["purpose"] == "job_offer_extraction"
            else evaluation_from_user(kwargs["user"])
        )

    provider = SimpleNamespace(name="openai", complete_json=complete)
    assert execute_rescore_plan(plan, provider, approved=True)[0]["status"] == "needs_extraction"
    assert calls == []
    approved_plan = prepare_rescore_plan(job_ids=[job_id], include_extractions=True)
    assert plan_summary(approved_plan)["extraction_requests"] == 1
    before = snapshot(job_id)
    assert execute_rescore_plan(approved_plan, provider, approved=True)[0]["status"] == "rescored"
    assert calls == ["job_offer_extraction", "job_fit_assessment"]
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
    assert result.exit_code == 0 and json.loads(result.output)["evaluation"] == "historical"
    with get_engine().begin() as connection:
        connection.execute(text("ALTER TABLE job_analyses DROP COLUMN fit_diagnostics"))
        connection.execute(text("ALTER TABLE analysis_batch_runs DROP COLUMN fit_context"))
    assert len(ensure_fit_schema()) == 2
    assert ensure_fit_schema() == []
    # The trace column was removed in this synthetic migration setup, never the score.
    assert snapshot(job_id)["analysis"]["fit_score"] == before["analysis"]["fit_score"]


def batch_fixture(session, job_id):
    context, facts, evaluation = fixture()
    run = AnalysisBatchRun(
        id="fit-test-run",
        state="active",
        model=context["model"],
        provider="openai",
        max_completion_tokens=6000,
        extraction_effort="medium",
        profile_data=get_profile().model_dump(mode="json"),
        fit_context=context,
        scope_context=configured_scope_context(),
    )
    session.add(run)
    session.flush()
    item = new_item(run.id, session.get(Job, job_id), screening_context=run.scope_context)
    item.offer_data = facts.model_dump(mode="json")
    item.state = "evaluation_submitted"
    session.add(item)
    session.flush()
    request = build_batch_request(
        custom_id=f"evaluation-{item.id}",
        system=context["system_prompt"],
        user=build_fit_user(
            context, facts, extraction_provenance={"analyzer_input": item.analyzer_input}
        ),
        schema=JobFitDecisionEvaluation,
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
            SimpleNamespace(name="openai", complete_json=lambda **kwargs: evaluation),
            run.fit_context,
            facts,
        )
        assert request.body["messages"][0]["content"] == run.fit_context["system_prompt"]
        assert request.body["messages"][1]["content"] == build_fit_user(
            run.fit_context, facts, extraction_provenance={"analyzer_input": item.analyzer_input}
        )
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


@pytest.mark.parametrize("malformed_admissibility", [False, True])
def test_missing_batch_schema_with_identifiable_levels_is_nonblocking(
    isolated_db, malformed_admissibility
):
    job_id = seed_jobs()[0]
    with session_scope() as session:
        run, item, request, _, evaluation = batch_fixture(session, job_id)
        body = copy.deepcopy(request.body)
        del body["response_format"]
        raw = evaluation.model_dump(mode="json")
        if malformed_admissibility:
            raw["admissibility"] = {
                "mandatory_conditions": {"status": "invalid_status", "findings": 7},
                "candidate_preferences": None,
            }
        import_result(
            session,
            run,
            "evaluation",
            {"body": body, "cache_key": request.cache_key, "item_ids": [item.id]},
            BatchResult(custom_id=request.custom_id, content=json.dumps(raw)),
        )
        assert item.state == "completed"
        saved = session.get(Job, job_id).analysis
        assert saved.fit_score == 0.84
        assert saved.fit_diagnostics["evaluation"]["admissibility"] == raw["admissibility"]
        assert any(
            "schema unavailable" in warning
            for warning in saved.fit_diagnostics["diagnostic_checks"]["warnings"]
        )


def test_grouped_batch_cannot_copy_response_to_different_offer(isolated_db):
    ids = seed_jobs(2)
    with session_scope() as session:
        run, item, request, facts, evaluation = batch_fixture(session, ids[0])
        other = new_item(run.id, session.get(Job, ids[1]), screening_context=run.scope_context)
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
                source_company="Example",
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
            source_company="Example",
            source="manual",
            description="Duplicate offer",
            canonical_job_id=canonical_id,
            duplicate_review_status="confirmed",
            analysis=SavedAnalysis(**copy.deepcopy(analysis_data)),
        )
        pending = Job(
            external_id="pending",
            title="Review pending",
            source_company="Example",
            source="manual",
            description="Review pending offer",
            duplicate_review_status="pending",
            analysis=SavedAnalysis(**copy.deepcopy(analysis_data)),
        )
        submitted = Job(
            external_id="submitted",
            title="Submitted",
            source_company="Example",
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


@pytest.mark.parametrize("change", ["policy", "prompt", "schema", "candidate"])
def test_content_fingerprints_invalidate_context_and_cache_without_rewriting_history(
    isolated_db, monkeypatch, change
):
    from smartapply.database import init_db
    from smartapply.llm import fit_context

    job_id = seeded_analysis()
    before = snapshot(job_id)["analysis"]
    profile = get_profile().model_copy(deep=True)
    model = resolve_route(get_llm_settings(), "job_fit_assessment").model
    context = build_fit_context(profile, model)
    frozen = copy.deepcopy(context)

    def request(current):
        return build_batch_request(
            custom_id="fingerprint-test",
            system=current["system_prompt"],
            user=build_fit_user(current, offer()),
            schema=JobFitDecisionEvaluation,
            model=current["model"],
            purpose="job_fit_assessment",
            max_completion_tokens=current["max_completion_tokens"],
        )

    previous_cache_key = request(context).cache_key
    if change == "policy":
        monkeypatch.setitem(POLICY, "weights", [40, 40, 20])
        key = "policy_fingerprint"
    elif change == "prompt":
        original = fit_context.load_prompt
        monkeypatch.setattr(
            fit_context,
            "load_prompt",
            lambda template: original(template) + "\nPrécision générique sur la portée du travail.",
        )
        key = "prompt_fingerprint"
    elif change == "schema":
        original = JobFitDecisionEvaluation.model_json_schema
        monkeypatch.setattr(
            JobFitDecisionEvaluation,
            "model_json_schema",
            lambda *args, **kwargs: {
                **original(*args, **kwargs),
                "description": "Contrat de sortie modifié",
            },
        )
        key = "schema_fingerprint"
    else:
        profile.skills.categories[0].skills.append("Nouvelle capacité documentée")
        key = "candidate_fingerprint"
    assert not context_is_current(context, profile)
    changed = build_fit_context(profile, model)
    assert changed[key] != context[key]
    assert request(changed).cache_key != previous_cache_key
    assert context == frozen
    init_db(run_maintenance=False)
    assert snapshot(job_id)["analysis"] == before


def test_alternatives_and_peripheral_questions_do_not_introduce_code_penalties():
    context, facts, evaluation = fixture(levels=(4, 4, 4), skills=["Python ou Julia"])
    data = evaluation.model_dump()
    data["issues"] = [
        {
            "axis": "context",
            "origin": "offer_information",
            "offer_refs": ["domain"],
            "candidate_refs": [],
            "text": "Question périphérique",
        }
    ]
    public, diagnostic = evaluate_fit(JobFitDecisionEvaluation.model_validate(data), context, facts)
    # The simulated interpretation chooses Python. Python validates one requirement;
    # it does not split alternatives or infer missing Julia from their names.
    assert public.fit_score == 1.0
    assert "applied_caps" not in diagnostic


def test_factual_profile_edit_during_immediate_request_prevents_publication(isolated_db):
    from tests.test_two_stage_analysis import extraction_offer_result, insert_job, runner

    profile = get_profile().model_copy(deep=True)
    job_id = insert_job()

    def complete(**kwargs):
        if kwargs["purpose"] == "job_offer_extraction":
            return extraction_offer_result()
        evaluation = evaluation_from_user(kwargs["user"])
        profile.experiences[0].bullets[0].text += " Changed after submission"
        return evaluation

    instance = runner(
        SimpleNamespace(name="openai", complete_json=complete, cheap_model="mock"), profile
    )
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
    instance = runner(SimpleNamespace(name="openai", complete_json=complete, cheap_model="mock"))
    instance._uses_global_profile = True
    with pytest.raises(StaleTaskResultError):
        instance._analyze_one(job_id=job_id)
    assert calls == ["job_offer_extraction"]
    with session_scope() as session:
        assert session.get(Job, job_id).analysis is None
