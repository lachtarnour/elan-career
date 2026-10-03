"""Cross-source job deduplication.

Two jobs are treated as duplicates when:
- both employers are disclosed and their normalized names match exactly,
- their precise locations do not conflict; an equivalent precise commune also
  contributes to the fuzzy confidence,
- their publication/fetch dates are at most 30 days apart, and
- their fuzzy title score >= ``title_threshold``, and
- their fuzzy description score >= ``desc_threshold``.

Durable source ids and direct offer URLs are strong identities handled outside
this fuzzy matcher. Repair code can still detect them through
``offers_share_strong_identity`` when legacy metadata was mislabeled.

The class returns transitive groups of duplicates and a deduplicated list
(keeping the first occurrence — useful when iterating sources in
priority order).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Protocol, runtime_checkable

from rapidfuzz import fuzz
from unidecode import unidecode

from smartapply.company_identity import is_anonymous_company
from smartapply.config import get_settings
from smartapply.dedup.geography import (
    commune_by_code,
    commune_name_key,
    communes_named,
    department_codes_named,
    postal_department_codes,
    resolve_commune,
)
from smartapply.offer_identity import direct_offer_url_key
from smartapply.parsing import clean_description

_COMPANY_SUFFIXES = re.compile(
    r"\b(s\.?a\.?s\.?|s\.?a\.?|sarl|s\.a\.r\.l\.|ltd\.?|inc\.?|llc|gmbh|b\.?v\.?|n\.?v\.?|plc|co\.?|corp\.?|corporation|company)\b",
    flags=re.IGNORECASE,
)

# A later republication is treated as a distinct opportunity once the two
# offers are more than one rolling month apart. The boundary itself remains
# eligible for duplicate matching.
DUPLICATE_TIME_WINDOW = timedelta(days=30)

# Fuzzy similarity is evidence for a review, never proof of identity. Keeping
# it below 1.0 also prevents the UI from presenting a fuzzy pair as 100% sure.
MAX_FUZZY_CONFIDENCE = 0.99

# Location is useful positive evidence only when both sources identify the same
# precise commune. Missing or compatible broad labels remain neutral, while
# conflicting country, region or department evidence rejects the fuzzy match.
_BASE_TITLE_WEIGHT = 0.55
_BASE_DESCRIPTION_WEIGHT = 0.45
_PRECISE_LOCATION_WEIGHT = 0.10

_FRANCE_TRAVAIL_LOCATION_RE = re.compile(
    r"^(?P<department>\d{2,3}|2[ab])\s*[-\u2013\u2014]\s*(?P<label>.+)$",
    flags=re.IGNORECASE,
)
_INSEE_COMMUNE_RE = re.compile(r"^(?:\d{5}|2[ab]\d{3})$", flags=re.IGNORECASE)
_POSTAL_CODE_RE = re.compile(r"\b\d{5}\b")
_ARRONDISSEMENT_SUFFIX_RE = re.compile(
    r"\s+\d{1,2}(?:er|e|eme)?\s+arrondissement$",
    flags=re.IGNORECASE,
)
_CITY_DISTRICT_SUFFIX_RE = re.compile(
    r"\s+\d{1,2}(?:er|e|eme)$",
    flags=re.IGNORECASE,
)
_LOCATION_WORK_MODE_SUFFIX_RE = re.compile(
    r"(?:\s*\((?:hybride|hybrid|remote|teletravail|work from anywhere)\)"
    r"|\s*[-,]\s*(?:hybride|hybrid|remote|teletravail))\s*$"
)
_MULTI_LOCATION_SEPARATOR_RE = re.compile(r"[;/|]|\sou\s", flags=re.IGNORECASE)

# These labels do not identify one city. They must remain compatible with a
# precise location because a broad source label can legitimately describe the
# same offer as a city-level label from another source.
_BROAD_LOCATION_KEYS = frozenset(
    {
        "ain",
        "aisne",
        "allier",
        "alpes de haute provence",
        "alpes maritimes",
        "ardeche",
        "ardennes",
        "ariege",
        "aube",
        "aude",
        "fr",
        "france",
        "france entiere",
        "france metropolitaine",
        "national",
        "nationwide",
        "remote",
        "remote france",
        "teletravail",
        "telework",
        "partout en france",
        "auvergne rhone alpes",
        "aveyron",
        "bas rhin",
        "bouches du rhone",
        "bourgogne franche comte",
        "bretagne",
        "brittany",
        "calvados",
        "cantal",
        "centre val de loire",
        "charente",
        "charente maritime",
        "cher",
        "corse",
        "corse du sud",
        "correze",
        "cote d or",
        "cotes d armor",
        "creuse",
        "deux sevres",
        "dordogne",
        "doubs",
        "drome",
        "essonne",
        "eure",
        "eure et loir",
        "finistere",
        "gard",
        "grand est",
        "gers",
        "gironde",
        "guadeloupe",
        "guyane",
        "haut rhin",
        "haute corse",
        "haute garonne",
        "haute loire",
        "haute marne",
        "haute saone",
        "haute savoie",
        "haute vienne",
        "hautes alpes",
        "hautes pyrenees",
        "hauts de seine",
        "hauts de france",
        "herault",
        "ile de france",
        "ille et vilaine",
        "indre",
        "indre et loire",
        "isere",
        "jura",
        "la reunion",
        "la defense",
        "landes",
        "loir et cher",
        "loire",
        "loire atlantique",
        "loiret",
        "lot",
        "lot et garonne",
        "lozere",
        "maine et loire",
        "manche",
        "marne",
        "maritime alps",
        "martinique",
        "mayotte",
        "mayenne",
        "meurthe et moselle",
        "meuse",
        "morbihan",
        "moselle",
        "nievre",
        "nord",
        "normandie",
        "normandy",
        "nouvelle aquitaine",
        "oise",
        "occitanie",
        "orne",
        "pas de calais",
        "paris saclay",
        "pays de la loire",
        "plateau de saclay",
        "provence alpes cote d azur",
        "puy de dome",
        "pyrenees atlantiques",
        "pyrenees orientales",
        "rhone",
        "saone et loire",
        "sarthe",
        "savoie",
        "seine et marne",
        "seine maritime",
        "seine saint denis",
        "silicon sentier",
        "sophia antipolis",
        "somme",
        "tarn",
        "tarn et garonne",
        "territoire de belfort",
        "val d oise",
        "val de marne",
        "var",
        "vaucluse",
        "vendee",
        "vienne",
        "vosges",
        "yonne",
        "yvelines",
    }
)
_REGION_LOCATION_KEYS = frozenset(
    {
        "auvergne rhone alpes",
        "bourgogne franche comte",
        "bretagne",
        "centre val de loire",
        "corse",
        "grand est",
        "guadeloupe",
        "guyane",
        "hauts de france",
        "ile de france",
        "la reunion",
        "martinique",
        "mayotte",
        "normandie",
        "nouvelle aquitaine",
        "occitanie",
        "pays de la loire",
        "provence alpes cote d azur",
    }
)
_ADMINISTRATIVE_REGION_ALIASES = {
    "brittany": "bretagne",
    "maritime alps": "provence alpes cote d azur",
    "normandy": "normandie",
}
_COUNTRY_LOCATION_ALIASES = {
    "allemagne": "germany",
    "belgique": "belgium",
    "belgium": "belgium",
    "espagne": "spain",
    "fr": "france",
    "france": "france",
    "france entiere": "france",
    "france metropolitaine": "france",
    "germany": "germany",
    "irlande": "ireland",
    "ireland": "ireland",
    "italie": "italy",
    "italy": "italy",
    "luxembourg": "luxembourg",
    "monaco": "monaco",
    "netherlands": "netherlands",
    "pays bas": "netherlands",
    "portugal": "portugal",
    "remote france": "france",
    "royaume uni": "united kingdom",
    "spain": "spain",
    "suisse": "switzerland",
    "switzerland": "switzerland",
    "united kingdom": "united kingdom",
}
_DEPARTMENT_TO_REGION = {
    "ain": "auvergne rhone alpes",
    "aisne": "hauts de france",
    "allier": "auvergne rhone alpes",
    "alpes de haute provence": "provence alpes cote d azur",
    "alpes maritimes": "provence alpes cote d azur",
    "ardeche": "auvergne rhone alpes",
    "ardennes": "grand est",
    "ariege": "occitanie",
    "aube": "grand est",
    "aude": "occitanie",
    "aveyron": "occitanie",
    "bas rhin": "grand est",
    "bouches du rhone": "provence alpes cote d azur",
    "calvados": "normandie",
    "cantal": "auvergne rhone alpes",
    "charente": "nouvelle aquitaine",
    "charente maritime": "nouvelle aquitaine",
    "cher": "centre val de loire",
    "correze": "nouvelle aquitaine",
    "corse du sud": "corse",
    "cote d or": "bourgogne franche comte",
    "cotes d armor": "bretagne",
    "creuse": "nouvelle aquitaine",
    "deux sevres": "nouvelle aquitaine",
    "dordogne": "nouvelle aquitaine",
    "doubs": "bourgogne franche comte",
    "drome": "auvergne rhone alpes",
    "essonne": "ile de france",
    "eure": "normandie",
    "eure et loir": "centre val de loire",
    "finistere": "bretagne",
    "gard": "occitanie",
    "gers": "occitanie",
    "gironde": "nouvelle aquitaine",
    "haut rhin": "grand est",
    "haute corse": "corse",
    "haute garonne": "occitanie",
    "haute loire": "auvergne rhone alpes",
    "haute marne": "grand est",
    "haute saone": "bourgogne franche comte",
    "haute savoie": "auvergne rhone alpes",
    "haute vienne": "nouvelle aquitaine",
    "hautes alpes": "provence alpes cote d azur",
    "hautes pyrenees": "occitanie",
    "hauts de seine": "ile de france",
    "herault": "occitanie",
    "ille et vilaine": "bretagne",
    "indre": "centre val de loire",
    "indre et loire": "centre val de loire",
    "isere": "auvergne rhone alpes",
    "jura": "bourgogne franche comte",
    "landes": "nouvelle aquitaine",
    "loir et cher": "centre val de loire",
    "loire": "auvergne rhone alpes",
    "loire atlantique": "pays de la loire",
    "loiret": "centre val de loire",
    "lot": "occitanie",
    "lot et garonne": "nouvelle aquitaine",
    "lozere": "occitanie",
    "maine et loire": "pays de la loire",
    "manche": "normandie",
    "marne": "grand est",
    "mayenne": "pays de la loire",
    "meurthe et moselle": "grand est",
    "meuse": "grand est",
    "morbihan": "bretagne",
    "moselle": "grand est",
    "nievre": "bourgogne franche comte",
    "nord": "hauts de france",
    "oise": "hauts de france",
    "orne": "normandie",
    "pas de calais": "hauts de france",
    "puy de dome": "auvergne rhone alpes",
    "pyrenees atlantiques": "nouvelle aquitaine",
    "pyrenees orientales": "occitanie",
    "rhone": "auvergne rhone alpes",
    "saone et loire": "bourgogne franche comte",
    "sarthe": "pays de la loire",
    "savoie": "auvergne rhone alpes",
    "seine et marne": "ile de france",
    "seine maritime": "normandie",
    "seine saint denis": "ile de france",
    "somme": "hauts de france",
    "tarn": "occitanie",
    "tarn et garonne": "occitanie",
    "territoire de belfort": "bourgogne franche comte",
    "val d oise": "ile de france",
    "val de marne": "ile de france",
    "var": "provence alpes cote d azur",
    "vaucluse": "provence alpes cote d azur",
    "vendee": "pays de la loire",
    "vienne": "nouvelle aquitaine",
    "vosges": "grand est",
    "yonne": "bourgogne franche comte",
    "yvelines": "ile de france",
}
_DEPARTMENT_CODES_BY_REGION = {
    "auvergne rhone alpes": {
        "01",
        "03",
        "07",
        "15",
        "26",
        "38",
        "42",
        "43",
        "63",
        "69",
        "73",
        "74",
    },
    "bourgogne franche comte": {"21", "25", "39", "58", "70", "71", "89", "90"},
    "bretagne": {"22", "29", "35", "56"},
    "centre val de loire": {"18", "28", "36", "37", "41", "45"},
    # Corsican postcodes still use 20 while administrative departments use
    # 2A/2B. Treat 20 as region evidence, not as a conflicting department.
    "corse": {"20", "2a", "2b"},
    "grand est": {"08", "10", "51", "52", "54", "55", "57", "67", "68", "88"},
    "guadeloupe": {"971"},
    "guyane": {"973"},
    "hauts de france": {"02", "59", "60", "62", "80"},
    "ile de france": {"75", "77", "78", "91", "92", "93", "94", "95"},
    "la reunion": {"974"},
    "martinique": {"972"},
    "mayotte": {"976"},
    "normandie": {"14", "27", "50", "61", "76"},
    "nouvelle aquitaine": {"16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"},
    "occitanie": {"09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"},
    "pays de la loire": {"44", "49", "53", "72", "85"},
    "provence alpes cote d azur": {"04", "05", "06", "13", "83", "84"},
}
_DEPARTMENT_CODE_TO_REGION = {
    code: region
    for region, department_codes in _DEPARTMENT_CODES_BY_REGION.items()
    for code in department_codes
}
_NON_PRECISE_LOCATION_MARKERS = (
    " a distance",
    " agglomeration",
    " area",
    " autour de ",
    " et alentours",
    " et peripherie",
    " greater ",
    " hybride",
    " metropole",
    " metropolitan area",
    " metropolitan region",
    " multi sites",
    " plusieurs sites",
    " remote",
    " region de ",
    " selon profil",
    " teletravail",
    " work from anywhere",
)
_METROPOLITAN_AREA_PATTERNS = (
    re.compile(r"^greater (?P<city>.+?)(?: metropolitan)? (?:area|region)$"),
    re.compile(r"^greater (?P<city>.+)$"),
    re.compile(r"^(?P<city>.+?) metropolitan (?:area|region)$"),
    re.compile(r"^metropole (?:de |du |d )?(?P<city>.+)$"),
)
_ADMINISTRATIVE_LOCATION_TRAILER_KEYS = _BROAD_LOCATION_KEYS | {"paris"}


@runtime_checkable
class JobLike(Protocol):
    title: str
    company: str
    description: str
    external_id: str


@dataclass
class DedupReport:
    unique: list[JobLike]
    duplicate_groups: list[list[JobLike]]

    @property
    def n_duplicates_removed(self) -> int:
        return sum(len(g) - 1 for g in self.duplicate_groups)


@dataclass(frozen=True)
class DuplicateCandidate:
    """A probable duplicate found without changing either offer."""

    job: JobLike
    title_score: float
    description_score: float
    confidence: float
    match_type: str = "fuzzy"
    location_score: float | None = None


@dataclass(frozen=True)
class _PreciseLocation:
    commune_code: str | None = None
    city_key: str | None = None
    department_code: str | None = None
    administrative_context: frozenset[str] = frozenset()
    area_anchor: str | None = None
    country_key: str | None = None


def normalize_company(name: str) -> str:
    s = unidecode(name or "").lower().strip()
    s = _COMPANY_SUFFIXES.sub(" ", s)
    s = re.sub(r"[^\w]+", " ", s).strip()
    return s


def normalize_title(title: str) -> str:
    s = unidecode(title or "").lower()
    # Drop common H/F-like role suffixes / contract indicators
    s = re.sub(r"\b(h/f|f/h|m/f|cdi|cdd|stagiaire|freelance|junior|senior)\b", "", s)
    s = re.sub(r"[^\w\s]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def offers_within_duplicate_time_window(
    left: JobLike,
    right: JobLike,
    *,
    reference_time: datetime | None = None,
) -> bool:
    """Return whether two offers are close enough in time to be duplicates.

    Each offer uses its publication date when available, otherwise its fetch
    date. A fresh raw offer has not been persisted yet, so the current ingest
    time is its fetch-date fallback.
    """
    fallback = reference_time or datetime.now(timezone.utc)
    left_at = _offer_timestamp(left, fallback=fallback)
    right_at = _offer_timestamp(right, fallback=fallback)
    return abs(left_at - right_at) <= DUPLICATE_TIME_WINDOW


def eligible_for_fuzzy_duplicate_comparison(left: JobLike, right: JobLike) -> bool:
    """Return whether company/location evidence permits a fuzzy comparison.

    Anonymous employer labels are display placeholders, not identities. A pair
    is also ineligible when the available commune, department or region
    evidence is incompatible. Missing locations and compatible broad labels
    remain neutral so an incomplete source cannot suppress a legitimate review.
    """
    if is_anonymous_company(getattr(left, "company", None)) or is_anonymous_company(
        getattr(right, "company", None)
    ):
        return False
    return not offers_have_incompatible_precise_locations(left, right)


def offers_share_strong_identity(left: JobLike, right: JobLike) -> bool:
    """Return whether durable source evidence identifies the same offer.

    This is deliberately separate from fuzzy matching. It lets repair code
    preserve old rows whose metadata incorrectly says ``fuzzy`` even though a
    direct offer URL or France Travail's native offer id proves the identity.
    """
    left_external_id = str(getattr(left, "external_id", None) or "").strip()
    right_external_id = str(getattr(right, "external_id", None) or "").strip()
    if left_external_id and left_external_id == right_external_id:
        return True

    left_native_id = _france_travail_native_id(left)
    right_native_id = _france_travail_native_id(right)
    if left_native_id and left_native_id == right_native_id:
        return True

    left_url = direct_offer_url_key(getattr(left, "application_url", None))
    right_url = direct_offer_url_key(getattr(right, "application_url", None))
    return bool(left_url and left_url == right_url)


def offers_have_incompatible_precise_locations(left: JobLike, right: JobLike) -> bool:
    """Return whether two offers expose incompatible French location evidence."""
    return fuzzy_location_score(left, right) == 0.0


def fuzzy_location_score(left: JobLike, right: JobLike) -> float | None:
    """Return comparable location evidence for fuzzy duplicate confidence.

    ``100.0`` means that both offers identify the same precise commune and
    ``0.0`` means that their country, region, department or commune evidence is
    incompatible. ``None`` means the evidence is missing or too broad to score.
    """
    left_location = _precise_location(left)
    right_location = _precise_location(right)

    if (
        left_location.country_key
        and right_location.country_key
        and left_location.country_key != right_location.country_key
    ):
        return 0.0

    if left_location.commune_code and right_location.commune_code:
        return 100.0 if left_location.commune_code == right_location.commune_code else 0.0

    left_named_departments = _location_department_codes(left_location)
    right_named_departments = _location_department_codes(right_location)
    if (
        left_named_departments
        and right_named_departments
        and all(
            _department_codes_are_incompatible(left_code, right_code)
            for left_code in left_named_departments
            for right_code in right_named_departments
        )
    ):
        return 0.0

    left_regions = _location_regions(left_location)
    right_regions = _location_regions(right_location)
    if left_regions and right_regions and left_regions.isdisjoint(right_regions):
        return 0.0

    # Metropolitan labels identify an area, not its central commune. Regional
    # evidence above can reject a different region; a suburb must stay neutral.
    return None


def linked_duplicate_components(
    candidates: Sequence[JobLike],
) -> dict[int, tuple[JobLike, ...]]:
    """Index persisted duplicate chains by Python object identity.

    The database stores links from pending/confirmed aliases to another job.
    Treat those links as an undirected component for safety checks so an offer
    with an unknown location cannot hide a conflict between the endpoints.
    """
    items = list(candidates)
    parent = list(range(len(items)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    index_by_database_id: dict[int, int] = {}
    references: dict[int, list[int]] = {}
    for index, item in enumerate(items):
        try:
            database_id = int(getattr(item, "id", 0) or 0)
        except (TypeError, ValueError):
            database_id = 0
        if database_id:
            index_by_database_id[database_id] = index
        for field in ("possible_duplicate_of_id", "canonical_job_id"):
            try:
                linked_id = int(getattr(item, field, 0) or 0)
            except (TypeError, ValueError):
                linked_id = 0
            if linked_id:
                references.setdefault(linked_id, []).append(index)

    for linked_id, indices in references.items():
        root_index = index_by_database_id.get(linked_id)
        connected = [*indices, *([root_index] if root_index is not None else [])]
        for index in connected[1:]:
            union(connected[0], index)

    members_by_root: dict[int, list[JobLike]] = {}
    for index, item in enumerate(items):
        members_by_root.setdefault(find(index), []).append(item)
    return {id(item): tuple(members_by_root[find(index)]) for index, item in enumerate(items)}


def eligible_for_fuzzy_duplicate_group(
    target: JobLike,
    candidate: JobLike,
    candidates: Sequence[JobLike],
    *,
    components: dict[int, tuple[JobLike, ...]] | None = None,
    reference_time: datetime | None = None,
) -> bool:
    """Return whether ``target`` is compatible with the candidate's full chain."""
    indexed_components = (
        linked_duplicate_components(candidates) if components is None else components
    )
    component = indexed_components.get(id(candidate), (candidate,))
    return offers_within_duplicate_group_time_window(
        target,
        candidate,
        candidates,
        components=indexed_components,
        reference_time=reference_time,
    ) and all(
        not offers_have_incompatible_precise_locations(target, member) for member in component
    )


