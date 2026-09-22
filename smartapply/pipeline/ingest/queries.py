"""Source-aware query expansion for ingestion."""

from __future__ import annotations

import re
from dataclasses import dataclass

from smartapply.pipeline.ingest.role_families import (
    expand_role_terms,
    normalize_role_title,
)

OR_SPLIT_RE = re.compile(r"\s+\bOR\b\s+", flags=re.IGNORECASE)
RESEARCH_CDD_SEARCH_TERMS = ("thèse", "doctorat", "PhD")


@dataclass(frozen=True)
class SourceQueryPlan:
    """Primary user searches followed by family-alias fallbacks."""

    primary: tuple[str, ...]
    fallbacks: tuple[str, ...] = ()

    @property
    def all_queries(self) -> tuple[str, ...]:
        return self.primary + self.fallbacks


def split_or_query(query: str) -> list[str]:
    """Split a user query like ``A OR B OR C`` into concrete searches.

    Job APIs do not all interpret boolean syntax consistently. Running one
    precise search per role is more predictable, then persistence de-duplicates
    the offers by external id.
    """
    parts = [part.strip() for part in OR_SPLIT_RE.split(query.strip())]
    return [part for part in parts if part] or [query.strip()]


def expand_query_for_source(source: str, query: str) -> list[str]:
    """Expand one title to all aliases in every family it belongs to."""
    normalized = re.sub(r"\s+", " ", query.strip())
    source_key = source.strip().lower()
    if source_key == "welcometothejungle":
        return [normalized]
    suffix = ""
    if normalized.lower().endswith(" cdi"):
        normalized = re.sub(r"\s+cdi$", "", normalized, flags=re.IGNORECASE).strip()
        if source_key != "serpapi":
            suffix = " CDI"
    return [f"{alias}{suffix}" for alias in expand_role_terms([normalized])]


def build_source_queries(
    source: str,
    query: str,
    *,
    split_or: bool = True,
    include_research_cdd: bool = False,
) -> list[str]:
    """Build the concrete query lanes required by one source.

    Family detection always examines individual ``OR`` terms. SerpAPI receives
    their expanded union as one boolean query, France Travail and LinkedIn use
    one lane per title, and WTTJ keeps its single query-agnostic feed. When
    ``include_research_cdd`` is enabled, dedicated doctoral-title lanes are
    added without a CDI suffix so CDD thesis offers can reach the local filter.
    """
    return list(
        build_source_query_plan(
            source,
            query,
            split_or=split_or,
            include_research_cdd=include_research_cdd,
        ).all_queries
    )


def build_source_query_plan(
    source: str,
    query: str,
    *,
    split_or: bool = True,
    include_research_cdd: bool = False,
) -> SourceQueryPlan:
    """Separate explicit role searches from interleaved alias fallbacks."""
    source_key = source.strip().lower()
    cleaned = query.strip()
    if source_key == "welcometothejungle" or not split_or:
        return SourceQueryPlan(primary=(cleaned,))

    parts = split_or_query(cleaned)
    all_cdi = bool(parts) and all(part.lower().endswith(" cdi") for part in parts)
    family_terms = [re.sub(r"\s+cdi$", "", part, flags=re.IGNORECASE).strip() for part in parts]
    primary_terms = _stable_unique_terms(family_terms)
    expanded = expand_role_terms(family_terms)
    if all_cdi and source_key != "serpapi" and not include_research_cdd:
        expanded = [f"{candidate} CDI" for candidate in expanded]
    if source_key == "serpapi":
        if include_research_cdd:
            expanded = expanded + _research_query_variants(primary_terms)
        return SourceQueryPlan(primary=(" OR ".join(_unique_queries(expanded)),))
    primary_count = min(len(primary_terms), len(expanded))
    primary = expanded[:primary_count]
    fallbacks = expanded[primary_count:]
    if include_research_cdd:
        primary = primary + _research_query_variants(primary)
        fallbacks = fallbacks + _research_query_variants(fallbacks)
    return SourceQueryPlan(
        primary=tuple(primary),
        fallbacks=tuple(fallbacks),
    )


def _research_query_variants(terms: list[str]) -> list[str]:
    return [
        f"{term} {research_term}"
        for term in terms
        for research_term in RESEARCH_CDD_SEARCH_TERMS
    ]


def _unique_queries(queries: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = re.sub(r"\s+", " ", query.strip()).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(query.strip())
    return unique


def _stable_unique_terms(terms: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = normalize_role_title(term)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(term)
    return unique
