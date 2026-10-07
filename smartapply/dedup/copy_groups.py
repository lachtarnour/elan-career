"""Pure evidence for exact-content copies and their presentation.

These helpers never mutate jobs. Automatic confirmation applies additional
workflow and full-group checks in automatic_copies and review_decisions.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from smartapply.company_identity import is_anonymous_company
from smartapply.dedup.deduplicator import (
    DUPLICATE_TIME_WINDOW,
    _precise_location,
    offers_have_incompatible_precise_locations,
)

STRICT_COPY_POLICY_VERSION = "strict-copy-v1"
STRICT_COPY_CLASSIFICATION_KEY_VERSION = "strict-copy-classification-v1"
MIN_STRICT_COPY_DESCRIPTION_CHARS = 200


def strict_copy_fingerprint(job: Any) -> str | None:
    """Return a stable fingerprint only for a sufficiently complete exact copy.

    Text normalization is intentionally limited to Unicode case folding and
    whitespace collapsing.  Punctuation, accents, seniority, contract words,
    numbers and the complete original description all remain significant.
    """

    title = _literal_text(getattr(job, "title", None))
    company = _literal_text(getattr(job, "company", None))
    contract = _literal_text(getattr(job, "contract_type", None))
    description = _literal_text(getattr(job, "description", None))
    if (
        not title
        or not company
        or is_anonymous_company(getattr(job, "company", None))
        or not contract
        or len(description) < MIN_STRICT_COPY_DESCRIPTION_CHARS
    ):
        return None

    location = _precise_location(job)
    if location.commune_code is None:
        return None
    employer_reference_is_valid, employer_reference = _explicit_employer_reference(job)
    if not employer_reference_is_valid:
        return None

    payload = {
        "company": company,
        "commune_code": location.commune_code.casefold(),
        "contract_type": contract,
        "description": description,
        "employer_reference": employer_reference,
        "remote_policy": _literal_text(getattr(job, "remote_policy", None)),
        "title": title,
    }
    digest = _stable_digest(payload)
    return f"{STRICT_COPY_POLICY_VERSION}:{digest}"


def strict_copy_classification_key(
    job: Any,
    *,
    profile_version: str,
    classifier_version: str,
    prompt_version: str,
    model_id: str,
) -> str | None:
    """Build a future-safe cache key without running any classifier.

    Every version is required even though the classifier is not implemented.
    This keeps a future caller from accidentally sharing results across a
    profile, classifier, prompt or model change.
    """

    fingerprint = strict_copy_fingerprint(job)
    if fingerprint is None:
        return None
    versions = {
        "classifier_version": _required_version("classifier_version", classifier_version),
        "model_id": _required_version("model_id", model_id),
        "profile_version": _required_version("profile_version", profile_version),
        "prompt_version": _required_version("prompt_version", prompt_version),
    }
    digest = _stable_digest(
        {
            "copy_fingerprint": fingerprint,
            "policy_version": STRICT_COPY_POLICY_VERSION,
            **versions,
        }
    )
    return f"{STRICT_COPY_CLASSIFICATION_KEY_VERSION}:{digest}"


def group_strict_copies(jobs: Iterable[Any]) -> tuple[tuple[int, ...], ...]:
    """Return deterministic, non-overlapping groups of strict-copy candidates.

    Every member must be within the inclusive 30-day window and geographically
    compatible with every other member.  Complete-link grouping prevents an
    intermediate J+20 offer from joining J0 to J+40 transitively.
    """

    buckets: dict[str, list[tuple[datetime, int, Any]]] = defaultdict(list)
    for job in jobs:
        fingerprint = strict_copy_fingerprint(job)
        job_id = _positive_job_id(job)
        timestamp = _offer_timestamp(job)
        if fingerprint is None or job_id is None or timestamp is None:
            continue
        buckets[fingerprint].append((timestamp, job_id, job))

    groups: list[tuple[int, ...]] = []
    for fingerprint in sorted(buckets):
        candidates = sorted(buckets[fingerprint], key=lambda row: (row[0], row[1]))
        clusters: list[list[tuple[datetime, int, Any]]] = []
        seen_ids: set[int] = set()
        for candidate in candidates:
            if candidate[1] in seen_ids:
                continue
            seen_ids.add(candidate[1])
            for cluster in clusters:
                if all(_pair_is_compatible(candidate, member) for member in cluster):
                    cluster.append(candidate)
                    break
            else:
                clusters.append([candidate])
        groups.extend(
            tuple(sorted(member[1] for member in cluster))
            for cluster in clusters
            if len(cluster) > 1
        )
    return tuple(sorted(groups))


def _literal_text(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _explicit_employer_reference(job: Any) -> tuple[bool, str]:
    """Return one employer-issued reference, ignoring source portal row IDs."""

    source_data = getattr(job, "source_data", None)
    if not isinstance(source_data, Mapping):
        return True, ""
    references = {
        normalized
        for field in ("employer_reference", "requisition_id")
        if (normalized := _literal_text(source_data.get(field)))
    }
    if len(references) > 1:
        return False, ""
    return True, next(iter(references), "")


def _stable_digest(payload: dict[str, str]) -> str:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _required_version(field: str, value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def _positive_job_id(job: Any) -> int | None:
    try:
        value = int(getattr(job, "id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _offer_timestamp(job: Any) -> datetime | None:
    value = getattr(job, "published_date", None) or getattr(job, "scraped_at", None)
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _pair_is_compatible(
    left: tuple[datetime, int, Any],
    right: tuple[datetime, int, Any],
) -> bool:
    return (
        abs(left[0] - right[0]) <= DUPLICATE_TIME_WINDOW
        and not offers_have_incompatible_precise_locations(left[2], right[2])
    )


__all__ = [
    "MIN_STRICT_COPY_DESCRIPTION_CHARS",
    "STRICT_COPY_CLASSIFICATION_KEY_VERSION",
    "STRICT_COPY_POLICY_VERSION",
    "group_strict_copies",
    "strict_copy_classification_key",
    "strict_copy_fingerprint",
]