def offers_within_duplicate_group_time_window(
    target: JobLike,
    candidate: JobLike,
    candidates: Sequence[JobLike],
    *,
    components: dict[int, tuple[JobLike, ...]] | None = None,
    reference_time: datetime | None = None,
) -> bool:
    """Return whether ``target`` is within 30 days of the full linked group."""
    indexed_components = (
        linked_duplicate_components(candidates) if components is None else components
    )
    return all(
        offers_within_duplicate_time_window(
            target,
            member,
            reference_time=reference_time,
        )
        for member in indexed_components.get(id(candidate), (candidate,))
    )


class Deduplicator:
    def __init__(
        self,
        title_threshold: int | None = None,
        desc_threshold: int | None = None,
    ):
        settings = get_settings()
        self.title_threshold = (
            title_threshold if title_threshold is not None else settings.dedup_title_threshold
        )
        self.desc_threshold = (
            desc_threshold if desc_threshold is not None else settings.dedup_desc_threshold
        )

    # -------------------- public API --------------------

    def find_probable_duplicate(
        self,
        target: JobLike,
        candidates: Sequence[JobLike],
        *,
        components: dict[int, tuple[JobLike, ...]] | None = None,
    ) -> DuplicateCandidate | None:
        """Find the strongest ambiguous match for ``target``.

        This method intentionally does not use the strict thresholds of
        :meth:`deduplicate`: its result is a review candidate, never an
        automatic merge.  The lower description floor catches source-specific
        rewrites while the exact company/title block prevents broad false
        positives.
        """
        if is_anonymous_company(getattr(target, "company", None)):
            return None
        target_company = normalize_company(target.company)
        target_title = normalize_title(target.title)
        if not target_company or not target_title:
            return None

        best: DuplicateCandidate | None = None
        reference_time = datetime.now(timezone.utc)
        linked_components = (
            linked_duplicate_components(candidates) if components is None else components
        )
        for candidate in candidates:
            if candidate is target or getattr(candidate, "external_id", None) == getattr(
                target, "external_id", None
            ):
                continue
            if normalize_company(candidate.company) != target_company:
                continue
            if not eligible_for_fuzzy_duplicate_comparison(target, candidate):
                continue
            if not eligible_for_fuzzy_duplicate_group(
                target,
                candidate,
                candidates,
                components=linked_components,
                reference_time=reference_time,
            ):
                continue

            title_score = fuzz.token_set_ratio(target_title, normalize_title(candidate.title))
            if title_score < 90:
                continue

            target_description = _dedup_description(target)
            candidate_description = _dedup_description(candidate)
            description_score = (
                fuzz.token_set_ratio(target_description, candidate_description)
                if target_description and candidate_description
                else 0.0
            )
            # Same company/title alone is not enough: employers commonly
            # publish several openings under one generic job label.
            if description_score < 55:
                continue

            location_score = fuzzy_location_score(target, candidate)
            base_similarity = _BASE_TITLE_WEIGHT * float(
                title_score
            ) + _BASE_DESCRIPTION_WEIGHT * float(description_score)
            weighted_similarity = (
                base_similarity
                if location_score is None
                else (1.0 - _PRECISE_LOCATION_WEIGHT) * base_similarity
                + _PRECISE_LOCATION_WEIGHT * location_score
            )
            confidence = min(
                MAX_FUZZY_CONFIDENCE,
                round(weighted_similarity / 100.0, 4),
            )
            match = DuplicateCandidate(
                job=candidate,
                title_score=round(float(title_score), 2),
                description_score=round(float(description_score), 2),
                confidence=confidence,
                location_score=location_score,
            )
            if best is None or match.confidence > best.confidence:
                best = match
        return best

    def deduplicate(self, jobs: Sequence[JobLike]) -> DedupReport:
        # Bucket by normalized company; only jobs sharing the same company
        # bucket get compared. This makes N² comparisons local instead of global.
        buckets: dict[str, list[int]] = {}
        for idx, job in enumerate(jobs):
            if is_anonymous_company(getattr(job, "company", None)):
                continue
            key = normalize_company(job.company)
            if not key:
                continue
            buckets.setdefault(key, []).append(idx)

        # Union-Find for transitive grouping. Before merging two components,
        # validate every cross-pair's location and freshness so an unknown
        # intermediate offer cannot bridge Paris to Lyon (or two republications
        # more than one month apart).
        parent = list(range(len(jobs)))
        component_members = {index: {index} for index in range(len(jobs))}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra == rb:
                return
            if any(
                offers_have_incompatible_precise_locations(jobs[left], jobs[right])
                or not offers_within_duplicate_time_window(jobs[left], jobs[right])
                for left in component_members[ra]
                for right in component_members[rb]
            ):
                return
            # Keep the smallest index as root to preserve original order.
            keep, drop = (ra, rb) if ra < rb else (rb, ra)
            parent[drop] = keep
            component_members[keep].update(component_members.pop(drop))

        for indices in buckets.values():
            if len(indices) < 2:
                continue
            for i_pos, i in enumerate(indices):
                for j in indices[i_pos + 1 :]:
                    if self._are_duplicates(jobs[i], jobs[j]):
                        union(i, j)

        groups: dict[int, list[int]] = {}
        for i in range(len(jobs)):
            groups.setdefault(find(i), []).append(i)

        unique: list[JobLike] = []
        duplicate_groups: list[list[JobLike]] = []
        for _root, members in groups.items():
            members.sort()
            unique.append(jobs[members[0]])
            if len(members) > 1:
                duplicate_groups.append([jobs[m] for m in members])

        # Preserve original ordering
        unique.sort(key=lambda j: jobs.index(j))
        return DedupReport(unique=unique, duplicate_groups=duplicate_groups)

    # -------------------- internals --------------------

    def _are_duplicates(self, a: JobLike, b: JobLike) -> bool:
        if not eligible_for_fuzzy_duplicate_comparison(a, b):
            return False
        if not offers_within_duplicate_time_window(a, b):
            return False
        title_a, title_b = normalize_title(a.title), normalize_title(b.title)
        title_score = fuzz.token_set_ratio(title_a, title_b)
        if title_score < self.title_threshold:
            return False
        # Truncate descriptions to keep the comparison fast
        desc_a = (a.description or "")[:2000]
        desc_b = (b.description or "")[:2000]
        if not desc_a or not desc_b:
            # No description on one side — rely on title alone
            return title_score >= self.title_threshold + 5
        desc_score = fuzz.token_set_ratio(desc_a, desc_b)
        return desc_score >= self.desc_threshold


