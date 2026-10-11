"""Fit decisions and frozen-source isolation; all provider responses are simulated."""

from __future__ import annotations

import ast
import copy
import hashlib
import itertools
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from smartapply.config import get_llm_settings
from smartapply.database import session_scope
from smartapply.database.analysis_batches import AnalysisBatchRun
from smartapply.database.models import Job, LLMCache
from smartapply.database.repository.llm_cache import cache_set
from smartapply.llm import (
    JobFitDecisionEvaluation,
    OfferAnalysis,
    OfferExtractionOutput,
    OpenAIProvider,
)
from smartapply.llm.batch import BatchResult, build_batch_request
from smartapply.llm.fit_context import (
    build_fit_context,
    build_fit_user,
    fingerprint,
    fit_source,
)
from smartapply.llm.fit_request import check_request_budget, complete_fit
from smartapply.llm.fit_response import read_fit_decisions
from smartapply.llm.fit_scoring import calculate_fit_decisions, evaluate_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.llm_policy import resolve_route
from smartapply.pipeline.batch_analysis.results import (
    accept_cache,
    accept_result,
    import_result,
    item_request,
    new_item,
    validate_request_response,
)
from smartapply.pipeline.process.analysis import _assemble_analysis
from smartapply.pipeline.rescore import execute_rescore_plan, prepare_rescore_plan
from smartapply.profile import get_profile
from tests.fit_fixtures import decision_response, evaluation_from_user
from tests.scope_fixtures import configured_scope_context
from tests.test_batch_analysis import offer, seed_jobs
from tests.test_evidence_fit import seeded_analysis, snapshot
from tests.test_fit_nonblocking import response


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


def inputs():
    context = build_fit_context(
        get_profile(), resolve_route(get_llm_settings(), "job_fit_assessment").model
    )
    facts = offer()
    provenance = {
        "analyzer_input": {
            "offer_body": "SOURCE_FIGÉE_UNIQUE : Python ou Julia ; Kubernetes apprécié.",
            "title": "AI Engineer",
            "source": "test",
        }
    }
    return context, facts, provenance


@pytest.mark.parametrize(
    "states,score",
    [
        (("strong_fit", "strong_fit", "credible_fit"), 0.95),
        (("strong_fit", "strong_fit", "partial_core_fit"), 0.90),
        (("weak_support", "strong_fit", "strong_fit"), 0.74),
        (("strong_fit", "very_low_support", "strong_fit"), 0.55),
        (("strong_fit", "strong_fit", "strong_fit"), 1.0),
        (("very_low_support", "very_low_support", "very_low_support"), 0),
        (("strong_fit", "credible_fit", "credible_fit"), 0.84),
    ],
)
def test_single_decision_table_and_one_rounding(states, score):
    public, diagnostic = evaluate_fit(decision_response(states=states), *inputs()[:2])
    assert public.fit_score == score
    assert diagnostic["used_decisions"] == dict(
        zip(("missions", "essentials", "responsibilities"), states, strict=True)
    )
    assert diagnostic["policy_name"] == "evidence_fit"
    assert not {"applied_caps", "limiting_caps", "preference_conflict"} & diagnostic.keys()


