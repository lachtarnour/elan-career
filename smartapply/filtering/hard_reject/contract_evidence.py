"""Evidence extraction and formatting for the deterministic contract gate."""

from __future__ import annotations

import re

from smartapply.filtering.hard_reject.context import HardRejectContext
from smartapply.filtering.hard_reject.types import Evidence
from smartapply.filtering.text import norm
from smartapply.utils.contracts import (
    CDD_EQUIVALENT_TAGS,
    TAG_FULL_TIME,
    contract_type_tags,
)

_VISIBLE_PERMANENT_PATTERNS = (
    r"\b(?:type\s+de\s+contrat|contract\s+type|contrat|poste|emploi)\s*"
    r"[:=-]?\s*(?:en\s+)?cdi\b",
    r"\bcdi\s+(?:a\s+temps\s+plein|temps\s+plein|full[- ]time|"
    r"des\s+que\s+possible|immediat)\b",
    r"\b(?:permanent|open[- ]ended|indefinite)\s+"
    r"(?:contract|role|position|job|employment)\b",
    r"\b(?:role|position|job|employment)\s+(?:is\s+)?permanent\b",
    r"\bcontrat\s+(?:a|à)\s+duree\s+indeterminee\b",
    # A contract menu that contains an accepted option cannot prove that the
    # incompatible option is exclusive.  Keep the accepted option as conflict
    # evidence so the offer is routed to the later classifier.
    r"\b(?:type\s+de\s+contrat|contract\s+type|contrat)\b"
    r"[^.!?]{0,90}\bcdi\b",
    r"\b(?:statut|modalites?|employment\s+type|job\s+type)\b"
    r"[^.!?\n]{0,120}\bcdi\b",
    r"\b(?:"
    r"cdi\b[^.!?\n]{0,80}\b(?:ou|or)\b[^.!?\n]{0,80}"
    r"\b(?:freelance|contractor|cdd|fixed[- ]term)|"
    r"(?:freelance|contractor|cdd|fixed[- ]term)\b[^.!?\n]{0,80}"
    r"\b(?:ou|or)\b[^.!?\n]{0,80}\bcdi"
    r")\b",
    # Many short descriptions end with the ATS label as a standalone clause.
    r"(?:^|[.;:\n])\s*cdi\s*(?:[.;,\n]|$)",
    # A blocked and an accepted option in the same offer is a conflict, not
    # proof that only the blocked option applies.
    r"\b(?:cdd\s*(?:(?:ou|or)\s+(?:en\s+)?|/)cdi|"
    r"cdi\s*(?:(?:ou|or)\s+(?:en\s+)?|/)cdd|"
    r"fixed[- ]term\s+or\s+permanent|permanent\s+or\s+fixed[- ]term)\b",
)

_VISIBLE_FULL_TIME_PATTERNS = (
    r"\b(?:poste|emploi|role|position|job|contrat|employment)\b"
    r"[^.!?\n]{0,55}\b(?:a\s+)?(?:temps\s+plein|full[- ]time)\b",
    r"\b(?:temps\s+plein|full[- ]time)\b[^.!?\n]{0,35}"
    r"\b(?:poste|emploi|role|position|job|contrat|employment)\b",
    r"\b(?:horaire|schedule|work\s+time)\s*[:=-]\s*"
    r"(?:temps\s+plein|full[- ]time)\b",
)

_FUTURE_PERMANENT_PATTERNS = (
    r"\b(?:possibilite|perspective|opportunite|option|conversion|evolution)\b"
    r"[^.!?\n]{0,80}\bcdi\b",
    r"\b(?:peut|pourra|susceptible\s+de)\b[^.!?\n]{0,80}"
    r"\b(?:evoluer|deboucher|converti|transforme)\b[^.!?\n]{0,50}\bcdi\b",
    r"\bcdi\b[^.!?\n]{0,60}\b(?:apres|a\s+l[' ]?issue|possible|eventuel)\b",
    r"\bpermanent\s+(?:offer|contract|role|position)\b[^.!?\n]{0,60}"
    r"\b(?:after|later|possible|potential)\b",
    r"\b(?:potential|possible)\s+permanent\s+(?:offer|contract|role|position)\b",
)


def permanent_contract_evidence(
    context: HardRejectContext,
    *,
    safe_title: str,
    safe_description: str,
) -> tuple[Evidence, ...]:
    """Return direct evidence that the advertised contract is permanent."""

    evidence: list[Evidence] = []

    if "permanent" in contract_type_tags(context.job.contract_type):
        evidence.append(
            Evidence(
                rule_id="contract.metadata_permanent",
                source="job.contract_type",
                quote=str(context.job.contract_type),
                field="contract_type",
            )
        )

    structured_value = context.facts.structured_contract_type
    if "permanent" in contract_type_tags(structured_value):
        evidence.append(
            Evidence(
                rule_id="contract.source_fact_permanent",
                source=(
                    context.facts.structured_contract_source
                    or structured_source(context, "structured_contract_type")
                ),
                quote=str(structured_value),
                field="structured_contract_type",
            )
        )

    visible_title = _mask_future_permanent(safe_title)
    visible_description = _mask_future_permanent(safe_description)
    if _matches_permanent_contract(visible_title, is_title=True):
        evidence.append(
            Evidence(
                rule_id="contract.visible_permanent",
                source="job.title",
                quote=str(context.job.title),
                field="title",
            )
        )
    if _matches_permanent_contract(visible_description, is_title=False):
        evidence.append(
            Evidence(
                rule_id="contract.visible_permanent",
                source="job.description",
                quote=_matching_excerpt(
                    str(context.job.description),
                    ("cdi", "permanent", "open-ended", "open ended"),
                ),
                field="description",
            )
        )
    return tuple(deduplicate_evidence(evidence))


