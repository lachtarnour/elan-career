"""Normalize labels used when the hiring company is not disclosed."""

from __future__ import annotations

import unicodedata

_ANONYMOUS_COMPANY_MARKERS = (
    "anonyme",
    "confidentiel",
    "inconnu",
    "non communiqu",
    "non renseign",
    "not disclosed",
    "not provided",
    "unknown",
)
_UNRECOGNIZED_COMPANY_LABELS = {
    "non reconnu",
    "non reconnue",
    "non reconnus",
    "non reconnues",
    "entreprise non reconnue",
    "entreprises non reconnues",
    "employeur non reconnu",
    "employeurs non reconnus",
}


def is_anonymous_company(name: str | None) -> bool:
    """Return whether ``name`` explicitly means that no company is known."""
    normalized = " ".join((name or "").casefold().replace("’", "'").split())
    return (
        not normalized
        or normalized in _UNRECOGNIZED_COMPANY_LABELS
        or any(marker in normalized for marker in _ANONYMOUS_COMPANY_MARKERS)
    )


# Explicit publisher names only. Domains classify URLs, not company names;
# the mapping makes the few reliable name associations explicit.
_PUBLISHER_NAMES_BY_DOMAIN = {
    "linkedin.com": ("LinkedIn", "LinkedIn Jobs"),
    "indeed.fr": ("Indeed",),
    "francetravail.fr": ("France Travail", "Pôle emploi", "Pole emploi"),
    "apec.fr": ("APEC",),
    "welcometothejungle.com": ("Welcome to the Jungle", "WTTJ"),
    "talents-handicap.com": ("Talents Handicap", "Forums Talents Handicap"),
    "handicap-job.com": ("Handicap Job", "Handicap-Job"),
}
_OTHER_PUBLISHER_NAMES = ("STATION F",)


def _name_key(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name.casefold())
    return " ".join(
        "".join(char for char in folded if not unicodedata.combining(char))
        .replace("’", "'")
        .split()
    )


def is_known_publisher_name(name: str | None) -> bool:
    """Match only explicit job publisher names, never URL hosts or substrings."""
    from smartapply.offers.domain_rules import PARTNER_JOB_BOARD_DOMAINS

    key = _name_key(name or "")
    return bool(key) and key in {
        _name_key(alias)
        for domain, aliases in _PUBLISHER_NAMES_BY_DOMAIN.items()
        if domain in PARTNER_JOB_BOARD_DOMAINS
        for alias in aliases
    } | {_name_key(alias) for alias in _OTHER_PUBLISHER_NAMES}


def document_company_name(extracted_company_name: str | None) -> str:
    """Use the extraction for documents unless it is anonymous or a publisher."""
    name = " ".join((extracted_company_name or "").split())
    return "" if is_anonymous_company(name) or is_known_publisher_name(name) else name


def source_company_of(job: object) -> str | None:
    """Read the source label from persisted jobs or raw source offers."""
    return getattr(job, "source_company", getattr(job, "company", None))


def publisher_reintroduced(name: str | None, *texts: str) -> bool:
    """Catch an exact neutralized publisher name in generated document prose."""
    if not is_known_publisher_name(name):
        return False
    label = (name or "").casefold().strip()
    for value in texts:
        body = value.casefold()
        start = 0
        while (index := body.find(label, start)) >= 0:
            end = index + len(label)
            if (index == 0 or not body[index - 1].isalnum()) and (
                end == len(body) or not body[end].isalnum()
            ):
                return True
            start = end
    return False
