"""Conservative decisions about duplicate relations, independent of relevance.

Source identity and strict original-content copies have separate safety rules.
Pending links constrain the complete group but never supply positive evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import combinations
from types import SimpleNamespace
from typing import Any

from smartapply.company_identity import is_anonymous_company
from smartapply.dedup.automatic_copies import (
    choose_copy_keeper,
    copy_alias_can_be_archived,
    copy_is_active,
    strict_copy_match_is_safe,
)
from smartapply.dedup.copy_groups import group_strict_copies
from smartapply.dedup.deduplicator import (
    Deduplicator,
    linked_duplicate_components,
    normalize_company,
    offers_have_incompatible_precise_locations,
    offers_within_duplicate_time_window,
)
from smartapply.dedup.review_policy import strong_identity_keys

REVIEW_POLICY_VERSION = "duplicate-review-v2"
RELATION_FIELDS = (
    "possible_duplicate_of_id", "canonical_job_id", "duplicate_review_status",
    "duplicate_match_type", "duplicate_confidence",
)
AUTOMATIC_STATE_FIELDS = (*RELATION_FIELDS, "status", "archived_at")


def literal_label(value: Any) -> str:
    """Ignore presentation whitespace and case, preserving all role qualifiers."""
    return " ".join(str(value or "").casefold().split())


def metadata_contradicts(left: Any, right: Any) -> bool:
    """Require review for explicit source metadata differences, even with an ID."""
    if offers_have_incompatible_precise_locations(left, right):
        return True
    for field in ("title", "contract_type", "remote_policy"):
        a, b = literal_label(getattr(left, field, None)), literal_label(getattr(right, field, None))
        if a and b and a != b:
            return True
    left_company, right_company = getattr(left, "company", None), getattr(right, "company", None)
    return bool(
        not is_anonymous_company(left_company) and not is_anonymous_company(right_company)
        and normalize_company(left_company) and normalize_company(right_company)
        and normalize_company(left_company) != normalize_company(right_company)
    )


def _source_identity_match_is_safe(target: Any, reference: Any, jobs: Sequence[Any]) -> bool:
    """Check source identity and every pair in the complete resulting group."""
    if not (strong_identity_keys(target) & strong_identity_keys(reference)):
        return False
    if (getattr(reference, "canonical_job_id", None) is not None
            or getattr(reference, "duplicate_review_status", None) == "pending"):
        return False
    components = linked_duplicate_components(jobs)
    members = {id(target): target}
    members.update({id(member): member for member in components.get(id(reference), (reference,))})
    return all(
        offers_within_duplicate_time_window(left, right)
        and not metadata_contradicts(left, right)
        for left, right in combinations(members.values(), 2)
    )


def automatic_match_reason(target: Any, reference: Any, jobs: Sequence[Any]) -> str | None:
    """Return an automatic evidence rule; workflow eligibility is checked separately."""
    if _source_identity_match_is_safe(target, reference, jobs):
        return "source_identity"
    if strict_copy_match_is_safe(target, reference, jobs):
        return "strict_content_copy"
    return None


def can_auto_link_offer(target: Any, reference: Any, jobs: Sequence[Any]) -> bool:
    """Compatibility wrapper for callers that only need the evidence decision."""
    return automatic_match_reason(target, reference, jobs) is not None


def automatic_state(job: Any) -> dict[str, Any]:
    return {field: getattr(job, field, None) for field in AUTOMATIC_STATE_FIELDS}


@dataclass(frozen=True)
class ReviewDecision:
    job_id: int
    reference_id: int | None
    action: str
    reason: str
    before: dict[str, Any]
    after: dict[str, Any]


def _confirmed_groups(by_id: dict[int, Any]) -> dict[int, tuple[Any, ...]]:
    """Index positive identity evidence without using any pending edge."""
    def root(item: Any) -> int | None:
        seen: set[int] = set()
        while getattr(item, "canonical_job_id", None) is not None:
            if (item.id in seen or item.canonical_job_id not in by_id
                    or item.duplicate_review_status != "confirmed"):
                return None
            seen.add(item.id)
            item = by_id[item.canonical_job_id]
        return int(item.id)

    roots = {job_id: root(item) for job_id, item in by_id.items()}
    groups: dict[int, list[Any]] = {}
    for job_id, root_id in roots.items():
        if root_id is not None:
            groups.setdefault(root_id, []).append(by_id[job_id])
    return {job_id: tuple(groups.get(root_id, ())) for job_id, root_id in roots.items()}


def _has_only_filter_metadata(score: Any | None) -> bool:
    """A two-state filter label is not evidence of downstream ranking work."""
    if score is None:
        return True
    # The pipeline facade imports this policy, so load its shared contracts
    # only when an existing score needs inspection.
    from smartapply.pipeline.process.filter_state import current_hard_reject_disposition
    from smartapply.pipeline.process.score_components import FILTER_COMPONENT_KEYS

    components = getattr(score, "components", None)
    allowed_keys = FILTER_COMPONENT_KEYS | {"filter_label_completion"}
    return bool(
        isinstance(components, Mapping)
        and set(components) <= allowed_keys
        and current_hard_reject_disposition(components) is not None
        and all(getattr(score, field, None) is None for field in (
            "rule_based_score", "semantic_score", "skill_score", "title_score",
            "seniority_score", "location_score", "domain_score", "final_score",
        ))
    )


def _unused_alias(job: Any) -> bool:
    return bool(
        getattr(job, "source", "") != "manual"
        and getattr(job, "status", None) == "scraped"
        and getattr(job, "application", None) is None
        and getattr(job, "analysis", None) is None
        and _has_only_filter_metadata(getattr(job, "score", None))
        and copy_alias_can_be_archived(job)
        and all(getattr(job, field, None) is None for field in (
            "canonical_job_id", "archived_at", "filtered_at", "ranked_at",
            "analyzed_at", "shortlisted_at", "shortlist_origin",
        ))
    )


def plan_pending_review_decisions(jobs: Sequence[Any]) -> tuple[ReviewDecision, ...]:
    """Remove unsupported edges before checking complete remaining groups.

    A small planning snapshot makes the graph evolve as relations are released.
    The supplied ORM/source objects remain untouched, including their history.
    """
    fields = (*AUTOMATIC_STATE_FIELDS, "id", "title", "company", "location", "source",
              "source_data", "application_url", "external_id", "published_date", "scraped_at",
              "contract_type", "remote_policy", "description", "cleaned_description",
              "application", "analysis", "score", "filtered_at", "ranked_at", "analyzed_at",
              "shortlisted_at", "shortlist_origin")
    shadow = [SimpleNamespace(**{field: getattr(job, field, None) for field in fields}) for job in jobs]
    by_id = {int(job.id): job for job in shadow}
    evidence_groups = _confirmed_groups(by_id)
    identities = {job.id: strong_identity_keys(job) for job in shadow}
    pending = sorted((job for job in shadow if job.duplicate_review_status == "pending"),
                     key=lambda item: int(item.id))
    decisions: list[ReviewDecision] = []

    def change(job: Any, action: str, reason: str, **updates: Any) -> None:
        before = automatic_state(job)
        after = {**before, **updates}
        reference_id = after["canonical_job_id"] if action == "confirm" else job.possible_duplicate_of_id
        decisions.append(ReviewDecision(int(job.id), reference_id,
                                        action, reason, before, after))
        for field, value in updates.items():
            setattr(job, field, value)

    def release(job: Any, reason: str) -> None:
        change(job, "release", reason, possible_duplicate_of_id=None,
               duplicate_review_status="none", duplicate_match_type=None,
               duplicate_confidence=None)

    def has_identity(job: Any, reference: Any) -> bool:
        return any(identities[job.id] & identities[member.id]
                   for member in evidence_groups.get(reference.id, ()) if member.id != job.id)

    # Direct contradictions must disappear before a pending sibling is used
    # as a constraint. Otherwise a false Paris/Lyon edge could invalidate a
    # legitimate Lyon/Lyon edge farther along the chain.
    for job in pending:
        reference = by_id.get(job.possible_duplicate_of_id)
        if reference is None:
            release(job, "missing_reference")
            continue
        if reference.id == job.id:
            release(job, "self_reference")
            continue
        members = tuple(member for member in evidence_groups.get(reference.id, ())
                        if member.id != job.id)
        if any(not offers_within_duplicate_time_window(job, member) for member in members):
            release(job, "outside_30_day_window")
            continue
        strong = has_identity(job, reference)
        if not strong and any(offers_have_incompatible_precise_locations(job, member)
                              for member in members):
            release(job, "incompatible_location")
        elif not strong and job.duplicate_match_type == "fuzzy":
            if Deduplicator().find_probable_duplicate(job, members) is None:
                release(job, "unsupported_text_match")
        elif job.duplicate_match_type == "exact_url" and not strong:
            match = Deduplicator().find_probable_duplicate(job, members)
            if match is None:
                release(job, "unverified_url_identity")
            else:
                change(job, "reclassify", "url_requires_text_review",
                       duplicate_match_type="fuzzy", duplicate_confidence=match.confidence)

    components = linked_duplicate_components(shadow)
    for job in pending:
        if job.duplicate_review_status != "pending":
            continue
        reference = by_id.get(job.possible_duplicate_of_id)
        if reference is None:
            continue
        members = components.get(id(reference), (reference,))
        if any(not offers_within_duplicate_time_window(job, member) for member in members):
            release(job, "outside_30_day_window")
            components = linked_duplicate_components(shadow)
        elif not has_identity(job, reference) and any(
            offers_have_incompatible_precise_locations(job, member) for member in members
        ):
            release(job, "incompatible_location")
            components = linked_duplicate_components(shadow)

    for job in pending:
        reference = by_id.get(job.possible_duplicate_of_id)
        if (job.duplicate_review_status == "pending" and reference is not None
                and _unused_alias(job)
                and _source_identity_match_is_safe(job, reference, shadow)):
            change(job, "confirm", "source_identity", possible_duplicate_of_id=None,
                   canonical_job_id=int(reference.id), duplicate_review_status="confirmed",
                   duplicate_match_type="source_identity", duplicate_confidence=1.0,
                   status="archived", archived_at=datetime.now(timezone.utc))

    _plan_strict_copy_confirmations(shadow, change)
    return tuple(decisions)


def _plan_strict_copy_confirmations(jobs: Sequence[Any], change) -> None:
    """Resolve proven copy edges to a stable keeper without moving any dossiers."""
    by_id = {int(job.id): job for job in jobs}
    for group_ids in group_strict_copies(jobs):
        group = [by_id[job_id] for job_id in group_ids]
        pending = [job for job in group if job.duplicate_review_status == "pending"]
        if not pending:
            continue
        # A clean reference can be replaced by a newer pending copy. Only
        # roots that participate in an existing copy relation are considered.
        participants: set[int] = set()
        for job in pending:
            reference = _canonical_reference(by_id.get(job.possible_duplicate_of_id), by_id)
            if job.possible_duplicate_of_id in group_ids:
                participants.update((int(job.id), int(job.possible_duplicate_of_id)))
            if reference is not None and int(reference.id) in group_ids:
                participants.add(int(job.id))
                participants.add(int(reference.id))
        candidates = [job for job in group if int(job.id) in participants]
        protected = [job for job in candidates if job.canonical_job_id is None
                     and not copy_alias_can_be_archived(job)]
        upstream_roots = [job for job in candidates if job.duplicate_review_status == "pending"
                          and job.possible_duplicate_of_id not in group_ids]
        # A root's unrelated fuzzy upstream review must survive. Keeping that
        # root avoids silently transferring or discarding an unproven edge.
        keeper = choose_copy_keeper(protected or upstream_roots or candidates)
        if keeper is None:
            continue
        aliases = [
            job for job in candidates
            if job is not keeper and copy_alias_can_be_archived(job)
            and not (job.duplicate_review_status == "pending"
                     and job.possible_duplicate_of_id not in group_ids)
            and not (copy_is_active(job) and not copy_is_active(keeper))
            and strict_copy_match_is_safe(job, keeper, jobs)
        ]
        if not aliases:
            continue
        alias_ids = {int(job.id) for job in aliases}
        if keeper.duplicate_review_status == "pending" and keeper.possible_duplicate_of_id in alias_ids:
            change(keeper, "release", "strict_copy_canonical_keeper",
                   possible_duplicate_of_id=None, duplicate_review_status="none",
                   duplicate_match_type=None, duplicate_confidence=None)
        for alias in sorted(aliases, key=lambda item: int(item.id)):
            change(alias, "confirm", "strict_content_copy", possible_duplicate_of_id=None,
                   canonical_job_id=int(keeper.id), duplicate_review_status="confirmed",
                   duplicate_match_type="strict_content_copy", duplicate_confidence=1.0,
                   status="archived", archived_at=alias.archived_at or datetime.now(timezone.utc))


def _canonical_reference(job: Any | None, by_id: dict[int, Any]) -> Any | None:
    """Follow confirmed aliases only; pending edges are never canonical evidence."""
    seen: set[int] = set()
    while job is not None and job.canonical_job_id is not None:
        if job.id in seen or job.duplicate_review_status != "confirmed":
            return None
        seen.add(int(job.id))
        job = by_id.get(job.canonical_job_id)
    return job
