"""Lossless fit representation for Claude's native JSON output grammar.

The three-key native representation groups repeated technical findings by axis.
The canonical business schema and decisions stay intact. Cardinality is an
explicit transport invariant, checked only as advisory diagnostics. Historical
flat outputs remain readable; malformed records are never repaired or retried.
"""

from __future__ import annotations

import copy

from smartapply.llm.provider import LLMError
from smartapply.llm.schemas import JobFitDecisionEvaluation

MARKER = "elan_fit_transport"
FORMAT = "flat_annotations"  # Historical read-only representation.
GROUPED_KEY = "fit_annotations"
_DIMENSIONS = {
    "mission_findings": "missions",
    "essential_findings": "essentials",
    "responsibility_findings": "responsibilities",
}


def fit_transport_schema(source: dict, *, name: str) -> dict:
    """Build a small grammar: three root keys and one structure per annotation kind."""
    from smartapply.llm.structured import to_strict_schema

    if name != JobFitDecisionEvaluation.__name__ or "reliability" not in source.get(
        "properties", {}
    ):
        return copy.deepcopy(source)
    if source != to_strict_schema(JobFitDecisionEvaluation):
        raise LLMError("Unsupported canonical fit schema for Claude transport")

    def inline(node):
        if isinstance(node, list):
            return [inline(child) for child in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return inline(source["$defs"][node["$ref"].split("/")[-1]])
        return {key: inline(child) for key, child in node.items()}

    def obj(properties):
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    finding = inline(source["$defs"]["FitDecisionFinding"])
    finding["properties"] = {
        "axis": {"type": "string", "enum": list(_DIMENSIONS.values())},
        **finding["properties"],
    }
    finding["required"] = list(finding["properties"])
    annotations = obj(
        {
            "technical_findings": {"type": "array", "items": finding},
            **{
                key: inline(source["properties"][key])
                for key in ("issues", "neutral_offer_refs", "offer_interpretations")
            },
        }
    )
    return {
        "title": source.get("title", JobFitDecisionEvaluation.__name__),
        **obj(
            {
                "decision": inline(source["properties"]["decision"]),
                GROUPED_KEY: annotations,
                "reliability_records": {
                    "type": "array",
                    "items": inline(source["$defs"]["FitReliability"]),
                    "description": "[] means canonical reliability=null; otherwise exactly one record.",
                },
            }
        ),
    }


def encode_legacy_fit_transport(value: dict) -> dict:
    """Offline inverse used to prove exact round trips, not to create LLM conclusions."""
    JobFitDecisionEvaluation.model_validate(value)
    value = copy.deepcopy(value)
    wire = {"decision": value["decision"], MARKER: FORMAT}
    wire["reliability_records"] = [] if value.get("reliability") is None else [value["reliability"]]
    for target, key in _DIMENSIONS.items():
        wire[target] = value["dimensions"][key]["findings"]
    for key in ("issues", "neutral_offer_refs", "offer_interpretations"):
        wire[key] = value[key]
    return wire


def decode_legacy_fit_transport(value: dict) -> tuple[dict, list[str]]:
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


def encode_fit_transport(value: dict) -> dict:
    """Offline inverse: tag and regroup existing values, without creating judgements."""
    JobFitDecisionEvaluation.model_validate(value)
    value = copy.deepcopy(value)
    findings = [
        {"axis": axis, **finding}
        for axis in _DIMENSIONS.values()
        for finding in value["dimensions"][axis]["findings"]
    ]
    return {
        "decision": value["decision"],
        GROUPED_KEY: {
            "technical_findings": findings,
            **{
                key: value[key] for key in ("issues", "neutral_offer_refs", "offer_interpretations")
            },
        },
        "reliability_records": [] if value.get("reliability") is None else [value["reliability"]],
    }


def _ambiguous(node):
    if isinstance(node, dict):
        if any(len(values) > 1 for values in getattr(node, "variants", {}).values()):
            return True
        return any(_ambiguous(child) for child in node.values())
    return any(_ambiguous(child) for child in node) if isinstance(node, list) else False


def decode_fit_transport(value: dict) -> tuple[dict, list[str]]:
    """Decode the known shape only; decisions have already passed the scoring reader."""
    if MARKER in value:
        return decode_legacy_fit_transport(value)
    if GROUPED_KEY not in value:
        return value, []
    if _ambiguous(value):
        return value, ["Claude fit transport: duplicate annotation keys; annotations retained raw"]
    raw = copy.deepcopy(value)
    warnings = []
    annotations = raw.get(GROUPED_KEY)
    if isinstance(annotations, dict):
        pending = copy.deepcopy(annotations)
        records = pending.get("technical_findings")
        if "technical_findings" in pending and "dimensions" in raw:
            warnings.append("Claude fit transport: simultaneous canonical and wire dimensions")
        elif (
            "technical_findings" in pending
            and isinstance(records, list)
            and all(
                isinstance(item, dict) and item.get("axis") in _DIMENSIONS.values()
                for item in records
            )
        ):
            # Empty lists explicitly encode empty findings on every axis, not a conclusion.
            dimensions = {axis: {"findings": []} for axis in _DIMENSIONS.values()}
            for item in records:
                finding = copy.deepcopy(item)
                axis = finding.pop("axis")
                dimensions[axis]["findings"].append(finding)
            raw["dimensions"] = dimensions
            del pending["technical_findings"]
        elif "technical_findings" in pending:
            warnings.append(
                "Claude fit transport: invalid technical findings; records retained raw"
            )
        for key in ("issues", "neutral_offer_refs", "offer_interpretations"):
            if key in raw and key in pending:
                warnings.append(f"Claude fit transport: simultaneous canonical and wire {key}")
            elif key in pending:
                raw[key] = pending.pop(key)
        if pending:
            raw[GROUPED_KEY] = pending
        else:
            del raw[GROUPED_KEY]
    else:
        warnings.append("Claude fit transport: unreadable annotation group retained raw")
    if "reliability_records" in raw:
        records = raw.get("reliability_records")
        if "reliability" in raw:
            warnings.append("Claude fit transport: simultaneous canonical and wire reliability")
        elif isinstance(records, list) and len(records) <= 1:
            raw["reliability"] = records[0] if records else None
            del raw["reliability_records"]
        else:
            warnings.append(
                "Claude fit transport: reliability cardinality invalid; records retained raw"
            )
    return raw, warnings
