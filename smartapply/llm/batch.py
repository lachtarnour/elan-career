"""OpenAI Batch transport for the existing structured analysis requests.

This module only handles request files and provider responses. The analysis
pipeline owns durable submission intent, polling, cache, usage and job updates.
In particular, ``create`` never retries a POST whose outcome may be unknown.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from pydantic import BaseModel

from smartapply.config import get_settings
from smartapply.llm.cache import make_cache_key
from smartapply.llm.fit_response import FitEvaluationResponse, read_fit_response
from smartapply.llm.openai_provider import OpenAIProvider, _to_strict_schema
from smartapply.llm.provider import LLMError, LLMValidationError
from smartapply.llm.schemas import JobFitEvaluation

T = TypeVar("T", bound=BaseModel)

BATCH_ENDPOINT = "/v1/chat/completions"
MAX_BATCH_REQUESTS = 50_000
# Keep below the documented 200 MB upload ceiling, using decimal megabytes.
MAX_BATCH_BYTES = 200_000_000
BATCH_STATUSES = frozenset(
    {
        "validating",
        "failed",
        "in_progress",
        "finalizing",
        "completed",
        "expired",
        "cancelling",
        "cancelled",
    }
)
TERMINAL_BATCH_STATUSES = frozenset({"failed", "completed", "expired", "cancelled"})


@dataclass(frozen=True)
class BatchRequest:
    custom_id: str
    body: dict[str, Any]
    cache_key: str = ""

    def as_record(self) -> dict[str, Any]:
        return {
            "custom_id": self.custom_id,
            "method": "POST",
            "url": BATCH_ENDPOINT,
            "body": self.body,
        }


def build_batch_request(
    *,
    custom_id: str,
    system: str,
    user: str,
    schema: type[BaseModel],
    model: str,
    max_completion_tokens: int,
    purpose: str,
    temperature: float = 0.2,
) -> BatchRequest:
    """Keep prompts, structured output and exact-cache identity unchanged."""
    if not custom_id or not model or max_completion_tokens <= 0:
        raise ValueError("Batch requests need an ID, a model and a positive token limit")
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "strict": True,
                "schema": _to_strict_schema(schema),
            },
        },
        "temperature": temperature,
        "max_completion_tokens": max_completion_tokens,
        "prompt_cache_key": f"elan:{purpose}:{schema.__name__}",
    }
    cache_key = make_cache_key(
        model=model,
        system=system,
        user=user,
        schema_name=schema.__name__,
        extra={"temperature": temperature, "schema": schema.model_json_schema()},
    )
    return BatchRequest(custom_id=custom_id, body=body, cache_key=cache_key)


def _request_line(request: BatchRequest) -> bytes:
    if not isinstance(request.custom_id, str) or not request.custom_id:
        raise ValueError("Every batch request needs a non-empty custom_id")
    if not isinstance(request.body, dict) or not request.body.get("model"):
        raise ValueError("Every batch request needs a model")
    if request.body.get("stream"):
        raise ValueError("Batch requests cannot stream their output")
    try:
        raw = json.dumps(
            request.as_record(), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid batch request {request.custom_id}: {exc}") from exc
    return (raw + "\n").encode("utf-8")


def encode_requests(
    requests: Iterable[BatchRequest],
    *,
    max_requests: int = MAX_BATCH_REQUESTS,
    max_bytes: int = MAX_BATCH_BYTES,
) -> bytes:
    """Build one JSONL file, rejecting duplicates and provider-limit violations."""
    if max_requests <= 0 or max_bytes <= 0:
        raise ValueError("Batch file limits must be positive")
    lines: list[bytes] = []
    seen: set[str] = set()
    models: set[str] = set()
    size = 0
    for request in requests:
        if request.custom_id in seen:
            raise ValueError(f"Duplicate batch custom_id: {request.custom_id}")
        line = _request_line(request)
        seen.add(request.custom_id)
        models.add(request.body["model"])
        size += len(line)
        if len(seen) > max_requests or size > max_bytes:
            raise ValueError(
                "Batch input exceeds the request count or file size limit; split the requests"
            )
        if len(models) > 1:
            raise ValueError("A batch input file must use a single model")
        lines.append(line)
    if not lines:
        raise ValueError("Cannot submit an empty batch")
    return b"".join(lines)


def split_requests(
    requests: Iterable[BatchRequest],
    *,
    max_requests: int = MAX_BATCH_REQUESTS,
    max_bytes: int = MAX_BATCH_BYTES,
) -> list[list[BatchRequest]]:
    """Split by model, count and encoded UTF-8 size without dropping a request."""
    if max_requests <= 0 or max_bytes <= 0:
        raise ValueError("Batch file limits must be positive")
    parts: list[list[BatchRequest]] = []
    current: list[BatchRequest] = []
    size = 0
    model: str | None = None
    seen: set[str] = set()
    for request in requests:
        if request.custom_id in seen:
            raise ValueError(f"Duplicate batch custom_id: {request.custom_id}")
        line = _request_line(request)
        if len(line) > max_bytes:
            raise ValueError(
                f"Batch request {request.custom_id} exceeds the file size limit on its own"
            )
        seen.add(request.custom_id)
        request_model = request.body["model"]
        if current and (
            len(current) >= max_requests or size + len(line) > max_bytes or model != request_model
        ):
            parts.append(current)
            current = []
            size = 0
        current.append(request)
        size += len(line)
        model = request_model
    if current:
        parts.append(current)
    return parts


@dataclass(frozen=True)
class BatchSnapshot:
    id: str
    status: str
    input_file_id: str
    output_file_id: str | None = None
    error_file_id: str | None = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    created_at: int | None = None
    in_progress_at: int | None = None
    expires_at: int | None = None
    finalizing_at: int | None = None
    completed_at: int | None = None
    failed_at: int | None = None
    expired_at: int | None = None
    cancelling_at: int | None = None
    cancelled_at: int | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    errors: dict[str, Any] | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_BATCH_STATUSES

    @property
    def request_counts(self) -> dict[str, int]:
        return {"total": self.total, "completed": self.completed, "failed": self.failed}

    @classmethod
    def from_provider(cls, value: Any) -> BatchSnapshot:
        raw = _as_mapping(value)
        status = raw.get("status")
        if status not in BATCH_STATUSES:
            raise LLMError(f"Unknown OpenAI batch status: {status!r}")
        if not raw.get("id") or not raw.get("input_file_id"):
            raise LLMError("OpenAI batch response is missing its batch or input file ID")
        counts = _as_mapping(raw.get("request_counts"))
        total = _nonnegative_int(counts.get("total"))
        completed = _nonnegative_int(counts.get("completed"))
        failed = _nonnegative_int(counts.get("failed"))
        if completed + failed > total:
            raise LLMError("OpenAI batch response has inconsistent request counts")
        names = (
            "created_at",
            "in_progress_at",
            "expires_at",
            "finalizing_at",
            "completed_at",
            "failed_at",
            "expired_at",
            "cancelling_at",
            "cancelled_at",
        )
        timestamps = {name: raw.get(name) for name in names}
        for value in timestamps.values():
            if value is not None:
                _nonnegative_int(value)
        return cls(
            id=raw["id"],
            status=status,
            input_file_id=raw["input_file_id"],
            output_file_id=raw.get("output_file_id"),
            error_file_id=raw.get("error_file_id"),
            total=total,
            completed=completed,
            failed=failed,
            metadata=dict(raw.get("metadata") or {}),
            errors=dict(_as_mapping(raw["errors"])) if raw.get("errors") else None,
            **timestamps,
        )


@dataclass(frozen=True)
class BatchResult:
    custom_id: str
    content: str = ""
    error_code: str | None = None
    error_message: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    cache_write_prompt_tokens: int = 0
    response_id: str | None = None
    request_id: str | None = None
    model: str | None = None

    @property
    def successful(self) -> bool:
        return self.error_code is None and self.error_message is None

    def validate(self, schema: type[T]) -> T | FitEvaluationResponse:
        if not self.successful:
            if schema is JobFitEvaluation and self.error_code in {"refusal", "incomplete_output"}:
                parsed = read_fit_response(self.content)
                return replace(
                    parsed, observations=[f"Provider {self.error_code}; usable levels retained"]
                )
            raise LLMValidationError(
                f"Batch request {self.custom_id} failed: {self.error_message or self.error_code}"
            )
        return OpenAIProvider._validate(self.content, schema)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if isinstance(value, BaseModel):
        return value.model_dump()
    raise LLMError("Malformed OpenAI batch response")


def _nonnegative_int(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LLMError("OpenAI batch response contains an invalid counter or timestamp")
    return value


def _parse_result(raw: Mapping[str, Any]) -> BatchResult:
    custom_id = raw.get("custom_id")
    if not isinstance(custom_id, str) or not custom_id:
        raise LLMError("OpenAI batch result has no custom_id")
    response = _as_mapping(raw.get("response"))
    body = _as_mapping(response.get("body"))
    usage = _as_mapping(body.get("usage"))
    details = _as_mapping(usage.get("prompt_tokens_details"))
    counts = {
        "prompt_tokens": _nonnegative_int(usage.get("prompt_tokens")),
        "completion_tokens": _nonnegative_int(usage.get("completion_tokens")),
        "cached_prompt_tokens": _nonnegative_int(details.get("cached_tokens")),
        "cache_write_prompt_tokens": _nonnegative_int(details.get("cache_write_tokens")),
    }
    if counts["cached_prompt_tokens"] > counts["prompt_tokens"]:
        raise LLMError("OpenAI batch result has inconsistent cached token counts")
    common = {
        "custom_id": custom_id,
        "response_id": body.get("id"),
        "request_id": response.get("request_id"),
        "model": body.get("model"),
        **counts,
    }
    error = _as_mapping(raw.get("error") or body.get("error"))
    if error or response.get("status_code") != 200:
        return BatchResult(
            **common,
            error_code=str(error.get("code") or f"http_{response.get('status_code', 'unknown')}"),
            error_message=str(error.get("message") or "OpenAI batch request failed"),
        )
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        return BatchResult(
            **common, error_code="invalid_response", error_message="Expected one completion choice"
        )
    choice = _as_mapping(choices[0])
    message = _as_mapping(choice.get("message"))
    content = message.get("content")
    if message.get("refusal"):
        return BatchResult(
            **common,
            content=content if isinstance(content, str) else "",
            error_code="refusal",
            error_message=str(message["refusal"]),
        )
    if choice.get("finish_reason") != "stop":
        return BatchResult(
            **common,
            content=content if isinstance(content, str) else "",
            error_code="incomplete_output",
            error_message=f"Completion ended with {choice.get('finish_reason')!r}",
        )
    if not isinstance(content, str) or not content.strip():
        return BatchResult(
            **common, error_code="empty_output", error_message="Completion returned no JSON content"
        )
    return BatchResult(**common, content=content)


def parse_results(content: str | bytes) -> dict[str, BatchResult]:
    """Map unordered output/error JSONL by ID; never silently overwrite an ID."""
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else content
    except UnicodeDecodeError as exc:
        raise LLMError("OpenAI batch result file is not UTF-8") from exc
    results: dict[str, BatchResult] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            result = _parse_result(_as_mapping(json.loads(line)))
        except (json.JSONDecodeError, LLMError) as exc:
            raise LLMError(f"Invalid batch result at line {number}: {exc}") from exc
        if result.custom_id in results:
            raise LLMError(f"Duplicate custom_id in batch output: {result.custom_id}")
        results[result.custom_id] = result
    return results


class OpenAIBatchTransport:
    """Small synchronous SDK boundary; polling is the caller's responsibility."""

    def __init__(self, client: Any = None):
        self._client = client

    def _client_lazy(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            api_key = get_settings().openai_api_key
            if not api_key:
                raise LLMError("OPENAI_API_KEY is not set")
            # Polling resumes later after a timeout. A bounded SDK call also
            # lets desktop shutdown wait for this worker without hanging for
            # the SDK's default timeout and retry sequence.
            self._client = OpenAI(api_key=api_key, timeout=30.0, max_retries=0)
        return self._client

    def upload(self, content: bytes) -> str:
        if not content or len(content) > MAX_BATCH_BYTES:
            raise ValueError("Batch input is empty or exceeds the file size limit")
        uploaded = (
            self._client_lazy()
            .with_options(max_retries=0)
            .files.create(
                file=("elan-analysis.jsonl", content, "application/jsonl"),
                purpose="batch",
            )
        )
        file_id = _as_mapping(uploaded).get("id")
        if not isinstance(file_id, str) or not file_id:
            raise LLMError("OpenAI batch upload returned no file ID")
        return file_id

    def create(self, input_file_id: str, metadata: dict[str, str]) -> BatchSnapshot:
        """Submit once; recover an uncertain outcome using persisted metadata."""
        if not input_file_id or not metadata:
            raise ValueError("Batch creation needs an input file and recovery metadata")
        raw = (
            self._client_lazy()
            .with_options(max_retries=0)
            .batches.create(
                input_file_id=input_file_id,
                endpoint=BATCH_ENDPOINT,
                completion_window="24h",
                metadata=metadata,
            )
        )
        return BatchSnapshot.from_provider(raw)

    def retrieve(self, batch_id: str) -> BatchSnapshot:
        return BatchSnapshot.from_provider(self._client_lazy().batches.retrieve(batch_id))

    def recover(self, metadata: dict[str, str]) -> list[BatchSnapshot]:
        """Find every matching intent; multiple matches must not be ignored."""
        if not metadata:
            raise ValueError("Batch recovery needs identifying metadata")
        matches = []
        # The SDK page iterator follows every next-page cursor automatically.
        for raw in self._client_lazy().batches.list(limit=100):
            values = _as_mapping(raw)
            provider_metadata = _as_mapping(values.get("metadata"))
            if all(provider_metadata.get(key) == value for key, value in metadata.items()):
                matches.append(BatchSnapshot.from_provider(values))
        return matches

    def download(self, file_id: str) -> dict[str, BatchResult]:
        response = self._client_lazy().files.content(file_id)
        return parse_results(response.text)

    def cancel(self, batch_id: str) -> BatchSnapshot:
        return BatchSnapshot.from_provider(self._client_lazy().batches.cancel(batch_id))
