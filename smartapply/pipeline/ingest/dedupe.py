"""Ingestion-time duplicate matching helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from smartapply.offers import RawJob


@dataclass(frozen=True)
class _KnownJobIndex:
    external_ids: frozenset[str]
    application_urls: frozenset[str]
    native_offer_ids: frozenset[tuple[str, str]] = frozenset()

    @classmethod
    def from_jobs(cls, jobs: list[Any]) -> _KnownJobIndex:
        urls: set[str] = set()
        external_ids: set[str] = set()
        native_offer_ids: set[tuple[str, str]] = set()
        for job in jobs:
            if job.external_id:
                external_ids.add(str(job.external_id))
            native_key = _native_offer_key(job.source, job.source_data)
            if native_key is not None:
                native_offer_ids.add(native_key)
            url_key = _normalize_application_url(job.application_url)
            if url_key:
                urls.add(url_key)
        return cls(
            external_ids=frozenset(external_ids),
            application_urls=frozenset(urls),
            native_offer_ids=frozenset(native_offer_ids),
        )

    def matches(self, raw: RawJob) -> bool:
        """Return whether the raw job is known by id or normalized URL."""
        return self.matches_source_identity(raw) or self.matches_application_url(raw)

    def matches_source_identity(self, raw: RawJob) -> bool:
        """Recognize legacy France Travail rows by their durable source ID."""
        native_key = _native_offer_key(raw.source, raw.source_data)
        return self.matches_external_id(raw) or bool(
            native_key is not None and native_key in self.native_offer_ids
        )

    def matches_external_id(self, raw: RawJob) -> bool:
        return bool(raw.external_id and raw.external_id in self.external_ids)

    def matches_application_url(self, raw: RawJob) -> bool:
        url_key = _normalize_application_url(raw.application_url)
        return bool(url_key and url_key in self.application_urls)


def _native_offer_key(source: str, source_data: Any) -> tuple[str, str] | None:
    """Use the France Travail offer ID even when an old external hash differs."""
    if source != "francetravail" or not isinstance(source_data, dict):
        return None
    native_id = str(source_data.get("id") or "").strip().casefold()
    return (source, native_id) if native_id else None


def _normalize_application_url(url: str | None) -> str:
    value = (url or "").strip()
    if not value or value.startswith("mailto:"):
        return ""
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    host = parsed.netloc.lower().removeprefix("www.")
    if not host:
        return ""
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    query = _normalized_non_tracking_query(parsed.query)
    normalized = f"{host}{path}".lower()
    if query:
        normalized = f"{normalized}?{query}"
    return normalized


def _normalized_non_tracking_query(query: str) -> str:
    tracking_prefixes = ("utm_",)
    tracking_names = {
        "fbclid",
        "alternatechannel",
        "ebp",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "refid",
        "source",
        "trackingid",
        "utm",
    }
    kept = [
        (key, value)
        for key, value in parse_qsl(query, keep_blank_values=True)
        if key.lower() not in tracking_names and not key.lower().startswith(tracking_prefixes)
    ]
    return urlencode(sorted(kept), doseq=True)
