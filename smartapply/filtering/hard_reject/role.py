"""Deterministic role-family blockers with a strict candidate-mission veto."""

from __future__ import annotations

import re
from dataclasses import dataclass

from smartapply.filtering.hard_reject.context import HardRejectContext
from smartapply.filtering.hard_reject.mission import candidate_ai_mission
from smartapply.filtering.hard_reject.role_families import (
    TitleFamilyMatch,
    TitleFamilyPolicy,
    match_title_families,
)
from smartapply.filtering.hard_reject.types import Evidence, GateDecision, GateStatus
from smartapply.filtering.text import norm
from smartapply.utils.contracts import contract_type_tags

_GATE = "role"

_BUILT_IN_TARGET_TITLE = re.compile(
    r"\b(?:data scientist|data science|scientifique des donnees|"
    r"machine learning engineer|ml engineer|"
    r"ingenieur machine learning|ai(?:[\s/&-]+\w+){0,3}[\s/&-]+engineer|"
    r"ia(?:[\s/&-]+\w+){0,3}[\s/&-]+engineer|"
    r"ingenieur(?:e)?(?:[\s/&-]+\w+){0,3}[\s/&-]+(?:ia|ai)|"
    r"ingenieur(?:e)?\s+logiciel\s+vision|"
    r"developpeur(?:se)?(?:[\s/&-]+\w+){0,7}[\s/&-]+(?:vision|ia|ai|"
    r"intelligence\s+artificielle|traitement\s+d[' ]?images?)|"
    r"(?:developpeur|developpeuse|ingenieur(?:e)?(?:\s+logiciel)?).{0,120}"
    r"(?:vision\s+par\s+ordinateur|traitement\s+d[' ]?images?|intelligence\s+artificielle)|"
    r"research engineer|"
    r"ingenieur de recherche|research scientist|applied scientist|data analyst|"
    r"analyste data|analyste de donnees|product data analyst|product analyst|"
    r"analytics engineer|decision scientist|nlp engineer|computer vision engineer)\b"
)

_DOMINANT_NON_TARGET_TITLE = re.compile(
    r"\b(?:product\s+(?:delivery\s+)?manager|chef(?:fe)?\s+de\s+projet|"
    r"business\s+analyst|architecte?|quality\s+assurance|qa|testeur|"
    r"psychologist)\b"
    r"|\bresearch\s+engineer\s+in\s+(?:machinability|metallurgy|materials?)\b"
)
_AI_TITLE_TOKEN = re.compile(
    r"(?<![a-z0-9])(?:ai|ia|ml|genai|machine\s+learning|deep\s+learning|"
    r"intelligence\s+artificielle)(?![a-z0-9])"
)
_TARGET_DOMAIN_QUALIFIER = re.compile(
    r"(?<![a-z0-9])(?:machine\s+learning\s+engineer|ml\s+engineer|"
    r"ai\s+engineer|ia\s+engineer|data\s+science|data\s+scientist|"
    r"data\s+analyst|analytics|research\s+engineer|applied\s+scientist|"
    r"nlp\s+engineer|computer\s+vision\s+engineer)(?![a-z0-9])"
)
_TECHNICAL_TITLE_TOKEN = re.compile(
    r"\b(?:engineer(?:ing)?|ingenieur(?:e)?|platform|devops|sre|"
    r"site\s+reliability)\b"
)
_ANALYTICAL_DATA_TITLE = re.compile(
    r"\b(?:previsions?|forecasting|tarification|pricing)\b.{0,45}\bdata\b"
    r"|\bdata\b.{0,45}\b(?:previsions?|forecasting|tarification|pricing)\b"
)


@dataclass(frozen=True)
class _Blocker:
    reason_code: str
    rule_id: str
    quote: str
    field: str
    candidate_mission_can_override: bool = True


