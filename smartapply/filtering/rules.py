"""Rule sets for the local job filter.

Keep rules data-driven and overridable so they can be tuned per profile
without changing code. ``Profile.preferences`` is the source of truth for
target roles, deal-breakers, locations and contract types.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Default target vocabulary shared by hard rejection and later relevance work.
DEFAULT_POSITIVE_TITLE_KEYWORDS = (
    "data scientist",
    "machine learning",
    "ml engineer",
    "ai engineer",
    "ia engineer",
    "ai researcher",
    "research engineer",
    "applied scientist",
    "nlp engineer",
    "data analyst",
    "product analyst",
    "product data analyst",
    "analytics engineer",
    "data analytics",
    "deep learning",
    "computer vision",
)

# These title markers feed the seniority gate. Ambiguous labels such as
# Sr/Lead require a firm candidate requirement or current hierarchical
# management; otherwise the offer remains pending relevance.
DEFAULT_SENIORITY_TITLE_HARD_REJECT = (
    "senior ",
    "sr.",
    "lead ",
    "principal ",
    "staff ",
)

# Operational policy shared by the local seniority gates and the LLM context.
# It is intentionally not a user preference: changing the old profile field
# never changed the deterministic five-year ceiling.
CANDIDATE_SENIORITY_CONTEXT = "junior to mid (1-3 years)"

# Contract types that should be hard-rejected regardless of other signals.
# Matched as a substring against the normalized ``contract_type`` field.
# We rely on ATS values like "Internship", "Stage", "Alternance",
# "Apprentissage" coming straight from SerpApi / France Travail.
DEFAULT_BLOCKED_CONTRACT_TYPES = (
    "stage",
    "stagiaire",
    "internship",
    "alternance",
    "alternant",
    "alternante",
    "apprenti",
    "apprentissage",
    "apprentice",
    "apprenticeship",
    "freelance",
    "contractor",
    "independant",
    "indépendant",
)


@dataclass
class RuleSet:
    """A bundle of rules used by the filter."""

    target_roles: list[str] = field(default_factory=list)
    deal_breakers: list[str] = field(default_factory=list)
    accepted_contract_types: list[str] = field(default_factory=list)
    accepted_remote_policies: list[str] = field(default_factory=list)
    accepted_job_languages: list[str] = field(default_factory=lambda: ["fr", "en"])

    positive_title_keywords: tuple[str, ...] = DEFAULT_POSITIVE_TITLE_KEYWORDS
    seniority_title_hard_reject: tuple[str, ...] = DEFAULT_SENIORITY_TITLE_HARD_REJECT
    blocked_contract_types: tuple[str, ...] = DEFAULT_BLOCKED_CONTRACT_TYPES

    max_required_years: int = 5
    """Hard reject if the offer asks for >= this many years of experience.

    Default 5 — user policy is to keep 3-4 years requirements reachable,
    while rejecting explicit 5+ ans / 5+ years requirements. Lowering this
    value tightens the funnel, raising it relaxes it.
    """
