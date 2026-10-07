"""Exact-content proof and workflow guards for automatic duplicate copies.

Ordinary deterministic scores are retained on an archived source alias. A
manual decision or an application workflow always keeps its own offer row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

from smartapply.dedup.copy_groups import strict_copy_fingerprint
from smartapply.dedup.deduplicator import (
    linked_duplicate_components,
    offers_have_incompatible_precise_locations,
    offers_within_duplicate_time_window,
)


def copy_alias_can_be_archived(job: Any) -> bool:
    """Allow machine-processed rows, while preserving every human/workflow choice."""
    if (
        getattr(job, "source", None) == "manual"
        or getattr(job, "canonical_job_id", None) is not None
        or getattr(job, "duplicate_review_status", None) == "rejected"
        or getattr(job, "application", None) is not None
        or getattr(job, "analysis", None) is not None
        or any(getattr(job, field, None) is not None for field in (
            "analyzed_at", "shortlisted_at", "shortlist_origin",
        ))
    ):
        return False

    score = getattr(job, "score", None)
    components = getattr(score, "components", None)
    components = components if isinstance(components, Mapping) else {}
    reasons = components.get("reasons")
    reasons = reasons if isinstance(reasons, (list, tuple)) else ()
    if (
        any(str(key).startswith("manual_") for key in components)
        or "reactivation" in components
        or any(str(reason).startswith("Manual override:") for reason in reasons)
        or components.get("rejection_stage") not in {None, "local_filter"}
    ):
        return False

    status = getattr(job, "status", None)
    archived_at = getattr(job, "archived_at", None)
    if status in {"scraped", "filtered"} and archived_at is None:
        return True
    if status != "archived":
        return False

    # Archived rows without an explicit machine rejection may be manual
    # archives. Never infer a user's choice from the numeric score alone.
    return bool(
        components.get("rejection_stage") == "local_filter"
        and components.get("filter_disposition") == "rejected"
        and components.get("rejection_reasons")
    )


def strict_copy_match_is_safe(target: Any, reference: Any, jobs: Sequence[Any]) -> bool:
    """Prove a literal copy, including every linked group's pairwise constraints."""
    if target is reference or getattr(reference, "canonical_job_id", None) is not None:
        return False
    if copy_is_active(target) and not copy_is_active(reference):
        return False
    fingerprint = strict_copy_fingerprint(target)
    if fingerprint is None or fingerprint != strict_copy_fingerprint(reference):
        return False

    components = linked_duplicate_components(jobs)
    members_by_identity = {
        id(item): item
        for candidate in (target, reference)
        for item in components.get(id(candidate), (candidate,))
    }
    return all(
        offers_within_duplicate_time_window(left, right)
        and not offers_have_incompatible_precise_locations(left, right)
        and _literal_metadata_agrees(left, right)
        for left, right in combinations(members_by_identity.values(), 2)
    )


def choose_copy_keeper(jobs: Sequence[Any]) -> Any | None:
    """Prefer protected history, then an active copy, then the latest publication."""
    roots = [job for job in jobs if getattr(job, "canonical_job_id", None) is None]
    if not roots:
        return None
    protected = [job for job in roots if not copy_alias_can_be_archived(job)]
    candidates = protected or [job for job in roots if copy_is_active(job)] or roots
    return max(candidates, key=_keeper_order)


def copy_is_active(job: Any) -> bool:
    """Preserve at least one visible copy when the previous reference is archived."""
    return getattr(job, "archived_at", None) is None and getattr(job, "status", None) != "archived"


def _literal_metadata_agrees(left: Any, right: Any) -> bool:
    for field in ("title", "company", "contract_type", "remote_policy"):
        a = " ".join(str(getattr(left, field, None) or "").casefold().split())
        b = " ".join(str(getattr(right, field, None) or "").casefold().split())
        if a and b and a != b:
            return False
    return True


def _keeper_order(job: Any) -> tuple[datetime, datetime, int]:
    minimum = datetime.min.replace(tzinfo=timezone.utc)

    def timestamp(field: str) -> datetime:
        value = getattr(job, field, None)
        if not isinstance(value, datetime):
            return minimum
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    published = timestamp("published_date")
    scraped = timestamp("scraped_at")
    return (published if published != minimum else scraped, scraped, int(job.id))
