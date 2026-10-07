"""Pure, conservative identity evidence for duplicate review policies.

The keys returned here are durable source identities.  They deliberately omit
fuzzy text similarity and generic-looking ``/jobs/...`` URLs: those signals can
prioritize a human review, but cannot prove that two rows describe one offer.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from smartapply.offer_identity import normalize_application_url

_LINKEDIN_ID_RE = re.compile(r"\d{6,}")
_FRANCE_TRAVAIL_ID_RE = re.compile(r"[0-9][a-z0-9]{6}", flags=re.IGNORECASE)
_LINKEDIN_URL_KEY_RE = re.compile(r"^linkedin\.com/jobs/view/(?P<id>\d{6,})$")
_FRANCE_TRAVAIL_URL_KEY_RE = re.compile(
    r"^(?:[a-z0-9-]+\.)*francetravail\.fr/offres/recherche/detail/"
    r"(?P<id>[0-9][a-z0-9]{6})$",
    flags=re.IGNORECASE,
)
_GENERIC_OFFER_LEAFS = frozenset(
    {"alert", "alerts", "collection", "collections", "detail", "results", "saved", "search"}
)


def strong_identity_keys(job: Any) -> frozenset[str]:
    """Return independently reproducible, source-namespaced offer identities.

    France Travail and LinkedIn are currently the only providers whose native
    identifier and direct-offer URL shapes are explicitly understood.  A URL
    discovered through an aggregator can still contribute a key when the URL
    itself is a validated provider URL.
    """

    identities: dict[str, set[str]] = defaultdict(set)
    source = _source_namespace(getattr(job, "source", None))
    source_data = getattr(job, "source_data", None)

    native_id = _trusted_native_id(source, source_data)
    if native_id is not None:
        identities[source].add(native_id)

    for url in _known_offer_urls(job, source_data):
        url_identity = _validated_provider_url_identity(url)
        if url_identity is not None:
            provider, native_id = url_identity
            identities[provider].add(native_id)

    # Conflicting evidence inside one provider is unsafe even when another
    # provider happens to expose a usable key.  Returning no evidence prevents
    # either side of the contradiction from becoming an automatic anchor.
    if any(len(native_ids) > 1 for native_ids in identities.values()):
        return frozenset()
    return frozenset(
        _offer_key(provider, native_id)
        for provider, native_ids in identities.items()
        for native_id in native_ids
    )


def _source_namespace(source: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(source or "").strip().casefold())


def _trusted_native_id(source: str, source_data: Any) -> str | None:
    if source not in {"francetravail", "linkedin"} or not isinstance(source_data, Mapping):
        return None
    value = str(source_data.get("id") or "").strip().casefold()
    if source == "linkedin":
        return value if _LINKEDIN_ID_RE.fullmatch(value) else None
    return _france_travail_id(value)


def _known_offer_urls(job: Any, source_data: Any) -> tuple[str, ...]:
    values: list[Any] = [getattr(job, "application_url", None)]
    if isinstance(source_data, Mapping):
        values.append(source_data.get("url"))
        normalized = source_data.get("_smartapply_normalized")
        if isinstance(normalized, Mapping):
            values.append(normalized.get("url"))
    return tuple(str(value).strip() for value in values if str(value or "").strip())


def _validated_provider_url_identity(url: str) -> tuple[str, str] | None:
    normalized = normalize_application_url(url).partition("?")[0]
    linkedin = _LINKEDIN_URL_KEY_RE.fullmatch(normalized)
    if linkedin is not None:
        return "linkedin", linkedin.group("id")
    france_travail = _FRANCE_TRAVAIL_URL_KEY_RE.fullmatch(normalized)
    if france_travail is not None:
        native_id = _france_travail_id(france_travail.group("id"))
        if native_id is not None:
            return "francetravail", native_id
    return None


def _france_travail_id(value: str) -> str | None:
    normalized = value.strip().casefold()
    if (
        normalized in _GENERIC_OFFER_LEAFS
        or _FRANCE_TRAVAIL_ID_RE.fullmatch(normalized) is None
        or not any(character.isdigit() for character in normalized)
    ):
        return None
    return normalized


def _offer_key(source: str, native_id: str) -> str:
    return f"offer:{source}:{native_id.strip().casefold()}"


__all__ = ["strong_identity_keys"]
