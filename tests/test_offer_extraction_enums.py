"""Closed new outputs must not reinterpret existing offer facts or cache identities."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from smartapply.config import get_llm_settings
from smartapply.database import session_scope
from smartapply.database.analysis_batches import AnalysisBatchRun
from smartapply.database.models import Job
from smartapply.llm import (
    JobAnalysis,
    OfferAnalysis,
    OfferExtractionOutput,
    OpenAIProvider,
)
from smartapply.llm.fit_context import build_fit_context
from smartapply.llm.provider import LLMValidationError
from smartapply.llm_policy import resolve_route
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.batch_analysis.results import (
    item_request,
    new_item,
    validate_request_response,
)
from smartapply.pipeline.process.analysis_checkpoint import extraction_key
from smartapply.profile import get_profile
from tests.test_evidence_fit import explicit_fit_routes as _explicit_fit_routes
from tests.test_two_stage_analysis import insert_job, offer_result

SENIORITIES = ("junior", "mid", "senior", "lead", "manager", "mixed", "unknown")
LANGUAGES = ("fr", "en", "mixed_fr_en", "other")

explicit_fit_routes = _explicit_fit_routes


def output(**updates):
    return OfferExtractionOutput.model_validate(
        {
            **offer_result().model_dump(),
            "seniority": "unknown",
            "offer_language": "other",
            **updates,
        }
    )


@pytest.mark.parametrize("seniority", SENIORITIES)
def test_every_frozen_seniority_is_accepted(seniority):
    assert output(seniority=seniority).seniority == seniority


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_frozen_language_is_accepted(language):
    assert output(offer_language=language).offer_language == language


@pytest.mark.parametrize(
    "field,value",
    [
        ("seniority", ""),
        ("seniority", "expert"),
        ("seniority", "Senior"),
        ("offer_language", ""),
        ("offer_language", "French"),
        ("offer_language", "ja"),
        *[("offer_language", language) for language in ("de", "es", "it", "nl", "pt", "unknown")],
    ],
)
def test_new_outputs_refuse_values_outside_the_fixed_lists(field, value):
    with pytest.raises(ValidationError):
        output(**{field: value})


def test_language_missing_from_a_local_new_output_is_other_not_assumed_french():
    data = output().model_dump()
    del data["offer_language"]
    assert OfferExtractionOutput.model_validate(data).offer_language == "other"


def test_legacy_offer_and_assembled_analysis_keep_their_original_values():
    data = {**offer_result().model_dump(), "seniority": "Expert confirmé", "offer_language": "ja"}
    assert OfferAnalysis.model_validate(data).model_dump() == data
    old = JobAnalysis.model_validate({**data, "fit_score": 0.7, "match_reasons": [], "risks": []})
    assert old.seniority == "Expert confirmé" and old.offer_language == "ja"


def assert_closed_request(body):
    schema = body["response_format"]["json_schema"]
    expected_name = "OfferExtractionOutput"
    assert schema["name"] == expected_name and schema["strict"] is True
    assert schema["schema"]["properties"]["seniority"]["enum"] == list(SENIORITIES)
    assert schema["schema"]["properties"]["offer_language"]["enum"] == list(LANGUAGES)
    expected_fields = set(OfferAnalysis.model_fields)
    assert set(schema["schema"]["required"]) == expected_fields


def test_real_immediate_provider_sends_enums_and_reuses_the_exact_cache(isolated_db, monkeypatch):
    provider = OpenAIProvider(model="gpt-5.4-mini")
    calls = []

    def api(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=output().model_dump_json(), refusal=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(provider, "_call_openai", api)
    for _ in range(2):
        result = provider.complete_json(
            system="test",
            user="source",
            schema=OfferExtractionOutput,
            purpose="job_offer_extraction",
        )
        assert result.seniority == "unknown" and result.offer_language == "other"
    assert len(calls) == 1
    assert_closed_request(calls[0])


def test_new_batch_requests_and_checkpoints_use_the_closed_contract(isolated_db):
    job_id = insert_job()
    with session_scope() as session:
        job = session.get(Job, job_id)
        model = resolve_route(get_llm_settings(), "job_offer_extraction").model
        run = AnalysisBatchRun(
            id="new-contract",
            model=model,
            provider="openai",
            max_completion_tokens=6000,
            extraction_effort="medium",
            scope_context={"model": model, "provider": "openai"},
            profile_data=get_profile().model_dump(mode="json"),
            fit_context=build_fit_context(get_profile(), model, provider="openai"),
        )
        item = new_item(run.id, job, screening_context=run.scope_context)
        session.add(run)
        session.flush()
        session.add(item)
        session.flush()
        request = item_request(item, run, "extraction", 6000)
        assert_closed_request(request.body)
        assert request.cache_key == extraction_key(
            build_analyzer_input(job), model, effort="medium"
        )
        assert validate_request_response(request.body, output().model_dump_json()) == output()
        with pytest.raises(LLMValidationError):
            validate_request_response(request.body, offer_result().model_dump_json())
