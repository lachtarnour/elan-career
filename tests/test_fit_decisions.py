"""Global assessment contracts and isolation, without paid provider calls."""

import copy
from datetime import date
from types import SimpleNamespace

import pytest

from smartapply.llm import JobFitAssessment
from smartapply.llm.batch import BatchResult
from smartapply.llm.fit_context import build_fit_user, context_is_current, fingerprint, fit_schema
from smartapply.llm.fit_request import complete_fit
from smartapply.llm.fit_scoring import evaluate_fit
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.structured import Completion, validate_completion
from tests.fit_fixtures import decision_response, global_response
from tests.test_evidence_fit import explicit_fit_routes as _explicit_fit_routes
from tests.test_evidence_fit import fixture

explicit_fit_routes = _explicit_fit_routes


@pytest.mark.parametrize("score", [0.0, 0.1937, 0.6137, 0.8849, 1.0])
def test_llm_score_is_preserved_without_rounding_or_axis_weights(score):
    context, offer, _ = fixture()
    raw = global_response(score=score, reasons=["Appui précis sur la mission"])
    raw["risks"] = ["Exigence centrale à confirmer"]
    before = copy.deepcopy(raw)
    public, diagnostics = evaluate_fit(raw, context, offer)
    assert public.model_dump() == before
    assert diagnostics["evaluation"] == before
    assert diagnostics["score_source"] == "llm"
    assert not {"used_decisions", "used_levels", "admissibility", "raw_score"} & diagnostics.keys()
    assert raw == before


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"fit_score": 0.8},
        global_response(score=-0.1),
        global_response(score=1.1),
        decision_response(),
        {**global_response(), "decision": decision_response()["decision"]},
    ],
)
def test_incomplete_or_weighted_responses_cannot_publish_as_global_scores(raw):
    context, offer, _ = fixture()
    with pytest.raises(LLMValidationError):
        evaluate_fit(raw, context, offer)


def test_old_signed_weighted_context_cannot_reuse_a_global_response():
    context, offer, _ = fixture()
    context["policy"] = {"name": "evidence_fit", "weights": [50, 30, 20]}
    with pytest.raises(LLMValidationError, match="policy"):
        evaluate_fit(global_response(), context, offer)


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_global_request_uses_the_shared_rubric_and_full_profile(provider, monkeypatch, tmp_path):
    context, offer, _ = fixture()
    if provider == "anthropic":
        from smartapply.llm.fit_context import build_fit_context
        from smartapply.profile import get_profile
        from tests.llm_config_helpers import configure_calls

        configure_calls(
            monkeypatch,
            tmp_path,
            job_fit_assessment={
                "provider": provider,
                "model": "claude-haiku-5-5",
                "reasoning": "high",
                "max_output_tokens": 12000,
            },
        )
        context = build_fit_context(get_profile(), "claude-haiku-5-5", provider=provider)
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        return JobFitAssessment(**global_response(score=0.6137))

    public, diagnostics = complete_fit(
        SimpleNamespace(name=provider, complete_json=complete),
        context,
        offer,
        extraction_provenance={"analyzer_input": {"offer_body": "Frozen offer body"}},
    )
    assert len(calls) == 1
    assert calls[0]["schema"] is JobFitAssessment is fit_schema(context)
    assert public.fit_score == 0.6137
    assert "without arithmetic weights" in calls[0]["system"]
    assert "0.60–0.79" in calls[0]["system"]
    assert "CANDIDATE EVIDENCE REFERENCES" not in calls[0]["user"]
    assert "SCORING POLICY" in calls[0]["user"]
    assert diagnostics["offer_source"]["offer_body"] == "Frozen offer body"
    assert diagnostics["request_user"] == calls[0]["user"]


def test_reference_date_is_frozen_for_batch_requests_across_midnight(monkeypatch):
    from smartapply.llm import fit_context
    from smartapply.profile import get_profile

    context, offer, _ = fixture()
    before = build_fit_user(context, offer)

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return date(2099, 1, 1)

    monkeypatch.setattr(fit_context, "date", Tomorrow)
    assert context_is_current(context, get_profile())
    assert build_fit_user(context, offer) == before


@pytest.mark.parametrize("finish,refusal", [("length", False), ("stop", True)])
def test_global_fit_rejects_incomplete_or_refused_transport(finish, refusal):
    import json

    with pytest.raises(LLMValidationError):
        validate_completion(
            Completion(
                content=json.dumps(global_response()),
                model="test",
                finish_reason=finish,
                refusal=refusal,
            ),
            JobFitAssessment,
        )


def test_batch_error_does_not_publish_a_score_despite_valid_json():
    import json

    with pytest.raises(LLMValidationError):
        BatchResult(
            custom_id="fit", content=json.dumps(global_response()), error_code="incomplete_output"
        ).validate(JobFitAssessment)


def test_preserved_offer_facts_and_request_context_cannot_be_mutated_through_diagnostics():
    context, offer, _ = fixture()
    before = fingerprint(context), offer.model_dump()
    public, diagnostics = evaluate_fit(global_response(), context, offer)
    diagnostics["context"]["candidate_facts"]["identity"]["title"] = "Changed"
    diagnostics["offer_facts"]["main_tasks"].append("Changed")
    diagnostics["evaluation"]["risks"].append("Changed")
    assert (fingerprint(context), offer.model_dump()) == before
    assert public.risks == []
