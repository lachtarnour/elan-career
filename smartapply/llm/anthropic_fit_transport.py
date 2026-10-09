"""Lossless fit representation for Claude's native JSON output grammar.

Business schemas and prompts remain canonical. Nullable descriptive objects are
represented by empty/singleton lists; findings are flattened without changing
any value. Cardinality is a transport invariant: Anthropic does not constrain
maxItems, so violations are retained and reported, never repaired or retried.
"""

from __future__ import annotations

import copy

from smartapply.llm.provider import LLMError
from smartapply.llm.schemas import JobFitDecisionEvaluation

MARKER = "elan_fit_transport"
FORMAT = "flat_annotations"
_DIMENSIONS = {
    "mission_findings": "missions",
    "essential_findings": "essentials",
    "responsibility_findings": "responsibilities",
}
_ADMISSIBILITY = {
    "mandatory_condition_records": "mandatory_conditions",
    "candidate_preference_records": "candidate_preferences",
}


def fit_transport_schema(source: dict, *, name: str) -> dict:
    """Adapt only the current exact fit schema, leaving historical/other outputs intact."""
    from smartapply.llm.structured import to_strict_schema

    properties = source.get("properties", {})
    if name != JobFitDecisionEvaluation.__name__ or not {"admissibility", "reliability"}.issubset(
        properties
    ):
        return copy.deepcopy(source)
    if source != to_strict_schema(JobFitDecisionEvaluation):
        raise LLMError("Unsupported canonical fit schema for Claude transport")
    schema = copy.deepcopy(source)
    definitions = schema["$defs"]
    flattened = {
        "decision": copy.deepcopy(properties["decision"]),
        MARKER: {"type": "string", "const": FORMAT},
    }
    for wire, canonical in _ADMISSIBILITY.items():
        flattened[wire] = {
            "type": "array",
            "maxItems": 1,
            "items": {"$ref": "#/$defs/FitAdmissibilityDimension"},
            "description": (
                f"Transport of admissibility.{canonical}. Exactly one item when "
                "admissibility is present; otherwise []. Both admissibility lists "
                "must be empty together or contain one item each. Do not invent a status."
            ),
        }
    flattened["reliability_records"] = {
        "type": "array",
        "maxItems": 1,
        "items": {"$ref": "#/$defs/FitReliability"},
        "description": "Transport of reliability: [] means null; otherwise exactly one object.",
    }
    finding_schema = definitions["FitDecisionDimension"]["properties"]["findings"]
    for wire, canonical in _DIMENSIONS.items():
        flattened[wire] = {
            **copy.deepcopy(finding_schema),
            "description": f"Exact findings of dimensions.{canonical}.findings, in order.",
        }
    for key in ("issues", "neutral_offer_refs", "offer_interpretations"):
        flattened[key] = copy.deepcopy(properties[key])
    schema["properties"] = flattened
    schema["required"] = list(flattened)
    schema["description"] = (
        "Transport representation of the canonical fit contract. Only field paths "
        "change; all business rules, decisions and annotation values stay the same."
    )
    for unused in ("FitAdmissibility", "FitDecisionAnnotations", "FitDecisionDimension"):
        del definitions[unused]
    return schema


def encode_fit_transport(value: dict) -> dict:
    """Offline inverse used to prove exact round trips, not to create LLM conclusions."""
    JobFitDecisionEvaluation.model_validate(value)
    value = copy.deepcopy(value)
    wire = {"decision": value["decision"], MARKER: FORMAT}
    for target, key in _ADMISSIBILITY.items():
        wire[target] = [] if value.get("admissibility") is None else [value["admissibility"][key]]
    wire["reliability_records"] = [] if value.get("reliability") is None else [value["reliability"]]
    for target, key in _DIMENSIONS.items():
        wire[target] = value["dimensions"][key]["findings"]
    for key in ("issues", "neutral_offer_refs", "offer_interpretations"):
        wire[key] = value[key]
    return wire


def decode_fit_transport(value: dict) -> tuple[dict, list[str]]:
    """Rearrange annotations; keep every unconvertible value for advisory inspection.

    This is called only after the three canonical decisions have been read with
    duplicate-key detection. It must never decide, cap, correct or veto a score.
    Unknown fields and ambiguous transport shapes remain in the raw dictionary.
    The native JSON text is separately retained by the existing fit reader/cache.
    """
    if MARKER not in value:
        return value, []
    if value[MARKER] != FORMAT:
        return value, ["Claude fit transport: unknown representation; annotations retained raw"]

    def ambiguous(node):
        if isinstance(node, dict):
            if any(len(values) > 1 for values in getattr(node, "variants", {}).values()):
                return True
            return any(ambiguous(child) for child in node.values())
        if isinstance(node, list):
            return any(ambiguous(child) for child in node)
        return False

    if ambiguous(value):
        return value, ["Claude fit transport: duplicate annotation keys; annotations retained raw"]
    result = copy.deepcopy(value)
    del result[MARKER]
    warnings = []
    # A simultaneous canonical and wire block is ambiguous. Keep both, choosing neither.
    if "dimensions" not in result:
        dimensions = {}
        for wire, key in _DIMENSIONS.items():
            if wire in result:
                dimensions[key] = {"findings": result.pop(wire)}
        if dimensions:
            result["dimensions"] = dimensions
    else:
        warnings.append("Claude fit transport: simultaneous canonical and wire dimensions")
    if "admissibility" not in result:
        groups = [result.get(key) for key in _ADMISSIBILITY]
        if all(isinstance(group, list) and not group for group in groups):
            result["admissibility"] = None
            for key in _ADMISSIBILITY:
                del result[key]
        elif all(isinstance(group, list) and len(group) == 1 for group in groups):
            result["admissibility"] = {
                key: result.pop(wire)[0] for wire, key in _ADMISSIBILITY.items()
            }
        else:
            warnings.append(
                "Claude fit transport: admissibility cardinality/presence inconsistent; "
                "all records retained raw"
            )
    else:
        warnings.append("Claude fit transport: simultaneous canonical and wire admissibility")
    if "reliability" not in result:
        group = result.get("reliability_records")
        if isinstance(group, list) and len(group) <= 1:
            result["reliability"] = group[0] if group else None
            del result["reliability_records"]
        else:
            warnings.append(
                "Claude fit transport: reliability cardinality invalid; records retained raw"
            )
    else:
        warnings.append("Claude fit transport: simultaneous canonical and wire reliability")
    return result, warnings