def _dedup_description(job: JobLike) -> str:
    """Return normalized text used for review matching.

    Persisted rows already have ``cleaned_description``; raw scraper objects
    do not.  Supporting both keeps ingestion and database audits on the same
    normalization path.
    """
    value = getattr(job, "cleaned_description", None) or getattr(job, "description", "")
    return clean_description(str(value or ""))


def _offer_timestamp(job: JobLike, *, fallback: datetime) -> datetime:
    value = getattr(job, "published_date", None) or getattr(job, "scraped_at", None) or fallback
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _precise_location(job: JobLike) -> _PreciseLocation:
    display_location = str(getattr(job, "location", None) or "")
    commune_code, structured_location_seen = _structured_commune_code(
        getattr(job, "source_data", None)
    )
    return _cached_precise_location(
        display_location,
        commune_code,
        structured_location_seen,
    )


@lru_cache(maxsize=16_384)
def _cached_precise_location(
    display_location: str,
    commune_code: str | None,
    structured_location_seen: bool,
) -> _PreciseLocation:
    """Parse immutable location evidence once per distinct source value."""
    if _MULTI_LOCATION_SEPARATOR_RE.search(display_location):
        return _PreciseLocation()
    # A France Travail payload without an INSEE commune may still expose a
    # useful department or region in its display label. Keep that broad
    # evidence, but never promote the label to a precise city.
    city_label = (
        None
        if structured_location_seen and commune_code is None
        else _precise_city_key(display_location)
    )
    department_code = _department_code(
        display_location, city_is_known=bool(communes_named(city_label))
    )
    administrative_context = _administrative_context_keys(display_location, city_key=city_label)
    named_departments = department_codes_named(administrative_context)
    numeric_departments = (
        frozenset({"2a", "2b"})
        if department_code == "20"
        else frozenset({department_code}) if department_code else frozenset()
    )
    regions = _administrative_regions(administrative_context)
    regional_departments = frozenset(
        code for code, region in _DEPARTMENT_CODE_TO_REGION.items() if region in regions
    )
    constraints = [
        scope for scope in (named_departments, numeric_departments, regional_departments) if scope
    ]
    departments = constraints[0].intersection(*constraints[1:]) if constraints else frozenset()
    conflicting_context = bool(constraints and not departments)
    postal_match = _POSTAL_CODE_RE.search(display_location)
    commune = commune_by_code(commune_code)
    if commune is None and city_label and not conflicting_context:
        commune = resolve_commune(
            city_label,
            departments=departments,
            postal_code=postal_match.group(0) if postal_match else None,
        )
    area_anchor = _metropolitan_area_anchor(display_location)
    resolved_commune_code = commune.code if commune else None
    if commune:
        department_code = commune.department
    country_key = _location_country_key(
        display_location,
        commune_code=resolved_commune_code,
        department_code=department_code,
        administrative_context=administrative_context,
    )
    return _PreciseLocation(
        commune_code=resolved_commune_code,
        city_key=commune_name_key(commune.name) if commune else None,
        department_code=department_code,
        administrative_context=administrative_context,
        area_anchor=area_anchor,
        country_key=country_key,
    )


