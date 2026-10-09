"""Shared exact cache, validation and telemetry for structured providers."""

from __future__ import annotations

from abc import abstractmethod

from smartapply.config import get_llm_settings
from smartapply.database import write_session_scope
from smartapply.database.repository import cache_get, cache_set, purge_expired_cache, record_usage
from smartapply.llm.cache import make_cache_key
from smartapply.llm.provider import LLMError, LLMProvider, LLMValidationError
from smartapply.llm.request_options import (
    request_cache_extra,
    stage_effort,
    validate_request_effort,
)
from smartapply.llm.structured import Completion, request_body, validate_completion, validate_output
from smartapply.llm.usage import estimate_cost_usd
from smartapply.llm_policy import (
    CALL_BY_PURPOSE,
    CallConfigurationError,
    configuration_purpose,
    resolve_route,
)
from smartapply.logging_setup import get_logger

logger = get_logger(__name__)


class StructuredProvider(LLMProvider):
    """One business contract; subclasses only implement their API transport."""

    def __init__(self, *, settings=None, model=None):
        self._settings = settings or get_llm_settings()
        self._model = model
        self._client = None

    def _configured_model(self):
        if not self._model:
            raise LLMError("Modèle IA non spécifié pour cet appel.")
        return self._model

    @abstractmethod
    def _generate(self, body: dict) -> Completion: ...

    _validate = staticmethod(validate_output)

    @staticmethod
    def _usage_writer(session, **kwargs):
        return record_usage(session, **kwargs)

    def _record_api_usage(self, **kwargs):
        try:
            with write_session_scope(notify=False) as session:
                self._usage_writer(session, provider=self.name, cached=False, **kwargs)
        except Exception as exc:
            logger.warning("Usage recording failed (continuing): %s", exc)

    def complete_json(
        self,
        *,
        system,
        user,
        schema,
        model=None,
        temperature=0.2,
        purpose="generic",
        job_id=None,
        use_cache=True,
        refresh_cache=False,
        max_completion_tokens=None,
        effort="auto",
    ):
        try:
            if purpose not in CALL_BY_PURPOSE:
                raise ValueError(f"Appel IA inconnu : {purpose}.")
            route = resolve_route(self._settings, purpose)
            configuration = configuration_purpose(purpose)
            model_name = (
                model if model is not None else
                self._model if self._model is not None else route.model
            )
            if not model_name:
                raise CallConfigurationError(purpose, "model", "modèle manquant")
            if (self.name, model_name) != (route.provider, route.model):
                raise ValueError(
                    f"Configuration IA — {purpose} : le fournisseur ou modèle demandé diffère "
                    f"de model_config.json → {configuration}.model."
                )
            if effort == "auto":
                effort = stage_effort(self.name, model_name, purpose, settings=self._settings)
            validate_request_effort(self.name, model_name, effort)
            if effort != route.reasoning:
                raise ValueError(
                    f"Configuration IA — {purpose} : niveau de raisonnement différent de "
                    f"model_config.json → {configuration}.reasoning."
                )
            if max_completion_tokens is None:
                max_completion_tokens = route.max_output_tokens
            elif max_completion_tokens != route.max_output_tokens:
                raise ValueError(
                    f"Configuration IA — {purpose} : limite de sortie différente de "
                    f"model_config.json → {configuration}.max_output_tokens."
                )
        except ValueError as exc:
            raise LLMError(str(exc)) from exc
        key = make_cache_key(
            provider=self.name,
            model=model_name,
            system=system,
            user=user,
            schema_name=schema.__name__,
            extra=request_cache_extra(schema, temperature, effort),
        )
        cached_result = None
        cached_tokens = None
        if use_cache:
            try:
                with write_session_scope(notify=False) as session:
                    purge_expired_cache(session, ttl_days=self._settings.llm_cache_ttl_days)
                    entry = None if refresh_cache else cache_get(session, key)
                    if entry is not None:
                        try:
                            cached_result = self._validate(entry.response, schema)
                        except LLMValidationError:
                            logger.warning(
                                "Discarding invalid LLM cache entry: %s [%s]", purpose, model_name
                            )
                            session.delete(entry)
                        else:
                            cached_tokens = (entry.prompt_tokens, entry.completion_tokens)
            except Exception as exc:
                logger.warning("Cache lookup failed (continuing): %s", exc)
        if cached_result is not None and cached_tokens is not None:
            # A usage/database error cannot convert this successful zero-cost
            # read into an additional provider request.
            try:
                with write_session_scope(notify=False) as session:
                    self._usage_writer(
                        session,
                        provider=self.name,
                        purpose=purpose,
                        model=model_name,
                        requested_model=model_name,
                        reasoning_effort=effort,
                        prompt_tokens=cached_tokens[0],
                        completion_tokens=cached_tokens[1],
                        cost_usd=0.0,
                        cached=True,
                        job_id=job_id,
                        cache_key=key,
                    )
            except Exception as exc:
                logger.warning("Cache-hit usage recording failed (continuing): %s", exc)
            return cached_result

        body = request_body(
            system=system,
            user=user,
            schema=schema,
            model=model_name,
            temperature=temperature,
            max_completion_tokens=max_completion_tokens,
            purpose=purpose,
            effort=effort,
            provider=self.name,
        )
        try:
            completion = self._generate(body)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"{self.name} call failed: {exc}") from exc
        usage = {
            "purpose": purpose,
            "model": completion.model or model_name,
            "requested_model": model_name,
            "reasoning_effort": effort,
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
            "cached_prompt_tokens": completion.cached_prompt_tokens,
            "cache_write_prompt_tokens": completion.cache_write_prompt_tokens,
            "cost_usd": estimate_cost_usd(
                completion.model or model_name,
                completion.prompt_tokens,
                completion.completion_tokens,
                cached_prompt_tokens=completion.cached_prompt_tokens,
                cache_write_prompt_tokens=completion.cache_write_prompt_tokens,
            ),
            "job_id": job_id,
        }
        try:
            result = validate_completion(completion, schema)
        except LLMValidationError:
            self._record_api_usage(**usage)
            raise
        self._record_api_usage(**usage, cache_key=key if use_cache else None)
        if use_cache:
            try:
                with write_session_scope(notify=False) as session:
                    cache_set(
                        session,
                        provider=self.name,
                        cache_key=key,
                        model=model_name,
                        response=completion.content,
                        prompt_tokens=completion.prompt_tokens,
                        completion_tokens=completion.completion_tokens,
                        purpose=purpose,
                    )
            except Exception as exc:
                logger.warning("Cache write failed (continuing): %s", exc)
        return result
