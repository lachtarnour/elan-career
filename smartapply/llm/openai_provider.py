"""OpenAI transport for the shared structured provider contract."""

from __future__ import annotations

from openai import APIConnectionError, APITimeoutError, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from smartapply.database.repository import record_usage
from smartapply.llm.provider import LLMError
from smartapply.llm.structured import Completion, to_strict_schema
from smartapply.llm.structured_provider import StructuredProvider

# Public compatibility for historical imports and frozen schema fingerprints.
_to_strict_schema = to_strict_schema
_RETRYABLE_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)


class OpenAIProvider(StructuredProvider):
    name = "openai"

    @property
    def smart_model(self) -> str:
        return self._configured_model()

    @property
    def cheap_model(self) -> str:
        return self._configured_model()

    def _client_lazy(self):
        if self._client is None:
            from openai import OpenAI

            if not self._settings.openai_api_key:
                raise LLMError("OPENAI_API_KEY is not set")
            # Application retry policy owns transient retries; do not multiply
            # them by the SDK's default retry loop.
            self._client = OpenAI(api_key=self._settings.openai_api_key, max_retries=0)
        return self._client

    @retry(
        retry=retry_if_exception_type(_RETRYABLE_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def _call_openai(self, **kwargs):
        return self._call_openai_once(**kwargs)

    def _call_openai_once(self, **kwargs):
        return self._client_lazy().chat.completions.create(**kwargs)

    @staticmethod
    def _usage_writer(session, **kwargs):
        # Preserve this established patch point for telemetry failure tests.
        return record_usage(session, **kwargs)

    def _generate(self, body):
        call = self._call_openai_once if getattr(self, "_disable_ambiguous_retries", False) else self._call_openai
        response = call(**body)
        choice = response.choices[0]
        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        return Completion(
            content=choice.message.content or "",
            model=getattr(response, "model", None) or body["model"],
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cached_prompt_tokens=getattr(details, "cached_tokens", 0) or 0,
            cache_write_prompt_tokens=getattr(details, "cache_write_tokens", 0) or 0,
            finish_reason=getattr(choice, "finish_reason", None),
            refusal=bool(getattr(choice.message, "refusal", None)),
        )
