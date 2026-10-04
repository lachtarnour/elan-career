"""Cross-source job deduplication."""

from smartapply.dedup.deduplicator import (
    DUPLICATE_TIME_WINDOW,
    Deduplicator,
    DedupReport,
    DuplicateCandidate,
    JobLike,
    fuzzy_location_score,
    normalize_company,
    normalize_title,
    offers_within_duplicate_group_time_window,
    offers_within_duplicate_time_window,
)

__all__ = [
    "DUPLICATE_TIME_WINDOW",
    "DedupReport",
    "DuplicateCandidate",
    "Deduplicator",
    "JobLike",
    "fuzzy_location_score",
    "normalize_company",
    "normalize_title",
    "offers_within_duplicate_group_time_window",
    "offers_within_duplicate_time_window",
]