def evaluate_role(context: HardRejectContext) -> GateDecision:
    """Reject established off-target work while preserving real AI missions.

    This gate never labels a surviving offer ``relevant`` or ``uncertain``.
    It only proves role-family blockers.  An explicit candidate AI mission
    invalidates title-only blockers; every other deterministic gate remains
    independent and can still reject the offer.
    """

    family_matches = match_title_families(context.title)
    blockers = _role_blockers(context, family_matches)
    mission = candidate_ai_mission(str(getattr(context.job, "description", "") or ""))
    strong_target_title = _has_target_title(context)
    mixed_target_title = _TARGET_DOMAIN_QUALIFIER.search(context.title) is not None
    title_veto: Evidence | None = None
    title_veto_note: str | None = None
    if mission is not None:
        # Concrete candidate-owned AI work contradicts a title-only blocker.
        # It also contradicts the narrow "AI only in a commercial context"
        # blocker.  The later relevance stage decides which work dominates.
        blockers = [
            blocker
            for blocker in blockers
            if not (
                (blocker.field == "title" and blocker.candidate_mission_can_override)
                or blocker.rule_id == "non_technical_ai_context"
            )
        ]
        title_veto = Evidence(
            rule_id="candidate_ai_mission",
            source="visible_text",
            quote=mission,
            field="description",
        )
        title_veto_note = "role_title_veto:candidate_ai_mission"
    elif strong_target_title or mixed_target_title:
        blockers = [blocker for blocker in blockers if blocker.field != "title"]
        title_veto = Evidence(
            rule_id=(
                "explicit_target_role_title" if strong_target_title else "mixed_target_role_title"
            ),
            source="visible_text",
            quote=str(getattr(context.job, "title", "") or ""),
            field="title",
        )
        title_veto_note = (
            "role_title_veto:target_role_title"
            if strong_target_title
            else "role_title_veto:mixed_target_title"
        )

    if not blockers:
        deferred = tuple(
            match
            for match in family_matches
            if match.policy is TitleFamilyPolicy.DEFER_TO_RELEVANCE
        )
        if title_veto is not None:
            deferred = tuple(dict.fromkeys((*deferred, *family_matches)))
        if deferred:
            evidence = (
                (title_veto,)
                if title_veto is not None
                else (
                    Evidence(
                        rule_id="adjacent_role_title",
                        source="visible_text",
                        quote=str(getattr(context.job, "title", "") or ""),
                        field="title",
                    ),
                )
            )
            notes = (title_veto_note,) if title_veto_note is not None else ()
            return GateDecision.unresolved(
                _GATE,
                conflicts=tuple(
                    f"role_family_requires_relevance:{match.name}" for match in deferred
                ),
                evidence=evidence,
                notes=notes,
            )
        if title_veto is not None and title_veto_note is not None:
            return GateDecision(
                gate=_GATE,
                status=GateStatus.CLEAR,
                evidence=(title_veto,),
                notes=(title_veto_note,),
            )
        return GateDecision.clear(_GATE)

    return GateDecision(
        gate=_GATE,
        status=GateStatus.BLOCKED,
        reason_codes=tuple(dict.fromkeys(blocker.reason_code for blocker in blockers)),
        evidence=tuple(
            Evidence(
                rule_id=blocker.rule_id,
                source="visible_text",
                quote=blocker.quote,
                field=blocker.field,
            )
            for blocker in blockers
        ),
    )


def _role_blockers(
    context: HardRejectContext,
    family_matches: tuple[TitleFamilyMatch, ...],
) -> list[_Blocker]:
    title = context.title
    raw_title = str(getattr(context.job, "title", "") or "").strip()
    blockers: list[_Blocker] = []

    has_hard_family = any(match.policy is TitleFamilyPolicy.HARD_REJECT for match in family_matches)
    has_only_deferred_families = bool(family_matches) and not has_hard_family
    for configured in context.rules.deal_breakers:
        blocker = norm(configured).strip()
        if (
            not has_only_deferred_families
            and _is_role_deal_breaker(blocker)
            and _has_phrase(title, blocker)
        ):
            blockers.append(
                _title_blocker(f"deal_breaker_in_title:{blocker}", "deal_breaker", raw_title)
            )

    for match in family_matches:
        if match.policy is not TitleFamilyPolicy.HARD_REJECT:
            continue
        blockers.append(
            _title_blocker(
                f"off_target_role_family:{match.name}",
                match.name,
                raw_title,
            )
        )

    return _deduplicate_blockers(blockers)


def _is_role_deal_breaker(value: str) -> bool:
    """Keep other deterministic concerns in their own independent gate."""

    if not value or contract_type_tags(value):
        return False
    return re.search(r"\b(?:years?|ans?|without|sans|only|uniquement)\b|\d", value) is None


def _has_target_title(context: HardRejectContext) -> bool:
    if _DOMINANT_NON_TARGET_TITLE.search(context.title):
        return False
    if _AI_TITLE_TOKEN.search(context.title) and _TECHNICAL_TITLE_TOKEN.search(context.title):
        return True
    if _ANALYTICAL_DATA_TITLE.search(context.title):
        return True
    if _BUILT_IN_TARGET_TITLE.search(context.title):
        return True
    configured = (*context.rules.target_roles, *context.rules.positive_title_keywords)
    return any(_has_phrase(context.title, norm(value)) for value in configured if norm(value))


def _has_phrase(text: str, phrase: str) -> bool:
    words = [re.escape(word) for word in phrase.split()]
    separator = r"[\s_/-]+"
    pattern = separator.join(words)
    return bool(re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", text))


def _title_blocker(
    reason_code: str,
    rule_id: str,
    title: str,
    *,
    candidate_mission_can_override: bool = True,
) -> _Blocker:
    return _Blocker(
        reason_code,
        rule_id,
        title,
        "title",
        candidate_mission_can_override,
    )


def _deduplicate_blockers(blockers: list[_Blocker]) -> list[_Blocker]:
    unique: dict[tuple[str, str], _Blocker] = {}
    for blocker in blockers:
        unique.setdefault((blocker.reason_code, blocker.rule_id), blocker)
    return list(unique.values())


__all__ = ["evaluate_role"]
