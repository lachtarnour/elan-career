"""Canonical URL identities shared by ingestion and duplicate repair."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

_LINKEDIN_JOB_ID_RE = re.compile(r"(?:^|-)(?P<job_id>\d{6,})$")
_TRACKING_NAMES = frozenset(
    {
        "alternatechannel",
        "ebp",
        "fbclid",
        "gclid",
        "mc_cid",
        "mc_eid",
        "ref",
        "refid",
        "source",
        "trackingid",
        "utm",
    }
)


def normalize_application_url(url: str | None) -> str:
    """Return one stable comparison key without URL tracking noise."""
    parsed = _parse_url(url)
    if parsed is None:
        return ""
    host = parsed.netloc.casefold().removeprefix("www.")
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    linkedin_job_id = _linkedin_job_id(host, path)
    if linkedin_job_id:
        return f"linkedin.com/jobs/view/{linkedin_job_id}"
    if host.endswith(".linkedin.com"):
        host = "linkedin.com"
    query = urlencode(
        sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in _TRACKING_NAMES and not key.casefold().startswith("utm_")
        ),
        doseq=True,
    )
    normalized = f"{host}{path.casefold()}"
    return f"{normalized}?{query}" if query else normalized


def is_direct_offer_url(url: str | None) -> bool:
    """Return whether a URL identifies one offer rather than a result page."""
    parsed = _parse_url(url)
    if parsed is None:
        return False
    host = parsed.netloc.casefold().removeprefix("www.")
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    path_key = path.casefold()
    if "/offres/recherche/detail/" in path_key:
        return True
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        return _linkedin_job_id(host, path) is not None

    segments = [segment.casefold() for segment in path.split("/") if segment]
    generic_leafs = {
        "alert",
        "alerts",
        "collection",
        "collections",
        "recommended",
        "results",
        "saved",
        "search",
    }
    return any(
        marker in segments and segments[-1] != marker and segments[-1] not in generic_leafs
        for marker in ("jobs", "job", "vacancies", "vacancy", "offers", "offer")
    )


def direct_offer_url_key(url: str | None) -> str | None:
    """Return a canonical key only when ``url`` identifies one direct offer."""
    return normalize_application_url(url) if is_direct_offer_url(url) else None


def _parse_url(url: str | None):  # noqa: ANN202
    value = str(url or "").strip()
    if not value or value.casefold().startswith("mailto:"):
        return None
    try:
        parsed = urlsplit(value if "://" in value else f"https://{value}")
    except ValueError:
        return None
    return parsed if parsed.netloc else None


def _linkedin_job_id(host: str, path: str) -> str | None:
    if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    segments = [segment.casefold() for segment in path.split("/") if segment]
    if len(segments) < 3 or segments[-3:-1] != ["jobs", "view"]:
        return None
    match = _LINKEDIN_JOB_ID_RE.search(segments[-1])
    return match.group("job_id") if match else None


__all__ = ["direct_offer_url_key", "is_direct_offer_url", "normalize_application_url"]
