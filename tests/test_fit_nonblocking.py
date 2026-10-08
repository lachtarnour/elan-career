"""Permanent scoring boundary: annotations and future inspections cannot veto levels."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.analysis_batches import AnalysisBatchItem, AnalysisBatchRun
from smartapply.database.models import Job, LLMCache
from smartapply.database.repository.llm_cache import cache_set
from smartapply.llm import JobFitEvaluation, OpenAIProvider
from smartapply.llm.batch import BatchResult, parse_results
from smartapply.llm.cache import make_cache_key
from smartapply.llm.fit_context import LEGACY_POLICY as POLICY
from smartapply.llm.fit_context import build_fit_user
from smartapply.llm.fit_request import complete_fit
from smartapply.llm.fit_response import read_fit_response
from smartapply.llm.fit_scoring import calculate_fit_score, evaluate_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.pipeline.batch_analysis.results import accept_cache, import_result
from smartapply.pipeline.process.analysis import _assemble_analysis, persist_job_analysis
from smartapply.pipeline.rescore import execute_rescore_plan, prepare_rescore_plan
from tests.fit_fixtures import decision_response, evaluation_from_user
from tests.test_batch_analysis import seed_jobs
from tests.test_evidence_fit import batch_fixture, fixture, seeded_analysis, snapshot


def response(content, *, finish="stop", refusal=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish, message=SimpleNamespace(content=content, refusal=refusal)
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
    )


def minimal(*, levels=(4, 4, 3)):
    return {
        "dimensions": {
            axis: {"level": level}
            for axis, level in zip(
                ("missions", "essentials", "responsibilities"), levels, strict=True
            )
        }
    }


def imperfect(data, mutation):
    finding = data["dimensions"]["missions"]["findings"][0]
    if mutation == "missing_annotations":
        return minimal()
    if mutation == "containers":
        data["dimensions"]["missions"]["findings"] = "invalid"
        data["issues"] = 23
        data["neutral_offer_refs"] = None
    elif mutation == "missing_fields":
        del finding["candidate_refs"]
        del finding["text"]
    elif mutation == "unknown_labels":
        finding["support"] = "future_status"
        data["neutral_offer_refs"] = [{"scope": "future_scope", "reason": "future_reason"}]
    elif mutation == "reference_types":
        finding["candidate_refs"] = {"unexpected": True}
        finding["offer_refs"] = [None, 12, ["main_tasks[0]"]]
    elif mutation == "unknown_references":
        finding["candidate_refs"] = ["absent"]
        finding["offer_refs"] = ["main_tasks[999]"]
    elif mutation == "identity_proof":
        finding["candidate_refs"] = ["identity:title"]
    elif mutation == "uncovered":
        data["dimensions"]["essentials"]["findings"] = []
    elif mutation == "bad_neutralisation":
        data["neutral_offer_refs"] = [
            {
                "offer_ref": "required_skills[0]",
                "scope": "whole_entry",
                "reason": "no_declared_preference",
                "candidate_refs": [],
            }
        ]
    elif mutation == "unknown_code":
        data["issues"] = [{"code": "future_business_code", "text": "Réserve originale"}]
    elif mutation == "free_score":
        data["fit_score"] = 0.01
        data["future_field"] = {"keep": ["original", 7]}
    elif mutation == "malformed_recognized_issue":
        data["issues"] = [
            {
                "code": "material_core_question",
                "axis": ["missions"],
                "origin": None,
                "offer_refs": "absent",
                "text": 2,
            }
        ]
    return data


MUTATIONS = (
    "missing_annotations",
    "containers",
    "missing_fields",
    "unknown_labels",
    "reference_types",
    "unknown_references",
    "identity_proof",
    "uncovered",
    "bad_neutralisation",
    "unknown_code",
    "free_score",
    "malformed_recognized_issue",
)


@pytest.mark.parametrize("path", ["immediate", "batch", "cache"])
@pytest.mark.parametrize("mutation", MUTATIONS)
def test_imperfect_annotations_score_and_persist_on_every_path(
    isolated_db, monkeypatch, path, mutation
):
    """Exercise actual readers, stored generation schema, cache and DB serialization."""
    job_id = seed_jobs()[0]
    calls = []
    with session_scope() as session:
        run, item, request, facts, evaluation = batch_fixture(session, job_id)
        run_id, item_id, model = run.id, item.id, run.model
        context = copy.deepcopy(run.fit_context)
    raw = evaluation.model_dump(mode="json")
    raw["dimensions"]["essentials"]["level"] = 4
    raw = imperfect(raw, mutation)
    original = copy.deepcopy(raw)
    content = json.dumps(raw, ensure_ascii=False)
    if path == "immediate":
        provider = OpenAIProvider()

        def api(**kwargs):
            calls.append(kwargs)
            return response(content)

        monkeypatch.setattr(provider, "_call_openai", api)
        public, diagnostic = complete_fit(provider, context, facts, job_id=job_id)
        # Calls own their cache transaction, outside the publication transaction.
        cached, cached_diagnostic = complete_fit(provider, context, facts)
        assert cached == public and cached_diagnostic["evaluation"] == original
        assert len(calls) == 1
        with session_scope() as session:
            persist_job_analysis(
                session,
                session.get(Job, job_id),
                _assemble_analysis(facts, public),
                model,
                fit_diagnostics=diagnostic,
            )
    else:
        with session_scope() as session:
            run = session.get(AnalysisBatchRun, run_id)
            item = session.get(AnalysisBatchItem, item_id)
            if path == "batch":
                import_result(
                    session,
                    run,
                    "evaluation",
                    {"body": request.body, "cache_key": request.cache_key, "item_ids": [item.id]},
                    BatchResult(custom_id=request.custom_id, content=content),
                )
            else:
                cache_set(
                    session,
                    cache_key=request.cache_key,
                    model=model,
                    response=content,
                    prompt_tokens=1,
                    completion_tokens=1,
                )
                assert accept_cache(session, item, run, "evaluation", request)
                assert (
                    session.scalar(select(LLMCache).where(LLMCache.cache_key == request.cache_key))
                    is not None
                )
            assert item.state == "completed", item.error
    # Fresh session proves persistence, beyond the in-memory objects.
    with session_scope() as session:
        saved = session.get(Job, job_id).analysis
        assert saved.fit_score == (0.79 if mutation == "malformed_recognized_issue" else 0.95)
        assert saved.raw_response["fit_score"] == saved.fit_score
        diagnostic = saved.fit_diagnostics
        assert diagnostic["evaluation"] == original
        assert diagnostic["original_response"] == content
        assert diagnostic["used_levels"] == {"missions": 4, "essentials": 4, "responsibilities": 3}
        assert diagnostic["diagnostic_checks"]["mode"] == "advisory"
        assert diagnostic["diagnostic_checks"]["warnings"]
        if mutation == "missing_annotations":
            assert saved.match_reasons == [] and saved.risks == facts.offer_risks


@pytest.mark.parametrize("code,cap", list(POLICY["caps"].items()))
@pytest.mark.parametrize("shape", ["bad_object", "singleton", "string_list"])
def test_recognized_caps_do_not_depend_on_annotation_conformity(code, cap, shape):
    context, facts, _ = fixture()
    raw = minimal(levels=(4, 4, 4))
    issue = {"code": code, "origin": "unknown", "axis": None, "text": None}
    raw["issues"] = [issue] if shape == "bad_object" else issue if shape == "singleton" else [code]
    public, diagnostic = evaluate_fit(raw, context, facts)
    assert public.fit_score == float(cap)
    assert diagnostic["applied_caps"] == {code: cap}
    assert diagnostic["evaluation"] == raw
    assert diagnostic["diagnostic_checks"]["warnings"]


@pytest.mark.parametrize("checker", ["schema_observations", "inspect_evaluation"])
@pytest.mark.parametrize("failure", ["validation", "runtime", "bad_return", "mutation"])
def test_future_checks_cannot_reject_or_rewrite_a_score(monkeypatch, checker, failure):
    context, facts, evaluation = fixture()
    raw = evaluation.model_dump(mode="json")
    calls = []

    def check(*args):
        if failure == "validation":
            raise LLMValidationError("Future global validation must remain advisory")
        if failure == "runtime":
            raise RuntimeError("Unexpected checker failure")
        if failure == "bad_return":
            return None
        # Even an accidental mutation must not affect raw, levels, caps or facts.
        if checker == "schema_observations":
            args[0].raw["dimensions"]["missions"]["level"] = 0
            args[0].raw["issues"] = [{"code": "confirmed_constraint_conflict"}]
        else:
            args[0].dimensions.missions.level = 0
            args[1]["candidate_facts"].clear()
            args[2].main_tasks.clear()
        return ["Future observation"]

    monkeypatch.setattr(f"smartapply.llm.fit_scoring.{checker}", check)

    def complete(**kwargs):
        calls.append(kwargs)
        return raw

    public, diagnostic = complete_fit(SimpleNamespace(complete_json=complete), context, facts)
    assert public.fit_score == 0.88 and len(calls) == 1
    assert diagnostic["evaluation"] == raw == evaluation.model_dump(mode="json")
    assert diagnostic["context"] == context and diagnostic["offer_facts"] == facts.model_dump(
        mode="json"
    )
    assert diagnostic["applied_caps"] == {}
    assert diagnostic["diagnostic_checks"]["warnings"]


def test_future_stored_schema_check_failure_cannot_block_batch_save(isolated_db, monkeypatch):
    job_id = seed_jobs()[0]

    def broken(*args):
        raise RuntimeError("Future stored-schema checker")

    monkeypatch.setattr("smartapply.pipeline.batch_analysis.results.Draft202012Validator", broken)
    with session_scope() as session:
        run, item, request, _, evaluation = batch_fixture(session, job_id)
        import_result(
            session,
            run,
            "evaluation",
            {"body": request.body, "cache_key": request.cache_key, "item_ids": [item.id]},
            BatchResult(custom_id=request.custom_id, content=evaluation.model_dump_json()),
        )
        assert item.state == "completed"
        saved = session.get(Job, job_id).analysis
        assert saved.fit_score == 0.88
        assert any(
            "Future stored-schema checker" in warning
            for warning in saved.fit_diagnostics["diagnostic_checks"]["warnings"]
        )


@pytest.mark.parametrize("value", [None, True, "4", 4.0, -1, 5])
def test_missing_or_unusable_levels_are_not_invented(value):
    raw = minimal()
    raw["dimensions"]["missions"]["level"] = value
    with pytest.raises(LLMValidationError, match="missions"):
        read_fit_response(raw)


def test_duplicate_level_keys_require_unambiguous_values_and_keep_original_response():
    context, facts, _ = fixture()
    raw = '{"dimensions":{"missions":{"level":4,"level":4},"essentials":4,"responsibilities":3}}'
    public, diagnostic = evaluate_fit(raw, context, facts)
    assert public.fit_score == 0.95 and diagnostic["original_response"] == raw
    assert diagnostic["diagnostic_checks"]["warnings"]  # Direct levels omit annotation envelopes.
    with pytest.raises(LLMValidationError, match="ambiguous missions"):
        read_fit_response(raw.replace('"level":4,"level":4', '"level":1,"level":4'))
    # A duplicated annotation cannot erase a recognized business code.
    raw = raw[:-1] + ',"issues":[{"code":"material_core_question"}],"issues":[]}'
    public, diagnostic = evaluate_fit(raw, context, facts)
    assert public.fit_score == 0.79 and diagnostic["original_response"] == raw


@pytest.mark.parametrize("content", ["", "{}", '{"dimensions":', json.dumps({"fit_score": 0.8})])
def test_unusable_response_is_one_call_no_cache(isolated_db, monkeypatch, content):
    context, facts, _ = fixture()
    provider = OpenAIProvider()
    calls = []

    def api(**kwargs):
        calls.append(kwargs)
        return response(content, finish="length")

    monkeypatch.setattr(provider, "_call_openai", api)
    with pytest.raises(LLMValidationError):
        complete_fit(provider, context, facts)
    assert len(calls) == 1
    with session_scope() as session:
        assert list(session.scalars(select(LLMCache))) == []


def test_only_unusable_exact_cache_entry_is_refreshed(isolated_db, monkeypatch):
    context, facts, _ = fixture()
    key = make_cache_key(
        model=context["model"],
        system=context["system_prompt"],
        user=build_fit_user(context, facts),
        schema_name="JobFitEvaluation",
        extra={"temperature": 0.2, "schema": JobFitEvaluation.model_json_schema()},
    )
    with session_scope() as session:
        for cache_key in (key, "unrelated"):
            cache_set(
                session,
                cache_key=cache_key,
                model=context["model"],
                response="{}",
                prompt_tokens=1,
                completion_tokens=1,
            )
    provider = OpenAIProvider()
    calls = []

    def api(**kwargs):
        calls.append(kwargs)
        return response(json.dumps(minimal()))

    monkeypatch.setattr(provider, "_call_openai", api)
    assert complete_fit(provider, context, facts)[0].fit_score == 0.95
    assert complete_fit(provider, context, facts)[0].fit_score == 0.95
    assert len(calls) == 1
    with session_scope() as session:
        entries = {entry.cache_key: entry.response for entry in session.scalars(select(LLMCache))}
        assert entries["unrelated"] == "{}" and json.loads(entries[key]) == minimal()


@pytest.mark.parametrize("usable", [True, False])
def test_approved_rescore_preserves_previous_result_only_when_levels_unusable(isolated_db, usable):
    job_id = seeded_analysis()
    before = snapshot(job_id)
    plan = prepare_rescore_plan(job_ids=[job_id])
    calls = []
    raw = decision_response() if usable else {"fit_score": 0.99}

    def complete(**kwargs):
        calls.append(kwargs["purpose"])
        return raw

    results = execute_rescore_plan(plan, SimpleNamespace(complete_json=complete), approved=True)
    after = snapshot(job_id)
    assert calls == ["job_fit_assessment"]
    if not usable:
        assert results[0]["status"] == "failed_or_stale" and after == before
    else:
        assert results[0]["status"] == "rescored"
        assert after["analysis"]["fit_score"] == 0.95
        assert after["analysis"]["match_reasons"] == []
        assert "Ancienne réserve" not in after["analysis"]["risks"]
        assert after["analysis"]["fit_diagnostics"]["evaluation"] == raw
        for part in ("job", "score", "application", "documents"):
            assert after[part] == before[part]


@pytest.mark.parametrize("finish,refusal", [("length", None), ("stop", "Refused")])
def test_batch_transport_keeps_usable_levels_with_provider_flags(finish, refusal):
    content = json.dumps(minimal())
    line = {
        "custom_id": "fit",
        "response": {
            "status_code": 200,
            "body": {
                "choices": [
                    {"finish_reason": finish, "message": {"content": content, "refusal": refusal}}
                ]
            },
        },
    }
    parsed = parse_results(json.dumps(line))["fit"].validate(JobFitEvaluation)
    assert parsed.levels == (4, 4, 3) and parsed.original_text == content
    assert parsed.observations


def test_batch_without_schema_or_levels_keeps_explicit_failure(isolated_db):
    job_id = seed_jobs()[0]
    with session_scope() as session:
        run, item, request, _, _ = batch_fixture(session, job_id)
        body = copy.deepcopy(request.body)
        del body["response_format"]
        import_result(
            session,
            run,
            "evaluation",
            {"body": body, "cache_key": request.cache_key, "item_ids": [item.id]},
            BatchResult(custom_id=request.custom_id, content='{"fit_score":0.99}'),
        )
        assert item.state == "evaluation_failed" and "Schéma historique" in item.error
        assert session.get(Job, job_id).analysis is None


def test_mixed_skill_and_bullet_sources_are_allowed_without_promoting_skills():
    context, facts, evaluation = fixture()
    skill_ref = evaluation.dimensions.essentials.findings[0].candidate_refs[0]
    evaluation.dimensions.missions.findings[0].candidate_refs.append(skill_ref)
    public, diagnostic = evaluate_fit(evaluation, context, facts)
    assert public.fit_score == 0.88 and diagnostic["diagnostic_checks"]["warnings"] == []
    assert "peuvent figurer ensemble" in context["system_prompt"]


def test_new_offer_pipeline_saves_score_despite_malformed_annotations(isolated_db, monkeypatch):
    from tests.test_two_stage_analysis import extraction_offer_result, insert_job, runner

    provider = OpenAIProvider()
    calls = []

    def api(**kwargs):
        calls.append(kwargs["response_format"]["json_schema"]["name"])
        if calls[-1] == "OfferExtractionOutput":
            return response(extraction_offer_result().model_dump_json())
        data = evaluation_from_user(kwargs["messages"][1]["content"]).model_dump(mode="json")
        data["dimensions"]["missions"]["findings"][0]["support"] = "unknown_status"
        data["issues"] = [{"code": "unknown_code"}]
        return response(json.dumps(data))

    monkeypatch.setattr(provider, "_call_openai", api)
    job_id = insert_job()
    runner(provider)._analyze_one(job_id=job_id)
    assert calls == ["OfferExtractionOutput", "JobFitDecisionEvaluation"]
    with session_scope() as session:
        saved = session.get(Job, job_id).analysis
        assert saved.fit_score == 0.88 and saved.fit_diagnostics["diagnostic_checks"]["warnings"]


def test_unknown_codes_do_not_change_the_business_barometer():
    assert calculate_fit_score(4, 4, 3, ["unknown_code"])[0] == 0.95
    assert POLICY["weights"] == [50, 30, 20]
    assert POLICY["caps"] == {
        "confirmed_constraint_conflict": "0.19",
        "distant_core_work": "0.39",
        "substantial_core_gap": "0.59",
        "unclear_core_work": "0.59",
        "material_core_question": "0.79",
    }
    assert POLICY["guards"] == {"missions_le_1": "0.39", "any_dimension_le_2": "0.59"}


def test_mock_provider_uses_the_same_nonblocking_reader():
    from smartapply.llm import MockLLMProvider

    context, facts, _ = fixture()
    raw = minimal()
    provider = MockLLMProvider({"job_fit_assessment": raw})
    public, diagnostic = complete_fit(provider, context, facts)
    assert public.fit_score == 0.95 and diagnostic["evaluation"] == raw


def test_future_stored_schema_checker_cannot_mutate_original_annotations(isolated_db, monkeypatch):
    job_id = seed_jobs()[0]

    class MutatingChecker:
        def __init__(self, schema):
            schema.clear()

        def iter_errors(self, raw):
            raw["dimensions"]["missions"]["level"] = 0
            raw["issues"] = [{"code": "confirmed_constraint_conflict"}]
            return []

    monkeypatch.setattr(
        "smartapply.pipeline.batch_analysis.results.Draft202012Validator", MutatingChecker
    )
    with session_scope() as session:
        run, item, request, _, evaluation = batch_fixture(session, job_id)
        original = copy.deepcopy(request.body)
        import_result(
            session,
            run,
            "evaluation",
            {"body": request.body, "cache_key": request.cache_key, "item_ids": [item.id]},
            BatchResult(custom_id=request.custom_id, content=evaluation.model_dump_json()),
        )
        saved = session.get(Job, job_id).analysis
        assert item.state == "completed" and saved.fit_score == 0.88
        assert saved.fit_diagnostics["evaluation"] == evaluation.model_dump(mode="json")
        assert request.body == original


def test_duplicate_issue_codes_cannot_hide_a_recognized_cap():
    context, facts, _ = fixture()
    content = (
        json.dumps(minimal())[:-1]
        + ',"issues":[{"code":"material_core_question","code":"unknown"}]}'
    )
    public, diagnostic = evaluate_fit(content, context, facts)
    assert public.fit_score == 0.79
    assert diagnostic["applied_caps"] == {"material_core_question": "0.79"}
    assert diagnostic["original_response"] == content


@pytest.mark.parametrize("outcome", ["exception", "bad_return", "mutation"])
def test_future_explanation_conversion_cannot_block_or_mutate_score(monkeypatch, outcome):
    context, facts, evaluation = fixture()
    raw = evaluation.model_dump(mode="json")

    def convert(data, candidate_context, offer):
        if outcome == "exception":
            raise LLMValidationError("Future explanation check")
        if outcome == "bad_return":
            return ([None], "invalid risks")
        data["issues"] = [{"code": "confirmed_constraint_conflict"}]
        candidate_context["candidate_facts"].clear()
        offer.main_tasks.clear()
        return ([], [])

    monkeypatch.setattr("smartapply.llm.fit_scoring._explanations", convert)
    public, diagnostic = evaluate_fit(raw, context, facts)
    assert public.fit_score == 0.88 and diagnostic["applied_caps"] == {}
    assert diagnostic["evaluation"] == raw
    assert diagnostic["context"] == context and diagnostic["offer_facts"] == facts.model_dump(
        mode="json"
    )
    if outcome != "mutation":
        assert diagnostic["diagnostic_checks"]["warnings"]
