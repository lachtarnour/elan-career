"""Neutral seniority signals shared by filtering and ranking.

This module only interprets visible title and description text. It does not
choose a filter disposition or assign a ranking score; those policies belong
to their respective adapters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from smartapply.filtering.text import norm


class SeniorityAssessmentStatus(str, Enum):
    """Outcome of the text-only seniority assessment."""

    CLEAR = "clear"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"


class SenioritySignalKind(str, Enum):
    """Stable identifiers for the seniority evidence found in an offer."""

    EXPLICIT_TITLE = "explicit_seniority_title"
    HIDDEN_ROLE = "hidden_senior_role"
    HIERARCHICAL_MANAGEMENT = "hierarchical_team_management"
    AMBIGUOUS_TITLE = "ambiguous_seniority_title"


@dataclass(frozen=True)
class SeniorityAssessment:
    """Text-only seniority finding, independent from rejection and scoring."""

    status: SeniorityAssessmentStatus
    kind: SenioritySignalKind | None = None
    quote: str | None = None
    title_marker: str | None = None
    conflict: str | None = None

    def __post_init__(self) -> None:
        if self.status is SeniorityAssessmentStatus.CLEAR:
            if any((self.kind, self.quote, self.title_marker, self.conflict)):
                raise ValueError("A clear seniority assessment cannot contain evidence")
            return
        if self.kind is None or not self.quote:
            raise ValueError("A non-clear seniority assessment requires a kind and quote")
        if self.status is SeniorityAssessmentStatus.BLOCKED and self.conflict:
            raise ValueError("A blocked seniority assessment cannot contain a conflict")
        if self.status is SeniorityAssessmentStatus.BLOCKED:
            if self.kind is SenioritySignalKind.AMBIGUOUS_TITLE:
                raise ValueError("An ambiguous title cannot establish a blocker")
            if self.kind is SenioritySignalKind.EXPLICIT_TITLE and not self.title_marker:
                raise ValueError("An explicit title blocker requires its matched marker")
        if self.status is SeniorityAssessmentStatus.UNRESOLVED and (
            self.kind is not SenioritySignalKind.AMBIGUOUS_TITLE or not self.conflict
        ):
            raise ValueError("An unresolved assessment requires an ambiguous-title conflict")

    @property
    def blocked(self) -> bool:
        """Whether the text establishes a seniority blocker."""

        return self.status is SeniorityAssessmentStatus.BLOCKED


_TITLE_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("senior", re.compile(r"(?<![a-z0-9])senior(?![a-z0-9])")),
    ("sr", re.compile(r"(?<![a-z0-9])sr\.?(?![a-z0-9])")),
    ("lead", re.compile(r"(?<![a-z0-9])lead(?![a-z0-9])")),
    ("leader", re.compile(r"(?<![a-z0-9])(?:techlead|leader)(?![a-z0-9])")),
    ("staff", re.compile(r"(?<![a-z0-9])staff(?![a-z0-9])")),
    ("principal", re.compile(r"(?<![a-z0-9])principal(?:e)?(?![a-z0-9])")),
    ("head of", re.compile(r"(?<![a-z0-9])head\s+of(?![a-z0-9])")),
    (
        "manager",
        re.compile(
            r"\b(?:data\s+science|analytics|machine\s+learning|ml|ai|ia|"
            r"engineering|tech)\s+manager\b"
            r"|\bmanager[\s,:-]+(?:data(?:\s+science)?|analytics|"
            r"machine\s+learning|ml|ai|ia|technique)\b"
        ),
    ),
    ("director", re.compile(r"\b(?:director|directeur|directrice)\b")),
    ("responsable", re.compile(r"(?<![a-z0-9])responsable(?![a-z0-9])")),
)
_NON_SENIOR_LEAD_RE = re.compile(
    r"\b(?:lead\s+generation|lead\s+gen|generation\s+de\s+leads?|"
    r"lead\s+scoring|lead\s+nurturing|sales\s+leads?)\b"
)
_JUNIOR_CONFIRMED_RE = re.compile(r"\b(?:junior|debutant(?:e)?)\s*[-/]\s*confirme(?:e)?\b")
_HIDDEN_SENIOR_ROLE_RE = re.compile(
    r"\b(?:"
    r"(?:senior|staff|principal)\s+(?:data\s+scientist|data\s+science|"
    r"analytics|machine\s+learning|ml|ai|ia)\s+(?:role|position|job|poste|profil)"
    r"|(?:senior|staff|principal)\s+(?:role|position|poste)\s+"
    r"(?:in|en)\s+(?:data|analytics|machine\s+learning|ml|ai|ia)"
    r"|(?:this|the)\s+(?:role|position|job)\s+(?:is|will\s+be)\s+(?:a\s+)?"
    r"(?:senior|staff|principal)\s+(?:data|analytics|machine\s+learning|ml|ai|ia)"
    r"|(?:poste|role|position|profil)\s+(?:de\s+)?(?:niveau\s+)?"
    r"(?:senior|staff|principal)\s+(?:en\s+)?(?:data|analytics|ml|ai|ia)"
    r"|(?:nous\s+recherchons|nous\s+recrutons|we\s+are\s+(?:looking\s+for|hiring))"
    r"\s+(?:un|une|a|an)?\s*(?:senior|staff|principal)\s+"
    r"(?:data|analytics|machine\s+learning|ml|ai|ia)"
    r"|as\s+a\s+(?:senior|staff|principal)\s+(?:data\s+scientist|data\s+engineer|"
    r"machine\s+learning\s+engineer|ai\s+engineer)"
    r"|(?:recherchons|recrutons|looking\s+for|hiring).{0,45}"
    r"(?:data\s+engineer|data\s+scientist).{0,20}senior"
    r")\b"
)
_MANAGEMENT_RE = re.compile(
    r"\bvous\s+(?:managez|managerez|encadrez|encadrerez|dirigez|dirigerez|"
    r"supervisez|superviserez|pilotez|piloterez)\s+(?:une|l[' ]?)\s*equipe\b"
    r"|\byou\s+(?:will\s+)?(?:manage|lead|supervise|direct)\s+"
    r"(?:a|an|the|our)?\s*(?:data\s+|analytics\s+|ml\s+|ai\s+)?team\b"
    r"|\b(?:responsable\s+d[' ]?une\s+equipe|management\s+d[' ]?equipe|"
    r"encadrement\s+d[' ]?(?:une\s+)?equipe|responsabilite\s+hierarchique|"
    r"responsabilite\s+manageriale|vous\s+serez\s+responsable\s+"
    r"d[' ]?(?:une\s+)?equipe)\b"
    r"|\b(?:responsible\s+for\s+(?:managing|leading|supervising)\s+"
    r"(?:a|an|the)?\s*team|line\s+management\s+responsibilit(?:y|ies)|"
    r"people\s+management\s+(?:responsibilit(?:y|ies)|experience)|"
    r"responsible\s+for\s+(?:the\s+)?(?:management|leadership|supervision)\s+"
    r"of\s+(?:a|an|the)?\s*team|"
    r"\d{1,2}\s+direct\s+reports?)\b"
    r"|\b(?:encadrer|manager|superviser|diriger)\s+"
    r"un(?:\(e\)|e)?\s+"
    r"(?:data\s+scientists?|data\s+engineers?|ingenieurs?|consultants?)\b"
)
_NEGATION_RE = re.compile(
    r"\b(?:no|not|without|ne\s+\w+(?:\s+\w+){0,4}\s+pas|sans|aucun(?:e)?|"
    r"n[' ]?\w+(?:\s+\w+){0,4}\s+pas|does\s+not|won[' ]?t|will\s+not)\b"
)
_FUTURE_OPTION_RE = re.compile(
    r"\b(?:a\s+terme|dans\s+le\s+futur|evolution\s+(?:possible|rapide|vers)|"
    r"possibilites?\s+(?:de|d[' ]|s[' ]?offr)|opportunites?\s+(?:de|d[' ])|"
    r"s[' ]?offriront|pourr(?:a|ez|ait)|"
    r"eventually|in\s+the\s+future|in\s+time|later|soon|"
    r"may|might|could|"
    r"potential(?:ly)?|evolve\s+towards?|path\s+to|grow\s+into|"
    r"opportunity\s+to|possibility\s+to)\b"
)
_NON_MANAGERIAL_RESPONSABLE_RE = re.compile(
    r"\b(?:jeu|ia|ai|intelligence\s+artificielle|numerique|innovation)\s+responsable\b"
    r"|\badjoint(?:e)?\b.{0,45}\bresponsable\b"
    r"|\bresponsable\s+d[' ]?(?:une\s+)?experimentation\b"
)
_MULTI_LEVEL_TITLE_RE = re.compile(
    r"\b(?:junior|debutant(?:e)?)\b.{0,35}"
    r"(?:/|\bou\b|\bor\b|\bto\b|\ba\b).{0,35}"
    r"\b(?:senior|confirme(?:e)?|lead)\b"
    r"|\b(?:multiple|all|tous|plusieurs)\s+(?:seniority\s+)?(?:levels?|niveaux?)\b"
)
_RESPONSABLE_TITLE_RE = re.compile(r"(?<![a-z0-9])responsable(?![a-z0-9])")
_HIERARCHICAL_RESPONSABLE_TITLE_RE = re.compile(
    r"\bresponsable\s+(?:de\s+|d[' ]|du\s+|des\s+|of\s+)?"
    r"(?:l[' ]|la\s+|le\s+|une?\s+|the\s+)?"
    r"(?:equipe|team|departement|department|pole|service|direction)\b"
)
_CLAUSE_SPLIT_RE = re.compile(r"(?:\r?\n|[;.!?•]+)\s*")
_OPTIONAL_MANAGEMENT_HEADER_RE = re.compile(
    r"^\s*(?:nice[- ]to[- ]have|preferred\s+qualifications?|bonus(?:\s+points?)?|"
    r"atouts?\s+(?:apprecies?|souhaites?))\s*:?\s*$"
)
_MANAGEMENT_SECTION_RESET_RE = re.compile(
    r"^\s*(?:requirements?|required\s+qualifications?|responsibilities|"
    r"your\s+responsibilities|missions?|vos\s+missions|responsabilites?|"
    r"profil\s+recherche|prerequis)\s*:?\s*$"
)
_OPTIONAL_MANAGEMENT_RE = re.compile(
    r"\b(?:nice[- ]to[- ]have|preferred|desirable|optional|bonus|"
    r"souhaite(?:e|es|s)?|apprecie(?:e|es|s)?|facultatif|un\s+plus)\b"
)
_SUBORDINATE_MANAGEMENT_BEFORE_RE = re.compile(r"\b(?:sous|under)\s+(?:(?:la|the)\s+)?$")
_THIRD_PARTY_MANAGEMENT_BEFORE_RE = re.compile(
    r"\b(?:par|by|avec|with|aupres\s+de)\s+"
    r"(?:(?:un(?:e)?|an?|le|la|the|votre|your|notre|our)\s+)?"
    r"[a-z][a-z'/-]*(?:\s+[a-z][a-z'/-]*){0,5}\s*[,:(-]\s*$"
    r"|\b(?:votre|your|notre|our)\s+[a-z][a-z'/-]*"
    r"(?:\s+[a-z][a-z'/-]*){0,4}\s*[,:(-]\s*$"
)


def assess_seniority(
    title: str,
    description: str,
    configured_title_markers: tuple[str, ...] = (),
) -> SeniorityAssessment:
    """Assess explicit seniority and current line-management requirements."""

    normalized_title = norm(title)
    normalized_description = norm(description)
    title_conflict = _title_ambiguity(normalized_title)
    title_marker = (
        None if title_conflict else _title_marker(normalized_title, configured_title_markers)
    )
    if title_marker:
        return SeniorityAssessment(
            status=SeniorityAssessmentStatus.BLOCKED,
            kind=SenioritySignalKind.EXPLICIT_TITLE,
            quote=normalized_title,
            title_marker=title_marker,
        )

    management_quote = _current_management_responsibility(normalized_description)
    if management_quote:
        return SeniorityAssessment(
            status=SeniorityAssessmentStatus.BLOCKED,
            kind=SenioritySignalKind.HIERARCHICAL_MANAGEMENT,
            quote=management_quote,
        )

    hidden_quote = _hidden_senior_role(normalized_description)
    if hidden_quote:
        return SeniorityAssessment(
            status=SeniorityAssessmentStatus.BLOCKED,
            kind=SenioritySignalKind.HIDDEN_ROLE,
            quote=hidden_quote,
        )

    if title_conflict:
        return SeniorityAssessment(
            status=SeniorityAssessmentStatus.UNRESOLVED,
            kind=SenioritySignalKind.AMBIGUOUS_TITLE,
            quote=normalized_title,
            conflict=title_conflict,
        )

    return SeniorityAssessment(status=SeniorityAssessmentStatus.CLEAR)


def has_candidate_leadership_responsibility(description: str) -> bool:
    """Compatibility helper for callers that only need a boolean signal."""

    return _current_management_responsibility(norm(description)) is not None


def has_hidden_senior_role(description: str) -> bool:
    """Compatibility helper for callers that only need a boolean signal."""

    return _hidden_senior_role(norm(description)) is not None


def title_seniority_or_management_marker(
    title: str,
    configured_markers: tuple[str, ...],
) -> str | None:
    """Compatibility helper returning the first explicit title marker."""

    return _title_marker(norm(title), configured_markers)


def _title_ambiguity(normalized_title: str) -> str | None:
    if _MULTI_LEVEL_TITLE_RE.search(normalized_title):
        return "multiple_seniority_levels_in_title"
    without_non_managerial = _NON_MANAGERIAL_RESPONSABLE_RE.sub(" ", normalized_title)
    if _RESPONSABLE_TITLE_RE.search(
        without_non_managerial
    ) and not _HIERARCHICAL_RESPONSABLE_TITLE_RE.search(without_non_managerial):
        return "responsable_title_without_hierarchy"
    return None


def _title_marker(normalized_title: str, configured_markers: tuple[str, ...]) -> str | None:
    for marker, pattern in _TITLE_MARKERS:
        candidate_text = normalized_title
        if marker == "lead":
            candidate_text = _NON_SENIOR_LEAD_RE.sub(" ", candidate_text)
        if marker == "responsable" and _NON_MANAGERIAL_RESPONSABLE_RE.search(normalized_title):
            candidate_text = _NON_MANAGERIAL_RESPONSABLE_RE.sub(" ", candidate_text)
        if pattern.search(candidate_text):
            return marker

    for configured in configured_markers:
        marker = norm(configured).strip()
        candidate_text = (
            _NON_SENIOR_LEAD_RE.sub(" ", normalized_title) if marker == "lead" else normalized_title
        )
        if marker and re.search(
            rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])",
            candidate_text,
        ):
            return marker
    return None


def _hidden_senior_role(normalized_description: str) -> str | None:
    for clause in _clauses(normalized_description):
        match = _HIDDEN_SENIOR_ROLE_RE.search(clause)
        if not match:
            continue
        local = clause[max(0, match.start() - 70) : min(len(clause), match.end() + 70)]
        if not (
            _NEGATION_RE.search(local)
            or _FUTURE_OPTION_RE.search(local)
            or _JUNIOR_CONFIRMED_RE.search(local)
        ):
            return clause
    return None


def _current_management_responsibility(normalized_description: str) -> str | None:
    clauses = _clauses(normalized_description)
    optional_section = False
    for index, clause in enumerate(clauses):
        if _OPTIONAL_MANAGEMENT_HEADER_RE.fullmatch(clause):
            optional_section = True
            continue
        if _MANAGEMENT_SECTION_RESET_RE.fullmatch(clause):
            optional_section = False
            continue
        matches = [
            match
            for match in _MANAGEMENT_RE.finditer(clause)
            if not _management_belongs_to_someone_else(clause, match)
        ]
        if not matches:
            continue
        match = matches[0]
        local = clause[max(0, match.start() - 70) : min(len(clause), match.end() + 70)]
        prefix = clause[max(0, match.start() - 70) : match.start()]
        suffix = clause[match.end() : match.end() + 70]
        zero_reports = re.fullmatch(r"0\s+direct\s+reports?", match.group(0)) is not None
        negative_value = (
            re.match(
                r"[\s):=-]*(?:non(?!\s+seulement)|no(?!\s+less)|none|false)\b",
                suffix,
            )
            is not None
        )
        optional_suffix = (
            re.match(
                r"\s*(?:(?:experience|skills?|is|are|est|serait|would\s+be)\s+)*"
                r"(?:preferred|desirable|optional|souhaite(?:e)?|apprecie(?:e)?|"
                r"(?:a\s+)?plus|un\s+plus)\b",
                suffix,
            )
            is not None
        )
        preceding_heading = clauses[index - 1] if index else ""
        decision_context = f"{preceding_heading} {local}"
        if (
            optional_section
            or zero_reports
            or _OPTIONAL_MANAGEMENT_RE.search(prefix)
            or optional_suffix
            or negative_value
            or _NEGATION_RE.search(local)
            or _FUTURE_OPTION_RE.search(decision_context)
        ):
            continue
        return clause
    return None


def _management_belongs_to_someone_else(clause: str, match: re.Match[str]) -> bool:
    """Distinguish reporting to a manager from being that manager."""

    if re.match(r"(?:vous|you)\b", match.group(0)):
        return False
    prefix = clause[max(0, match.start() - 100) : match.start()]
    return bool(
        _SUBORDINATE_MANAGEMENT_BEFORE_RE.search(prefix)
        or _THIRD_PARTY_MANAGEMENT_BEFORE_RE.search(prefix)
    )


def _clauses(normalized_text: str) -> list[str]:
    return [part.strip(" :-\t") for part in _CLAUSE_SPLIT_RE.split(normalized_text) if part.strip()]