def _structured_commune_code(source_data: Any) -> tuple[str | None, bool]:
    if not isinstance(source_data, Mapping):
        return None, False
    workplace = source_data.get("lieuTravail")
    if not isinstance(workplace, Mapping):
        return None, False
    value = str(workplace.get("commune") or "").strip().upper()
    return (
        value if _INSEE_COMMUNE_RE.fullmatch(value) and commune_by_code(value) is not None else None
    ), True


def _precise_city_key(location: str | None) -> str | None:
    """Parse a candidate name; only the official resolver establishes identity."""
    value = _normalize_location_text(location)
    broad_key = _location_word_key(value)
    if not value or broad_key in _BROAD_LOCATION_KEYS or broad_key in _COUNTRY_LOCATION_ALIASES:
        return None
    if any(marker in f" {broad_key}" for marker in _NON_PRECISE_LOCATION_MARKERS):
        return None
    if any(separator in value for separator in (" / ", " | ", " ou ")):
        return None

    france_travail_match = _FRANCE_TRAVAIL_LOCATION_RE.fullmatch(value)
    if france_travail_match:
        value = france_travail_match.group("label").strip()

    comma_parts = [part.strip() for part in value.split(",") if part.strip()]
    if len(comma_parts) > 1:
        trailers = comma_parts[1:]
        if not all(
            _location_word_key(trailer) in _ADMINISTRATIVE_LOCATION_TRAILER_KEYS
            or _POSTAL_CODE_RE.fullmatch(trailer)
            for trailer in trailers
        ):
            return None
        value = comma_parts[0]

    value = _location_word_key(value)
    value = _POSTAL_CODE_RE.sub(" ", value)
    value = re.sub(r"\s*\((?:\d{2,3}|2[ab])\)\s*$", "", value)
    value = re.sub(r"\s+cedex(?:\s+\d+)?$", "", value)
    value = _ARRONDISSEMENT_SUFFIX_RE.sub("", value)
    if value.startswith(("paris ", "lyon ", "marseille ")):
        value = _CITY_DISTRICT_SUFFIX_RE.sub("", value)
    value = re.sub(r"\s+", " ", value).strip()
    if (
        not value
        or value in _BROAD_LOCATION_KEYS
        or value in _COUNTRY_LOCATION_ALIASES
        or any(char.isdigit() for char in value)
    ):
        return None
    if len(value.split()) > 6:
        return None

    return commune_name_key(value) or None


