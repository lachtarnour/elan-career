"""Parse and assess candidate experience requirements from a job offer.

This module emits no rejection disposition or ranking score.  It extracts the
same source-backed assessment for every caller.  Optional wishes, company
history, project durations, and conflicts remain explicit so downstream stages
can apply their own policy without reparsing the offer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from smartapply.filtering.facts import FilterFacts
from smartapply.filtering.text import norm

__all__ = [
    "ExperienceAssessment",
    "ExperienceRequirement",
    "assess_candidate_experience",
]

_WRITTEN_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "un": 1,
    "une": 1,
    "deux": 2,
    "trois": 3,
    "quatre": 4,
    "cinq": 5,
    "sept": 7,
    "huit": 8,
    "neuf": 9,
    "dix": 10,
    "onze": 11,
    "douze": 12,
    "treize": 13,
    "quatorze": 14,
    "quinze": 15,
    "seize": 16,
    "dix-sept": 17,
    "dix-huit": 18,
    "dix-neuf": 19,
    "vingt": 20,
}
_WRITTEN_NUMBER_PATTERN = "|".join(
    re.escape(value).replace(r"\-", r"(?:-|\s+)")
    for value in sorted(_WRITTEN_NUMBERS, key=len, reverse=True)
)
_NUMBER = r"(?:\d{1,3}(?:[.,]\d+)?|" + _WRITTEN_NUMBER_PATTERN + ")"
_YEAR_UNIT = r"(?:an(?:s|\(s\))?|annees?|year(?:s|\(s\))?|yrs?)"
_MONTH_UNIT = r"(?:mois|months?)"

_BETWEEN_YEARS_RE = re.compile(
    rf"\b(?:entre|between)\s+(?P<low>{_NUMBER})\s+"
    rf"(?:et|and)\s+(?P<high>{_NUMBER})\s*(?P<unit>{_YEAR_UNIT})\b"
)
_YEAR_RANGE_WITH_UNITS_RE = re.compile(
    rf"\b(?P<low>{_NUMBER})\s*(?P<low_unit>{_YEAR_UNIT})\b"
    rf"(?:\s+(?:minimum|min\.?|maximum|max\.?))?\s*"
    rf"(?:-|–|—|/|a|to)\s*"
    rf"(?P<high>{_NUMBER})\s*(?P<unit>{_YEAR_UNIT})\b"
)
_YEAR_RANGE_RE = re.compile(
    rf"\b(?P<low>{_NUMBER})\s*(?:-|–|—|/|a|to)\s*"
    rf"(?P<high>{_NUMBER})\s*(?P<unit>{_YEAR_UNIT})\b"
)
_SINGLE_YEAR_RE = re.compile(rf"\b(?P<low>{_NUMBER})\s*(?P<plus>\+)?\s*(?P<unit>{_YEAR_UNIT})\b")
_BETWEEN_MONTHS_RE = re.compile(
    rf"\b(?:entre|between)\s+(?P<low>{_NUMBER})\s+"
    rf"(?:et|and)\s+(?P<high>{_NUMBER})\s*(?P<unit>{_MONTH_UNIT})\b"
)
_MONTH_RANGE_RE = re.compile(
    rf"\b(?P<low>{_NUMBER})\s*(?:-|–|—|a|to)\s*"
    rf"(?P<high>{_NUMBER})\s*(?P<unit>{_MONTH_UNIT})\b"
)
_SINGLE_MONTH_RE = re.compile(rf"\b(?P<low>{_NUMBER})\s*(?P<plus>\+)?\s*(?P<unit>{_MONTH_UNIT})\b")

_EXPERIENCE_RE = re.compile(r"\b(?:experiences?|exp|anciennete)(?:\s+professionnelles?)?\b")
_REQUIRED_RE = re.compile(
    r"\b(?:minimum|min\.?|minimal(?:e)?|a\s+minima|au\s+moins|at\s+least|"
    r"more\s+than|required|requis(?:e|es|s)?|"
    r"exige(?:e|es|s)?|obligatoire|imperatif|necessaire|must|doit|doivent|"
    r"devrez|justifiez|justifier|require[sd]?|need(?:ed|s)?|requiert|"
    r"demande(?:e|es|s)?)\b"
)
_STRICT_REQUIRED_RE = re.compile(
    r"\b(?:required|requis(?:e|es|s)?|exige(?:e|es|s)?|obligatoire|"
    r"imperatif|necessaire|must|doit|doivent|devrez|justifiez|justifier|"
    r"require[sd]?|need(?:ed|s)?|requiert|demande(?:e|es|s)?)\b"
)
_OPTIONAL_RE = re.compile(
    r"\b(?:idealement|ideally|souhaite(?:e|es|s)?|souhaitable|"
    r"apprecie(?:e|es|s)?|prefer(?:red|ably)|desirable|desired|optional|"
    r"nice\s+to\s+have|bonus|un\s+plus|serait\s+un\s+plus|"
    r"preference|around|about|approximately|circa|environ|"
    r"would\s+be\s+(?:a\s+plus|preferred))\b"
)
_OPTIONAL_REQUIREMENT_AFTER_RE = re.compile(
    r"^(?:\s+(?:d[' ]?|of\s+)?(?:professional\s+)?experiences?)?"
    r"[\s,:;-]*(?:(?:minimum|min\.?|au\s+moins|at\s+least)[\s,:;-]*)?"
    r"(?:(?:is|are|est|sont|serait|seraient|would\s+be)[\s,:;-]*)?"
    rf"(?:{_OPTIONAL_RE.pattern}|\b(?:a\s+plus|un\s+atout)\b)"
)
_IDEAL_EXPERIENCE_BEFORE_RE = re.compile(r"\bideal(?:e)?\s*$")
_SALARY_BAND_BEFORE_RE = re.compile(
    r"\b\d{1,3}(?:[.,]\d+)?\s*(?:k\s*(?:eur|euros?|€)?|euros?|eur|€|usd|\$)"
    r".{0,80}\b(?:for|pour)\s+(?:(?:an?|un(?:e)?)\s+)?"
    r"(?:profil|profile|candidate)\b"
)
_CANDIDATE_RE = re.compile(
    r"\b(?:vous|tu|candidat(?:e|es|s)?|profil|applicants?|candidates?|you|"
    r"ideal\s+candidate|person|role|poste|position|job)\b"
)
_NO_EXPERIENCE_REQUIRED_RE = re.compile(
    r"\b(?:debutant(?:e)?s?\s+accepte(?:e)?s?|sans\s+experience|"
    r"aucune\s+experience|experience\s+non\s+exigee?|"
    r"no\s+experience\s+(?:is\s+)?required|entry\s+level)\b"
)
_COMPANY_HISTORY_RE = re.compile(
    r"\b(?:notre|our)\s+(?:entreprise|societe|cabinet|groupe|organisation|"
    r"organization|company|firm|business|team)\s+"
    r"(?:a|has|have|brings?|cumule)\b"
    r"|\b(?:entreprise|societe|cabinet|groupe|organisation|organization|"
    r"company|firm|business|team)\s+(?:avec|with|forte?\s+de|has|having|brings?)\b"
    r"|\b(?:we\s+(?:have|bring)|nous\s+(?:avons|cumulons))\b"
    r"|\b(?:we[' ]?ve|we\s+have)\s+(?:worked|operated|served|built)\b"
    r"|\bfor\s+more\s+than\s+\d{1,3}\s+years?\b.{0,80}"
    r"\b(?:has|have|we[' ]?ve)\b"
    r"|\b(?:builds?|rests?)\s+on\s+(?:more\s+than\s+)?\d{1,3}\s+years?\b"
    r"|\bsupported\s+by\s+\d{1,3}\s*\+?\s+years?\s+of\s+experience\b"
    r"|\b(?:avec\s+(?:plus\s+de\s+)?|with\s+(?:more\s+than\s+)?)"
    r"\d{1,3}\s+(?:ans?|years?)\s+(?:d[' ]|of\s+)?experience\b.{0,100}"
    r"\b(?:employe(?:e)?s?|employees?|collaborateurs?)\b"
    r"|\b(?:fondee?|cree(?:e)?|founded|established)\b"
    r"|\bforte?s?\s+de\b"
    r"|\b(?:depuis|since)\s+\d"
    r"|\b(?:notre|our)\s+savoir[- ]faire\b"
    r"|\bcompany\s+history\b"
)
_NON_CANDIDATE_DURATION_RE = re.compile(
    r"\b(?:roadmap|feuille\s+de\s+route|strategie|strategy|plan|initiative|"
    r"partenariat|partnership|financement|funding|duree\s+de\s+vie|"
    r"lifetime|lifespan)\b"
    r"|\b(?:operation|fonctionnement|exploitation|service)\b.{0,90}"
    r"\b(?:pendant|for)\b"
)
_EDUCATION_DURATION_AFTER_RE = re.compile(
    r"^\s*(?:post[- ]?baccalaureate|post[- ]?bac|"
    r"after\s+(?:the\s+)?baccalaureate)\b"
)
_TENURE_OR_MILESTONE_RE = re.compile(
    r"\b(?:prime\s+d[' ]?anciennete|anciennete\s+(?:ouvre|donne|permet)|"
    r"at\s+\d+\s+months?|a\s+\d+\s+mois|"
    r"ce\s+a\s+quoi\s+ressemble\s+la\s+reussite|success\s+milestones?)\b"
)
_PROJECT_DURATION_RE = re.compile(
    r"\b(?:projet|project|mission|programme|program|contrat|contract)\b"
    r".{0,60}\b(?:duree|duration)\b"
    r"|\b(?:duree|duration)\b.{0,60}"
    r"\b(?:projet|project|mission|programme|program|contrat|contract)\b"
    rf"|\b(?:projet|project|mission|programme|program|contrat|contract)\b"
    rf".{{0,35}}\b{_NUMBER}\s*\+?\s*(?:{_YEAR_UNIT}|{_MONTH_UNIT})\b"
    rf"|\b{_NUMBER}\s*\+?\s*(?:{_YEAR_UNIT}|{_MONTH_UNIT})\b.{{0,25}}"
    r"\b(?:projet|project|mission|programme|program|contrat|contract)\b"
)
_AGE_RE = re.compile(r"\b(?:age|agee?|moins\s+de|younger\s+than)\b")
_CLAUSE_SPLIT_RE = re.compile(r"(?:\r?\n\s*\r?\n|[;.!?•]+)\s*")
_DIRECT_CANDIDATE_YEARS_RE = re.compile(
    r"\b(?:vous\s+(?:avez|justifiez|disposez|possedez)|"
    r"you\s+(?:have|bring|possess)|candidat(?:e)?\s+avec)\b"
)
_EXPERIENCE_POSSESSOR_BEFORE_RE = re.compile(
    r"\b(?P<subject>(?:[a-z][a-z'/-]*\s+){0,7}[a-z][a-z'/-]*)\s+"
    r"(?:has|have|had|brings?|possesses?|a|avez|avait|possede(?:z|nt)?|"
    r"dispose(?:z|nt)?\s+de|cumule(?:z|nt)?)\s+"
    r"(?:(?:more\s+than|over|at\s+least|plus\s+de|au\s+moins|environ)\s+)?$"
)
_CANDIDATE_POSSESSOR_RE = re.compile(
    r"\b(?:you|vous|tu|candidat(?:e|es|s)?|candidates?|applicants?|profil)"
    r"(?:\s+(?:must|should|will|can|who|need\s+to|(?:is|are)\s+required\s+to))?$"
)
_UPPER_BOUND_BEFORE_RE = re.compile(
    r"\b(?:up\s+to|no\s+more\s+than|not\s+more\s+than|at\s+most|"
    r"maximum|max\.?|jusqu[' ]?a|pas\s+plus\s+de|au\s+maximum)\s*$"
)
_UPPER_BOUND_AFTER_RE = re.compile(
    r"^\s*(?:(?:d[' ]?|of\s+)?(?:professional\s+)?experiences?\s+)?"
    r"(?:maximum|max\.?|au\s+maximum)\b"
)
_NEGATED_REQUIREMENT_BEFORE_RE = re.compile(
    r"\b(?:(?:do|does|did)\s+not\s+(?:need|require)|"
    r"(?:need|require)\s+not|"
    r"n[' ]\w+(?:\s+\w+){0,3}\s+pas\s+besoin\s+de|"
    r"ne\s+\w+(?:\s+\w+){0,3}\s+pas\s+besoin\s+de)"
    r"(?:\s+(?:at\s+least|a\s+minimum\s+of|au\s+moins))?\s*$"
)
_NEGATED_REQUIREMENT_AFTER_RE = re.compile(
    r"^\s*(?:(?:d[' ]?|of\s+)?(?:professional\s+)?experiences?\s+)?"
    r"(?:(?:is|are|would\s+be)\s+)?not\s+"
    r"(?:required|mandatory|necessary|essential|compulsory)\b"
    r"|^\s*(?:d[' ]?experience\s+)?(?:ne\s+sont|n[' ]est)\s+pas\s+"
    r"(?:requis(?:e|es|s)?|exige(?:e|es|s)?|necessaire|obligatoire)\b"
)
_MULTIPLE_LEVELS_RE = re.compile(
    r"\b(?:plusieurs\s+niveaux\s+de\s+seniorite|"
    r"multiple\s+seniority\s+levels?|roles?\s+at\s+multiple\s+levels?)\b"
)
_STRUCTURED_EXPERIENCE_ECHO_RE = re.compile(
    r"^\s*(?:experience\s+demandee|required\s+experience|experience\s+required)\s*:"
)
_EXPERIENCE_HEADER_RE = re.compile(r"\bexperience\s*:\s*\d")
_NUMERIC_EXPERIENCE_BANNER_RE = re.compile(
    rf"(?:^|\n)\s*experience\s*:\s*(?:[>≥]\s*)?{_NUMBER}\s*{_YEAR_UNIT}\b"
)
_FIRST_EXPERIENCE_PROFILE_RE = re.compile(
    r"\b(?:tu\s+as|vous\s+(?:avez|justifiez|disposez(?:\s+de)?)|"
    r"you\s+(?:have|bring))\s+(?:une?\s+|an?\s+)?"
    r"(?:premiere?\s+experience|first\s+(?:significant\s+)?experience)\b"
)
_LEVEL_DURATION_RE = re.compile(
    rf"(?:^|[;,])\s*[-*•]?\s*(?P<level>juniors?|debutant(?:e)?s?|seniors?|managers?)\b"
    rf"[\s(:=-]*(?P<low>{_NUMBER})\s*"
    rf"(?:\+|(?:[-–—/]|a|to)\s*{_NUMBER})?\s*{_YEAR_UNIT}\b",
    re.MULTILINE,
)
_REQUIRED_EXPERIENCE_SECTION = (
    r"requirements?|candidate(?:/profile)?\s+requirements?|"
    r"minimum\s+qualifications?|required\s+qualifications?|your\s+profile|"
    r"profil(?:\s+recherche)?|prerequis|exigences?"
)
_OPTIONAL_EXPERIENCE_SECTION = (
    r"nice[- ]to[- ]have|preferred\s+qualifications?|bonus(?:\s+points?)?|"
    r"atouts?\s+(?:apprecies?|souhaites?)"
)
_NEUTRAL_EXPERIENCE_SECTION = (
    r"responsibilities|your\s+responsibilities|missions?|vos\s+missions|"
    r"responsabilites?|about\s+us|our\s+company|notre\s+entreprise"
)
_EXPERIENCE_SECTION_SPLIT_RE = re.compile(
    rf"(?=^[ \t]*(?:{_REQUIRED_EXPERIENCE_SECTION}|{_OPTIONAL_EXPERIENCE_SECTION}|"
    rf"{_NEUTRAL_EXPERIENCE_SECTION})[ \t]*(?::|$))",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ExperienceRequirement:
    """One visible or structured minimum, stored without lossy rounding."""

    months: float
    required: bool
    source: str
    quote: str
    field: str

    @property
    def years(self) -> float:
        return self.months / 12


@dataclass(frozen=True)
class ExperienceAssessment:
    """Neutral interpretation of candidate experience evidence."""

    visible: tuple[ExperienceRequirement, ...]
    structured: ExperienceRequirement | None
    structured_note: str | None
    conflict: str | None
    mandatory: tuple[ExperienceRequirement, ...]

    @property
    def resolved_mandatory_years(self) -> tuple[float, ...]:
        """Return requirements safe to consume as numeric ranking signals."""

        if self.conflict:
            return ()
        return tuple(sorted(requirement.years for requirement in self.mandatory))


@dataclass(frozen=True)
class _ExperienceContext:
    title: str
    description: str
    facts: FilterFacts
    max_required_years: float


def assess_candidate_experience(
    *,
    title: str,
    description: str,
    facts: FilterFacts,
    max_required_years: float,
) -> ExperienceAssessment:
    """Build one reusable assessment from visible and structured evidence.

    ``max_required_years`` only determines whether a structured/visible
    disagreement crosses the caller's decision boundary.  The returned object
    itself contains no rejection disposition or score.
    """

    context = _ExperienceContext(
        title=norm(title),
        description=norm(description),
        facts=facts,
        max_required_years=max_required_years,
    )
    visible = tuple(_visible_requirements(context))
    structured = _structured_requirement(context)
    structured_note = _structured_diagnostic(context)
    conflict = _find_visible_path_conflict(context, visible) or _find_conflict(
        context,
        structured=structured,
        visible=list(visible),
    )
    mandatory = tuple(item for item in visible if item.required)
    if structured is not None and structured.required:
        mandatory = (*mandatory, structured)
    return ExperienceAssessment(
        visible=visible,
        structured=structured,
        structured_note=structured_note,
        conflict=conflict,
        mandatory=mandatory,
    )


def _visible_requirements(context: _ExperienceContext) -> list[ExperienceRequirement]:
    requirements: list[ExperienceRequirement] = []
    requirements.extend(_extract_visible(context.title, source="visible_title", field="title"))
    requirements.extend(
        _extract_visible(context.description, source="visible_description", field="description")
    )
    return [
        requirement
        for requirement in requirements
        if not _is_structured_experience_echo(context, requirement)
    ]


def _extract_visible(
    text: str,
    *,
    source: str,
    field: str,
) -> list[ExperienceRequirement]:
    requirements: list[ExperienceRequirement] = []
    for raw_clause, required_section, optional_section in _sectioned_clauses(text):
        clause = raw_clause.strip(" :-\t")
        if not clause:
            continue

        spans: list[tuple[int, int]] = []
        clause_requirements: list[tuple[tuple[int, int], ExperienceRequirement]] = []
        patterns = (
            (_BETWEEN_YEARS_RE, 12.0),
            (_YEAR_RANGE_WITH_UNITS_RE, 12.0),
            (_YEAR_RANGE_RE, 12.0),
            (_BETWEEN_MONTHS_RE, 1.0),
            (_MONTH_RANGE_RE, 1.0),
            (_SINGLE_YEAR_RE, 12.0),
            (_SINGLE_MONTH_RE, 1.0),
        )
        for pattern, multiplier in patterns:
            for match in pattern.finditer(clause):
                if any(_overlaps(match.span(), span) for span in spans):
                    continue
                spans.append(match.span())
                amount = _number(match.group("low"))
                if amount < 0:
                    continue
                if not _is_experience_requirement(clause, match, required_section=required_section):
                    continue
                clause_requirements.append(
                    (
                        match.span(),
                        ExperienceRequirement(
                            months=amount * multiplier,
                            required=not (optional_section or _is_optional(clause, match)),
                            source=source,
                            quote=clause,
                            field=field,
                        ),
                    )
                )
        clause_requirements.sort(key=lambda item: item[0])
        if _MULTIPLE_LEVELS_RE.search(clause) and clause_requirements:
            requirements.append(min(clause_requirements, key=lambda item: item[1].months)[1])
            continue
        for index, (_, requirement) in enumerate(clause_requirements):
            if _is_stricter_alternative(clause, clause_requirements, index):
                continue
            requirements.append(requirement)
    return requirements


def _sectioned_clauses(text: str) -> list[tuple[str, bool, bool]]:
    """Carry a qualifications header without splitting soft line wrapping."""

    clauses: list[tuple[str, bool, bool]] = []
    required_section = False
    optional_section = False
    for section in _EXPERIENCE_SECTION_SPLIT_RE.split(norm(text)):
        if not section.strip():
            continue
        first_line = section.strip().splitlines()[0]
        header = first_line.partition(":")[0].strip()
        if re.fullmatch(_REQUIRED_EXPERIENCE_SECTION, header):
            required_section, optional_section = True, False
        elif re.fullmatch(_OPTIONAL_EXPERIENCE_SECTION, header):
            required_section, optional_section = False, True
        elif re.fullmatch(_NEUTRAL_EXPERIENCE_SECTION, header):
            required_section, optional_section = False, False
        clauses.extend(
            (clause, required_section, optional_section)
            for clause in _CLAUSE_SPLIT_RE.split(section)
        )
    return clauses


def _is_stricter_alternative(
    clause: str,
    requirements: list[tuple[tuple[int, int], ExperienceRequirement]],
    index: int,
) -> bool:
    """Ignore the stricter side of a nearby ``or/ou`` experience alternative.

    The lower admissible path governs whether the candidate is excluded,
    regardless of whether the offer writes the lower or the higher number
    first.  Requirements joined by another relation remain independent.
    """

    current_span, current = requirements[index]
    neighbours: list[tuple[tuple[int, int], ExperienceRequirement]] = []
    if index:
        neighbours.append(requirements[index - 1])
    if index + 1 < len(requirements):
        neighbours.append(requirements[index + 1])

    for neighbour_span, neighbour in neighbours:
        if current.months <= neighbour.months:
            continue
        left = min(current_span[1], neighbour_span[1])
        right = max(current_span[0], neighbour_span[0])
        between = clause[left:right]
        if len(between) <= 140 and re.search(r"\b(?:or|ou)\b", between):
            return True
    return False


def _is_experience_requirement(
    clause: str, match: re.Match[str], *, required_section: bool = False
) -> bool:
    after_duration = clause[match.end() : min(len(clause), match.end() + 45)]
    if _EDUCATION_DURATION_AFTER_RE.search(after_duration):
        return False
    if _AGE_RE.search(clause) and not _EXPERIENCE_RE.search(clause):
        return False
    if _PROJECT_DURATION_RE.search(clause) and not _EXPERIENCE_RE.search(clause):
        return False
    if _NON_CANDIDATE_DURATION_RE.search(clause) and not _EXPERIENCE_RE.search(clause):
        return False
    if _TENURE_OR_MILESTONE_RE.search(clause) and not re.search(
        r"\b(?:experience|experiences|exp)\b", clause
    ):
        return False
    if _is_salary_band_experience(clause, match):
        return False
    # An explicit candidate and hard requirement in the same clause takes
    # precedence over company boilerplate.
    if _has_third_party_possessor(clause, match) or _is_company_history(clause, match):
        return False

    local = clause[max(0, match.start() - 90) : min(len(clause), match.end() + 90)]
    has_experience = _EXPERIENCE_RE.search(local) is not None
    has_required_marker = _REQUIRED_RE.search(local) is not None
    has_optional_marker = _OPTIONAL_RE.search(local) is not None
    has_candidate = required_section or _CANDIDATE_RE.search(local) is not None
    has_plus_or_range = bool(match.groupdict().get("plus")) or "high" in match.groupdict()
    direct_candidate_years = _DIRECT_CANDIDATE_YEARS_RE.search(local) is not None
    required_before = clause[max(0, match.start() - 40) : match.start()]
    required_after = clause[match.end() : min(len(clause), match.end() + 22)]
    tight_required = (
        _REQUIRED_RE.search(required_before) is not None
        or _REQUIRED_RE.search(required_after) is not None
    )
    return (
        (
            has_experience
            and (has_required_marker or has_candidate or has_plus_or_range or has_optional_marker)
        )
        or direct_candidate_years
        or tight_required
    )


def _is_optional(clause: str, match: re.Match[str]) -> bool:
    before = clause[max(0, match.start() - 90) : match.start()]
    after = clause[match.end() : min(len(clause), match.end() + 55)]
    local = f"{before}{match.group(0)}{after}"
    if (
        _UPPER_BOUND_BEFORE_RE.search(before)
        or _UPPER_BOUND_AFTER_RE.search(after)
        or _NEGATED_REQUIREMENT_BEFORE_RE.search(before)
        or _NEGATED_REQUIREMENT_AFTER_RE.search(after)
    ):
        return True
    optional_before = (
        _OPTIONAL_RE.search(before) is not None
        or _IDEAL_EXPERIENCE_BEFORE_RE.search(before) is not None
    )
    optional_after = _OPTIONAL_REQUIREMENT_AFTER_RE.search(after) is not None
    if not (optional_before or optional_after):
        return False
    if optional_before:
        # A preference or approximation placed before the number qualifies
        # that number, even in a clause that contains another hard minimum.
        return True

    # ``5 years minimum desired`` is still a preference: "minimum" describes
    # the preferred amount, while words such as "required" or "must" make the
    # requirement mandatory.  Keep those two concepts separate.
    return _STRICT_REQUIRED_RE.search(local) is None


def _is_structured_experience_echo(
    context: _ExperienceContext,
    requirement: ExperienceRequirement,
) -> bool:
    """Avoid counting a rendered structured field as independent evidence.

    Some sources prepend ``Experience required: N years`` to the visible
    description while exposing the same value as structured metadata.  The
    duplicate line must not outvote a more specific optional or contradictory
    requirement later in the offer.
    """

    years = context.facts.experience_min_years
    if years is None or requirement.field != "description":
        return False
    return bool(
        _STRUCTURED_EXPERIENCE_ECHO_RE.search(norm(requirement.quote))
        and requirement.months == float(years) * 12
    )


def _structured_requirement(
    context: _ExperienceContext,
) -> ExperienceRequirement | None:
    years = context.facts.experience_min_years
    if years is None or years < 0:
        return None
    source = context.facts.experience_source or "structured_metadata"
    requirement = context.facts.experience_requirement or _format_number(years)
    return ExperienceRequirement(
        months=float(years) * 12,
        required=context.facts.experience_required is not False,
        source=source,
        quote=requirement,
        field="experience_min_years",
    )


def _structured_diagnostic(context: _ExperienceContext) -> str | None:
    facts = context.facts
    value: str | None = None
    if facts.experience_min_years is not None:
        value = _format_number(float(facts.experience_min_years))
    elif facts.experience_requirement:
        value = facts.experience_requirement
    if value is None:
        return None

    source = norm(facts.source).replace(" ", "_") or "unknown"
    return f"experience_structured_{source}:{value}"


def _find_conflict(
    context: _ExperienceContext,
    *,
    structured: ExperienceRequirement | None,
    visible: list[ExperienceRequirement],
) -> str | None:
    visible_required = [item for item in visible if item.required]
    visible_optional = [item for item in visible if not item.required]
    visible_says_none = _NO_EXPERIENCE_REQUIRED_RE.search(context.description) is not None

    if context.facts.experience_required is False and visible_required:
        return "structured_optional_but_visible_required"
    if structured is None:
        return None
    if visible_says_none:
        return "structured_required_but_visible_not_required"
    if visible_optional and not visible_required:
        return "structured_required_but_visible_optional"
    if visible_required:
        threshold_months = context.max_required_years * 12
        visible_blocks = any(item.months >= threshold_months for item in visible_required)
        structured_blocks = structured.months >= threshold_months
        if structured_blocks != visible_blocks:
            if not structured_blocks and any(
                item.months >= threshold_months and _rendered_subskill_and_total(structured, item)
                for item in visible_required
            ):
                return None
            visible_min = min(item.months for item in visible_required)
            return (
                f"structured={_format_number(structured.years)}y;"
                f"visible={_format_number(visible_min / 12)}y"
            )
    return None


def _rendered_subskill_and_total(
    structured: ExperienceRequirement,
    visible: ExperienceRequirement,
) -> bool:
    """A normalized first amount need not be the overall experience floor.

    Some source labels say ``4 years ML and 7/8 years overall``. When that
    complete label is rendered verbatim, its explicit total remains mandatory
    rather than contradicting the normalized four-year sub-skill amount.
    """

    if not _STRUCTURED_EXPERIENCE_ECHO_RE.search(visible.quote):
        return False
    first = _SINGLE_YEAR_RE.search(visible.quote)
    if first is None or _number(first.group("low")) * 12 != structured.months:
        return False
    for pattern in (_YEAR_RANGE_RE, _SINGLE_YEAR_RE):
        for match in pattern.finditer(visible.quote):
            if _number(match.group("low")) * 12 != visible.months:
                continue
            after = visible.quote[match.end() : match.end() + 25]
            if re.match(r"\s*(?:au\s+total|en\s+tout|overall|in\s+total)\b", after):
                return True
    return False


def _find_visible_path_conflict(
    context: _ExperienceContext,
    visible: tuple[ExperienceRequirement, ...],
) -> str | None:
    """Retain explicit multi-level ads and conflicting global header floors.

    A junior profile heading is an alternative recruitment path, whereas a
    lower sub-skill introduced by ``dont`` remains cumulative. Likewise a
    top-level experience banner cannot silently be overwritten by the body.
    """

    levels = list(_LEVEL_DURATION_RE.finditer(context.description))
    threshold = context.max_required_years
    if (
        len({item.group("level").rstrip("s") for item in levels}) > 1
        and any(_number(item.group("low")) < threshold for item in levels)
        and any(_number(item.group("low")) >= threshold for item in levels)
    ):
        return "multiple_experience_levels_include_eligible_path"

    required = [item for item in visible if item.required]
    high_requirements = [item for item in required if item.years >= threshold]
    if (
        _FIRST_EXPERIENCE_PROFILE_RE.search(context.description)
        and high_requirements
        and all(_NUMERIC_EXPERIENCE_BANNER_RE.search(item.quote) for item in high_requirements)
    ):
        return "entry_profile_conflicts_with_high_experience_banner"
    low_headers = [
        item
        for item in required
        if item.years < threshold and _EXPERIENCE_HEADER_RE.search(item.quote)
    ]
    if low_headers and any(item.years >= threshold for item in required):
        return "visible_experience_header_conflicts_with_profile"
    return None


def _number(value: str) -> float:
    written = re.sub(r"\s+", "-", value.strip())
    return (
        float(_WRITTEN_NUMBERS[written])
        if written in _WRITTEN_NUMBERS
        else float(value.replace(",", "."))
    )


def _format_number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _overlaps(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] < right[1] and right[0] < left[1]


def _is_company_history(clause: str, match: re.Match[str]) -> bool:
    if not _COMPANY_HISTORY_RE.search(clause):
        return False

    # If the mention follows an explicit candidate subject, it belongs to the
    # candidate even when company boilerplate appeared earlier in the clause.
    prefix = clause[: match.start()]
    candidate_positions = [item.start() for item in _CANDIDATE_RE.finditer(prefix)]
    history_positions = [item.start() for item in _COMPANY_HISTORY_RE.finditer(prefix)]
    return not candidate_positions or (
        bool(history_positions) and history_positions[-1] > candidate_positions[-1]
    )


def _has_third_party_possessor(clause: str, match: re.Match[str]) -> bool:
    """Attribute ``X has N years`` to X before considering amount markers.

    The owner is grammatical, independent of their name or job title. A
    later explicit candidate subject remains a separate requirement.
    """

    owner = _EXPERIENCE_POSSESSOR_BEFORE_RE.search(clause[: match.start()])
    if owner is None or _CANDIDATE_POSSESSOR_RE.search(owner.group("subject")):
        return False
    # "Must have" is an imperative requirement with an omitted subject.
    # A preceding section heading must not become its grammatical owner.
    subject = re.sub(
        r"\b(?:(?:is|are)\s+required\s+to|must|should|will|can|need\s+to)$",
        "",
        owner.group("subject").strip(),
    ).strip()
    subject = " ".join(
        line
        for line in subject.splitlines()
        if not re.fullmatch(
            rf"(?:{_REQUIRED_EXPERIENCE_SECTION}|{_OPTIONAL_EXPERIENCE_SECTION})",
            line.strip(" :-"),
        )
    ).strip()
    return bool(subject)


def _is_salary_band_experience(clause: str, match: re.Match[str]) -> bool:
    """A salary tier for experienced profiles is not an admission minimum."""

    before = clause[max(0, match.start() - 160) : match.start()]
    band = _SALARY_BAND_BEFORE_RE.search(before)
    if band is None:
        return False
    local_requirement = before[band.end() :] + clause[match.end() : match.end() + 55]
    return _STRICT_REQUIRED_RE.search(local_requirement) is None
