"""Bilingual job-title families used to expand source search queries.

This nomenclature is intentionally separate from ``smartapply.cv.role_family``:
the CV module classifies one offer after ingestion, while this module increases
search recall before offers exist. Matching remains many-to-many inside the
canonical catalogue; shared aliases are de-duplicated only in a concrete search.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from unidecode import unidecode


@dataclass(frozen=True)
class SearchRoleFamily:
    """One distinct job type and its commonly searched English/French titles."""

    key: str
    aliases: tuple[str, ...]
    legacy_aliases: tuple[str, ...] = ()

    @property
    def canonical_title(self) -> str:
        return self.aliases[0]


# Families and aliases are ordered by search priority. The canonical title is
# a precise representative used by the desktop's default search. Legacy terms
# activate families but are replaced before a provider receives a query.
# The catalogue stays deliberately compact: a
# modifier-only or word-order variant is omitted when a shorter retained query
# already covers the same title.
ROLE_FAMILIES: Final[tuple[SearchRoleFamily, ...]] = (
    SearchRoleFamily(
        key="ai_engineer",
        aliases=(
            "AI Engineer",
            "Ingénieur IA",
            "IA Engineer",
            "Applied AI Engineer",
            "Generative AI Engineer",
            "Agentic AI Engineer",
            "Multimodal AI Engineer",
            "Medical AI Engineer",
            "Healthcare Data Scientist",
            "Artificial Intelligence Engineer",
            "Ingénieur en intelligence artificielle",
            "Ingénieur Intelligence Artificielle",
            "AI/ML Engineer",
            "Ingénieur IA/ML",
            "LLM Engineer",
            "AI Developer",
            "Développeur IA",
        ),
    ),
    SearchRoleFamily(
        key="machine_learning_engineer",
        aliases=(
            "Machine Learning Engineer",
            "ML Engineer",
            "Ingénieur Machine Learning",
            "Ingénieur ML",
            "AI/ML Engineer",
            "Ingénieur IA/ML",
            "Machine Learning Developer",
            "Développeur Machine Learning",
            "Ingénieur en apprentissage automatique",
            "Deep Learning Engineer",
            "Ingénieur Deep Learning",
            "Machine Learning Research Engineer",
            "ML Research Engineer",
        ),
    ),
    SearchRoleFamily(
        key="data_scientist",
        aliases=(
            "Data Scientist",
            "Scientifique des données",
            "Machine Learning Scientist",
            "Ingénieur Data Science",
            "Ingénieur en science des données",
        ),
        legacy_aliases=("Data Science Engineer",),
    ),
    SearchRoleFamily(
        key="research_engineer",
        aliases=(
            "AI Research Engineer",
            "Ingénieur recherche IA",
            "Machine Learning Research Engineer",
            "ML Research Engineer",
            "Computer Vision Research Engineer",
            "Ingénieur recherche en vision par ordinateur",
        ),
        legacy_aliases=(
            "Research Engineer",
            "Ingénieur de recherche",
            "R&D Engineer",
            "Ingénieur R&D",
            "Ingénieur recherche et développement",
            "Ingénieur recherche algorithmique",
            "Speech Research Engineer",
            "Ingénieur recherche en traitement de la parole",
        ),
    ),
    SearchRoleFamily(
        key="applied_scientist",
        aliases=(
            "Applied ML Scientist",
            "AI Scientist",
            "Machine Learning Scientist",
            "AI Research Scientist",
            "Machine Learning Research Scientist",
            "Chercheur en IA appliquée",
            "Chercheur en apprentissage automatique",
            "Scientifique IA appliquée",
            "Chercheur Data Science",
        ),
        legacy_aliases=("Applied Scientist",),
    ),
    SearchRoleFamily(
        key="nlp_engineer",
        aliases=(
            "NLP Engineer",
            "Ingénieur NLP",
            "Natural Language Processing Engineer",
            "Ingénieur traitement automatique du langage",
            "Ingénieur TAL",
            "Ingénieur TALN",
            "Language AI Engineer",
            "Language Model Engineer",
            "Ingénieur modèles de langage",
            "Conversational AI Engineer",
            "Ingénieur IA conversationnelle",
            "Text Mining Engineer",
        ),
    ),
    SearchRoleFamily(
        key="computer_vision_engineer",
        aliases=(
            "Computer Vision Engineer",
            "Ingénieur Computer Vision",
            "Ingénieur vision par ordinateur",
            "Vision AI Engineer",
            "Ingénieur IA Vision",
            "Computer Vision ML Engineer",
            "Computer Vision Scientist",
            "Scientifique en vision par ordinateur",
            "Ingénieur IA traitement d'images",
            "Computer Vision Research Engineer",
            "Ingénieur recherche en vision par ordinateur",
            "AI Perception Engineer",
            "Ingénieur perception IA",
        ),
    ),
    SearchRoleFamily(
        key="speech_audio_ai_engineer",
        aliases=(
            "Speech AI Engineer",
            "Ingénieur IA vocale",
            "Audio AI Engineer",
            "Ingénieur IA Audio",
            "Speech ML Engineer",
            "Ingénieur Machine Learning parole",
            "Audio ML Engineer",
            "Speech Recognition Engineer",
            "Ingénieur reconnaissance vocale",
            "Ingénieur IA traitement de la parole",
            "Voice AI Engineer",
            "Audio Deep Learning Engineer",
            "Conversational AI Engineer",
            "Ingénieur IA conversationnelle",
        ),
        legacy_aliases=(
            "Speech Research Engineer",
            "Ingénieur recherche en traitement de la parole",
        ),
    ),
    SearchRoleFamily(
        key="data_analyst",
        aliases=(
            "Data Analyst",
            "Analyste Data",
            "Analyste de données",
            "Analyste des données",
            "Analyste statistique de données",
            "Research Data Analyst",
            "Analyste données recherche",
        ),
    ),
    SearchRoleFamily(
        key="analytics_engineer",
        aliases=(
            "Analytics Engineer",
            "Ingénieur Analytics",
            "Ingénieur Data Analytics",
            "Data Transformation Engineer",
            "Ingénieur transformation des données",
            "Data Modeling Engineer",
            "Ingénieur modélisation des données",
            "Analytics Platform Engineer",
        ),
    ),
    SearchRoleFamily(
        key="data_ai_consultant",
        aliases=(
            "Technical AI Consultant",
            "Data & AI Consultant",
            "Consultant Data Science",
            "AI/ML Consultant",
            "Consultant IA/ML",
            "Machine Learning Consultant",
            "Consultant Machine Learning",
            "Applied AI Consultant",
            "Consultant IA appliquée",
            "Consultant technique IA",
        ),
        legacy_aliases=("Data Consultant", "Consultant Data"),
    ),
)


def normalize_role_title(value: str) -> str:
    """Normalize accents, punctuation and spacing for exact set membership."""
    normalized = unidecode(value).casefold()
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


ROLE_FAMILY_SETS: Final[dict[str, frozenset[str]]] = {
    family.key: frozenset(normalize_role_title(alias) for alias in family.aliases)
    for family in ROLE_FAMILIES
}

ROLE_FAMILY_TRIGGER_SETS: Final[dict[str, frozenset[str]]] = {
    family.key: ROLE_FAMILY_SETS[family.key]
    | frozenset(normalize_role_title(alias) for alias in family.legacy_aliases)
    for family in ROLE_FAMILIES
}

SEARCH_TERM_REPLACEMENTS: Final[dict[str, tuple[str, ...]]] = {
    normalize_role_title(term): replacements
    for term, replacements in {
        "Research Engineer": ("AI Research Engineer", "Machine Learning Research Engineer"),
        "Ingénieur de recherche": ("Ingénieur recherche IA",),
        "R&D Engineer": ("AI Research Engineer", "Machine Learning Research Engineer"),
        "Ingénieur R&D": ("Ingénieur recherche IA",),
        "Ingénieur recherche et développement": ("Ingénieur recherche IA",),
        "Applied Scientist": ("Applied ML Scientist", "AI Research Scientist"),
        "Ingénieur recherche algorithmique": (
            "Ingénieur recherche IA", "Machine Learning Research Engineer",
        ),
        "Data Science Engineer": ("Data Scientist", "Machine Learning Engineer"),
        "Speech Research Engineer": ("Speech ML Engineer", "Speech AI Engineer"),
        "Ingénieur recherche en traitement de la parole": (
            "Ingénieur Machine Learning parole", "Ingénieur IA traitement de la parole",
        ),
        "Data Consultant": (
            "Machine Learning Consultant", "Applied AI Consultant", "Technical AI Consultant",
        ),
        "Consultant Data": (
            "Consultant Data Science", "Consultant Machine Learning", "Consultant technique IA",
        ),
    }.items()
}


def default_search_query() -> str:
    """Activate every supported family with one precise representative title."""
    return " OR ".join(family.canonical_title for family in ROLE_FAMILIES)


def precise_role_terms(terms: list[str] | tuple[str, ...]) -> list[str]:
    """Replace known broad terms while preserving unknown custom titles."""
    resolved: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = re.sub(r"\s+", " ", term.strip())
        replacements = SEARCH_TERM_REPLACEMENTS.get(normalize_role_title(cleaned), (cleaned,))
        for candidate in replacements:
            key = normalize_role_title(candidate)
            if key and key not in seen:
                seen.add(key)
                resolved.append(candidate)
    return resolved


def matching_role_families(terms: list[str] | tuple[str, ...]) -> tuple[SearchRoleFamily, ...]:
    """Return intersecting families once, following submitted term order."""
    matched: list[SearchRoleFamily] = []
    seen: set[str] = set()
    for term in terms:
        normalized = normalize_role_title(term)
        for family in ROLE_FAMILIES:
            if normalized not in ROLE_FAMILY_TRIGGER_SETS[family.key] or family.key in seen:
                continue
            seen.add(family.key)
            matched.append(family)
    return tuple(matched)


def expand_role_terms(terms: list[str] | tuple[str, ...]) -> list[str]:
    """Expand all intersecting families while retaining stable, unique titles.

    Precise user terms stay first. Remaining aliases are then emitted one round at a
    time across the activated families, preserving the priority inside each
    family without letting one long family monopolize the fallback order.
    """
    expanded: list[str] = []
    seen: set[str] = set()

    def append_unique(value: str) -> None:
        cleaned = re.sub(r"\s+", " ", value).strip()
        key = normalize_role_title(cleaned)
        if cleaned and key and key not in seen:
            seen.add(key)
            expanded.append(cleaned)

    for term in precise_role_terms(terms):
        append_unique(term)
    families = matching_role_families(terms)
    alias_queues = tuple(
        tuple(alias for alias in family.aliases if normalize_role_title(alias) not in seen)
        for family in families
    )
    rounds = max((len(aliases) for aliases in alias_queues), default=0)
    for alias_index in range(rounds):
        for aliases in alias_queues:
            if alias_index < len(aliases):
                append_unique(aliases[alias_index])
    return expanded
