from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from scripts.replay_hard_reject_audit import (
    DEFAULT_REASON_CORRECTIONS,
    EXPECTED_AUDITED_IDS,
    EXPECTED_IDENTITY_ONLY_IDS,
    AuditExpectation,
    AuditReasonCorrection,
    _finding_classification,
    _load_expectations,
    _load_jobs,
    _load_old_rejections,
    _load_reason_corrections,
)

AUDIT_DIR = Path("artifacts/classification-audit-20261005")
PRIVATE_AUDIT_AVAILABLE = all(
    (AUDIT_DIR / filename).is_file()
    for filename in (
        "exhaustive-role.json",
        "exhaustive-experience.json",
        "exhaustive-contract.json",
        "exhaustive-score.json",
        "exhaustive-duplicates.json",
        "exhaustive-summary.json",
        "agent-review-rejected-rows.json",
        "before.db",
    )
)


@pytest.mark.skipif(not PRIVATE_AUDIT_AVAILABLE, reason="private exhaustive audit unavailable")
def test_frozen_audit_has_exact_semantic_and_identity_coverage() -> None:
    expectations, identity_reasons = _load_expectations(AUDIT_DIR)
    corrections = _load_reason_corrections(DEFAULT_REASON_CORRECTIONS, expectations)
    old_rejections = _load_old_rejections(AUDIT_DIR, set(expectations))
    cohorts = Counter(expectation.cohort for expectation in expectations.values())

    assert len(expectations) == EXPECTED_AUDITED_IDS
    assert len(old_rejections) == EXPECTED_AUDITED_IDS
    assert cohorts == {
        "protected_rejection": 5_665,
        "certain_false_rejection": 61,
        "probable_false_rejection": 83,
        "ambiguous": 146,
        "identity_only": EXPECTED_IDENTITY_ONLY_IDS,
    }
    assert identity_reasons == {
        "not_a_distinct_lost_opportunity": 783,
        "ambiguous_duplicate_identity": 25,
    }
    assert {job_id: correction.gate for job_id, correction in corrections.items()} == {
        450: "experience",
        2263: "experience",
        2625: "seniority",
        5583: "experience",
        8621: "experience",
        9644: "experience",
    }


def test_tracked_reason_corrections_are_gate_specific_and_validated() -> None:
    expected_gates = {
        450: "experience",
        2263: "experience",
        2625: "seniority",
        5583: "experience",
        8621: "experience",
        9644: "experience",
    }
    expectations = {
        job_id: AuditExpectation(
            id=job_id,
            audit_file="fixture.json",
            audit_decision="valid_rejection",
            cohort="protected_rejection",
        )
        for job_id in expected_gates
    }

    corrections = _load_reason_corrections(DEFAULT_REASON_CORRECTIONS, expectations)

    assert {job_id: item.gate for job_id, item in corrections.items()} == expected_gates


def test_policy_corrections_and_relevance_handoffs_are_distinct_from_regressions() -> None:
    correction = AuditReasonCorrection(
        id=450,
        gate="experience",
        explanation="The visible requirement is two years.",
    )

    released_exception = _finding_classification(
        cohort="protected_rejection",
        rejected=False,
        correction=correction,
    )
    rejected_by_another_gate = _finding_classification(
        cohort="protected_rejection",
        rejected=True,
        correction=correction,
    )
    corrected_gate_regression = _finding_classification(
        cohort="protected_rejection",
        rejected=True,
        correction=correction,
        corrected_gate_blocked=True,
    )
    protected_handoff = _finding_classification(
        cohort="protected_rejection",
        rejected=False,
    )
    confirmed_regression = _finding_classification(
        cohort="certain_false_rejection",
        rejected=True,
    )

    assert released_exception is not None
    assert released_exception[0] == "policy_correction_exception"
    assert rejected_by_another_gate is not None
    assert rejected_by_another_gate[0] == "policy_correction_satisfied_other_gate_rejected"
    assert corrected_gate_regression is not None
    assert corrected_gate_regression[0] == "policy_correction_regression"
    assert protected_handoff is not None
    assert protected_handoff[0] == "protected_release_pending_relevance"
    assert confirmed_regression is not None
    assert confirmed_regression[0] == "true_regression"


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_replay_refuses_journal_content_that_immutable_sqlite_would_ignore(
    tmp_path: Path,
    suffix: str,
) -> None:
    database = tmp_path / "before.db"
    database.touch()
    Path(f"{database}{suffix}").write_bytes(b"uncheckpointed")

    with pytest.raises(RuntimeError, match="non-empty SQLite sidecar"):
        _load_jobs(database, {1})


def test_replay_refuses_corrupt_frozen_database(tmp_path: Path) -> None:
    database = tmp_path / "before.db"
    database.write_bytes(b"not a sqlite database")

    with pytest.raises(RuntimeError, match="integrity check failed"):
        _load_jobs(database, {1})