@pytest.mark.parametrize("levels", list(itertools.product(range(5), repeat=3)))
def test_all_125_combinations_use_only_the_ordinal_weighted_index(levels):
    from decimal import ROUND_HALF_UP, Decimal

    states = (
        "very_low_support",
        "weak_support",
        "partial_core_fit",
        "credible_fit",
        "strong_fit",
    )
    expected = (
        sum(Decimal(weight * level) for weight, level in zip((35, 45, 20), levels, strict=True))
        / 400
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    score, diagnostic = calculate_fit_decisions(tuple(states[level] for level in levels))
    assert score == float(expected)
    assert diagnostic["final_score"] == str(expected)
    assert diagnostic["policy_name"] == "evidence_fit"


@pytest.mark.parametrize(
    "annotations",
    [
        {},
        {"dimensions": 7, "issues": "invalid", "neutral_offer_refs": None},
        {
            "dimensions": {
                "missions": {"findings": [{"candidate_refs": ["absent"], "support": "invalid"}]}
            }
        },
        {"issues": [{"code": "confirmed_constraint_conflict"}, {"code": "substantial_core_gap"}]},
        {"fit_score": 0.01, "offer_interpretations": [{"source_quote": "invented"}]},
        {"admissibility": None, "reliability": None},
        {"admissibility": {"mandatory_conditions": 7}, "reliability": "invalid"},
        {
            "admissibility": {
                dimension: {"status": "incompatible", "findings": []}
                for dimension in ("mandatory_conditions", "candidate_preferences")
            },
        },
    ],
)
def test_annotations_cannot_change_score_or_add_caps(annotations):
    context, facts, provenance = inputs()
    raw = {**decision_response(), **annotations}
    public, diagnostic = evaluate_fit(raw, context, facts, extraction_provenance=provenance)
    assert public.fit_score == 0.95
    assert diagnostic["diagnostic_checks"]["warnings"]
    assert diagnostic["evaluation"] == raw


@pytest.mark.parametrize("function", ["schema_observations", "inspect_evaluation", "_explanations"])
def test_future_inspection_exception_or_mutation_never_changes_decision(monkeypatch, function):
    context, facts, provenance = inputs()
    raw = decision_response()
    before = copy.deepcopy((raw, context, facts.model_dump(), provenance))

    def broken(*args):
        if hasattr(args[0], "raw"):
            args[0].raw["decision"]["missions"] = "very_low_support"
        elif isinstance(args[0], dict):
            args[0].clear()
        else:
            args[0].decision.missions = "very_low_support"
        raise RuntimeError("Future diagnostic check failed")

    monkeypatch.setattr(f"smartapply.llm.fit_scoring.{function}", broken)
    result, diagnostic = complete_fit(
        SimpleNamespace(name="openai", complete_json=lambda **kwargs: raw),
        context,
        facts,
        extraction_provenance=provenance,
    )
    assert result.fit_score == 0.95
    assert (raw, context, facts.model_dump(), provenance) == before
    assert any(
        "Future diagnostic check failed" in text
        for text in diagnostic["diagnostic_checks"]["warnings"]
    )


@pytest.mark.parametrize(
    "suffix",
    [
        ',"issues":[{"text":"cut',
        ',"admissibility":',
        ',"reliability":',
        ',"offer_interpre',
        ',"iss',
        ',"trace',
        ',"\\u0069ss',
    ],
)
def test_annotations_truncated_after_three_decisions_remain_recoverable(suffix):
    content = json.dumps(decision_response())[:-1] + suffix
    parsed = read_fit_decisions(content)
    public, diagnostic = evaluate_fit(parsed, *inputs()[:2])
    assert public.fit_score == 0.95 and diagnostic["original_response"] == content


@pytest.mark.parametrize(
    "content",
    [
        '{"decision":{"missions":"strong_fit","essentials":"strong_fit"}}',
        '{"decision":{"missions":"strong_fit","essentials":"strong_fit","responsibilities":"str',
        json.dumps(decision_response()).replace('"credible_fit"', '"unsupported"'),
        json.dumps(decision_response()).replace('"strong_fit"', "4", 1),
        json.dumps(decision_response())[:-1] + ',"dec',
        json.dumps(decision_response())[:-2] + ',"respon',
        json.dumps(decision_response()).replace(
            '"missions": "strong_fit"', '"missions":"strong_fit","missions":"weak_support"'
        ),
    ],
)
def test_incomplete_or_contradictory_decisions_are_not_invented(content):
    with pytest.raises(LLMValidationError):
        read_fit_decisions(content)


def test_json_escapes_and_identical_duplicates_have_unambiguous_decisions():
    content = json.dumps(decision_response()).replace(
        '"missions": "strong_fit"', '"\\u006dissions":"strong_fit","missions":"strong_fit"'
    )
    assert read_fit_decisions(content).levels == (4, 4, 3)


def test_unrelated_policy_diagnostic_does_not_veto_three_valid_axes():
    context, facts, _ = inputs()
    context["policy"] = {"invalid_diagnostic": True}
    public, diagnostics = evaluate_fit(decision_response(), context, facts)
    assert public.fit_score == 0.95
    assert diagnostics["used_decisions"] == decision_response()["decision"]


def test_frozen_source_sent_once_and_snapshot_kept_even_if_caller_mutates():
    context, facts, provenance = inputs()
    original = copy.deepcopy(provenance)
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        provenance["analyzer_input"]["offer_body"] = "Changed during provider call"
        return decision_response()

    result, diagnostic = complete_fit(
        SimpleNamespace(name="openai", complete_json=complete),
        context,
        facts,
        extraction_provenance=provenance,
    )
    assert result.fit_score == 0.95 and len(calls) == 1
    call = calls[0]
    assert call["schema"] is JobFitDecisionEvaluation
    assert call["user"].count(original["analyzer_input"]["offer_body"]) == 1
    assert diagnostic["offer_source"] == fit_source(original)
    assert diagnostic["request_user"] == call["user"]
    assert diagnostic["offer_facts"] == facts.model_dump(mode="json")


def test_source_changes_fit_identity_without_changing_global_candidate_context():
    context, facts, provenance = inputs()
    before = copy.deepcopy(context)
    users = []
    keys = []
    for text in ("Python ou Julia", "Python et Julia", ""):
        saved = {"analyzer_input": {"offer_body": text}}
        user = build_fit_user(context, facts, extraction_provenance=saved)
        users.append(user)
        request = build_batch_request(
            custom_id="fit",
            system=context["system_prompt"],
            user=user,
            schema=JobFitDecisionEvaluation,
            model=context["model"],
            purpose="job_fit_assessment",
            max_completion_tokens=context["max_completion_tokens"],
        )
        keys.append(request.cache_key)
    assert len(set(keys)) == 3 and context == before
    assert '"available": false' in users[-1]
    assert context["schema_fingerprint"] == fingerprint(context["schema"])
    assert not {"schema_version", "prompt_version", "response_reader_version"} & context.keys()
    assert check_request_budget(context, users[0])["output_reserved"] == 12000


@pytest.mark.parametrize("path", ["cache", "batch"])
@pytest.mark.parametrize("admissibility", ["compatible", "unknown", "incompatible"])
def test_immediate_batch_and_cache_share_source_decisions_and_nonblocking_annotations(
    isolated_db, path, admissibility
):
    job_id = seed_jobs()[0]
    context = inputs()[0]
    with session_scope() as session:
        run = AnalysisBatchRun(
            id="decision-test",
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
        item.offer_data = offer().model_dump(mode="json")
        item.state = "evaluation_pending"
        session.add(item)
        session.flush()
        request = item_request(item, run, "evaluation", 6000)
        raw = {
            **decision_response(),
            "admissibility": {
                dimension: {"status": admissibility, "findings": []}
                for dimension in ("mandatory_conditions", "candidate_preferences")
            },
            "reliability": 42,
            "issues": [{"invalid": True}],
        }
        content = json.dumps(raw)[:-1] + ',"trace'
        provenance = {"analyzer_input": item.analyzer_input}
        immediate, diagnostic = complete_fit(
            SimpleNamespace(name="openai", complete_json=lambda **kwargs: content),
            context,
            offer(),
            extraction_provenance=provenance,
        )
        assert diagnostic["request_user"] == request.body["messages"][1]["content"]
        if path == "cache":
            cache_set(
                session,
                cache_key=request.cache_key,
                model=run.model,
                response=content,
                prompt_tokens=1,
                completion_tokens=1,
            )
            assert accept_cache(session, item, run, "evaluation", request)
        else:
            import_result(
                session,
                run,
                "evaluation",
                {"item_ids": [item.id], "body": request.body, "cache_key": request.cache_key},
                BatchResult(custom_id=request.custom_id, content=content),
            )
        session.flush()
        saved = session.get(Job, job_id).analysis
        assert saved.fit_score == immediate.fit_score == 0.95
        assert saved.fit_diagnostics["diagnostic_checks"]["warnings"]
        assert saved.fit_diagnostics["evaluation"]["admissibility"] == raw["admissibility"]
        assert saved.fit_diagnostics["offer_source"] == diagnostic["offer_source"]
        assert saved.fit_diagnostics["request_user"] == diagnostic["request_user"]
        assert (
            session.scalar(select(LLMCache).where(LLMCache.cache_key == request.cache_key)).response
            == content
        )


def test_changed_source_after_submission_cannot_publish_under_another_context(isolated_db):
    job_id = seed_jobs()[0]
    context = inputs()[0]
    with session_scope() as session:
        run = AnalysisBatchRun(
            id="source-test",
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
        item.offer_data = offer().model_dump(mode="json")
        session.add(item)
        session.flush()
        request = item_request(item, run, "evaluation", 6000)
        item.analyzer_input = {**item.analyzer_input, "offer_body": "A different frozen source"}
        with pytest.raises(LLMValidationError, match="autre extraction"):
            accept_result(
                session,
                item,
                run,
                "evaluation",
                read_fit_decisions(decision_response()),
                request_body=request.body,
            )
        assert session.get(Job, job_id).analysis is None


@pytest.mark.parametrize("missing", [False, True])
def test_rescore_preserves_history_or_atomically_saves_only_fit(isolated_db, missing):
    job_id = seeded_analysis()
    before = snapshot(job_id)
    plan = prepare_rescore_plan(job_ids=[job_id])
    raw = decision_response()
    if missing:
        del raw["decision"]["essentials"]
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        assert kwargs["schema"] is JobFitDecisionEvaluation
        return raw

    result = execute_rescore_plan(
        plan, SimpleNamespace(name="openai", complete_json=complete), approved=True
    )
    after = snapshot(job_id)
    assert len(calls) == 1 and calls[0]["purpose"] == "job_fit_assessment"
    if missing:
        assert result[0]["status"] == "failed_or_stale" and after == before
        return
    assert result[0]["status"] == "rescored"
    for section in ("job", "score", "application", "documents"):
        assert after[section] == before[section]
    for key in before["analysis"]["raw_response"].keys() - {"fit_score", "match_reasons", "risks"}:
        assert after["analysis"]["raw_response"][key] == before["analysis"]["raw_response"][key]
    assert prepare_rescore_plan(job_ids=[job_id])["entries"][0]["operation"] == "already_compatible"


@pytest.mark.parametrize("finish,refusal", [("length", None), ("stop", "flag"), (None, None)])
def test_immediate_extraction_acceptance_matches_fixed_main(
    isolated_db, monkeypatch, finish, refusal
):
    provider = OpenAIProvider(model="gpt-5.4-mini")
    raw = offer().model_dump()
    del raw["supporting_excerpts"]
    # main 6a9309d accepts immediate extraction solely from this Pydantic content.
    expected = OfferAnalysis.model_validate(raw)
    calls = []

    def api(**kwargs):
        calls.append(kwargs)
        return response(json.dumps(raw), finish=finish, refusal=refusal)

    monkeypatch.setattr(provider, "_call_openai", api)
    for _ in range(2):
        got = provider.complete_json(
            system="baseline", user="source", schema=OfferAnalysis, purpose="job_offer_extraction"
        )
        assert got == expected
    assert len(calls) == 1


def test_current_extraction_cache_survives_publication_error(isolated_db, monkeypatch):
    job_id = seed_jobs()[0]
    context = inputs()[0]
    with session_scope() as session:
        run = AnalysisBatchRun(
            id="extraction-test",
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
        session.add(item)
        session.flush()
        request = item_request(item, run, "extraction", 6000)
        raw = {**offer().model_dump(), "seniority": "unknown"}
        del raw["supporting_excerpts"]
        content = json.dumps(raw)
        expected = OfferExtractionOutput.model_validate_json(content)
        assert validate_request_response(request.body, content) == expected
        assert (
            BatchResult(custom_id="extract", content=content).validate(OfferExtractionOutput)
            == expected
        )
        cache_set(
            session,
            cache_key=request.cache_key,
            model=run.model,
            response=content,
            prompt_tokens=1,
            completion_tokens=1,
        )

        def publication_error(*args, **kwargs):
            raise ValueError("Publication failed")

        with monkeypatch.context() as patch:
            patch.setattr(
                "smartapply.pipeline.batch_analysis.results.accept_result", publication_error
            )
            with pytest.raises(ValueError, match="Publication failed"):
                accept_cache(session, item, run, "extraction", request)
        session.flush()
        assert (
            session.scalar(select(LLMCache).where(LLMCache.cache_key == request.cache_key)).response
            == content
        )
        assert accept_cache(session, item, run, "extraction", request)
        assert (
            item.offer_data == expected.model_dump(mode="json")
            and item.state == "evaluation_pending"
        )


@pytest.mark.parametrize("code", ["refusal", "incomplete_output", "provider_error"])
def test_batch_transport_errors_still_reject_extraction_but_not_usable_fit(code):
    result = BatchResult(custom_id="extract", content=offer().model_dump_json(), error_code=code)
    with pytest.raises(LLMValidationError):
        result.validate(OfferAnalysis)
    if code != "provider_error":
        result = BatchResult(
            custom_id="fit", content=json.dumps(decision_response()), error_code=code
        )
        assert result.validate(JobFitDecisionEvaluation).levels == (4, 4, 3)


def test_other_schema_acceptance_and_invalid_extraction_are_unchanged(isolated_db, monkeypatch):
    from smartapply.llm import JobFitAssessment

    provider = OpenAIProvider(model="gpt-5.4-mini")
    monkeypatch.setattr(provider, "_call_openai", lambda **kwargs: response("{}", finish="length"))
    with pytest.raises(LLMValidationError):
        provider.complete_json(
            system="test", user="source", schema=OfferAnalysis, purpose="job_offer_extraction"
        )
    content = JobFitAssessment(fit_score=0.8, match_reasons=[], risks=[]).model_dump_json()
    monkeypatch.setattr(
        provider, "_call_openai", lambda **kwargs: response(content, finish="length")
    )
    with pytest.raises(LLMValidationError, match="Incomplete"):
        provider.complete_json(
            system="test", user="source", schema=JobFitAssessment, purpose="job_fit_assessment"
        )


def test_reasons_include_each_positive_axis_and_generator_nonfit_inputs_are_preserved():
    context, facts, provenance = inputs()
    data = evaluation_from_user(build_fit_user(context, facts)).model_dump()
    for axis in ("missions", "essentials", "responsibilities"):
        first = copy.deepcopy(data["dimensions"][axis]["findings"][0])
        data["dimensions"][axis]["findings"] = [
            {**first, "text": f"{axis} preuve {i}"} for i in range(6)
        ]
    result, _ = evaluate_fit(data, context, facts, extraction_provenance=provenance)
    assert len(result.match_reasons) == 5
    assert all(
        any(axis in text for text in result.match_reasons)
        for axis in ("missions", "essentials", "responsibilities")
    )
    old = _assemble_analysis(
        facts,
        result.model_copy(update={"fit_score": 0.1, "match_reasons": ["Old"], "risks": ["Old"]}),
    )
    new = _assemble_analysis(facts, result)
    assert old.model_dump(exclude={"fit_score", "match_reasons", "risks"}) == new.model_dump(
        exclude={"fit_score", "match_reasons", "risks"}
    )


def test_historical_extraction_readers_and_source_input_remain_identical():
    # Prompt templates remain frozen; analyzer_input only reads the renamed
    # persisted source_company while keeping the external company payload.
    root = Path(__file__).resolve().parents[1]
    signatures = {
        "smartapply/llm/prompts/templates/job_analysis/user.j2": "1630d597e1c1e70a04971483fecf528af5c4b6b9be195d08494dadfad67892cc",
        "smartapply/llm/prompts/templates/job_analysis/source_metadata_block.j2": "5b6a532adf452ca4a1ffa90930c72a5f92fed841d56e5b06685495530d94cf17",
        "smartapply/llm/prompts/job_analysis.py": "714ffcb156b5e48fff49c4497dfe8e3ded67c54def8b9148285269569fffd4df",
        "smartapply/offers/analyzer_input.py": "3512eeeda1d568795daa53f1b884f33540e890f56cb7f3bfbde319c2c37b81c1",
    }
    for path, signature in signatures.items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == signature
    tree = ast.parse((root / "smartapply/llm/schemas.py").read_text())
    for name, signature in {
        "OfferAnalysis": "0ad29027fbee9ba29f7d4c14700405fe32a7c3fdda7ca65918de20c099a8105f",
        "JobAnalysis": "83a61904164c8a88a1e4f4cd31e2da8141f987367d47b8b3248eb92185262ad3",
    }.items():
        node = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name
        )
        assert hashlib.sha256(ast.dump(node).encode()).hexdigest() == signature


@pytest.mark.parametrize(
    "case,body",
    [
        ("optional", "Kubernetes : connaissances appréciées"),
        (
            "alternative",
            "Une ou plusieurs technologies parmi Python, FastAPI, TypeScript ou Node.js",
        ),
        (
            "mixed_tenure",
            "Trois ans d’expérience et maîtrise avancée de Kubernetes",
        ),
        (
            "qualification",
            "Bac + 5 obligatoire, formation initiale de trois mois, contrat initial de cinq ans",
        ),
    ],
)
def test_source_examples_check_inputs_rules_simulated_diagnostics_and_preserved_facts(case, body):
    context, facts, _ = inputs()
    if case == "optional":
        facts.required_skills = ["Kubernetes"]
    elif case == "alternative":
        facts.required_skills = ["Python", "FastAPI", "TypeScript", "Node.js"]
    else:
        facts.constraints = [body]
    original = facts.model_dump(mode="json")
    provenance = {"analyzer_input": {"offer_body": body}}
    user = build_fit_user(context, facts, extraction_provenance=provenance)
    data = evaluation_from_user(user).model_dump()
    if case in {"optional", "alternative"}:
        data["offer_interpretations"] = [
            {
                "offer_refs": [f"required_skills[{i}]" for i in range(len(facts.required_skills))],
                "source_ref": "offer_source:body",
                "source_quote": body,
                "meaning": case,
                "origin": "offer_information",
                "text": "Modalité explicitement citée",
            }
        ]
        if case == "optional":
            data["dimensions"]["essentials"]["findings"] = []
            data["neutral_offer_refs"] = [
                {
                    "offer_ref": "required_skills[0]",
                    "reason": "optional_only",
                    "scope": "whole_entry",
                    "candidate_refs": [],
                }
            ]
        else:
            finding = data["dimensions"]["essentials"]["findings"][0]
            finding["offer_refs"] = data["offer_interpretations"][0]["offer_refs"]
            data["dimensions"]["essentials"]["findings"] = [finding]
    elif case == "mixed_tenure":
        data["neutral_offer_refs"] = [
            {
                "offer_ref": "constraints[0]",
                "reason": "years_only",
                "scope": "years_component",
                "candidate_refs": [],
            }
        ]
    else:
        data["dimensions"]["essentials"]["findings"] = [
            finding
            for finding in data["dimensions"]["essentials"]["findings"]
            if "constraints[0]" not in finding["offer_refs"]
        ]
        data["admissibility"]["mandatory_conditions"] = {
            "status": "unknown",
            "findings": [
                {
                    "offer_refs": ["constraints[0]"],
                    "candidate_refs": [],
                    "status": "unknown",
                    "text": "Qualification obligatoire à confirmer",
                }
            ],
        }
        data["issues"] = [
            {
                "axis": "context",
                "origin": "offer_information",
                "offer_refs": ["constraints[0]"],
                "candidate_refs": [],
                "text": "Qualification obligatoire à confirmer",
            }
        ]
    public, diagnostic = complete_fit(
        SimpleNamespace(name="openai", complete_json=lambda **kwargs: data),
        context,
        facts,
        extraction_provenance=provenance,
    )
    assert user.count(body) >= 1
    assert diagnostic["offer_source"]["offer_body"] == body
    assert diagnostic["evaluation"] == data
    assert diagnostic["offer_facts"] == facts.model_dump(mode="json") == original
    assert public.fit_score == 0.84
    if case == "qualification":
        assert (
            diagnostic["evaluation"]["admissibility"]["mandatory_conditions"]["status"] == "unknown"
        )
    assert not any("not covered" in text for text in diagnostic["diagnostic_checks"]["warnings"])
    # A simulated diagnostic proves wiring and isolation, not future LLM judgement.


def test_invalid_source_citation_is_only_an_observation():
    context, facts, provenance = inputs()
    data = evaluation_from_user(build_fit_user(context, facts)).model_dump()
    data["offer_interpretations"] = [
        {
            "offer_refs": ["required_skills[0]"],
            "source_ref": "offer_source:body",
            "source_quote": "INVENTED QUOTE",
            "meaning": "optional",
            "origin": "offer_information",
            "text": "Invalid citation retained for audit",
        }
    ]
    public, diagnostic = evaluate_fit(data, context, facts, extraction_provenance=provenance)
    assert public.fit_score == 0.84
    assert any("quote not found" in text for text in diagnostic["diagnostic_checks"]["warnings"])