def _department_code(location: str | None, *, city_is_known: bool = False) -> str | None:
    value = _normalize_location_text(location)
    france_travail_match = _FRANCE_TRAVAIL_LOCATION_RE.fullmatch(value)
    if france_travail_match:
        code = france_travail_match.group("department").casefold()
        return code if code in _DEPARTMENT_CODE_TO_REGION else None
    postal_match = _POSTAL_CODE_RE.search(value)
    if postal_match is None:
        return None
    postal_code = postal_match.group(0)
    if not postal_department_codes(postal_code):
        return None
    if not city_is_known and not (
        value == postal_code and postal_department_codes(postal_code)
    ):
        return None
    departments = postal_department_codes(postal_code)
    # Postal prefixes are not administrative identities: 94390 covers an
    # Essonne commune, and some postal areas cross departmental boundaries.
    return next(iter(departments)) if len(departments) == 1 else None


def _department_codes_are_incompatible(left: str, right: str) -> bool:
    # A 20xxx postcode cannot distinguish Corse-du-Sud from Haute-Corse.
    # Keep 20 vs 2A/2B neutral and let the city/region evidence decide.
    codes = {left, right}
    return left != right and not ("20" in codes and codes <= {"20", "2a", "2b"})


def _administrative_context_keys(
    location: str | None,
    *,
    city_key: str | None,
) -> frozenset[str]:
    value = _normalize_location_text(location)
    france_travail_match = _FRANCE_TRAVAIL_LOCATION_RE.fullmatch(value)
    if france_travail_match:
        value = france_travail_match.group("label").strip()
    parts = [part.strip() for part in value.split(",") if part.strip()]
    ignored = {"fr", "france", "france metropolitaine", city_key}
    trailer_keys = frozenset(
        key
        for part in parts[1:]
        if (key := _location_word_key(part)) in _BROAD_LOCATION_KEYS and key not in ignored
    )
    first_key = _location_word_key(parts[0]) if parts else ""
    if city_key is None and first_key in _DEPARTMENT_TO_REGION:
        # A named department remains useful even when followed by its region.
        return trailer_keys | {first_key}
    if trailer_keys:
        return trailer_keys
    if city_key is None and first_key in _BROAD_LOCATION_KEYS and first_key not in ignored:
        return frozenset({first_key})
    return frozenset()


