"""Batch transport contracts, exercised without a key or network requests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from smartapply.llm.batch import (
    BATCH_ENDPOINT,
    BatchRequest,
    BatchSnapshot,
    OpenAIBatchTransport,
    build_batch_request,
    encode_requests,
    parse_results,
    split_requests,
)
from smartapply.llm.cache import make_cache_key
from smartapply.llm.provider import LLMError, LLMValidationError


class Extraction(BaseModel):
    role: str
    skills: list[str] = []


def request(custom_id: str = "job-42", *, model: str = "gpt-5.4-mini") -> BatchRequest:
    return build_batch_request(
        custom_id=custom_id,
        system="Extract only documented facts.",
        user="Ingénieur IA",
        schema=Extraction,
        model=model,
        max_completion_tokens=6000,
        purpose="job_analysis",
        temperature=0.2,
    )


def snapshot(**changes):
    return {
        "id": "batch-42",
        "input_file_id": "file-input",
        "status": "in_progress",
        "request_counts": {"total": 3, "completed": 1, "failed": 0},
        "metadata": {"elan_intent": "intent-42", "stage": "extraction"},
        "created_at": 123,
        "expires_at": 86523,
        **changes,
    }


def success(custom_id: str = "job-42", **body_changes):
    return {
        "custom_id": custom_id,
        "response": {
            "status_code": 200,
            "request_id": "req-42",
            "body": {
                "id": "chatcmpl-42",
                "model": "gpt-5.4-mini",
                "choices": [
                    {
                        "message": {"content": '{"role":"AI Engineer","skills":["Python"]}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
                **body_changes,
            },
        },
        "error": None,
    }


def as_jsonl(*records):
    return "\n".join(json.dumps(record) for record in records)


def test_request_preserves_sync_parameters_and_exact_cache_identity():
    built = request()
    expected = make_cache_key(
        model="gpt-5.4-mini",
        system="Extract only documented facts.",
        user="Ingénieur IA",
        schema_name="Extraction",
        extra={"temperature": 0.2, "schema": Extraction.model_json_schema()},
    )
    assert built.cache_key == expected
    assert built.body["messages"] == [
        {"role": "system", "content": "Extract only documented facts."},
        {"role": "user", "content": "Ingénieur IA"},
    ]
    assert built.body["max_completion_tokens"] == 6000
    assert built.body["temperature"] == 0.2
    assert built.body["prompt_cache_key"] == "elan:job_analysis:Extraction"
    strict = built.body["response_format"]["json_schema"]
    assert strict["strict"] is True
    assert strict["schema"]["additionalProperties"] is False
    assert strict["schema"]["required"] == ["role", "skills"]


def test_jsonl_encoding_keeps_unicode_and_endpoint():
    encoded = encode_requests([request()])
    assert "Ingénieur IA" in encoded.decode("utf-8")
    raw = json.loads(encoded)
    assert raw["url"] == BATCH_ENDPOINT
    assert raw["method"] == "POST"
    assert raw["custom_id"] == "job-42"
    assert encoded.endswith(b"\n")


@pytest.mark.parametrize(
    ("requests", "kwargs", "message"),
    [
        ([], {}, "empty"),
        ([request(), request()], {}, "Duplicate"),
        ([request("a"), request("b", model="gpt-5.4")], {}, "single model"),
        ([request("a"), request("b")], {"max_requests": 1}, "exceeds"),
        ([request()], {"max_bytes": 1}, "exceeds"),
        ([BatchRequest("a", {"model": "gpt-5.4-mini", "stream": True})], {}, "stream"),
        (
            [BatchRequest("a", {"model": "gpt-5.4-mini", "temperature": float("nan")})],
            {},
            "Invalid",
        ),
    ],
)
def test_invalid_request_files_are_rejected(requests, kwargs, message):
    with pytest.raises(ValueError, match=message):
        encode_requests(requests, **kwargs)


def test_splitting_respects_count_byte_and_model_boundaries():
    requests = [
        request("a"),
        request("b"),
        request("c", model="gpt-5.4"),
        request("d", model="gpt-5.4"),
    ]
    one_size = len(encode_requests(requests[:1]))
    parts = split_requests(requests, max_requests=2, max_bytes=one_size + 100)
    assert [item.custom_id for part in parts for item in part] == ["a", "b", "c", "d"]
    assert len(parts) == 4
    for part in parts:
        assert len(encode_requests(part)) <= one_size + 100
    assert len(split_requests(requests, max_requests=2)) == 2


def test_splitting_does_not_allow_duplicates_across_files_or_oversize_requests():
    with pytest.raises(ValueError, match="Duplicate"):
        split_requests([request(), request()], max_requests=1)
    with pytest.raises(ValueError, match="on its own"):
        split_requests([request()], max_bytes=1)


def test_completed_snapshot_keeps_partial_failures_and_timestamps():
    value = BatchSnapshot.from_provider(
        snapshot(
            status="completed",
            output_file_id="file-output",
            error_file_id="file-errors",
            request_counts={"total": 3, "completed": 2, "failed": 1},
            in_progress_at=200,
            finalizing_at=300,
            completed_at=400,
        )
    )
    assert value.terminal
    assert value.request_counts == {"total": 3, "completed": 2, "failed": 1}
    assert value.failed == 1
    assert value.created_at == 123
    assert value.completed_at == 400
    assert value.error_file_id == "file-errors"


@pytest.mark.parametrize("status", ["completed", "expired", "cancelled", "failed"])
def test_terminal_states_can_retain_partial_output(status):
    value = BatchSnapshot.from_provider(snapshot(status=status, output_file_id="file-output"))
    assert value.terminal
    assert value.output_file_id == "file-output"


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "unexpected"},
        {"id": ""},
        {"request_counts": {"total": 2, "completed": 2, "failed": 1}},
        {"request_counts": {"total": -1}},
        {"created_at": "yesterday"},
    ],
)
def test_malformed_snapshot_is_not_treated_as_completed(changes):
    with pytest.raises(LLMError):
        BatchSnapshot.from_provider(snapshot(**changes))


def test_unordered_results_map_by_custom_id_and_validate_schema():
    results = parse_results(as_jsonl(success("job-99"), success("job-42")))
    assert set(results) == {"job-42", "job-99"}
    result = results["job-42"]
    assert result.validate(Extraction).role == "AI Engineer"
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50
    assert result.cached_prompt_tokens == 80
    assert result.response_id == "chatcmpl-42"
    assert result.request_id == "req-42"


def test_error_file_records_are_preserved_as_failed_results():
    raw = {
        "custom_id": "job-42",
        "response": None,
        "error": {"code": "batch_expired", "message": "Expired"},
    }
    result = parse_results(as_jsonl(raw))["job-42"]
    assert not result.successful
    assert result.error_code == "batch_expired"
    with pytest.raises(LLMValidationError, match="Expired"):
        result.validate(Extraction)


def test_http_failure_preserves_error_details():
    raw = {
        "custom_id": "job-42",
        "response": {
            "status_code": 429,
            "body": {"error": {"code": "rate_limit", "message": "Try later"}},
        },
        "error": None,
    }
    result = parse_results(as_jsonl(raw))["job-42"]
    assert result.error_code == "rate_limit"
    assert result.error_message == "Try later"


@pytest.mark.parametrize(
    ("choice", "code"),
    [
        ({"message": {"content": "{}", "refusal": "Refused"}, "finish_reason": "stop"}, "refusal"),
        ({"message": {"content": "{}"}, "finish_reason": "length"}, "incomplete_output"),
        ({"message": {"content": ""}, "finish_reason": "stop"}, "empty_output"),
    ],
)
def test_refused_or_incomplete_results_still_preserve_billable_usage(choice, code):
    result = parse_results(as_jsonl(success(choices=[choice])))["job-42"]
    assert result.error_code == code
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50


def test_invalid_json_content_is_never_validated_as_an_analysis():
    raw = success(choices=[{"message": {"content": "not JSON"}, "finish_reason": "stop"}])
    result = parse_results(as_jsonl(raw))["job-42"]
    with pytest.raises(LLMValidationError):
        result.validate(Extraction)
    assert result.prompt_tokens == 100


@pytest.mark.parametrize("content", ["{}", "not-json", '["wrong type"]', b"\xff"])
def test_malformed_result_files_fail_before_import(content):
    with pytest.raises(LLMError):
        parse_results(content)


def test_duplicate_result_id_is_never_silently_overwritten():
    with pytest.raises(LLMError, match="Duplicate"):
        parse_results(as_jsonl(success(), success()))


def test_create_upload_disable_post_retries_and_use_recovery_metadata():
    client = MagicMock()
    no_retry = client.with_options.return_value
    no_retry.files.create.return_value = {"id": "file-input"}
    no_retry.batches.create.return_value = snapshot(status="validating")
    transport = OpenAIBatchTransport(client)
    assert transport.upload(encode_requests([request()])) == "file-input"
    state = transport.create("file-input", {"elan_intent": "intent-42"})
    assert state.id == "batch-42"
    assert client.with_options.call_count == 2
    client.with_options.assert_called_with(max_retries=0)
    no_retry.batches.create.assert_called_once_with(
        input_file_id="file-input",
        endpoint=BATCH_ENDPOINT,
        completion_window="24h",
        metadata={"elan_intent": "intent-42"},
    )


def test_ambiguous_create_error_propagates_without_a_second_submission():
    client = MagicMock()
    client.with_options.return_value.batches.create.side_effect = TimeoutError("Outcome unknown")
    with pytest.raises(TimeoutError):
        OpenAIBatchTransport(client).create("file-input", {"elan_intent": "intent-42"})
    assert client.with_options.return_value.batches.create.call_count == 1


def test_recovery_preserves_all_matches_and_does_not_submit():
    client = MagicMock()
    client.batches.list.return_value = iter(
        [
            snapshot(id="batch-1", metadata={"elan_intent": "unrelated"}),
            snapshot(id="batch-2"),
            snapshot(id="batch-3", status="completed"),
        ]
    )
    matches = OpenAIBatchTransport(client).recover(
        {"elan_intent": "intent-42", "stage": "extraction"}
    )
    assert [item.id for item in matches] == ["batch-2", "batch-3"]
    client.batches.create.assert_not_called()
    client.batches.list.assert_called_once_with(limit=100)


def test_retrieve_download_and_cancel_use_the_existing_provider_ids():
    client = MagicMock()
    client.batches.retrieve.return_value = snapshot(
        status="completed", output_file_id="file-output"
    )
    client.batches.cancel.return_value = snapshot(status="cancelling")
    client.files.content.return_value = SimpleNamespace(text=as_jsonl(success()))
    transport = OpenAIBatchTransport(client)
    assert transport.retrieve("batch-42").terminal
    assert transport.download("file-output")["job-42"].successful
    assert transport.cancel("batch-42").status == "cancelling"
    client.batches.retrieve.assert_called_once_with("batch-42")
    client.files.content.assert_called_once_with("file-output")
    client.batches.cancel.assert_called_once_with("batch-42")


def test_lazy_client_bounds_every_call_and_does_not_retry_network_requests(monkeypatch):
    client = MagicMock()
    client.batches.retrieve.return_value = snapshot()
    constructor = MagicMock(return_value=client)
    monkeypatch.setattr("openai.OpenAI", constructor)
    monkeypatch.setattr(
        "smartapply.llm.batch.get_settings",
        lambda: SimpleNamespace(openai_api_key="test-only-key"),
    )
    transport = OpenAIBatchTransport()
    transport.retrieve("batch-42")
    transport.retrieve("batch-42")
    constructor.assert_called_once_with(
        api_key="test-only-key",
        timeout=30.0,
        max_retries=0,
    )
