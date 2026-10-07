"""Public report objects returned by pipeline phases.

Keeping these dataclasses in one small module makes the phase implementations
focus on orchestration while preserving their public return shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProcessReport:
    total: int
    kept_after_filter: int
    duplicates_removed: int
    top_ranked: int
    analyzed: int
    analysis_errors: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class LocalFilterReport:
    total: int
    kept: int
    rejected: int
    duplicates_removed: int
    kept_ids: list[int]
    rejected_ids: list[int]
    pending_relevance: int = 0
    pending_relevance_ids: list[int] = field(default_factory=list)


@dataclass
class RankingReport:
    total: int
    kept_after_filter: int
    duplicates_removed: int
    ranked: int
    shortlisted: int
    ranked_ids: list[int]
    shortlisted_ids: list[int]


@dataclass
class AnalyzeReport:
    requested: int
    already_analyzed: int
    analyzed: int
    skipped_missing: int
    errors: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class ApplyReport:
    job_id: int
    application_id: int | None
    docx_path: str | None = None
    cv_html_path: str | None = None
    cv_pdf_path: str | None = None
    letter_html_path: str | None = None
    letter_pdf_path: str | None = None
    form_url: str | None = None
    status: str | None = None
    validation_warnings: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    document_review: list[dict[str, Any]] = field(default_factory=list)
