from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.replay_hard_reject_positive_control as positive_control
from scripts.replay_hard_reject_positive_control import (
    DEFAULT_BASELINE,
    DEFAULT_DATABASE,
    DEFAULT_MANIFEST,
    EXPECTED_ALLOWED_REJECTED,
    EXPECTED_ALLOWED_UNCERTAIN_REJECTED,
    EXPECTED_AMBIGUOUS_KEEP,
    EXPECTED_FALSE_REGRESSIONS,
    EXPECTED_RELEVANT,
    EXPECTED_UNCERTAIN,
    PositiveControlManifest,
    _load_baseline,
    _load_jobs,
    _strict_findings,
    load_manifest,
)
from smartapply.maintenance._sqlite_safety import (
    UnsafeOutputPathError,
    file_sha256,
)


def _manifest(
    *,
    allowed: set[int],
    false_regressions: set[int] | None = None,
    ambiguous: set[int] | None = None,
    allowed_uncertain: set[int] | None = None,
    max_uncertain_rejected: int = 0,
) -> PositiveControlManifest:
    return PositiveControlManifest(
        allowed_rejected_ids=frozenset(allowed),
        false_regression_ids=frozenset(false_regressions or ()),
        ambiguous_keep_ids=frozenset(ambiguous or ()),
        allowed_uncertain_rejected_ids=frozenset(allowed_uncertain or ()),
        expected_relevant=EXPECTED_RELEVANT,
        expected_uncertain=EXPECTED_UNCERTAIN,
        max_uncertain_rejected=max_uncertain_rejected,
        database_sha256="0" * 64,
        baseline_sha256="0" * 64,
    )


def test_tracked_manifest_has_exact_disjoint_manual_decisions() -> None:
    manifest = load_manifest(DEFAULT_MANIFEST)

    assert len(manifest.allowed_rejected_ids) == EXPECTED_ALLOWED_REJECTED
    assert len(manifest.false_regression_ids) == EXPECTED_FALSE_REGRESSIONS
    assert len(manifest.ambiguous_keep_ids) == EXPECTED_AMBIGUOUS_KEEP
    assert (
        len(manifest.allowed_uncertain_rejected_ids)
        == EXPECTED_ALLOWED_UNCERTAIN_REJECTED
    )
    assert len(manifest.reviewed_relevant_ids) == 134
    assert len(manifest.reviewed_ids) == 134 + EXPECTED_ALLOWED_UNCERTAIN_REJECTED
    assert len(manifest.protected_keep_ids) == 32
    assert manifest.max_uncertain_rejected == EXPECTED_ALLOWED_UNCERTAIN_REJECTED
    assert not manifest.allowed_rejected_ids & manifest.protected_keep_ids
    assert not manifest.reviewed_relevant_ids & manifest.allowed_uncertain_rejected_ids


def test_manifest_refuses_overlapping_decision_categories(tmp_path: Path) -> None:
    payload = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    payload["false_regression_ids"].append(payload["allowed_rejected_ids"][0])
    path = tmp_path / "overlap.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="decision categories overlap"):
        load_manifest(path)


@pytest.mark.skipif(
    not (DEFAULT_BASELINE.is_file() and DEFAULT_DATABASE.is_file()),
    reason="private frozen positive-control inputs unavailable",
)
def test_manifest_provenance_and_frozen_baseline_counts_match() -> None:
    manifest = load_manifest(DEFAULT_MANIFEST)
    selected, dispositions, total = _load_baseline(
        DEFAULT_BASELINE,
        expected_relevant=manifest.expected_relevant,
        expected_uncertain=manifest.expected_uncertain,
    )

    assert file_sha256(DEFAULT_BASELINE) == manifest.baseline_sha256
    assert file_sha256(DEFAULT_DATABASE) == manifest.database_sha256
    assert total == 11_137
    assert dispositions == {
        "null": 421,
        "rejected": 6_763,
        "relevant": EXPECTED_RELEVANT,
        "uncertain": EXPECTED_UNCERTAIN,
    }
    assert len(selected) == EXPECTED_RELEVANT + EXPECTED_UNCERTAIN
    relevant_ids = {
        job_id for job_id, row in selected.items() if row.old_disposition == "relevant"
    }
    uncertain_ids = set(selected) - relevant_ids
    assert manifest.reviewed_relevant_ids <= relevant_ids
    assert manifest.allowed_uncertain_rejected_ids <= uncertain_ids


@pytest.mark.parametrize("uncertain_rejected_count", [0, 1, 2])
def test_strict_allows_reviewed_uncertain_rejections_up_to_manifest_ceiling(
    uncertain_rejected_count: int,
) -> None:
    uncertain_ids = {4, 5}
    findings = _strict_findings(
        relevant_ids={1},
        uncertain_ids=uncertain_ids,
        results={
            1: SimpleNamespace(rejected=False),
            **{
                job_id: SimpleNamespace(
                    rejected=index < uncertain_rejected_count
                )
                for index, job_id in enumerate(sorted(uncertain_ids))
            },
        },
        manifest=_manifest(
            allowed={1},
            allowed_uncertain=uncertain_ids,
            max_uncertain_rejected=2,
        ),
    )

    assert findings["passed"] is True
    assert findings["unexpected_relevant_rejected_ids"] == []
    assert findings["protected_keep_rejected_ids"] == []
    assert findings["unexpected_uncertain_rejected_ids"] == []
    assert findings["uncertain_rejected_count"] == uncertain_rejected_count
    assert findings["max_uncertain_rejected"] == 2
    assert findings["uncertain_rejection_ceiling_exceeded"] is False
    assert findings["uncertain_rejection_overage"] == 0


