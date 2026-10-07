"""Coordinator for the independent deterministic rejection gates."""

from __future__ import annotations

from collections.abc import Callable

from smartapply.filtering.filter_helpers import (
    _reason_value,
    _rome_context_reason,
    _search_context_reason,
)
from smartapply.filtering.hard_reject.context import (
    CandidateQualifications,
    HardRejectContext,
)
from smartapply.filtering.hard_reject.contract import evaluate_contract
from smartapply.filtering.hard_reject.experience import evaluate_experience
from smartapply.filtering.hard_reject.language import evaluate_language
from smartapply.filtering.hard_reject.qualification import evaluate_qualification
from smartapply.filtering.hard_reject.role import evaluate_role
from smartapply.filtering.hard_reject.seniority import evaluate_seniority
from smartapply.filtering.hard_reject.types import GateDecision, HardRejectResult
from smartapply.filtering.rules import RuleSet
from smartapply.filtering.source_facts import FilterFacts, build_filter_facts
from smartapply.filtering.types import HasJobFields

GateEvaluator = Callable[[HardRejectContext], GateDecision]

_GATES: tuple[GateEvaluator, ...] = (
    evaluate_language,
    evaluate_contract,
    evaluate_experience,
    evaluate_qualification,
    evaluate_seniority,
    evaluate_role,
)


class DeterministicRejector:
    """Reject only when at least one local gate proves an incompatibility.

    The class never decides whether a surviving offer is relevant or
    uncertain.  Survivors receive ``pending_relevance`` and continue to the
    separate ranking/classification stages.
    """

    def __init__(
        self,
        rules: RuleSet,
        *,
        use_source_facts: bool = True,
        qualifications: CandidateQualifications | None = None,
    ):
        self.rules = rules
        self.use_source_facts = use_source_facts
        self.qualifications = qualifications or CandidateQualifications()

    def evaluate(self, job: HasJobFields) -> HardRejectResult:
        facts = build_filter_facts(job) if self.use_source_facts else FilterFacts()
        context = HardRejectContext.build(
            job=job,
            rules=self.rules,
            facts=facts,
            qualifications=self.qualifications,
        )
        decisions = tuple(evaluator(context) for evaluator in _GATES)
        return HardRejectResult(
            decisions=decisions,
            context_reasons=tuple(self._context_reasons(context)),
        )

    @staticmethod
    def _context_reasons(context: HardRejectContext) -> list[str]:
        reasons: list[str] = []
        if rome_context := _rome_context_reason(context.facts):
            reasons.append(rome_context)
        if search_context := _search_context_reason(context.facts):
            reasons.append(search_context)
        if context.facts.structured_remote_policy:
            reasons.append(
                "remote_structured:"
                f"{_reason_value(context.facts.structured_remote_policy)}"
            )
        return reasons

    def filter_many(
        self,
        jobs: list[HasJobFields],
    ) -> tuple[list[HasJobFields], list[tuple[HasJobFields, HardRejectResult]]]:
        kept: list[HasJobFields] = []
        evaluated: list[tuple[HasJobFields, HardRejectResult]] = []
        for job in jobs:
            result = self.evaluate(job)
            evaluated.append((job, result))
            if result.kept:
                kept.append(job)
        return kept, evaluated


# Compatibility name used by the pipeline and external callers.  Its behavior
# is intentionally the deterministic-rejection behavior described above.
JobFilter = DeterministicRejector

__all__ = ["DeterministicRejector", "JobFilter"]