def full_time_evidence(
    context: HardRejectContext,
    *,
    safe_title: str,
    safe_description: str,
) -> tuple[Evidence, ...]:
    """Return structured or visible evidence of a compatible full-time schedule."""

    evidence: list[Evidence] = []
    if TAG_FULL_TIME in contract_type_tags(context.job.contract_type):
        evidence.append(
            Evidence(
                rule_id="contract.metadata_full_time",
                source="job.contract_type",
                quote=str(context.job.contract_type),
                field="contract_type",
            )
        )

    structured_value = context.facts.structured_contract_type
    if TAG_FULL_TIME in contract_type_tags(structured_value):
        evidence.append(
            Evidence(
                rule_id="contract.source_fact_full_time",
                source=(
                    context.facts.structured_contract_source
                    or structured_source(context, "structured_contract_type")
                ),
                quote=str(structured_value),
                field="structured_contract_type",
            )
        )

    if _matches_full_time(safe_title, is_title=True):
        evidence.append(
            Evidence(
                rule_id="contract.visible_full_time",
                source="job.title",
                quote=str(context.job.title),
                field="title",
            )
        )
    if _matches_full_time(safe_description, is_title=False):
        evidence.append(
            Evidence(
                rule_id="contract.visible_full_time",
                source="job.description",
                quote=_matching_excerpt(
                    str(context.job.description),
                    ("temps plein", "full-time", "full time"),
                ),
                field="description",
            )
        )
    return tuple(deduplicate_evidence(evidence))


def visible_evidence(
    context: HardRejectContext,
    *,
    title_matches: bool,
    needles: tuple[str, ...],
) -> tuple[str, str, str]:
    """Describe the title or description fragment supporting a visible rule."""

    if title_matches:
        return "job.title", str(context.job.title), "title"
    return (
        "job.description",
        _matching_excerpt(str(context.job.description), needles),
        "description",
    )


def marker_needles(marker: str) -> tuple[str, ...]:
    """Return display aliases used to find a readable evidence excerpt."""

    normalized = norm(marker)
    aliases: dict[str, tuple[str, ...]] = {
        "stage": ("stage", "stagiaire", "internship", "intern"),
        "alternance": (
            "alternance",
            "alternant",
            "apprentissage",
            "apprenticeship",
            "work-study",
            "work study",
            "co-op",
            "co op",
        ),
        "freelance": (
            "freelance",
            "contractor",
            "contract position",
            "contract role",
        ),
        "independant": (
            "independant",
            "indépendant",
            "self-employed",
            "self employed",
        ),
    }
    return aliases.get(normalized, (marker,))


def structured_source(context: HardRejectContext, field: str) -> str:
    """Return an auditable fallback source name for a structured fact."""

    source = norm(context.facts.source).replace(" ", "_") or "unknown"
    return f"{source}:{field}"


def research_cdd_label(context: HardRejectContext) -> str:
    """Return the human-readable fixed-term label covered by the exception."""

    for value in (
        context.job.contract_type,
        context.facts.structured_contract_type,
    ):
        if value and contract_type_tags(value) & CDD_EQUIVALENT_TAGS:
            return norm(value).strip()
    return "visible_text"


def reason_value(value: str | None) -> str:
    """Normalize a source value for a stable reason code."""

    return norm(value).replace(" ", "_") or "unknown"


def deduplicate_evidence(evidence: list[Evidence]) -> list[Evidence]:
    """Preserve evidence order while removing exact audit duplicates."""

    unique: list[Evidence] = []
    seen: set[tuple[str, str, str]] = set()
    for item in evidence:
        key = (item.rule_id, item.source, item.quote)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _matches_full_time(text: str, *, is_title: bool) -> bool:
    if is_title and re.search(r"\b(?:temps\s+plein|full[- ]time)\b", text):
        return True
    return any(re.search(pattern, text) for pattern in _VISIBLE_FULL_TIME_PATTERNS)


def _mask_future_permanent(text: str) -> str:
    masked = text
    for pattern in _FUTURE_PERMANENT_PATTERNS:
        masked = re.sub(pattern, " ", masked)
    return masked


def _matches_permanent_contract(text: str, *, is_title: bool) -> bool:
    if is_title and re.search(r"(?<![a-z0-9])cdi(?![a-z0-9])", text):
        return True
    if is_title and re.search(r"(?<![a-z0-9])permanent(?![a-z0-9])", text):
        return True
    return any(re.search(pattern, text) for pattern in _VISIBLE_PERMANENT_PATTERNS)


def _matching_excerpt(raw_text: str, needles: tuple[str, ...]) -> str:
    clauses = re.split(r"(?<=[.!?;])\s+|\n+", raw_text)
    normalized_needles = tuple(norm(needle) for needle in needles)
    for clause in clauses:
        normalized_clause = norm(clause)
        if any(needle in normalized_clause for needle in normalized_needles):
            return _shorten(clause.strip())
    return _shorten(raw_text.strip())


def _shorten(text: str, limit: int = 240) -> str:
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


__all__ = [
    "deduplicate_evidence",
    "full_time_evidence",
    "marker_needles",
    "permanent_contract_evidence",
    "reason_value",
    "research_cdd_label",
    "structured_source",
    "visible_evidence",
]
