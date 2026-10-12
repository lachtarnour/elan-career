"""Strict Claude fit transport, scored decisions and durable raw replies, offline only."""

from __future__ import annotations

import copy
import itertools
import json
from dataclasses import asdict
from unittest.mock import MagicMock

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from smartapply.database import session_scope
from smartapply.database.models import LLMCache
from smartapply.llm.anthropic_batch import AnthropicBatchTransport, parse_anthropic_results
from smartapply.llm.anthropic_fit_transport import (
    FORMAT,
    GROUPED_KEY,
    MARKER,
    encode_fit_transport,
    encode_legacy_fit_transport,
    fit_transport_schema,
)
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.anthropic_transport import build_message_params
from smartapply.llm.batch import BatchResult, build_batch_request, encode_requests
from smartapply.llm.fit_context import build_fit_context
from smartapply.llm.fit_response import read_fit_response, schema_observations
from smartapply.llm.fit_scoring import calculate_fit_decisions, evaluate_fit
from smartapply.llm.provider import LLMError, LLMValidationError
from smartapply.llm.schemas import JobFitDecisionEvaluation
from smartapply.llm.structured import request_body, to_strict_schema
from smartapply.pipeline.batch_analysis.results import validate_request_response
from smartapply.profile import get_profile
from tests.llm_config_helpers import configure_calls
from tests.test_anthropic_batch import METADATA, PREFIX, snapshot, success
from tests.test_anthropic_provider import message
from tests.test_batch_analysis import offer

STATES = ("very_low_support", "weak_support", "partial_core_fit", "credible_fit", "strong_fit")


def canonical():
    finding = {
        "offer_refs": ["main_tasks[0]", "required_skills[0]", 'ref / é\\"\n'],
        "candidate_refs": ["experiences[0].bullets[0]", "languages[0]"],
        "text": "Python ou Julia ; preuve partielle, qualification non documentée.",
    }
    supports = (
        "direct",
        "transferable",
        "declared_only",
        "partial_support",
        "not_documented",
        "contradicted",
    )
    value = {
        "decision": dict(
            zip(("missions", "essentials", "responsibilities"), STATES[2:], strict=True)
        ),
        "dimensions": {
            axis: {
                "findings": [{**copy.deepcopy(finding), "support": support} for support in supports]
            }
            for axis in ("missions", "essentials", "responsibilities")
        },
        "reliability": {
            "offer_information": "Alternative ou obligation à préciser",
            "candidate_information": "Qualification non documentée, pas incompatible",
            "traceability": "Références conservées exactement",
            "business_uncertainties": ["Un transfert reste à vérifier", "é / \n"],
        },
        "issues": [
            {**copy.deepcopy(finding), "axis": axis, "origin": origin}
            for axis, origin in itertools.product(
                ("missions", "essentials", "responsibilities", "context"),
                ("candidate_evidence", "offer_information", "preference_alignment"),
            )
        ],
        "offer_interpretations": [
            {
                "offer_refs": finding["offer_refs"],
                "source_ref": "offer_source:body",
                "source_quote": "Python ou Julia. Kubernetes apprécié.",
                "meaning": meaning,
                "origin": "offer_information",
                "text": finding["text"],
            }
            for meaning in ("mandatory", "optional", "alternative", "conditional", "uncertain")
        ],
        "neutral_offer_refs": [
            {
                "offer_ref": "experience_requirements[0]",
                "candidate_refs": finding["candidate_refs"],
                "reason": reason,
                "scope": scope,
            }
            for reason, scope in itertools.product(
                (
                    "years_only",
                    "explicit_exemption",
                    "optional_only",
                    "no_declared_preference",
                    "compatible_preference",
                ),
                ("whole_entry", "years_component"),
            )
        ],
    }
    JobFitDecisionEvaluation.model_validate(value)
    return value


def body():
    return request_body(
        system="système inchangé é\n",
        user="profil/offre inchangés",
        schema=JobFitDecisionEvaluation,
        model="claude-haiku-5-5",
        temperature=0.2,
        max_completion_tokens=32768,
        purpose="job_fit_assessment",
        effort="high",
        provider="anthropic",
    )


def raw_wire(value=None):
    return json.dumps(encode_fit_transport(value or canonical()), ensure_ascii=False)


