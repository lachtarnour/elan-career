"""Shared fit annotation categories and advisory decision inspection."""

from __future__ import annotations

from smartapply.llm.schemas import JobFitDecisionEvaluation, OfferAnalysis

AXES = ("missions", "essentials", "responsibilities")
POSITIVE = {"direct", "transferable", "declared_only", "partial_support"}
PREFERENCE_FIELDS = {"accepted_contract_types", "accepted_remote_policies", "deal_breakers"}


def inspect_evaluation(
    evaluation: JobFitDecisionEvaluation, context: dict, offer: OfferAnalysis
) -> list[str]:
    """Report annotation inconsistencies without changing fit decisions."""
    from smartapply.llm.fit_decision_validation import inspect_decisions

    return inspect_decisions(evaluation, context, offer)
