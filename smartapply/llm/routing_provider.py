"""Route new calls using a single immutable settings snapshot per operation."""

from __future__ import annotations

from smartapply.config import LLMSettingsSnapshot
from smartapply.llm.provider import LLMProvider
from smartapply.llm.routes import CALL_BY_PURPOSE, resolve_route
from smartapply.llm_policy import CallConfigurationError, configuration_purpose


class RoutingProvider(LLMProvider):
    def __init__(self, settings):
        self._settings = settings
        self._providers = {}
        self.name = "routing"

    def for_purpose(self, purpose):
        purpose = configuration_purpose(purpose)
        if purpose not in self._providers:
            route = resolve_route(self._settings, purpose)
            self._providers[purpose] = self.for_request(route.provider, route.model)
        return self._providers[purpose]

    def for_request(self, provider, model):
        from smartapply.llm.factory import get_llm_provider

        return get_llm_provider(provider, model=model, settings=self._settings)

    def for_frozen_route(self, purpose, provider, model, reasoning, max_output_tokens):
        """Use the complete saved request route for a resumed operation."""
        from smartapply.llm.factory import get_llm_provider

        snapshot = LLMSettingsSnapshot(
            self._settings.runtime,
            {configuration_purpose(purpose): {
                "provider": provider,
                "model": model,
                "reasoning": reasoning,
                "max_output_tokens": max_output_tokens,
            }},
        )
        resolve_route(snapshot, purpose)
        return get_llm_provider(provider, model=model, settings=snapshot)

    @property
    def cheap_model(self):
        return self.for_purpose("job_offer_extraction").cheap_model

    @property
    def smart_model(self):
        return self.for_purpose("application_texts").smart_model

    def complete_json(self, **kwargs):
        purpose = kwargs.get("purpose", "generic")
        if purpose in CALL_BY_PURPOSE:
            route = resolve_route(self._settings, purpose)
            requested = kwargs.get("model")
            if requested is not None and requested != route.model:
                raise CallConfigurationError(
                    purpose, "model", "le modèle demandé diffère du réglage de cet appel"
                )
            provider = self.for_purpose(purpose)
            kwargs["model"] = route.model
        else:
            raise ValueError(f"Aucune configuration par appel IA pour {purpose!r}.")
        return provider.complete_json(**kwargs)
