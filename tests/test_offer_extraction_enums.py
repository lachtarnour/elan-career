"""Closed new outputs must not reinterpret existing offer facts or cache identities."""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from smartapply.config import get_settings
from smartapply.database import session_scope
from smartapply.database.analysis_batches import AnalysisBatchRun
from smartapply.database.analysis_checkpoints import AnalysisCheckpoint
from smartapply.database.models import Job
from smartapply.llm import JobAnalysis, OfferAnalysis, OfferExtractionOutput, OpenAIProvider
from smartapply.llm.batch import build_batch_request
from smartapply.llm.fit_context import build_fit_context
from smartapply.llm.provider import LLMValidationError
from smartapply.offers import build_analyzer_input
from smartapply.pipeline.batch_analysis.results import (
    accept_result,
    item_request,
    new_item,
    validate_request_response,
)
from smartapply.pipeline.process.analysis_checkpoint import extraction_key, load_extraction
from smartapply.profile import get_profile
from tests.test_two_stage_analysis import insert_job, offer_result

SENIORITIES = ("junior", "mid", "senior", "lead", "manager", "mixed", "unknown")
LANGUAGES = ("fr", "en", "de", "es", "it", "nl", "pt", "other", "unknown")


def output(**updates):
    return OfferExtractionOutput.model_validate(
        {**offer_result().model_dump(), "seniority": "unknown", "offer_language": "unknown", **updates}
    )


@pytest.mark.parametrize("seniority", SENIORITIES)
def test_every_frozen_seniority_is_accepted(seniority):
    assert output(seniority=seniority).seniority == seniority


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_frozen_language_is_accepted(language):
    assert output(offer_language=language).offer_language == language


@pytest.mark.parametrize(
    "field,value",
    [("seniority", ""), ("seniority", "expert"), ("seniority", "Senior"),
     ("offer_language", ""), ("offer_language", "French"), ("offer_language", "ja")],
)
def test_new_outputs_refuse_values_outside_the_fixed_lists(field, value):
    with pytest.raises(ValidationError):
        output(**{field: value})


def test_language_missing_from_a_local_new_output_is_unknown_not_assumed_french():
    data = output().model_dump()
    del data["offer_language"]
    assert OfferExtractionOutput.model_validate(data).offer_language == "unknown"


def test_legacy_offer_and_assembled_analysis_keep_their_original_values():
    data = {**offer_result().model_dump(), "seniority": "Expert confirmé", "offer_language": "ja"}
    assert OfferAnalysis.model_validate(data).model_dump() == data
    old = JobAnalysis.model_validate({**data, "fit_score": 0.7, "match_reasons": [], "risks": []})
    assert old.seniority == "Expert confirmé" and old.offer_language == "ja"


def assert_closed_request(body):
    schema = body["response_format"]["json_schema"]
    assert schema["name"] == "OfferExtractionOutput" and schema["strict"] is True
    assert schema["schema"]["properties"]["seniority"]["enum"] == list(SENIORITIES)
    assert schema["schema"]["properties"]["offer_language"]["enum"] == list(LANGUAGES)
    assert set(schema["schema"]["required"]) == set(OfferAnalysis.model_fields)


def test_real_immediate_provider_sends_enums_and_reuses_the_exact_cache(isolated_db, monkeypatch):
    provider = OpenAIProvider()
    calls = []

    def api(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                content=output().model_dump_json(), refusal=None))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=10),
        )

    monkeypatch.setattr(provider, "_call_openai", api)
    for _ in range(2):
        result = provider.complete_json(
            system="test", user="source", schema=OfferExtractionOutput,
            purpose="job_offer_analysis",
        )
        assert result.seniority == "unknown" and result.offer_language == "unknown"
    assert len(calls) == 1
    assert_closed_request(calls[0])


def test_new_batch_requests_and_checkpoints_use_the_closed_contract(isolated_db):
    job_id = insert_job()
    with session_scope() as session:
        job = session.get(Job, job_id)
        model = get_settings().openai_model_cheap
        run = AnalysisBatchRun(id="new-contract", model=model,
                               profile_data=get_profile().model_dump(mode="json"))
        item = new_item(run.id, job)
        session.add(run)
        session.flush()
        session.add(item)
        session.flush()
        request = item_request(item, run, "extraction", 6000)
        assert_closed_request(request.body)
        assert request.cache_key == extraction_key(build_analyzer_input(job), model)
        assert validate_request_response(request.body, output().model_dump_json()) == output()
        with pytest.raises(LLMValidationError):
            validate_request_response(request.body, offer_result().model_dump_json())


def test_legacy_batch_source_is_not_relabeled_with_the_new_extraction_key(isolated_db):
    job_id = insert_job()
    profile = get_profile()
    model = get_settings().openai_model_cheap
    with session_scope() as session:
        job = session.get(Job, job_id)
        run = AnalysisBatchRun(
            id="historical-contract", model=model, profile_data=profile.model_dump(mode="json"),
            fit_context=build_fit_context(profile, model),
        )
        item = new_item(run.id, job)
        session.add(run)
        session.flush()
        session.add(item)
        session.flush()
        old_request = build_batch_request(
            custom_id="old", system="historical system", user="historical source",
            schema=OfferAnalysis, model=model, purpose="job_offer_analysis",
            max_completion_tokens=6000,
        )
        old = offer_result()
        validated = validate_request_response(old_request.body, old.model_dump_json())
        assert validated == old
        accept_result(session, item, run, "extraction", validated,
                      request_body=old_request.body, request_cache_key=old_request.cache_key)
        current_key = extraction_key(build_analyzer_input(job), model)
        checkpoint = session.get(AnalysisCheckpoint, job_id)
        assert checkpoint.extraction_key == old_request.cache_key != current_key
        assert item.offer_data == old.model_dump(mode="json")
        assert item.analyzer_input == asdict(build_analyzer_input(job))
        assert load_extraction(session, job, current_key) is None
        assert item.offer_data == old.model_dump(mode="json")
