"""Public compatibility facade for deterministic rejection.

The implementation lives in :mod:`smartapply.filtering.hard_reject`. Keeping
this module small preserves the historical ``JobFilter`` import without
mixing rejection, relevance classification and scoring again.
"""

from smartapply.filtering.hard_reject import DeterministicRejector, JobFilter
from smartapply.filtering.preferences import ruleset_from_preferences

__all__ = [
    "DeterministicRejector",
    "JobFilter",
    "ruleset_from_preferences",
]