def _location_department_codes(location: _PreciseLocation) -> frozenset[str]:
    named_codes = department_codes_named(location.administrative_context)
    return named_codes | ({location.department_code} if location.department_code else set())


def _administrative_regions(context: frozenset[str]) -> frozenset[str]:
    regions: set[str] = set()
    for key in context:
        canonical = _ADMINISTRATIVE_REGION_ALIASES.get(key, key)
        if canonical in _REGION_LOCATION_KEYS:
            regions.add(canonical)
        elif region := _DEPARTMENT_TO_REGION.get(canonical):
            regions.add(region)
    return frozenset(regions)


def _location_country_key(
    location: str | None,
    *,
    commune_code: str | None,
    department_code: str | None,
    administrative_context: frozenset[str],
) -> str | None:
    value = _normalize_location_text(location)
    parts = [_location_word_key(part) for part in value.split(",") if part.strip()]
    explicit_key = parts[-1] if len(parts) > 1 else _location_word_key(value)
    if country := _COUNTRY_LOCATION_ALIASES.get(explicit_key):
        return country
    if (
        commune_code
        or department_code in _DEPARTMENT_CODE_TO_REGION
        or _administrative_regions(administrative_context)
    ):
        return "france"
    return None


def _location_regions(location: _PreciseLocation) -> frozenset[str]:
    regions = set(_administrative_regions(location.administrative_context))
    if location.department_code and (
        region := _DEPARTMENT_CODE_TO_REGION.get(location.department_code)
    ):
        regions.add(region)
    return frozenset(regions)


def _metropolitan_area_anchor(location: str | None) -> str | None:
    value = _location_word_key(_normalize_location_text(location))
    for pattern in _METROPOLITAN_AREA_PATTERNS:
        match = pattern.fullmatch(value)
        if match is None:
            continue
        anchor = _location_word_key(match.group("city"))
        if anchor and anchor not in _BROAD_LOCATION_KEYS and resolve_commune(anchor) is not None:
            return anchor
    return None


def _normalize_location_text(location: str | None) -> str:
    value = unidecode(str(location or "")).casefold().replace("'", " ")
    value = re.sub(r"[^\w,|/()\-\u2013\u2014\s]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    value = _LOCATION_WORK_MODE_SUFFIX_RE.sub("", value)
    return value.strip()


def _location_word_key(value: str) -> str:
    value = re.sub(r"[-\u2013\u2014]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _france_travail_native_id(job: JobLike) -> str | None:
    if str(getattr(job, "source", "")).strip().casefold() != "francetravail":
        return None
    source_data = getattr(job, "source_data", None)
    if not isinstance(source_data, Mapping):
        return None
    value = str(source_data.get("id") or "").strip().casefold()
    return value or None