@pytest.mark.parametrize("states", list(itertools.product(STATES, repeat=3)))
@pytest.mark.parametrize("null_reliability", [False, True])
def test_all_axis_combinations_and_nullable_reliability_roundtrip_losslessly(
    states, null_reliability
):
    value = canonical()
    value["decision"] = dict(zip(value["decision"], states, strict=True))
    if null_reliability:
        value["reliability"] = None
    before = copy.deepcopy(value)
    wire = encode_fit_transport(value)
    source = body()["response_format"]["json_schema"]
    schema = fit_transport_schema(source["schema"], name=source["name"])
    Draft202012Validator(schema).validate(wire)
    text = json.dumps(wire, ensure_ascii=False)
    parsed = read_fit_response(text)
    assert parsed.raw == before and value == before
    assert parsed.original_text == text and parsed.observations == []
    assert JobFitDecisionEvaluation.model_validate(parsed.raw).model_dump(mode="json") == before
    assert calculate_fit_decisions(parsed.decisions) == calculate_fit_decisions(tuple(states))


def test_empty_annotations_remain_empty_without_inventing_explanations():
    value = canonical()
    for group in value["dimensions"].values():
        group["findings"] = []
    for key in ("issues", "neutral_offer_refs", "offer_interpretations"):
        value[key] = []
    value["reliability"]["business_uncertainties"] = []
    assert read_fit_response(raw_wire(value)).raw == value


def test_native_schema_has_no_unions_optional_properties_or_unused_definitions():
    request = body()
    frozen = copy.deepcopy(request)
    params = build_message_params(request)
    schema = params["output_config"]["format"]["schema"]

    def walk(node):
        if isinstance(node, dict):
            assert not {"anyOf", "oneOf", "allOf", "maxItems"}.intersection(node)
            assert not isinstance(node.get("type"), list)
            if node.get("type") == "object":
                assert set(node["required"]) == set(node["properties"])
                assert node["additionalProperties"] is False
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(schema)
    assert list(schema["properties"])[0] == "decision"
    assert set(schema["properties"]) == {
        "decision",
        GROUPED_KEY,
        "reliability_records",
    }
    assert "$defs" not in schema and "$ref" not in str(schema)
    assert params["system"][0]["text"] == request["messages"][0]["content"]
    assert params["messages"][0]["content"] == request["messages"][1]["content"]
    assert params["output_config"]["effort"] == "high"
    assert params["max_tokens"] == 32768 and "temperature" not in params
    assert "tools" not in params and "tool_choice" not in params and "thinking" not in params
    assert request == frozen


@pytest.mark.parametrize("legacy", [False, True])
def test_obsolete_annotations_are_preserved_raw_without_conversion(legacy):
    wire = encode_legacy_fit_transport(canonical()) if legacy else encode_fit_transport(canonical())
    obsolete = {
        "admissibility_records": [
            {"dimension": "mandatory_conditions", "status": "unknown", "findings": []}
        ],
        "mandatory_condition_records": [{"status": "compatible", "findings": []}],
        "candidate_preference_records": [{"status": "compatible", "findings": []}],
    }
    wire.update(copy.deepcopy(obsolete))
    raw = json.dumps(wire)
    parsed = read_fit_response(raw)
    assert "admissibility" not in parsed.raw
    assert all(parsed.raw[key] == value for key, value in obsolete.items())
    assert parsed.original_text == raw
    assert parsed.decisions == tuple(canonical()["decision"].values())
    assert schema_observations(parsed)


def test_unexpected_canonical_changes_fail_before_any_provider_call():
    request = body()
    request["response_format"]["json_schema"]["schema"]["properties"]["new_field"] = {
        "type": "string"
    }
    with pytest.raises(LLMError, match="Unsupported canonical fit schema"):
        build_message_params(request)


