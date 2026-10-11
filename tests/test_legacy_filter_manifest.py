"""Migration-manifest guards using transient, entirely synthetic decisions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smartapply.maintenance.legacy_filter_manifest import (
    EXPECTED_ALLOWED_REJECTED,
    EXPECTED_ALLOWED_UNCERTAIN_REJECTED,
    EXPECTED_AMBIGUOUS_KEEP,
    EXPECTED_FALSE_REGRESSIONS,
    EXPECTED_RELEVANT,
    EXPECTED_UNCERTAIN,
    load_manifest,
)


@pytest.fixture
def payload() -> dict:
    """Generate IDs only during the test; never reuse saved audit decisions."""
    counts = {
        "allowed_rejected_ids": EXPECTED_ALLOWED_REJECTED,
        "false_regression_ids": EXPECTED_FALSE_REGRESSIONS,
        "ambiguous_keep_ids": EXPECTED_AMBIGUOUS_KEEP,
        "allowed_uncertain_rejected_ids": EXPECTED_ALLOWED_UNCERTAIN_REJECTED,
    }
    next_id = 100_000
    decisions = {}
    for name, count in counts.items():
        decisions[name] = list(range(next_id, next_id + count))
        next_id += count
    return {
        "schema_version": 3,
        **decisions,
        "expected_counts": {
            "baseline_relevant": EXPECTED_RELEVANT,
            "baseline_uncertain": EXPECTED_UNCERTAIN,
            "reviewed_relevant_rejected": (
                EXPECTED_ALLOWED_REJECTED + EXPECTED_FALSE_REGRESSIONS + EXPECTED_AMBIGUOUS_KEEP
            ),
            "allowed_rejected": EXPECTED_ALLOWED_REJECTED,
            "false_regression": EXPECTED_FALSE_REGRESSIONS,
            "ambiguous_keep": EXPECTED_AMBIGUOUS_KEEP,
            "allowed_uncertain_rejected": EXPECTED_ALLOWED_UNCERTAIN_REJECTED,
            "max_uncertain_rejected": EXPECTED_ALLOWED_UNCERTAIN_REJECTED,
        },
        "provenance": {
            "database": {"sha256": "a" * 64},
            "baseline": {"sha256": "b" * 64},
            "uncertain_rejection_control": {
                "kind": "id_allowlist_and_aggregate_count_ceiling",
                "id_level_review": True,
                "allowed_rejected": EXPECTED_ALLOWED_UNCERTAIN_REJECTED,
                "maximum_rejected": EXPECTED_ALLOWED_UNCERTAIN_REJECTED,
                "basis": "Synthetic test cohort",
                "method": "Synthetic test decisions",
                "status": "Synthetic test only",
            },
        },
    }


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "reviewed-manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_synthetic_manifest_preserves_reviewed_sets_and_provenance(tmp_path, payload) -> None:
    manifest = load_manifest(_write(tmp_path, payload))

    assert manifest.allowed_rejected_ids == frozenset(payload["allowed_rejected_ids"])
    assert manifest.allowed_uncertain_rejected_ids == frozenset(
        payload["allowed_uncertain_rejected_ids"]
    )
    assert manifest.protected_keep_ids == frozenset(
        payload["false_regression_ids"] + payload["ambiguous_keep_ids"]
    )
    assert manifest.reviewed_relevant_ids == (
        manifest.allowed_rejected_ids | manifest.protected_keep_ids
    )
    assert manifest.reviewed_ids == (
        manifest.reviewed_relevant_ids | manifest.allowed_uncertain_rejected_ids
    )
    assert manifest.expected_relevant == EXPECTED_RELEVANT
    assert manifest.expected_uncertain == EXPECTED_UNCERTAIN
    assert manifest.max_uncertain_rejected == EXPECTED_ALLOWED_UNCERTAIN_REJECTED
    assert manifest.database_sha256 == "a" * 64
    assert manifest.baseline_sha256 == "b" * 64


def test_missing_manifest_is_not_reconstructed(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="does not exist"):
        load_manifest(tmp_path / "missing.json")


@pytest.mark.parametrize("invalid", [None, [], {"schema_version": 2}])
def test_manifest_requires_object_and_supported_schema(tmp_path, invalid) -> None:
    with pytest.raises(RuntimeError):
        load_manifest(_write(tmp_path, invalid))


@pytest.mark.parametrize("invalid", [True, 0, -1, "100000", 1.5])
def test_manifest_rejects_invalid_ids(tmp_path, payload, invalid) -> None:
    payload["allowed_rejected_ids"][0] = invalid
    with pytest.raises(RuntimeError, match="positive integer IDs"):
        load_manifest(_write(tmp_path, payload))


def test_manifest_rejects_duplicate_ids(tmp_path, payload) -> None:
    payload["allowed_rejected_ids"][1] = payload["allowed_rejected_ids"][0]
    with pytest.raises(RuntimeError, match="Duplicate IDs"):
        load_manifest(_write(tmp_path, payload))


@pytest.mark.parametrize("category", ["false_regression_ids", "ambiguous_keep_ids"])
def test_manifest_rejects_overlapping_relevant_decisions(tmp_path, payload, category) -> None:
    payload[category][0] = payload["allowed_rejected_ids"][0]
    with pytest.raises(RuntimeError, match="decision categories overlap"):
        load_manifest(_write(tmp_path, payload))


def test_manifest_rejects_cross_cohort_decisions(tmp_path, payload) -> None:
    payload["allowed_uncertain_rejected_ids"][0] = payload["allowed_rejected_ids"][0]
    with pytest.raises(RuntimeError, match="relevant and uncertain decisions overlap"):
        load_manifest(_write(tmp_path, payload))


def test_manifest_rejects_contract_count_changes(tmp_path, payload) -> None:
    payload["expected_counts"]["allowed_rejected"] += 1
    with pytest.raises(RuntimeError, match="expected count"):
        load_manifest(_write(tmp_path, payload))


def test_manifest_rejects_missing_reviewed_decisions(tmp_path, payload) -> None:
    payload["allowed_rejected_ids"].pop()
    with pytest.raises(RuntimeError, match="contains .* IDs; expected"):
        load_manifest(_write(tmp_path, payload))


@pytest.mark.parametrize("ceiling", [True, -1, EXPECTED_ALLOWED_UNCERTAIN_REJECTED + 1])
def test_manifest_rejects_invalid_or_excessive_ceiling(tmp_path, payload, ceiling) -> None:
    payload["expected_counts"]["max_uncertain_rejected"] = ceiling
    with pytest.raises(RuntimeError, match="ceiling|non-negative integer"):
        load_manifest(_write(tmp_path, payload))


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("kind", "count_only", "id_allowlist_and_aggregate_count_ceiling"),
        ("id_level_review", False, "ID-level review exists"),
        ("allowed_rejected", 0, "allowlist count disagrees"),
        ("maximum_rejected", 0, "ceiling disagrees"),
        ("basis", "", "non-empty"),
        ("method", None, "non-empty"),
        ("status", " ", "non-empty"),
    ],
)
def test_manifest_requires_consistent_review_provenance(
    tmp_path,
    payload,
    key,
    value,
    message,
) -> None:
    payload["provenance"]["uncertain_rejection_control"][key] = value
    with pytest.raises(RuntimeError, match=message):
        load_manifest(_write(tmp_path, payload))


@pytest.mark.parametrize(("source", "digest"), [("database", "A" * 64), ("baseline", "b")])
def test_manifest_requires_exact_sha256_provenance(tmp_path, payload, source, digest) -> None:
    payload["provenance"][source]["sha256"] = digest
    with pytest.raises(RuntimeError, match="lowercase SHA-256"):
        load_manifest(_write(tmp_path, payload))
