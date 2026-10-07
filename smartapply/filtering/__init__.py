"""Local rule-based filtering — keep this cheap and explainable."""

from smartapply.filtering.filters import (
    DeterministicRejector,
    JobFilter,
    ruleset_from_preferences,
)
from smartapply.filtering.hard_reject import (
    CandidateQualifications,
    Evidence,
    GateDecision,
    GateStatus,
    HardRejectDisposition,
    HardRejectResult,
)
from smartapply.filtering.rules import (
    DEFAULT_POSITIVE_TITLE_KEYWORDS,
    RuleSet,
)
from smartapply.filtering.source_facts import (
    FilterFacts,
    build_filter_facts,
    build_francetravail_filter_facts,
    build_serpapi_filter_facts,
    build_wttj_filter_facts,
)
from smartapply.filtering.types import FilterDisposition

__all__ = [
    "DEFAULT_POSITIVE_TITLE_KEYWORDS",
    "CandidateQualifications",
    "DeterministicRejector",
    "Evidence",
    "FilterFacts",
    "FilterDisposition",
    "GateDecision",
    "GateStatus",
    "HardRejectDisposition",
    "HardRejectResult",
    "JobFilter",
    "RuleSet",
    "build_filter_facts",
    "build_francetravail_filter_facts",
    "build_serpapi_filter_facts",
    "build_wttj_filter_facts",
    "ruleset_from_preferences",
]
