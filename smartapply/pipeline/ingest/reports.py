"""Ingestion report models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from smartapply.offers import RawJob


@dataclass
class IngestReport:
    source: str
    fetched: int
    persisted: int
    job_ids: list[int] = field(default_factory=list)
    inserted: int = 0
    updated_pending: int = 0
    skipped_processed: int = 0
    skipped_existing_during_collect: int = 0
    skipped_existing_during_persist: int = 0
    # Raw offers whose external_id was already in the DB and were dropped
    # after collection. LinkedIn and France Travail keep these rows inside
    # their per-query raw-result limit; this is not a count of replacement rows.
    skipped_known_during_collect: int = 0
    # True when the collector stopped because it hit the raw-fetch safety
    # cap instead of either reaching ``max_results`` new offers or exhausting
    # the source. SerpApi uses a strict cap equal to ``max_results``;
    # LinkedIn/France Travail have no global cap; other sources use extra scan headroom.
    hit_raw_seen_cap: bool = False
    # True when collection stopped because the UI/user cancellation callback
    # requested a cooperative stop.
    cancelled: bool = False
    search_audit: list[dict[str, Any]] = field(default_factory=list)
    # Recoverable source failures (for example one unreadable WTTJ detail)
    # that did not invalidate the offers successfully collected.
    warnings: list[str] = field(default_factory=list)
    # Probable cross-source duplicates are persisted but held for human review.
    duplicate_review_ids: list[int] = field(default_factory=list)
    # Certain URL matches are retained as archived source aliases.
    aliases_created: int = 0
    # Actor datasets returned by collection. Their recovery journal stays in
    # place if skipped_busy is nonzero, so paid results can be replayed.
    completed_source_run_ids: list[str] = field(default_factory=list)
    # Collected rows whose existing offer or duplicate group is in use. They
    # do not interrupt persistence of unrelated rows from the same source.
    skipped_busy: int = 0


@dataclass(frozen=True)
class IngestCollection:
    """Network-only collection result, persisted later by one DB writer."""

    source: str
    raw_jobs: list[RawJob]
    search_audit: list[dict[str, Any]] = field(default_factory=list)
    skipped_known_during_collect: int = 0
    skipped_existing_during_collect: int = 0
    hit_raw_seen_cap: bool = False
    cancelled: bool = False
    warnings: list[str] = field(default_factory=list)
    completed_source_run_ids: list[str] = field(default_factory=list)
    source_run_journal_path: str | None = None


# How many raw offers a single non-strict source run is allowed to *examine*
# before giving up. SerpApi is intentionally stricter in ``collection.py``;
# LinkedIn/France Travail instead bound each query without capping the combined results.
_RAW_SEEN_MULTIPLIER = 10
_RAW_SEEN_MIN = 50


@dataclass(frozen=True)
class _CollectResult:
    """Outcome of one ``_collect_round_robin`` invocation."""

    raw_jobs: list[RawJob]
    skipped_known: int
    skipped_existing: int
    hit_raw_seen_cap: bool
    cancelled: bool = False
    warnings: list[str] = field(default_factory=list)
    completed_source_run_ids: list[str] = field(default_factory=list)
