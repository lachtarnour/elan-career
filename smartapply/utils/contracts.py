"""Contract-type normalization shared by scrapers, filters and source queries."""

from __future__ import annotations

import re

from unidecode import unidecode

TAG_PERMANENT = "permanent"
TAG_FULL_TIME = "full_time"
TAG_PART_TIME = "part_time"
TAG_FIXED_TERM = "fixed_term"
TAG_TEMPORARY = "temporary"
TAG_CONTRACTOR = "contractor"
TAG_FREELANCE = "freelance"
TAG_INTERNSHIP = "internship"
TAG_APPRENTICESHIP = "apprenticeship"

CDD_EQUIVALENT_TAGS = {TAG_FIXED_TERM, TAG_TEMPORARY}

INCOMPATIBLE_CONTRACT_TAGS = {
    TAG_PART_TIME,
    TAG_FIXED_TERM,
    TAG_TEMPORARY,
    TAG_CONTRACTOR,
    TAG_FREELANCE,
    TAG_INTERNSHIP,
    TAG_APPRENTICESHIP,
}

_STABLE_FULL_TIME_TAGS = {TAG_PERMANENT, TAG_FULL_TIME}
_SERPAPI_FULLTIME_COMPATIBLE_TAGS = {TAG_PERMANENT, TAG_FULL_TIME}

_CONTRACT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        TAG_PERMANENT,
        (
            "cdi",
            "permanent",
            "permanent contract",
            "contrat permanent",
            "contrat a duree indeterminee",
            "contrat duree indeterminee",
            "indefinite contract",
            "open ended contract",
        ),
    ),
    (
        TAG_FULL_TIME,
        (
            "full time",
            "full-time",
            "fulltime",
            "temps plein",
            "a temps plein",
            "a plein temps",
            "plein temps",
        ),
    ),
    (
        TAG_PART_TIME,
        (
            "part time",
            "part-time",
            "parttime",
            "temps partiel",
            "a temps partiel",
            "mi temps",
            "mi-temps",
        ),
    ),
    (
        TAG_FIXED_TERM,
        (
            "cdd",
            "fixed term",
            "fixed-term",
            "contrat a duree determinee",
            "contrat duree determinee",
        ),
    ),
    (
        TAG_TEMPORARY,
        (
            "temporary",
            "temporary contract",
            "temporary position",
            "temporary job",
            "temporaire",
            "contrat temporaire",
            "poste temporaire",
            "interim",
        ),
    ),
    (
        TAG_FREELANCE,
        (
            "freelance",
            "independant",
            "independent",
            "self employed",
            "self-employed",
        ),
    ),
    (
        TAG_CONTRACTOR,
        (
            "contractor",
            "contract role",
            "contract position",
            "contract job",
        ),
    ),
    (
        TAG_INTERNSHIP,
        (
            "stage",
            "stagiaire",
            "internship",
            "intern",
        ),
    ),
    (
        TAG_APPRENTICESHIP,
        (
            "alternance",
            "alternant",
            "alternante",
            "apprenti",
            "apprentissage",
            "apprenticeship",
            "work study",
            "work-study",
            "co op",
            "co-op",
            "traineeship",
        ),
    ),
)


