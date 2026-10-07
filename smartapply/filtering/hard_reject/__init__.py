"""Deterministic, evidence-backed rejection gates.

This package deliberately answers one question only: does the offer contain a
locally provable blocker?  Relevance classification belongs to a later stage.
"""

from smartapply.filtering.hard_reject.context import (
    CandidateQualifications,
    HardRejectContext,
)
from smartapply.filtering.hard_reject.engine import DeterministicRejector, JobFilter
from smartapply.filtering.hard_reject.types import (
    Evidence,
    GateDecision,
    GateStatus,
    HardRejectDisposition,
    HardRejectResult,
)

__all__ = [
    "CandidateQualifications",
    "DeterministicRejector",
    "Evidence",
    "GateDecision",
    "GateStatus",
    "HardRejectContext",
    "HardRejectDisposition",
    "HardRejectResult",
    "JobFilter",
]
