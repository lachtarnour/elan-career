"""Permanent scoring boundary: annotations and future inspections cannot veto levels."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.models import Job, LLMCache
from smartapply.database.repository.llm_cache import cache_set
from smartapply.llm import JobFitDecisionEvaluation, OpenAIProvider
from smartapply.llm.cache import make_cache_key
from smartapply.llm.fit_context import build_fit_user
from smartapply.llm.fit_request import complete_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.request_options import request_cache_extra
from smartapply.pipeline.rescore import execute_rescore_plan, prepare_rescore_plan
from tests.fit_fixtures import decision_response, evaluation_from_user
from tests.test_evidence_fit import explicit_fit_routes as _explicit_fit_routes
from tests.test_evidence_fit import fixture, seeded_analysis, snapshot

explicit_fit_routes = _explicit_fit_routes


def response(content, *, finish="stop", refusal=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish, message=SimpleNamespace(content=content, refusal=refusal)
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
    )


@pytest.mark.parametrize("content", ["", "{}", '{"dimensions":', json.dumps({"fit_score": 0.8})])
def test_unusable_response_is_one_call_no_cache(isolated_db, monkeypatch, content):
    context, facts, _ = fixture()
    provider = OpenAIProvider(model="gpt-5.4-mini")
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


@pytest.mark.parametrize(
    "annotations",
    [
        {},
        {"admissibility": None, "reliability": None},
        {"admissibility": "broken", "reliability": 42, "dimensions": False, "issues": {}},
        {
            "admissibility": {
                dimension: {"status": status, "findings": []}
                for dimension, status in (
                    ("mandatory_conditions", "unknown"),
                    ("candidate_preferences", "incompatible"),
                )
            },
            "reliability": {
                "offer_information": "Condition obligatoire peu renseignée",
                "candidate_information": "Qualification non documentée",
                "traceability": "Référence importante à vérifier",
                "business_uncertainties": ["Qualification à confirmer"],
            },
        },
    ],
)
def test_annotations_never_refresh_usable_cache_or_trigger_another_call(
    isolated_db, monkeypatch, annotations
):
    context, facts, _ = fixture()
    key = make_cache_key(
        model=context["model"],
        system=context["system_prompt"],
        user=build_fit_user(context, facts),
        schema_name="JobFitDecisionEvaluation",
        extra=request_cache_extra(
            JobFitDecisionEvaluation, context["temperature"], context.get("effort")
        ),
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
    provider = OpenAIProvider(model="gpt-5.4-mini")
    calls = []
    raw = {**decision_response(), **annotations}
    content = json.dumps(raw)[:-1] + ',"reliab'  # Truncated annotation remains recoverable.

    def api(**kwargs):
        calls.append(kwargs)
        return response(content, finish="length")

    monkeypatch.setattr(provider, "_call_openai", api)
    for _ in range(2):
        public, diagnostics = complete_fit(provider, context, facts)
        assert public.fit_score == 0.95
        assert diagnostics["diagnostic_checks"]["warnings"]
        assert diagnostics["original_response"] == content
        if "admissibility" in raw:
            assert diagnostics["evaluation"]["admissibility"] == raw["admissibility"]
    assert len(calls) == 1
    with session_scope() as session:
        entries = {entry.cache_key: entry.response for entry in session.scalars(select(LLMCache))}
        assert entries["unrelated"] == "{}" and entries[key] == content


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

    results = execute_rescore_plan(
        plan, SimpleNamespace(name="openai", complete_json=complete), approved=True
    )
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


def test_new_offer_pipeline_saves_score_despite_malformed_annotations(isolated_db, monkeypatch):
    from tests.test_two_stage_analysis import extraction_offer_result, insert_job, runner

    provider = OpenAIProvider(model="gpt-5.4-mini")
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
        assert saved.fit_score == 0.84 and saved.fit_diagnostics["diagnostic_checks"]["warnings"]
