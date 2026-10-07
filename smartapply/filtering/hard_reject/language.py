"""Deterministic gate for confidently identified offer languages."""

from __future__ import annotations

from smartapply.filtering.hard_reject.context import HardRejectContext
from smartapply.filtering.hard_reject.types import Evidence, GateDecision, GateStatus
from smartapply.language import detect_offer_language_confident

_GATE = "language"


def evaluate_language(context: HardRejectContext) -> GateDecision:
    """Reject only a confidently detected language excluded by the ruleset.

    Short and mixed-language offers deliberately remain unresolved by the
    detector and pass this hard gate.  They can still be handled by a later
    relevance stage without becoming deterministic false negatives.
    """

    detected = detect_offer_language_confident(context.combined)
    if detected is None:
        return GateDecision.clear(_GATE)

    note = f"offer_language:{detected}"
    evidence = Evidence(
        rule_id="offer_language_confident",
        source="visible_text",
        quote=_excerpt(
            f"{getattr(context.job, 'title', '')}\n"
            f"{getattr(context.job, 'description', '')}"
        ),
        field="title_description",
    )
    accepted = tuple(language.lower() for language in context.rules.accepted_job_languages)
    if accepted and detected not in accepted:
        return GateDecision(
            gate=_GATE,
            status=GateStatus.BLOCKED,
            reason_codes=(note, f"offer_language_not_accepted:{detected}"),
            evidence=(evidence,),
        )

    return GateDecision(
        gate=_GATE,
        status=GateStatus.CLEAR,
        evidence=(evidence,),
        notes=(note,),
    )


def _excerpt(text: str, *, limit: int = 180) -> str:
    return text.strip()[:limit]


__all__ = ["evaluate_language"]
