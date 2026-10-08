"""Explicit manual additions bypass automatic rejection, not workflow safety."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from smartapply.filtering.hard_reject.types import HardRejectResult
from smartapply.filtering.types import HasJobFields

MANUAL_ADDITION_REASON = "Manual override: filters bypassed for manual addition"


def has_manual_filter_bypass(components: object) -> bool:
    """Trust a recorded user action, never a source name or scraper metadata."""
    if not isinstance(components, Mapping):
        return False
    marker = components.get("manual_filter_bypass")
    return (
        isinstance(marker, Mapping)
        and marker.get("schema_version") == 1
        and marker.get("origin") == "manual_addition"
        and isinstance(marker.get("added_at"), str)
        and bool(marker["added_at"].strip())
    )


class ManualFilterBypassResult(HardRejectResult):
    """Route a manual addition without claiming that rejection gates were run."""

    def audit_payload(self) -> dict[str, object]:
        return {"skipped": True, "skip_reason": "manual_addition", "gates": []}


def manual_filter_bypass_result() -> HardRejectResult:
    return ManualFilterBypassResult(decisions=(), context_reasons=(MANUAL_ADDITION_REASON,))


class Rejector(Protocol):
    def evaluate(self, job: HasJobFields) -> HardRejectResult: ...


def evaluate_or_bypass(rejector: Rejector, job: HasJobFields) -> HardRejectResult:
    score = getattr(job, "score", None)
    if has_manual_filter_bypass(score.components if score is not None else None):
        return manual_filter_bypass_result()
    return rejector.evaluate(job)
