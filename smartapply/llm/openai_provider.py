"""OpenAI implementation of LLMProvider with structured outputs and cache."""

from __future__ import annotations

import json
from typing import TypeVar

from openai import APIConnectionError, APITimeoutError, RateLimitError
from pydantic import BaseModel, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from smartapply.config import get_settings
from smartapply.database import session_scope
from smartapply.database.repository import (
    cache_get,
    cache_set,
    purge_expired_cache,
    record_usage,
)
from smartapply.llm.cache import make_cache_key
from smartapply.llm.provider import LLMError, LLMProvider, LLMValidationError
from smartapply.llm.usage import estimate_cost_usd
from smartapply.logging_setup import get_logger

logger = get_logger(__name__)

# Only these exception types should trigger a retry. Anything else (auth
# failures, validation errors, bad-request) must fail fast so the caller can
# fix the input or escalate.
_RETRYABLE_ERRORS = (APIConnectionError, APITimeoutError, RateLimitError)

T = TypeVar("T", bound=BaseModel)


def _to_strict_schema(model_cls: type[BaseModel]) -> dict:
    """Convert a Pydantic schema to OpenAI strict json_schema format.

    OpenAI's strict mode requires:
    - additionalProperties: false on every object
    - every property listed in `required`
    - no `$ref` to definitions at the root (we inline them when possible)
    """
    schema = model_cls.model_json_schema()

    def _walk(node: dict) -> None:
        if not isinstance(node, dict):
            return
        if node.get("type") == "object" or "properties" in node:
            node["additionalProperties"] = False
            props = node.get("properties", {})
            node["required"] = list(props.keys())
            for v in props.values():
                _walk(v)
        if "items" in node:
            _walk(node["items"])
        for defs_key in ("$defs", "definitions"):
            if defs_key in node:
                for v in node[defs_key].values():
                    _walk(v)

    _walk(schema)
    return schema


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self):
        self._settings = get_settings()
        self._client = None  # lazy

    @property
    def smart_model(self) -> str:
        return self._settings.openai_model_smart

    @property
    def cheap_model(self) -> str:
        return self._settings.openai_model_cheap

    def _client_lazy(self):
        if self._client is None:
            from openai import OpenAI

            api_key = self._settings.openai_api_key
            if not api_key:
                raise LLMError("OPENAI_API_KEY is not set")
            self._client = OpenAI(api_key=api_key)
        return self._client

    @retry(
        # Only retry transient infrastructure errors. Validation failures and
        # 4xx (bad request / quota exceeded) should fail fast.
        retry=retry_if_exception_type(_RETRYABLE_ERRORS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    def _call_openai(self, **kwargs):
        return self._client_lazy().chat.completions.create(**kwargs)

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: type[T],
        model: str | None = None,
        temperature: float = 0.2,
        purpose: str = "generic",
        job_id: int | None = None,
        use_cache: bool = True,
        refresh_cache: bool = False,
    ) -> T:
        model_name = model or self.cheap_model
        cache_key = make_cache_key(
            model=model_name,
            system=system,
            user=user,
            schema_name=schema.__name__,
            # The schema body is part of the exact-cache identity. Keeping only
            # its class name allowed stale JSON to survive schema migrations.
            extra={
                "temperature": temperature,
                "schema": schema.model_json_schema(),
            },
        )

        # --- Cache lookup ---
        cached_result: T | None = None
        cached_token_counts: tuple[int, int] | None = None
        if use_cache:
            try:
                with session_scope() as s:
                    purge_expired_cache(
                        s,
                        ttl_days=self._settings.llm_cache_ttl_days,
                    )
                    # A forced regeneration deliberately skips the exact
                    # response lookup, but its fresh result is written below.
                    cached = None if refresh_cache else cache_get(s, cache_key)
                    if cached is not None:
                        try:
                            validated = self._validate(cached.response, schema)
                        except LLMValidationError:
                            # A schema change can make a previously valid exact
                            # cache entry stale. Delete it so the paid refresh
                            # is reusable instead of paying again every run.
                            logger.warning(
                                "Discarding invalid LLM cache entry: %s [%s]",
                                purpose,
                                model_name,
                            )
                            s.delete(cached)
                        else:
                            logger.info("LLM cache hit: %s [%s]", purpose, model_name)
                            cached_result = validated
                            cached_token_counts = (
                                cached.prompt_tokens,
                                cached.completion_tokens,
                            )
            except Exception as e:
                logger.warning("Cache lookup failed (continuing): %s", e)
        if cached_result is not None and cached_token_counts is not None:
            # Telemetry must never turn a valid zero-cost cache hit into a paid
            # provider call, so record it independently and always return it.
            try:
                with session_scope() as s:
                    record_usage(
                        s,
                        purpose=purpose,
                        model=model_name,
                        prompt_tokens=cached_token_counts[0],
                        completion_tokens=cached_token_counts[1],
                        cost_usd=0.0,
                        cached=True,
                        job_id=job_id,
                    )
            except Exception as e:
                logger.warning("Cache-hit usage recording failed (continuing): %s", e)
            return cached_result

        # --- API call ---
        json_schema = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "strict": True,
                "schema": _to_strict_schema(schema),
            },
        }
        try:
            response = self._call_openai(
                model=model_name,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=json_schema,
                temperature=temperature,
                max_completion_tokens=self._settings.openai_max_completion_tokens,
                # Groups requests sharing the same stable system/schema prefix
                # on cache-friendly infrastructure. OpenAI prompt caching is
                # automatic; this key contains no candidate or job data.
                prompt_cache_key=f"elan:{purpose}:{schema.__name__}",
            )
        except Exception as e:
            raise LLMError(f"OpenAI call failed: {e}") from e

        content = response.choices[0].message.content or ""
        usage = response.usage
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        cached_prompt_tokens = getattr(prompt_details, "cached_tokens", 0) or 0
        cache_write_prompt_tokens = getattr(prompt_details, "cache_write_tokens", 0) or 0
        cost = estimate_cost_usd(
            model_name,
            prompt_tokens,
            completion_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            cache_write_prompt_tokens=cache_write_prompt_tokens,
        )

        # Validate before caching. Invalid output still incurred provider cost,
        # so usage is recorded independently from the cache write.
        try:
            validated = self._validate(content, schema)
        except LLMValidationError:
            self._record_api_usage(
                purpose=purpose,
                model=model_name,
                prompt_tokens=prompt_tokens,
                cached_prompt_tokens=cached_prompt_tokens,
                cache_write_prompt_tokens=cache_write_prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost,
                job_id=job_id,
            )
            raise

        # --- Usage + exact response cache ---
        self._record_api_usage(
            purpose=purpose,
            model=model_name,
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            cache_write_prompt_tokens=cache_write_prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            job_id=job_id,
        )
        if use_cache:
            try:
                with session_scope() as s:
                    cache_set(
                        s,
                        cache_key=cache_key,
                        model=model_name,
                        response=content,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        purpose=purpose,
                    )
            except Exception as e:
                logger.warning("Cache write failed (continuing): %s", e)

        return validated

    # -------------------- helpers --------------------

    @staticmethod
    def _validate(raw: str, schema: type[T]) -> T:
        try:
            return schema.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as e:
            raise LLMValidationError(f"Invalid LLM output for {schema.__name__}: {e}") from e

    @staticmethod
    def _record_api_usage(
        *,
        purpose: str,
        model: str,
        prompt_tokens: int,
        cached_prompt_tokens: int,
        cache_write_prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        job_id: int | None,
    ) -> None:
        try:
            with session_scope() as s:
                record_usage(
                    s,
                    purpose=purpose,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    cached_prompt_tokens=cached_prompt_tokens,
                    cache_write_prompt_tokens=cache_write_prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=cost_usd,
                    cached=False,
                    job_id=job_id,
                )
        except Exception as e:
            logger.warning("Usage recording failed (continuing): %s", e)