@pytest.mark.parametrize(
    "changes",
    [
        {"reliability_records": [{}, {}]},
        {"reliability_records": "unconvertible"},
        {"mission_findings": "unconvertible", "issues": [{"origin": "garbage"}]},
        {MARKER: "unknown-format"},
        {"dimensions": {"missions": "ambiguous"}},
        {"reliability": "ambiguous"},
        {"unknown_annotation": {"text": "Réserve à conserver"}},
    ],
)
def test_malformed_annotations_preserve_score_and_raw_evidence(monkeypatch, tmp_path, changes):
    configure_calls(
        monkeypatch,
        tmp_path,
        job_fit_assessment={
            "provider": "anthropic",
            "model": "claude-haiku-5-5",
            "reasoning": "high",
            "max_output_tokens": 32768,
        },
    )
    wire = encode_fit_transport(canonical())
    wire.update(copy.deepcopy(changes))
    raw = json.dumps(wire, ensure_ascii=False)
    context = build_fit_context(get_profile(), "claude-haiku-5-5", provider="anthropic")
    parsed = read_fit_response(raw)
    public, diagnostics = evaluate_fit(parsed, context, offer())
    assert public.fit_score == calculate_fit_decisions(parsed.decisions)[0]
    assert diagnostics["original_response"] == raw
    assert diagnostics["diagnostic_checks"]["warnings"]
    for key in changes:
        if key not in {MARKER, "mission_findings", "issues"}:
            assert key in parsed.raw, f"Lost {key}"


def test_unconstrained_cardinality_is_explicit_and_never_silently_repaired():
    wire = encode_fit_transport(canonical())
    wire["reliability_records"] *= 2
    native = build_message_params(body())["output_config"]["format"]["schema"]
    Draft202012Validator(native).validate(wire)  # API cannot enforce maxItems.
    parsed = read_fit_response(json.dumps(wire))
    assert parsed.raw["reliability_records"] == wire["reliability_records"]
    assert "reliability" not in parsed.raw
    assert any("cardinality" in warning for warning in parsed.observations)
    assert schema_observations(parsed)


def test_adapter_exception_cannot_veto_valid_decisions(monkeypatch):
    def failure(value):
        raise RuntimeError("unexpected annotation failure")

    monkeypatch.setattr("smartapply.llm.anthropic_fit_transport.decode_fit_transport", failure)
    raw = raw_wire()
    parsed = read_fit_response(raw)
    assert parsed.decisions == tuple(canonical()["decision"].values())
    assert parsed.original_text == raw and "unexpected annotation failure" in parsed.observations[0]