def _contract_words(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", unidecode(value or "").lower()).strip()


def _contains_marker(text_words: str, marker: str) -> bool:
    marker_words = _contract_words(marker)
    if not marker_words:
        return False
    return bool(
        re.search(
            rf"(?<![a-z0-9]){re.escape(marker_words)}(?![a-z0-9])",
            text_words,
        )
    )


def contract_type_tags(value: str | None) -> set[str]:
    """Return canonical contract tags visible in a raw source label."""
    text_words = _contract_words(value)
    if not text_words:
        return set()

    tags: set[str] = set()
    known_tags = {
        TAG_PERMANENT,
        TAG_FULL_TIME,
        TAG_PART_TIME,
        TAG_FIXED_TERM,
        TAG_TEMPORARY,
        TAG_CONTRACTOR,
        TAG_FREELANCE,
        TAG_INTERNSHIP,
        TAG_APPRENTICESHIP,
    }
    if text_words in known_tags:
        tags.add(text_words)

    for tag, markers in _CONTRACT_MARKERS:
        if any(_contains_marker(text_words, marker) for marker in markers):
            tags.add(tag)

    # Google Jobs may return the schedule type as exactly "Contract". Avoid
    # treating "permanent contract" as contractor by requiring an exact label.
    if text_words == "contract":
        tags.add(TAG_CONTRACTOR)
    return tags


def equivalent_contract_tags(tags: set[str]) -> set[str]:
    """Expand tags that should be treated the same by the filter.

    Intérim/temporary missions are considered equivalent to CDD/fixed-term
    contracts for accept/reject decisions, while preserving their source label
    for display.
    """
    expanded = set(tags)
    if expanded & CDD_EQUIVALENT_TAGS:
        expanded.update(CDD_EQUIVALENT_TAGS)
    return expanded


def contract_types_to_tags(values: list[str] | tuple[str, ...] | set[str]) -> set[str]:
    tags: set[str] = set()
    for value in values:
        tags.update(equivalent_contract_tags(contract_type_tags(value)))
    return tags


def normalize_contract_preferences(values: list[str]) -> list[str]:
    """Dedupe user preferences into canonical tags plus unknown custom labels."""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        tags = equivalent_contract_tags(contract_type_tags(value))
        tokens = sorted(tags) if tags else [_contract_words(value)]
        for token in tokens:
            if token and token not in seen:
                seen.add(token)
                normalized.append(token)
    return normalized


def blocked_contract_tags_from_tags(
    contract_tags: set[str],
    accepted_values: list[str],
) -> set[str]:
    """Return incompatible tags that are not explicitly accepted."""
    if not contract_tags:
        return set()

    accepted_tags = contract_types_to_tags(accepted_values)
    blocked = contract_tags & (INCOMPATIBLE_CONTRACT_TAGS - CDD_EQUIVALENT_TAGS)
    if contract_tags & CDD_EQUIVALENT_TAGS and not accepted_tags & CDD_EQUIVALENT_TAGS:
        blocked.update(contract_tags & CDD_EQUIVALENT_TAGS)
    return blocked


def should_filter_france_travail_to_cdi(accepted_values: list[str]) -> bool:
    """True when the accepted contracts are only permanent/full-time variants."""
    tags = contract_types_to_tags(accepted_values)
    return bool(tags) and TAG_PERMANENT in tags and tags <= _STABLE_FULL_TIME_TAGS


def should_filter_serpapi_to_fulltime(accepted_values: list[str]) -> bool:
    """True when SerpApi's FULLTIME chip is compatible with all accepted tags."""
    tags = contract_types_to_tags(accepted_values)
    return bool(tags & _SERPAPI_FULLTIME_COMPATIBLE_TAGS) and tags <= _STABLE_FULL_TIME_TAGS


def normalize_source_contract_type(
    value: str | None,
    *,
    source: str | None = None,
) -> str | None:
    """Return a stable display label for common source contract values."""
    if not value:
        return None
    text_words = _contract_words(value)
    # WelcomeKit's contract enums use French labels: FULL_TIME is CDI and
    # TEMPORARY is CDD / Temporaire. Keep these meanings specific to WTTJ.
    # https://developers.welcomekit.co/jobs-api/jobs/get-jobs-dependencies
    if source == "welcometothejungle":
        if text_words == "full time":
            return "CDI"
        if text_words == "temporary":
            return "CDD"
    tags = contract_type_tags(value)

    if TAG_INTERNSHIP in tags:
        return "Internship"
    if TAG_APPRENTICESHIP in tags:
        return "Apprenticeship"
    if TAG_FREELANCE in tags:
        return "Freelance"
    if TAG_CONTRACTOR in tags:
        return "Contract"
    if TAG_PART_TIME in tags:
        return "Part-time"
    if TAG_FIXED_TERM in tags:
        return "CDD" if _contains_marker(text_words, "cdd") else "Fixed-term"
    if TAG_TEMPORARY in tags:
        return "Temporary"
    if TAG_PERMANENT in tags:
        return "CDI" if _contains_marker(text_words, "cdi") else "Permanent"
    if TAG_FULL_TIME in tags:
        return "Full-time"
    return value
