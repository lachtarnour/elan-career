"""Read the scoring inputs independently of diagnostic annotation conformity."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from typing import Any

from pydantic import BaseModel, ValidationError

from smartapply.llm.fit_partial_json import recover_truncated_json
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.schemas import (
    FitAdmissibility,
    FitDecisionAnnotations,
    FitDecisionDimension,
    FitDecisionFinding,
    FitDecisions,
    FitOfferInterpretation,
    FitReliability,
    FitReservation,
    JobFitDecisionEvaluation,
    NeutralOfferReference,
)

AXES = ("missions", "essentials", "responsibilities")


@dataclass(frozen=True, kw_only=True)
class FitDecisionResponse:
    levels: tuple[int, int, int]
    raw: dict[str, Any]
    original_text: str | None = None
    observations: list[str] = field(default_factory=list)
    decisions: tuple[str, str, str]


class _JSONObject(dict):
    def __init__(self, pairs, *, incomplete_keys=(), unread_key_prefix=None):
        super().__init__(pairs)
        self.variants = {}
        self.incomplete_keys = incomplete_keys
        self.unread_key_prefix = unread_key_prefix
        for key, value in pairs:
            self.variants.setdefault(key, []).append(value)


def _variants(value, key):
    if not isinstance(value, _JSONObject):
        return [value.get(key)]
    values = list(value.variants.get(key, []))
    # Only a compatible prefix can introduce another occurrence of this key.
    if key in value.incomplete_keys or (
        value.unread_key_prefix is not None and value.unread_key_prefix.could_match(key)
    ):
        values.append(None)
    return values or [None]


def _read_object(value):
    original = value if isinstance(value, str) else None
    observations = []
    if isinstance(value, str):
        try:
            value = json.loads(value, object_pairs_hook=_JSONObject)
        except ValueError:
            try:
                value = recover_truncated_json(value, object_factory=_JSONObject)
            except (ValueError, RecursionError) as exc:
                raise LLMValidationError("Fit response is unreadable JSON") from exc
            observations.append("Truncated JSON response; only fully read values retained")
    elif isinstance(value, BaseModel):
        value = value.model_dump(mode="json", warnings=False)
    return value, original, observations


def read_fit_response(value: Any, *, schema=JobFitDecisionEvaluation) -> FitDecisionResponse:
    """Read the three technical decisions; annotations never control scoring."""
    if schema is not JobFitDecisionEvaluation:
        raise LLMValidationError("Unsupported fit response schema")
    response = read_fit_decisions(value)
    if not {"elan_fit_transport", "fit_annotations"}.intersection(response.raw):
        return response
    # Read decisions first. No annotation adapter failure may veto them.
    from smartapply.llm.anthropic_fit_transport import GROUPED_KEY, MARKER, decode_fit_transport

    if {MARKER, GROUPED_KEY}.intersection(response.raw) and any(
        warning.startswith("Truncated JSON response") for warning in response.observations
    ):
        return replace(
            response,
            observations=response.observations
            + [
                "Claude fit transport: incomplete annotations retained raw, not interpreted as absent"
            ],
        )
    try:
        raw, warnings = decode_fit_transport(response.raw)
    except Exception as exc:
        return replace(
            response,
            observations=response.observations
            + [f"Claude fit transport annotation conversion: {type(exc).__name__}: {exc}"],
        )
    return replace(response, raw=raw, observations=response.observations + warnings)


def read_fit_decisions(value: Any) -> FitDecisionResponse:
    from smartapply.llm.fit_context import DECISION_LEVELS

    if isinstance(value, FitDecisionResponse):
        return value
    value, original, observations = _read_object(value)
    if not isinstance(value, dict):
        raise LLMValidationError("Fit response has no usable decision envelope")
    states = []
    for key in AXES:
        candidates = []
        for decision in _variants(value, "decision"):
            if not isinstance(decision, dict):
                raise LLMValidationError("Ambiguous fit decision envelope")
            candidates.extend(_variants(decision, key))
        allowed = DECISION_LEVELS
        if any(not isinstance(state, str) or state not in allowed for state in candidates):
            raise LLMValidationError(f"Fit response has no usable {key} decision")
        if len(set(candidates)) != 1:
            raise LLMValidationError(f"Fit response has ambiguous {key} decisions")
        states.append(candidates[0])
    return FitDecisionResponse(
        levels=tuple(DECISION_LEVELS[state] for state in states),
        raw=copy.deepcopy(value),
        original_text=original,
        observations=observations,
        decisions=tuple(states),
    )


def annotation_dicts(value: Any) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def dimension_annotations(raw: dict, axis: str) -> dict:
    dimensions = raw.get("dimensions")
    dimension = dimensions.get(axis) if isinstance(dimensions, dict) else None
    return dimension if isinstance(dimension, dict) else {}


def annotation_view(response: FitDecisionResponse) -> JobFitDecisionEvaluation:
    """Best-effort typed view for existing checks; discarded items remain in raw."""

    def items(value, schema):
        parsed = []
        for item in annotation_dicts(value):
            try:
                parsed.append(schema.model_validate(item))
            except ValidationError:
                continue
        return parsed

    def auxiliary(key, model):
        try:
            return model.model_validate(response.raw.get(key))
        except ValidationError:
            return None

    return JobFitDecisionEvaluation(
        admissibility=auxiliary("admissibility", FitAdmissibility),
        reliability=auxiliary("reliability", FitReliability),
        decision=FitDecisions(
            **dict(zip(AXES, response.decisions, strict=True)),
        ),
        dimensions=FitDecisionAnnotations(
            **{
                axis: FitDecisionDimension(
                    findings=items(
                        dimension_annotations(response.raw, axis).get("findings"),
                        FitDecisionFinding,
                    )
                )
                for axis in AXES
            }
        ),
        issues=items(response.raw.get("issues"), FitReservation),
        neutral_offer_refs=items(response.raw.get("neutral_offer_refs"), NeutralOfferReference),
        offer_interpretations=items(
            response.raw.get("offer_interpretations"), FitOfferInterpretation
        ),
    )


def schema_observations(response: FitDecisionResponse) -> list[str]:
    observations = []
    for name in ("admissibility", "reliability"):
        if response.raw.get(name) is None:
            observations.append(f"{name}: descriptive information unavailable")
    try:
        JobFitDecisionEvaluation.model_validate(response.raw)
    except ValidationError as exc:
        observations.extend(
            f"Annotation {'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
    return observations