def test_immediate_one_call_then_cache_keeps_native_json(isolated_db, monkeypatch, tmp_path):
    configure_calls(
        monkeypatch,
        tmp_path,
        job_fit_assessment={
            "provider": "anthropic",
            "model": "claude-haiku-5-5",
            "reasoning": "high",
            "max_output_tokens": 32768,
        },
    )
    provider = AnthropicProvider(model="claude-haiku-5-5")
    raw = raw_wire()
    calls = []

    def mocked(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return message(raw, model="claude-haiku-5-5")

    monkeypatch.setattr(provider, "_call_anthropic", mocked)
    args = dict(system="s", user="u", schema=JobFitDecisionEvaluation, purpose="job_fit_assessment")
    first = provider.complete_json(**args)
    second = provider.complete_json(**args)
    assert first.raw == second.raw == canonical()
    assert first.original_text == second.original_text == raw
    assert len(calls) == 1 and calls[0]["output_config"]["effort"] == "high"
    with session_scope() as session:
        assert session.scalar(select(LLMCache)).response == raw


@pytest.mark.parametrize("stop", ["end_turn", "max_tokens", "refusal", "stream_interrupted"])
def test_batch_resume_uses_shared_reader_with_raw_journal_and_no_submission(stop):
    built = build_batch_request(
        custom_id="fit-42",
        **{
            "system": "s",
            "user": "u",
            "schema": JobFitDecisionEvaluation,
            "model": "claude-haiku-5-5",
            "max_completion_tokens": 32768,
            "purpose": "job_fit_assessment",
            "provider": "anthropic",
            "effort": "high",
        },
    )
    raw = raw_wire()
    client = MagicMock()
    transport = AnthropicBatchTransport(client)
    client.messages.batches.results.return_value = [
        success(PREFIX + "fit-42", stop_reason=stop, content=raw)
    ]
    result = transport.download("anthropic-results:msgbatch-42", metadata=METADATA)["fit-42"]
    assert result.content == raw and result.prompt_tokens == 110
    restored = BatchResult(**json.loads(json.dumps(asdict(result))))
    parsed = restored.validate(JobFitDecisionEvaluation)
    assert parsed.raw == canonical() and parsed.original_text == raw
    if stop != "end_turn":
        assert any("Provider" in warning for warning in parsed.observations)
    validated = validate_request_response(built.body, restored.content)
    assert validated.raw == canonical() and validated.observations == []
    client.messages.batches.create.assert_not_called()
    client.with_options.assert_not_called()


def test_native_batch_submission_and_immediate_call_use_identical_format():
    built = build_batch_request(
        custom_id="fit-42",
        system="s",
        user="u",
        schema=JobFitDecisionEvaluation,
        model="claude-haiku-5-5",
        max_completion_tokens=32768,
        purpose="job_fit_assessment",
        provider="anthropic",
        effort="high",
    )
    frozen = encode_requests([built])
    client = MagicMock()
    client.with_options.return_value.messages.batches.create.return_value = snapshot()
    transport = AnthropicBatchTransport(client)
    transport.create(transport.upload(frozen), METADATA, requests=[built])
    params = client.with_options.return_value.messages.batches.create.call_args.kwargs["requests"][
        0
    ]["params"]
    assert params == build_message_params(built.body)
    assert encode_requests([built]) == frozen
    client.with_options.return_value.messages.batches.create.assert_called_once()


def test_truncated_wire_and_ambiguous_decisions_keep_existing_safety():
    value = canonical()
    partial = json.dumps({"decision": value["decision"]})[:-1] + ',"elan_fit_transport":'
    parsed = read_fit_response(partial)
    assert parsed.decisions == tuple(value["decision"].values())
    assert parsed.original_text == partial and parsed.observations
    duplicate = (
        '{"decision":'
        + json.dumps(value["decision"])
        + ',"decision":{"missions":"weak_support","essentials":"credible_fit","responsibilities":"strong_fit"}}'
    )
    with pytest.raises(LLMValidationError, match="ambiguous"):
        read_fit_response(duplicate)


def test_historical_canonical_responses_remain_byte_for_byte_readable():
    raw = json.dumps(canonical(), ensure_ascii=False)
    parsed = read_fit_response(raw)
    assert parsed.raw == canonical() and parsed.original_text == raw and parsed.observations == []
    legacy_schema = to_strict_schema(JobFitDecisionEvaluation)
    del legacy_schema["properties"]["reliability"]
    assert fit_transport_schema(legacy_schema, name="JobFitDecisionEvaluation") == legacy_schema
    nonfit = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert fit_transport_schema(nonfit, name="OtherPurpose") == nonfit


def test_unordered_batch_results_match_marker_without_saved_schema():
    raw = raw_wire()
    results = parse_anthropic_results(
        [success(PREFIX + "fit-99", content=raw), success(PREFIX + "fit-42", content=raw)]
    )
    assert set(results) == {"fit-42", "fit-99"}
    assert all(
        result.validate(JobFitDecisionEvaluation).raw == canonical() for result in results.values()
    )


@pytest.mark.parametrize("suffix", ['"reliability_records":[', '"mission_findings":[{'])
def test_incomplete_container_is_not_interpreted_as_absence(suffix):
    raw = json.dumps({"decision": canonical()["decision"], MARKER: FORMAT})[:-1] + "," + suffix
    parsed = read_fit_response(raw)
    assert "reliability" not in parsed.raw
    assert parsed.original_text == raw and any(
        "not interpreted as absent" in item for item in parsed.observations
    )
    assert calculate_fit_decisions(parsed.decisions)[0] == 0.71


def test_duplicate_annotation_keys_are_not_silently_selected():
    wire = encode_fit_transport(canonical())
    raw = json.dumps(wire)[:-1] + ',"reliability_records":[]}'
    parsed = read_fit_response(raw)
    assert parsed.original_text == raw and GROUPED_KEY in parsed.raw
    assert any("duplicate annotation" in item for item in parsed.observations)
    assert parsed.decisions == tuple(canonical()["decision"].values())


@pytest.mark.parametrize(
    "changes",
    [
        {GROUPED_KEY: {"technical_findings": [{"axis": "unknown", "text": "Must remain visible"}]}},
        {GROUPED_KEY: {"technical_findings": "unreadable"}},
    ],
)
def test_grouping_never_drops_invalid_tags_or_fabricates_the_missing_dimension(changes):
    wire = encode_fit_transport(canonical())
    wire.update(copy.deepcopy(changes))
    raw = json.dumps(wire)
    parsed = read_fit_response(raw)
    assert parsed.original_text == raw and parsed.observations
    assert calculate_fit_decisions(parsed.decisions)[0] == 0.71
    for key, value in changes.items():
        assert parsed.raw[key] == value


def test_interleaved_findings_keep_order_within_each_business_axis():
    wire = encode_fit_transport(canonical())
    records = wire[GROUPED_KEY]["technical_findings"]
    wire[GROUPED_KEY]["technical_findings"] = [
        records[index] for index in (6, 0, 12, 7, 1, 13, 8, 2, 14, 9, 3, 15, 10, 4, 16, 11, 5, 17)
    ]
    parsed = read_fit_response(json.dumps(wire))
    assert parsed.raw == canonical()


def test_previous_flat_transport_is_readable_without_resubmission():
    raw = json.dumps(encode_legacy_fit_transport(canonical()))
    parsed = read_fit_response(raw)
    assert parsed.raw == canonical() and parsed.original_text == raw
    assert parsed.observations == []


def test_empty_grouped_findings_are_explicit_empty_containers_not_invented_judgements():
    wire = encode_fit_transport(canonical())
    wire[GROUPED_KEY]["technical_findings"] = []
    parsed = read_fit_response(json.dumps(wire))
    assert parsed.raw["dimensions"] == {
        axis: {"findings": []} for axis in ("missions", "essentials", "responsibilities")
    }
    assert parsed.decisions == tuple(canonical()["decision"].values())


def test_missing_record_list_does_not_invent_an_absent_block():
    wire = encode_fit_transport(canonical())
    del wire["reliability_records"]
    parsed = read_fit_response(json.dumps(wire))
    assert "reliability" not in parsed.raw and schema_observations(parsed)


def test_prompt_is_compact_only_for_anthropic_and_signed_legacy_context_is_supported(
    monkeypatch, tmp_path
):
    from smartapply.llm.fit_context import context_is_current, fingerprint
    from smartapply.llm.prompts.job_fit import build_system_prompt

    configure_calls(
        monkeypatch,
        tmp_path,
        job_fit_assessment={
            "provider": "anthropic",
            "model": "claude-haiku-5-5",
            "reasoning": "high",
            "max_output_tokens": 32768,
        },
    )
    profile = get_profile()
    ctx = build_fit_context(profile, "claude-haiku-5-5", provider="anthropic")
    assert ctx["system_prompt"] == build_system_prompt(provider="anthropic")
    assert ctx["system_prompt"] != build_system_prompt(provider="openai")
    assert len(ctx["system_prompt"].split()) < len(build_system_prompt().split())
    legacy = copy.deepcopy(ctx)
    legacy["system_prompt"] = build_system_prompt()
    legacy["prompt_fingerprint"] = fingerprint(
        {"system": legacy["system_prompt"], "user_template": legacy["user_template"]}
    )
    legacy["context_fingerprint"] = fingerprint(
        {key: value for key, value in legacy.items() if key != "context_fingerprint"}
    )
    frozen = copy.deepcopy(legacy)
    assert context_is_current(legacy, profile)
    assert legacy == frozen
    legacy["system_prompt"] += " Unknown business modification."
    legacy["prompt_fingerprint"] = fingerprint(
        {"system": legacy["system_prompt"], "user_template": legacy["user_template"]}
    )
    legacy["context_fingerprint"] = fingerprint(
        {key: value for key, value in legacy.items() if key != "context_fingerprint"}
    )
    assert not context_is_current(legacy, profile)


def test_partially_decoded_invalid_annotations_are_idempotent():
    wire = encode_fit_transport(canonical())
    wire[GROUPED_KEY]["unexpected_record"] = {"text": "Keep this reservation"}
    parsed = read_fit_response(json.dumps(wire))
    second = read_fit_response(parsed)
    assert second.raw == parsed.raw
    assert second.observations == parsed.observations
    assert second.decisions == parsed.decisions