def test_strict_fails_when_uncertain_rejections_exceed_manifest_ceiling() -> None:
    findings = _strict_findings(
        relevant_ids={1},
        uncertain_ids={4, 5},
        results={
            1: SimpleNamespace(rejected=False),
            4: SimpleNamespace(rejected=True),
            5: SimpleNamespace(rejected=True),
        },
        manifest=_manifest(
            allowed={1},
            allowed_uncertain={4, 5},
            max_uncertain_rejected=1,
        ),
    )

    assert findings["passed"] is False
    assert findings["violation_ids"] == []
    assert findings["uncertain_rejected_ids"] == [4, 5]
    assert findings["uncertain_rejected_count"] == 2
    assert findings["max_uncertain_rejected"] == 1
    assert findings["uncertain_rejection_ceiling_exceeded"] is True
    assert findings["uncertain_rejection_overage"] == 1


def test_strict_fails_for_unreviewed_uncertain_rejection_below_ceiling() -> None:
    findings = _strict_findings(
        relevant_ids={1},
        uncertain_ids={4, 5},
        results={
            1: SimpleNamespace(rejected=False),
            4: SimpleNamespace(rejected=True),
            5: SimpleNamespace(rejected=False),
        },
        manifest=_manifest(
            allowed={1},
            allowed_uncertain={5},
            max_uncertain_rejected=2,
        ),
    )

    assert findings["passed"] is False
    assert findings["unexpected_uncertain_rejected_ids"] == [4]
    assert findings["violation_ids"] == [4]
    assert findings["uncertain_rejection_ceiling_exceeded"] is False


def test_strict_reports_protected_and_unreviewed_relevant_rejections() -> None:
    findings = _strict_findings(
        relevant_ids={1, 2, 3},
        uncertain_ids={4},
        results={
            1: SimpleNamespace(rejected=True),
            2: SimpleNamespace(rejected=True),
            3: SimpleNamespace(rejected=True),
            4: SimpleNamespace(rejected=True),
        },
        manifest=_manifest(
            allowed={1},
            false_regressions={2},
            allowed_uncertain={4},
            max_uncertain_rejected=1,
        ),
    )

    assert findings["passed"] is False
    assert findings["unexpected_relevant_rejected_ids"] == [2, 3]
    assert findings["protected_keep_rejected_ids"] == [2]
    assert findings["violation_ids"] == [2, 3]
    assert findings["uncertain_rejected_ids"] == [4]
    assert findings["unexpected_uncertain_rejected_ids"] == []
    assert findings["uncertain_rejection_ceiling_exceeded"] is False


@pytest.mark.parametrize("suffix", ["-wal", "-journal"])
def test_positive_control_refuses_journal_content_immutable_sqlite_would_ignore(
    tmp_path: Path,
    suffix: str,
) -> None:
    database = tmp_path / "before.db"
    database.touch()
    Path(f"{database}{suffix}").write_bytes(b"uncheckpointed")

    with pytest.raises(RuntimeError, match="non-empty SQLite sidecar"):
        _load_jobs(database, {1})


def test_positive_control_refuses_corrupt_frozen_database(tmp_path: Path) -> None:
    database = tmp_path / "before.db"
    database.write_bytes(b"not a sqlite database")

    with pytest.raises(RuntimeError, match="integrity check failed"):
        _load_jobs(database, {1})


@pytest.mark.parametrize("protected_input", ["baseline", "database", "manifest"])
def test_positive_control_cli_refuses_to_overwrite_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    protected_input: str,
) -> None:
    paths = {
        "baseline": tmp_path / "baseline.json",
        "database": tmp_path / "before.db",
        "manifest": tmp_path / "manifest.json",
    }
    for path in paths.values():
        path.write_bytes(f"{path.name} sentinel".encode())
    target = paths[protected_input]
    before = target.read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "replay_hard_reject_positive_control.py",
            "--baseline",
            str(paths["baseline"]),
            "--database",
            str(paths["database"]),
            "--manifest",
            str(paths["manifest"]),
            "--output",
            str(target),
        ],
    )

    with pytest.raises(UnsafeOutputPathError):
        positive_control.main()

    assert target.read_bytes() == before


def test_positive_control_cli_strict_exits_after_rendering_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        positive_control,
        "replay",
        lambda *_args: {"strict": {"passed": False, "violation_ids": [7]}},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["replay_hard_reject_positive_control.py", "--strict"],
    )

    with pytest.raises(SystemExit) as exc_info:
        positive_control.main()

    assert exc_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["strict"]["violation_ids"] == [7]
