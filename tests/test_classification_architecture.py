"""Static contracts that keep rejection and relevance work independent."""

from __future__ import annotations

import ast
from pathlib import Path

from smartapply.filtering.hard_reject.types import (
    HardRejectDisposition,
    HardRejectResult,
)
from smartapply.filtering.types import FilterDisposition

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HARD_REJECT_ROOT = PROJECT_ROOT / "smartapply" / "filtering" / "hard_reject"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_hard_reject_result_cannot_express_relevance_or_a_score() -> None:
    assert {item.value for item in HardRejectDisposition} == {
        "rejected",
        "pending_relevance",
    }
    assert not hasattr(HardRejectResult(decisions=()), "score")


def test_persisted_filter_dispositions_match_deterministic_rejection() -> None:
    assert {item.value for item in FilterDisposition} == {
        item.value for item in HardRejectDisposition
    }


def test_hard_reject_does_not_import_relevance_ranking_or_llm() -> None:
    forbidden_prefixes = (
        "smartapply.filtering.relevance",
        "smartapply.llm",
    )
    violations = {
        str(path.relative_to(PROJECT_ROOT)): sorted(
            module
            for module in _imports(path)
            if module.startswith(forbidden_prefixes)
        )
        for path in HARD_REJECT_ROOT.glob("*.py")
    }
    assert not {path: modules for path, modules in violations.items() if modules}


def test_hard_reject_rules_cannot_branch_on_offer_identity() -> None:
    forbidden_attributes = {"id", "job_id", "external_id"}
    violations: dict[str, list[str]] = {}
    for path in HARD_REJECT_ROOT.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        attributes = sorted(
            {
                node.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and node.attr in forbidden_attributes
            }
        )
        if attributes:
            violations[str(path.relative_to(PROJECT_ROOT))] = attributes
    assert not violations
