"""Reviewed-manifest validation for the guarded legacy-filter migration.

The schema-v3 safety contract remains fixed. Historical decisions, baselines
and replay inputs are deliberately not stored in this module or the repository.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXPECTED_RELEVANT = 1_268
EXPECTED_UNCERTAIN = 2_685
EXPECTED_ALLOWED_REJECTED = 102
EXPECTED_FALSE_REGRESSIONS = 14
EXPECTED_AMBIGUOUS_KEEP = 18
# Fixed schema-v3 bounds make a manifest-only expansion fail closed.
EXPECTED_ALLOWED_UNCERTAIN_REJECTED = 396


@dataclass(frozen=True)
class PositiveControlManifest:
    allowed_rejected_ids: frozenset[int]
    false_regression_ids: frozenset[int]
    ambiguous_keep_ids: frozenset[int]
    allowed_uncertain_rejected_ids: frozenset[int]
    expected_relevant: int
    expected_uncertain: int
    max_uncertain_rejected: int
    database_sha256: str
    baseline_sha256: str

    @property
    def protected_keep_ids(self) -> frozenset[int]:
        return self.false_regression_ids | self.ambiguous_keep_ids

    @property
    def reviewed_relevant_ids(self) -> frozenset[int]:
        return self.allowed_rejected_ids | self.protected_keep_ids

    @property
    def reviewed_ids(self) -> frozenset[int]:
        return self.reviewed_relevant_ids | self.allowed_uncertain_rejected_ids


def _format_ids(ids: Iterable[int], *, limit: int = 20) -> str:
    ordered = sorted(ids)
    suffix = "" if len(ordered) <= limit else f" ... (+{len(ordered) - limit} more)"
    return f"{ordered[:limit]}{suffix}"


def _require_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Expected {label} to be a JSON object")
    return value


def _require_id_set(payload: dict[str, Any], key: str) -> frozenset[int]:
    values = payload.get(key)
    if not isinstance(values, list) or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values
    ):
        raise RuntimeError(f"Expected {key} to be a list of positive integer IDs")
    if len(values) != len(set(values)):
        raise RuntimeError(f"Duplicate IDs in manifest field {key}")
    return frozenset(values)


def _require_sha256(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeError(f"Expected a lowercase SHA-256 digest for {label}")
    return value


def _require_non_negative_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"Expected {label} to be a non-negative integer")
    return value


def load_manifest(path: Path) -> PositiveControlManifest:
    """Load and fully validate the manually reviewed positive-control manifest."""

    if not path.is_file():
        raise RuntimeError(f"Positive-control manifest does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload = _require_mapping(payload, label="positive-control manifest")
    if payload.get("schema_version") != 3:
        raise RuntimeError(f"Unsupported positive-control manifest schema in {path}")

    allowed = _require_id_set(payload, "allowed_rejected_ids")
    false_regressions = _require_id_set(payload, "false_regression_ids")
    ambiguous = _require_id_set(payload, "ambiguous_keep_ids")
    allowed_uncertain = _require_id_set(payload, "allowed_uncertain_rejected_ids")
    categories = {
        "allowed_rejected_ids": allowed,
        "false_regression_ids": false_regressions,
        "ambiguous_keep_ids": ambiguous,
    }
    for name, ids in categories.items():
        overlap = ids & set().union(
            *(other for other_name, other in categories.items() if other_name != name)
        )
        if overlap:
            raise RuntimeError(
                f"Manifest decision categories overlap in {name}: {_format_ids(overlap)}"
            )
    reviewed_relevant = allowed | false_regressions | ambiguous
    cross_cohort_overlap = reviewed_relevant & allowed_uncertain
    if cross_cohort_overlap:
        raise RuntimeError(
            "Manifest relevant and uncertain decisions overlap: "
            f"{_format_ids(cross_cohort_overlap)}"
        )

    expected_counts = _require_mapping(
        payload.get("expected_counts"), label="manifest expected_counts"
    )
    max_uncertain_rejected = _require_non_negative_int(
        expected_counts.get("max_uncertain_rejected"),
        label="manifest expected_counts.max_uncertain_rejected",
    )
    if max_uncertain_rejected > EXPECTED_UNCERTAIN:
        raise RuntimeError(
            "Manifest uncertain rejection ceiling exceeds the frozen uncertain cohort: "
            f"{max_uncertain_rejected} > {EXPECTED_UNCERTAIN}"
        )
    exact_counts = {
        "baseline_relevant": EXPECTED_RELEVANT,
        "baseline_uncertain": EXPECTED_UNCERTAIN,
        "reviewed_relevant_rejected": (
            EXPECTED_ALLOWED_REJECTED + EXPECTED_FALSE_REGRESSIONS + EXPECTED_AMBIGUOUS_KEEP
        ),
        "allowed_rejected": EXPECTED_ALLOWED_REJECTED,
        "false_regression": EXPECTED_FALSE_REGRESSIONS,
        "ambiguous_keep": EXPECTED_AMBIGUOUS_KEEP,
        "allowed_uncertain_rejected": EXPECTED_ALLOWED_UNCERTAIN_REJECTED,
    }
    for key, expected in exact_counts.items():
        if expected_counts.get(key) != expected:
            raise RuntimeError(
                f"Manifest expected count {key!r} is {expected_counts.get(key)!r}; "
                f"expected {expected}"
            )
    actual_counts = {
        "allowed_rejected": len(allowed),
        "false_regression": len(false_regressions),
        "ambiguous_keep": len(ambiguous),
        "allowed_uncertain_rejected": len(allowed_uncertain),
    }
    for key, actual in actual_counts.items():
        if actual != exact_counts[key]:
            raise RuntimeError(
                f"Manifest field {key!r} contains {actual} IDs; expected {exact_counts[key]}"
            )
    if max_uncertain_rejected > len(allowed_uncertain):
        raise RuntimeError(
            "Manifest uncertain rejection ceiling exceeds its reviewed ID allowlist: "
            f"{max_uncertain_rejected} > {len(allowed_uncertain)}"
        )
    if len(allowed | false_regressions | ambiguous) != exact_counts["reviewed_relevant_rejected"]:
        raise RuntimeError(
            "Manifest decisions do not cover exactly "
            f"{exact_counts['reviewed_relevant_rejected']} reviewed IDs"
        )

    provenance = _require_mapping(payload.get("provenance"), label="manifest provenance")
    database = _require_mapping(provenance.get("database"), label="database provenance")
    baseline = _require_mapping(provenance.get("baseline"), label="baseline provenance")
    uncertain_control = _require_mapping(
        provenance.get("uncertain_rejection_control"),
        label="uncertain rejection control provenance",
    )
    if uncertain_control.get("kind") != "id_allowlist_and_aggregate_count_ceiling":
        raise RuntimeError(
            "Manifest uncertain rejection control must use id_allowlist_and_aggregate_count_ceiling"
        )
    if uncertain_control.get("id_level_review") is not True:
        raise RuntimeError(
            "Manifest uncertain rejection control must state that ID-level review exists"
        )
    if uncertain_control.get("allowed_rejected") != len(allowed_uncertain):
        raise RuntimeError(
            "Manifest uncertain rejection allowlist count disagrees with its provenance"
        )
    if uncertain_control.get("maximum_rejected") != max_uncertain_rejected:
        raise RuntimeError("Manifest uncertain rejection ceiling disagrees with its provenance")
    for key in ("basis", "method", "status"):
        value = uncertain_control.get(key)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(
                f"Manifest uncertain rejection control requires non-empty {key!r} provenance"
            )
    return PositiveControlManifest(
        allowed_rejected_ids=allowed,
        false_regression_ids=false_regressions,
        ambiguous_keep_ids=ambiguous,
        allowed_uncertain_rejected_ids=allowed_uncertain,
        expected_relevant=EXPECTED_RELEVANT,
        expected_uncertain=EXPECTED_UNCERTAIN,
        max_uncertain_rejected=max_uncertain_rejected,
        database_sha256=_require_sha256(database.get("sha256"), label="database"),
        baseline_sha256=_require_sha256(baseline.get("sha256"), label="baseline"),
    )
