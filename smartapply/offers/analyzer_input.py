"""Canonical input object for the job-analysis LLM call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from smartapply.company_identity import source_company_of
from smartapply.offers.source_metadata import build_analyzer_source_metadata
from smartapply.offers.sources import get_offer_source_adapter


@dataclass(frozen=True, slots=True)
class AnalyzerInput:
    """Stable payload consumed by the job-analysis prompt builder."""

    title: str
    company: str
    location: str | None
    application_url: str | None
    offer_body: str
    source: str
    source_metadata: str = ""


def build_analyzer_input(job: Any) -> AnalyzerInput:
    """Normalize a persisted job into the LLM analyzer input contract."""
    source = str(getattr(job, "source", "") or "")
    offer_body = _build_offer_body(job, source)
    return AnalyzerInput(
        title=str(getattr(job, "title", "") or ""),
        company=str(source_company_of(job) or ""),
        location=getattr(job, "location", None),
        application_url=getattr(job, "application_url", None),
        offer_body=offer_body,
        source=source,
        source_metadata=build_analyzer_source_metadata(job),
    )


def _build_offer_body(job: Any, source: str) -> str:
    base_body = str(
        getattr(job, "cleaned_description", None) or getattr(job, "description", "") or ""
    )
    normalized_source = source.strip().lower()
    adapter = get_offer_source_adapter(normalized_source)
    if adapter is None:
        return base_body
    return adapter.build_offer_body(base_body, getattr(job, "source_data", None))
