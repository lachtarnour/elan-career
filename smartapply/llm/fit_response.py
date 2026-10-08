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
    FitDimension,
    FitDimensions,
    FitFinding,
    FitIssue,
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


class _JSONObject(dict):
    def __init__(self, pairs, *, incomplete_keys=(), unread_key=False):
        super().__init__(pairs)
        self.variants = {}
        self.incomplete_keys = incomplete_keys
        self.unread_key = unread_key
        for key, value in pairs:
            self.variants.setdefault(key, []).append(value)


def _variants(value, key):
    if not isinstance(value, _JSONObject):
        return [value.get(key)]
    values = list(value.variants.get(key, []))
    # An unfinished key here could introduce another dimensions/axis/level key.
    if key in value.incomplete_keys or value.unread_key:
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


def read_fit_response(value: Any) -> FitEvaluationResponse:
    """Only absence of unambiguous integer levels makes this response unusable."""
    if isinstance(value, FitEvaluationResponse):
        return value
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


def annotation_dicts(value: Any) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def dimension_annotations(raw: dict, axis: str) -> dict:
    dimension = raw["dimensions"][axis]
    return dimension if isinstance(dimension, dict) else {}


def annotation_view(response: FitEvaluationResponse) -> JobFitEvaluation:
    """Best-effort typed view for existing checks; discarded items remain in raw."""

    def items(value, schema):
        parsed = []
        for item in annotation_dicts(value):
            try:
                parsed.append(schema.model_validate(item))
            except ValidationError:
                continue
        return parsed

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
        JobFitEvaluation.model_validate(response.raw)
    except ValidationError as exc:
        return [
            f"Annotation {'.'.join(map(str, error['loc']))}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
    return []
