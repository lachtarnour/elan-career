"""Validate the persisted contract between hard rejection and later phases."""

from __future__ import annotations

from collections.abc import Mapping

from smartapply.filtering.hard_reject.types import (
    HardRejectDisposition,
    HardRejectResult,
)
from smartapply.filtering.manual_policy import has_manual_filter_bypass
from smartapply.jobsearch.offer_state import recorded_filter_disposition

MANUAL_KEEP_OVERRIDE_REASON = "Manual override: kept by user"
CURRENT_HARD_REJECT_RULESET = HardRejectResult(decisions=()).ruleset_version


def has_manual_keep_override(components: object) -> bool:
    """Return whether the persisted reasons contain the explicit keep choice."""

    if not isinstance(components, Mapping):
        return False
    reasons = components.get("reasons")
    return (
        isinstance(reasons, (list, tuple))
        and MANUAL_KEEP_OVERRIDE_REASON in reasons
    )


def current_hard_reject_disposition(
    components: object,
) -> HardRejectDisposition | None:
    """Return the persisted disposition only when its current proof agrees.

    A manual keep override remains a pending offer even though its audit records
    the blocker that was overridden.  The exact persisted reason makes that
    exception explicit and auditable.
    """

    if not isinstance(components, Mapping):
        return None
    try:
        disposition = HardRejectDisposition(recorded_filter_disposition(components))
    except (TypeError, ValueError):
        return None

    audit = components.get("hard_reject")
    if (
        disposition is HardRejectDisposition.PENDING_RELEVANCE
        and has_manual_filter_bypass(components)
        and isinstance(audit, Mapping)
        and audit.get("skipped") is True
        and audit.get("skip_reason") == "manual_addition"
    ):
        return disposition
    if (
        not isinstance(audit, Mapping)
        or audit.get("ruleset_version") != CURRENT_HARD_REJECT_RULESET
    ):
        return None

    rejected = audit.get("rejected")
    if disposition is HardRejectDisposition.REJECTED:
        return disposition if rejected is True else None
    if rejected is False:
        return disposition

    if rejected is True and has_manual_keep_override(components):
        return disposition
    return None


def is_current_hard_reject_survivor(components: object) -> bool:
    """Return whether later ranking and analysis may consume this state."""

    return (
        current_hard_reject_disposition(components)
        is HardRejectDisposition.PENDING_RELEVANCE
    )


__all__ = [
    "CURRENT_HARD_REJECT_RULESET",
    "MANUAL_KEEP_OVERRIDE_REASON",
    "current_hard_reject_disposition",
    "has_manual_keep_override",
    "is_current_hard_reject_survivor",
]
