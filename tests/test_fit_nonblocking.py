"""Global score validation preserves cache and existing dossiers on failure."""

import json
from types import SimpleNamespace

import pytest

from smartapply.llm import OpenAIProvider
from smartapply.llm.fit_request import complete_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.pipeline.rescore import execute_rescore_plan, prepare_rescore_plan
from tests.fit_fixtures import decision_response, global_response
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


@pytest.mark.parametrize(
    "content",
    ["", "{}", '{"fit_score":', json.dumps({"fit_score": 0.8}), json.dumps(decision_response())],
)
def test_invalid_global_response_is_one_call_and_no_automatic_retry(
    isolated_db, monkeypatch, content
):
    context, offer, _ = fixture()
    provider = OpenAIProvider(model="gpt-5.4-mini")
    calls = []

    def api(**kwargs):
        calls.append(kwargs)
        return response(content)

    monkeypatch.setattr(provider, "_call_openai", api)
    with pytest.raises(LLMValidationError):
        complete_fit(provider, context, offer)
    assert len(calls) == 1


def test_completed_global_response_is_reused_from_exact_cache(isolated_db, monkeypatch):
    context, offer, _ = fixture()
    provider = OpenAIProvider(model="gpt-5.4-mini")
    calls = []

    def api(**kwargs):
        calls.append(kwargs)
        return response(json.dumps(global_response(score=0.6137)))

    monkeypatch.setattr(provider, "_call_openai", api)
    for _ in range(2):
        public, _ = complete_fit(provider, context, offer)
        assert public.fit_score == 0.6137
    assert len(calls) == 1


@pytest.mark.parametrize("usable", [True, False])
def test_rescore_preserves_dossier_and_prior_analysis_on_invalid_global_response(
    isolated_db, usable
):
    job_id = seeded_analysis()
    before = snapshot(job_id)
    plan = prepare_rescore_plan(job_ids=[job_id])
    raw = global_response(score=0.6137) if usable else decision_response()
    result = execute_rescore_plan(
        plan, SimpleNamespace(name="openai", complete_json=lambda **kwargs: raw), approved=True
    )
    after = snapshot(job_id)
    if usable:
        assert result[0]["status"] == "rescored"
        assert after["analysis"]["fit_score"] == 0.6137
        for part in ("job", "score", "application", "documents"):
            assert after[part] == before[part]
    else:
        assert result[0]["status"] == "failed_or_stale"
        assert after == before
