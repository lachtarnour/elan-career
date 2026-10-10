"""Shared audit and normalization helpers for processing phases."""

from __future__ import annotations

from smartapply.company_identity import is_anonymous_company
from smartapply.jobsearch.archive_reasons import decisive_rejection_reasons

_GENERIC_LOCATION_MARKERS = {
    "",
    "france",
    "remote",
    "remote france",
    "remote (france)",
    "remote fr",
    "teletravail",
    "hybride",
    "ile-de-france",
    "ile de france",
    "idf",
}


def _rejection_audit_components(stage: str, reasons: list[str]) -> dict[str, object]:
    clean_reasons = [str(reason) for reason in reasons if str(reason).strip()]
    rejection_reasons = decisive_rejection_reasons(clean_reasons)
    return {
        "reasons": clean_reasons,
        "rejection_stage": stage,
        "rejection_reasons": rejection_reasons,
        "rejection_summary": " · ".join(rejection_reasons[:5]) or stage,
    }


def _is_anonymous_company(name: str | None) -> bool:
    return is_anonymous_company(name)


def _should_replace_job_location(current: str | None, extracted: str | None) -> bool:
    extracted = " ".join((extracted or "").split())
    if not extracted:
        return False
    current_value = " ".join((current or "").split())
    if not current_value:
        return True
    current_norm = current_value.lower()
    extracted_norm = extracted.lower()
    if extracted_norm in current_norm or current_norm in extracted_norm:
        return False
    return current_norm in _GENERIC_LOCATION_MARKERS
