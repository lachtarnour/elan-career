"""LLM call catalogue, model capabilities and configuration resolution.

This module is independent of transports, database setup and Qt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

RequestEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]

# Official effort reference, checked 2026-10-09. Unknown models use an explicit
# auto setting so the transport does not manufacture a reasoning level.
_EFFORT_MODELS = (
    "claude-haiku-5-5",
    "claude-sonnet-5-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-opus-5-5",
    "claude-fable-5",
    "claude-fable-5-1",
    "claude-mythos-5",
    "claude-mythos-5-1",
)


def _matches_model(model: str, alias: str) -> bool:
    return model == alias or (
        model.startswith(alias + "-")
        and re.fullmatch(r"\d{8}|\d{4}-\d{2}-\d{2}", model[len(alias) + 1 :]) is not None
    )


def supported_efforts(provider: str, model: str) -> frozenset[str] | None:
    """Model-specific native values; unknown models keep API defaults in auto mode."""
    if provider == "openai":
        if _matches_model(model, "gpt-5.4-pro"):
            return frozenset({"medium", "high", "xhigh"})
        if any(
            _matches_model(model, alias) for alias in ("gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano")
        ):
            return frozenset({"none", "low", "medium", "high", "xhigh"})
    if provider in {"anthropic", "claude"}:
        for name in sorted(_EFFORT_MODELS, key=len, reverse=True):
            if _matches_model(model, name):
                values = {"low", "medium", "high"}
                if name != "claude-opus-4-5":
                    values.add("max")
                if name not in {"claude-opus-4-5", "claude-opus-4-6", "claude-sonnet-4-6"}:
                    values.add("xhigh")
                return frozenset(values)
    return None


def validate_request_effort(provider: str, model: str, effort: str | None) -> None:
    if effort is None:
        return
    allowed = supported_efforts(provider, model)
    if allowed is None or effort not in allowed:
        choices = ", ".join(sorted(allowed)) if allowed else "no explicit effort"
        raise ValueError(f"Unsupported effort for {provider} model {model}; allowed: {choices}")




@dataclass(frozen=True)
class CallType:
    purpose: str
    label: str
    tier: str = "smart"


CALL_TYPES = (
    CallType("job_scope_screening", "Préfiltre des missions", "cheap"),
    CallType("job_offer_extraction", "Extraction des faits de l’offre", "cheap"),
    CallType("job_fit_assessment", "Évaluation de l’adéquation", "cheap"),
    CallType("cv_adaptation", "Sélection des éléments du CV"),
    CallType("application_texts", "Résumé du CV et lettre"),
    CallType("document_review", "Relecture des documents"),
)
CALL_BY_PURPOSE = {call.purpose: call for call in CALL_TYPES}
CALL_BY_PURPOSE.update({
    "motivation_letter_repair": CallType("motivation_letter_repair", "Correction de la lettre"),
    "cv_selection_correction": CallType("cv_selection_correction", "Correction de la sélection CV"),
})
INHERITED_CALLS = {
    "cv_selection_correction": "cv_adaptation",
    "motivation_letter_repair": "application_texts",
}


def configuration_purpose(purpose: str) -> str:
    """Corrections keep their audit purpose and inherit their original call's route."""
    return INHERITED_CALLS.get(purpose, purpose)


@dataclass(frozen=True)
class CallRoute:
    provider: str
    model: str
    reasoning: str | None
    max_output_tokens: int


class CallConfigurationError(ValueError):
    """One call route is incomplete or incompatible; safe to show in the UI."""

    def __init__(self, purpose: str, field: str, detail: str):
        call = CALL_BY_PURPOSE[purpose]
        location = f"model_config.json → {configuration_purpose(purpose)}.{field}"
        super().__init__(f"Configuration IA — {call.label} : {detail} ({location}).")
        self.purpose = purpose
        self.field = field


def resolve_route(settings, purpose: str) -> CallRoute:
    if purpose not in CALL_BY_PURPOSE:
        raise ValueError(f"Type d’appel IA inconnu : {purpose}.")
    if getattr(settings, "load_error", ""):
        raise CallConfigurationError(purpose, "configuration", settings.load_error)
    calls = getattr(settings, "calls", {})
    call = calls.get(configuration_purpose(purpose))
    if not isinstance(call, dict):
        raise CallConfigurationError(purpose, "configuration", "appel absent")
    unexpected = sorted(set(call) - {"provider", "model", "reasoning", "max_output_tokens"})
    if unexpected:
        raise CallConfigurationError(
            purpose, "configuration", f"paramètres inconnus : {', '.join(unexpected)}"
        )
    provider = call.get("provider")
    model = call.get("model")
    reasoning = call.get("reasoning")
    max_output_tokens = call.get("max_output_tokens")
    if not provider:
        raise CallConfigurationError(purpose, "provider", "fournisseur manquant")
    if not isinstance(provider, str) or provider not in {"openai", "anthropic", "mock"}:
        raise CallConfigurationError(purpose, "provider", f"fournisseur invalide : {provider}")
    if not isinstance(model, str) or not model.strip():
        raise CallConfigurationError(purpose, "model", "modèle manquant")
    model = model.strip()
    if provider == "openai" and not re.fullmatch(r"(?:gpt-[\w.-]+|o[134][\w.-]*|ft:gpt-[\w.:-]+)", model):
        raise CallConfigurationError(purpose, "model", f"identifiant de modèle OpenAI invalide : {model}")
    if provider == "anthropic" and not re.fullmatch(r"claude-[\w.-]+", model):
        raise CallConfigurationError(purpose, "model", f"identifiant de modèle Anthropic invalide : {model}")
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or not 256 <= max_output_tokens <= 100_000:
        raise CallConfigurationError(purpose, "max_output_tokens", "nombre maximal de tokens invalide (256 à 100 000)")
    if not isinstance(reasoning, str):
        raise CallConfigurationError(purpose, "reasoning", "niveau de raisonnement manquant ou invalide")
    if provider == "mock":
        if reasoning != "auto":
            raise CallConfigurationError(purpose, "reasoning", "utilisez auto pour ce fournisseur de test")
        return CallRoute(provider, model, None, max_output_tokens)
    supported = supported_efforts(provider, model)
    if supported:
        if not reasoning or reasoning == "auto":
            raise CallConfigurationError(purpose, "reasoning", "niveau de raisonnement manquant")
        try:
            validate_request_effort(provider, model, reasoning)
        except ValueError as exc:
            raise CallConfigurationError(purpose, "reasoning", str(exc)) from exc
        return CallRoute(provider, model, reasoning, max_output_tokens)
    if reasoning != "auto":
        raise CallConfigurationError(
            purpose, "reasoning", "utilisez auto si ce modèle ne prend pas en charge le raisonnement"
        )
    return CallRoute(provider, model, None, max_output_tokens)


def stage_effort(provider: str, model: str, purpose: str, *, settings=None) -> RequestEffort | None:
    """Read only the explicit route for a new request; frozen requests pass their stored effort."""
    from smartapply.config import get_llm_settings

    route = resolve_route(settings or get_llm_settings(), purpose)
    if (provider, model) != (route.provider, route.model):
        raise CallConfigurationError(
            purpose, "model", "le fournisseur ou modèle demandé diffère du réglage de cet appel"
        )
    return route.reasoning
