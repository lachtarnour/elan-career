"""Read the scoring inputs independently of diagnostic annotation conformity."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from smartapply.llm.fit_partial_json import recover_truncated_json
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.schemas import (
    FitDecisionAnnotations,
    FitDecisionDimension,
    FitDecisionFinding,
    FitDecisions,
    FitDimension,
    FitDimensions,
    FitFinding,
    FitIssue,
    FitOfferInterpretation,
    FitReservation,
    JobFitDecisionEvaluation,
    JobFitEvaluation,
    NeutralOfferReference,
)

AXES = ("missions", "essentials", "responsibilities")


@dataclass(frozen=True)
class FitEvaluationResponse:
    levels: tuple[int, int, int]
    raw: dict[str, Any]
    original_text: str | None = None
    issue_codes: tuple[str, ...] = ()
    observations: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class FitDecisionResponse(FitEvaluationResponse):
    decisions: tuple[str, str, str]
    preference_conflict: str


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


def issue_codes(value) -> list[str]:
    items = [value] if isinstance(value, dict) else value if isinstance(value, list) else []
    codes = []
    for item in items:
        for code in _variants(item, "code") if isinstance(item, dict) else [item]:
            if isinstance(code, str):
                codes.append(code)
    return codes


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


def read_fit_response(value: Any, *, schema=JobFitEvaluation) -> FitEvaluationResponse:
    """Use the recorded contract; annotation conformity never controls scoring."""
    if schema is JobFitDecisionEvaluation:
        return read_fit_decisions(value)
    if isinstance(value, FitDecisionResponse):
        raise LLMValidationError("Decision response cannot be read as legacy levels")
    if isinstance(value, FitEvaluationResponse):
        return value
    value, original, observations = _read_object(value)
    if not isinstance(value, dict) or not isinstance(value.get("dimensions"), dict):
        raise LLMValidationError("Fit response has no usable dimensions")
    levels = []
    for axis in AXES:
        candidates = []
        for dimensions in _variants(value, "dimensions"):
            if not isinstance(dimensions, dict):
                raise LLMValidationError("Ambiguous fit dimensions")
            for dimension in _variants(dimensions, axis):
                candidates.extend(
                    _variants(dimension, "level") if isinstance(dimension, dict) else [dimension]
                )
        if any(type(level) is not int or not 0 <= level <= 4 for level in candidates):
            raise LLMValidationError(f"Fit response has no usable {axis} level (integer 0..4)")
        if len(set(candidates)) != 1:
            raise LLMValidationError(f"Fit response has ambiguous {axis} levels")
        levels.append(candidates[0])
    codes = [code for group in _variants(value, "issues") for code in issue_codes(group)]
    return FitEvaluationResponse(
        tuple(levels), copy.deepcopy(value), original, tuple(codes), observations
    )


def read_fit_decisions(value: Any) -> FitDecisionResponse:
    from smartapply.llm.fit_context import DECISION_LEVELS

    if isinstance(value, FitDecisionResponse):
        return value
    if isinstance(value, FitEvaluationResponse):
        raise LLMValidationError("Legacy levels cannot be remapped to decisions")
    value, original, observations = _read_object(value)
    if not isinstance(value, dict):
        raise LLMValidationError("Fit response has no usable decision envelope")
    states = []
    for key in (*AXES, "preference_conflict"):
        candidates = []
        for decision in _variants(value, "decision"):
            if not isinstance(decision, dict):
                raise LLMValidationError("Ambiguous fit decision envelope")
            candidates.extend(_variants(decision, key))
        allowed = (
            {"none", "unknown", "confirmed"} if key == "preference_conflict" else DECISION_LEVELS
        )
        if any(not isinstance(state, str) or state not in allowed for state in candidates):
            raise LLMValidationError(f"Fit response has no usable {key} decision")
        if len(set(candidates)) != 1:
            raise LLMValidationError(f"Fit response has ambiguous {key} decisions")
        states.append(candidates[0])
    return FitDecisionResponse(
        levels=tuple(DECISION_LEVELS[state] for state in states[:3]),
        raw=copy.deepcopy(value),
        original_text=original,
        observations=observations,
        decisions=tuple(states[:3]),
        preference_conflict=states[3],
    )


def annotation_dicts(value: Any) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def dimension_annotations(raw: dict, axis: str) -> dict:
    dimensions = raw.get("dimensions")
    dimension = dimensions.get(axis) if isinstance(dimensions, dict) else None
    return dimension if isinstance(dimension, dict) else {}


def annotation_view(response: FitEvaluationResponse) -> JobFitEvaluation | JobFitDecisionEvaluation:
    """Best-effort typed view for existing checks; discarded items remain in raw."""

    def items(value, schema):
        parsed = []
        for item in annotation_dicts(value):
            try:
                parsed.append(schema.model_validate(item))
            except ValidationError:
                continue
        return parsed

    if isinstance(response, FitDecisionResponse):
        return JobFitDecisionEvaluation(
            decision=FitDecisions(
                **dict(zip(AXES, response.decisions, strict=True)),
                preference_conflict=response.preference_conflict,
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
    dimensions = {}
    for axis, level in zip(AXES, response.levels, strict=True):
        dimensions[axis] = FitDimension(
            level=level,
            findings=items(dimension_annotations(response.raw, axis).get("findings"), FitFinding),
        )
    return JobFitEvaluation(
        dimensions=FitDimensions(**dimensions),
        issues=items(response.raw.get("issues"), FitIssue),
        neutral_offer_refs=items(response.raw.get("neutral_offer_refs"), NeutralOfferReference),
    )


def schema_observations(response: FitEvaluationResponse) -> list[str]:
    try:
        schema = (
            JobFitDecisionEvaluation
            if isinstance(response, FitDecisionResponse)
            else JobFitEvaluation
        )
        schema.model_validate(response.raw)
    except ValidationError as exc:
        return [
            f"Annotation {'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
    return []
