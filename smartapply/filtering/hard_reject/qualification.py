"""Deterministic gate for explicit educational requirements."""

from __future__ import annotations

import re
from dataclasses import dataclass

from smartapply.filtering.hard_reject.context import HardRejectContext
from smartapply.filtering.hard_reject.types import Evidence, GateDecision, GateStatus
from smartapply.filtering.text import norm

_GATE = "qualification"
_DEGREE = r"(?:ph\s*\.?\s*d\.?|doctorate|doctoral\s+degree|doctorat)"
_MANDATORY = (
    r"(?:required|requirement|mandatory|compulsory|essential|must|required\s+to|"
    r"obligatoire|requis(?:e)?|exige(?:e)?|imperatif|indispensable|"
    r"devez|devrez|doit)"
)
_MANDATORY_DOCTORATE_PATTERNS = (
    re.compile(rf"\b{_DEGREE}\b.{{0,55}}\b{_MANDATORY}\b"),
    re.compile(rf"\b{_MANDATORY}\b.{{0,55}}\b{_DEGREE}\b"),
    re.compile(
        rf"\b(?:must|devez|devrez)\b.{{0,35}}"
        rf"\b(?:have|hold|possess|etre\s+titulaire)\b.{{0,35}}\b{_DEGREE}\b"
    ),
    re.compile(
        rf"\b(?:you\s+(?:hold|have|possess)|holder\s+of|"
        rf"titulaire\s+d[' ]?(?:un\s+|une\s+)?(?:diplome\s+de\s+niveau\s+)?)"
        rf".{{0,30}}\b{_DEGREE}\b"
    ),
    re.compile(rf"^\s*{_DEGREE}\s+(?:in|en|dans|de|d[' ])\b"),
)
_POSTDOCTORAL_TITLE = re.compile(
    r"\b(?:post[- ]?doc(?:toral|torant|torante)?|post[- ]?doctorat|"
    r"chercheur(?:euse)?\s+post[- ]?doctoral(?:e)?|"
    r"post[- ]?doctoral\s+(?:researcher|position|fellow))\b"
)
_NON_MANDATORY = re.compile(
    r"\b(?:not\s+required|non\s+obligatoire|optional|facultatif|preferred|desirable|"
    r"ideally|nice\s+to\s+have|would\s+be\s+a\s+plus|souhaite|apprecie|idealement|"
    r"de\s+preference|recommended|recommande(?:e)?|is\s+a\s+plus|as\s+a\s+plus|"
    r"(?:est|constitue|serait)\s+un\s+plus|un\s+plus|"
    r"serait\s+un\s+plus)\b"
)
_ALTERNATIVE = re.compile(
    r"\b(?:or\s+(?:have\s+)?(?:an?\s+)?(?:equivalent|comparable)"
    r"(?:\s+(?:experience|qualification|record|track\s+record))?|"
    r"ou\s+(?:experience|qualification|formation)\s+equivalente?|ou\s+equivalent)\b"
)
_OTHER_ELIGIBLE_DEGREE = re.compile(
    r"\b(?:bac\s*\+?\s*5|bachelor(?:'s)?(?:\s+degree)?|"
    r"(?<!post-)(?<!post\s)master(?:'?s)?(?:\s+degree|\s*2)?|msc|ms|"
    r"engineering\s+degree|engineer(?:ing)?\s+degree|engineering\s+school|"
    r"ingenieur(?:e)?\s+grandes?\s+ecoles?|"
    r"ingenieur(?:e)?\s+de\s+recherche|research\s+engineer|"
    r"diplome\s+d[' ]?ingenieur|ecole\s+d[' ]?ingenieur|"
    r"formation\s+equivalente?)\b"
)
_ALTERNATIVE_SEPARATOR = re.compile(r"(?:\b(?:or|ou|and/or|et/ou)\b|\bet\s*/\s*ou\b|[,/])")
_ALTERNATIVE_LIST_HEADER = re.compile(
    r"\b(?:one\s+of\s+the\s+following\s+qualifications?|"
    r"one\s+of\s+the\s+following|"
    r"l[' ]?un\s+des\s+(?:deux\s+)?profils?\s+suivants?|"
    r"l[' ]?une\s+des\s+qualifications?\s+suivantes?)\b"
)
_QUALIFICATION_ALTERNATIVE_LIST_HEADER = re.compile(
    r"\b(?:formation\s+academique|niveau\s+de\s+diplome\s+et\s+formations?)\s*:"
)
_OPTIONAL_SECTION_HEADER = re.compile(
    r"^\s*(?:nice\s+to\s+have|nice[- ]to[- ]have|preferred\s+qualifications?|"
    r"atouts?\s+(?:apprecies?|souhaites?)|bonus(?:\s+points?)?|"
    r"les\s+petits\s+plus)\s*:?\s*$"
)
_SECTION_RESET_HEADER = re.compile(
    r"^\s*(?:requirements?|required\s+qualifications?|minimum\s+qualifications?|"
    r"must\s+have|what\s+you\s+bring|responsibilities|your\s+responsibilities|"
    r"exigences?|prerequis|qualifications?\s+requises?|profil\s+recherche|"
    r"missions?|vos\s+missions|responsabilites?)\s*:?\s*$"
)
_RECOMMENDED_JOBS_TAIL = re.compile(
    r"\b(?:des\s+)?offres?\s+d['’]emplois?\s+recommand[ée]es?\s+pour\s+vous\b"
    r"|\bces\s+offres?\s+pourraient\s+aussi\s+vous\s+int[ée]resser\b"
    r"|\brecommended\s+jobs?\s+for\s+you\b"
    r"|\byou\s+(?:may|might)\s+also\s+be\s+interested\b",
    re.IGNORECASE,
)
_NON_DOCTORAL_TITLE_PATH = re.compile(
    r"\b(?:ingenieur(?:/e|e)?\s+de\s+recherche|research\s+engineer|"
    r"stage|internship|(?<!post-)(?<!post\s)doctorant(?:/e|e)?|"
    r"doctoral\s+candidate|these)\b"
)
_NEGATED_DOCTORATE = re.compile(rf"\b(?:no|aucun)\s+{_DEGREE}\b")
_CANDIDATE_QUALIFICATION_HEADER = re.compile(
    r"^(?:candidate(?:/profile)?\s+requirements?|profile\s+requirements?|"
    r"expected\s+(?:profile|qualifications?)|your\s+(?:profile|qualifications?)|"
    r"(?:required|minimum)\s+qualifications?|requirements?|qualifications?|"
    r"education|profil(?:\s+recherche)?|prerequis|qualifications?\s+requises?)$"
)
_CANDIDATE_DEGREE_POSSESSION = re.compile(
    rf"\b(?:you|vous|candidates?|applicants?|(?:le|la)\s+candidat(?:e)?)\s+"
    r"(?:(?:must|should|need\s+to|(?:is|are)\s+required\s+to|"
    r"doit|doivent|devez|devrez|etes|etant)\s+)?"
    r"(?:have|hold|possess|be\s+(?:a\s+)?holder\s+of|"
    r"etre\s+titulaire|titulaire|avez|possedez)\b"
    rf".{{0,45}}\b{_DEGREE}\b"
)
_ROLE_DOCTORATE_REQUIREMENT = re.compile(
    rf"\b{_DEGREE}\b.{{0,55}}\b{_MANDATORY}\b.{{0,25}}"
    r"\bfor\s+(?:this|the)\s+(?:role|position|job)\b"
)
_BARE_DEGREE_REQUIREMENT = re.compile(rf"^\s*(?:an?\s+)?{_DEGREE}\b")
_BARE_MANDATORY_DEGREE = re.compile(
    rf"^\s*(?:an?\s+)?{_DEGREE}\s+"
    rf"(?:(?:is|are|est|sont)\s+)?{_MANDATORY}\b"
)
_ALTERNATIVE_BRANCH_LINE = re.compile(r"^\s*(?:or|ou)\s*$", re.MULTILINE)
_ALTERNATIVE_UNION = re.compile(r"\b(?:or|ou|and/or|et/ou)\b")
_OTHER_QUALIFICATION_PATH_AFTER_OR = re.compile(
    r"^\s*(?:(?:have|hold|possess|justifi(?:ez|er)|disposez)\b|"
    r"(?:d[' ]?(?:une?\s+)?|an?\s+)?(?:experience|formation|qualification|equivalent)\b|"
    r"(?:au\s+moins|at\s+least)\b|\d)"
)
_EXPLICIT_FOLLOWING_NON_DOCTORAL_PATH = re.compile(
    rf"\b(?:we\s+are\s+also\s+open\s+to|"
    rf"nous\s+sommes\s+egalement\s+ouvert(?:e|s|es)?\s+aux?|"
    rf"alternatively|a\s+defaut)\b.{{0,240}}{_OTHER_ELIGIBLE_DEGREE.pattern}"
)


@dataclass(frozen=True)
class _DoctorateRequirement:
    quote: str
    candidate_owned: bool
    conflict: str | None = None


def evaluate_qualification(context: HardRejectContext) -> GateDecision:
    """Handle an explicit mandatory doctorate without guessing profile facts.

    A preferred doctorate or a doctorate that can be replaced by equivalent
    experience is not a hard requirement.  When the requirement is strict but
    the candidate qualification is unknown, the gate reports ``unresolved``.
    """

    raw_title = str(getattr(context.job, "title", "") or "")
    raw_description = _offer_body(str(getattr(context.job, "description", "") or ""))
    finding = _mandatory_doctorate_requirement(raw_description)
    requirement = finding.quote if finding else None
    candidate_requirement = finding.candidate_owned if finding else False
    field = "description"
    normalized_title = norm(raw_title)
    if (
        (requirement is None or not candidate_requirement)
        and _POSTDOCTORAL_TITLE.search(normalized_title)
        and not _postdoctoral_title_is_alternative(normalized_title)
        and not _body_relaxes_doctorate(raw_description)
    ):
        requirement = raw_title
        candidate_requirement = True
        field = "title"
    if requirement is None:
        return GateDecision.clear(_GATE)

    evidence = Evidence(
        rule_id="mandatory_doctorate",
        source="visible_text",
        quote=requirement,
        field=field,
    )
    has_doctorate = context.qualifications.has_doctorate
    if finding is not None and finding.conflict and has_doctorate is not True:
        return GateDecision.unresolved(
            _GATE,
            conflicts=(finding.conflict,),
            evidence=(evidence,),
        )
    if has_doctorate is False and not candidate_requirement:
        return GateDecision.unresolved(
            _GATE,
            conflicts=("doctorate_requirement_owner_unresolved",),
            evidence=(evidence,),
        )
    if has_doctorate is False:
        return GateDecision(
            gate=_GATE,
            status=GateStatus.BLOCKED,
            reason_codes=("qualification_required:doctorate",),
            evidence=(evidence,),
        )
    if has_doctorate is None:
        return GateDecision.unresolved(
            _GATE,
            conflicts=("candidate_doctorate_unknown",),
            evidence=(evidence,),
        )

    return GateDecision(
        gate=_GATE,
        status=GateStatus.CLEAR,
        evidence=(evidence,),
        notes=("qualification_satisfied:doctorate",),
    )


def _mandatory_doctorate_requirement(description: str) -> _DoctorateRequirement | None:
    sentences = _sentences(description)
    optional_section = False
    candidate_section = False
    unresolved: _DoctorateRequirement | None = None
    for index, raw_sentence in enumerate(sentences):
        sentence = _normalize_degree_punctuation(norm(raw_sentence))
        if _OPTIONAL_SECTION_HEADER.fullmatch(sentence):
            optional_section = True
            candidate_section = False
            continue
        header, separator, body = sentence.partition(":")
        if _CANDIDATE_QUALIFICATION_HEADER.fullmatch(header.strip()):
            optional_section = False
            candidate_section = True
            if not separator or not body.strip():
                continue
            sentence = body.strip()
        if _SECTION_RESET_HEADER.fullmatch(sentence):
            optional_section = False
            candidate_section = False
            continue
        if (
            optional_section
            or _NEGATED_DOCTORATE.search(sentence)
            or _NON_MANDATORY.search(sentence)
            or _doctorate_is_degree_alternative(
                sentence,
                surrounding=_surrounding_qualification_context(sentences, index),
            )
        ):
            continue
        if any(pattern.search(sentence) for pattern in _MANDATORY_DOCTORATE_PATTERNS):
            if _has_following_non_doctoral_path(sentences, index):
                continue
            if _ALTERNATIVE.search(sentence) or _doctorate_has_unassessed_path(sentence):
                unresolved = unresolved or _DoctorateRequirement(
                    raw_sentence, True, "qualification_alternatives_unresolved"
                )
                continue
            if (
                _CANDIDATE_DEGREE_POSSESSION.search(sentence)
                or _ROLE_DOCTORATE_REQUIREMENT.search(sentence)
                or _BARE_MANDATORY_DEGREE.search(sentence)
                or (candidate_section and _BARE_DEGREE_REQUIREMENT.search(sentence))
            ):
                return _DoctorateRequirement(raw_sentence, True)
            unresolved = unresolved or _DoctorateRequirement(raw_sentence, False)
    return unresolved


def _body_relaxes_doctorate(description: str) -> bool:
    """An explicit degree alternative/preference qualifies a postdoc label too.

    This is considered only after looking for a mandatory body requirement:
    a separate required doctorate still wins over an unrelated bonus.
    """

    sentences = _sentences(description)
    for index, raw_sentence in enumerate(sentences):
        sentence = _normalize_degree_punctuation(norm(raw_sentence))
        if not re.search(rf"\b{_DEGREE}\b", sentence):
            continue
        if _NON_MANDATORY.search(sentence) or _doctorate_is_degree_alternative(
            sentence,
            surrounding=_surrounding_qualification_context(sentences, index),
        ):
            return True
    return False


def _doctorate_is_degree_alternative(sentence: str, *, surrounding: str) -> bool:
    """Return whether another accepted degree can replace the doctorate."""

    doctorate = re.search(rf"\b{_DEGREE}\b", sentence)
    other_degree = _OTHER_ELIGIBLE_DEGREE.search(sentence)
    if doctorate and other_degree:
        left = min(doctorate.start(), other_degree.start())
        right = max(doctorate.end(), other_degree.end())
        if _ALTERNATIVE_SEPARATOR.search(sentence[left:right]):
            return True

    return bool(
        (
            _ALTERNATIVE_LIST_HEADER.search(surrounding)
            or _QUALIFICATION_ALTERNATIVE_LIST_HEADER.search(surrounding)
            or _ALTERNATIVE_BRANCH_LINE.search(surrounding)
        )
        and _OTHER_ELIGIBLE_DEGREE.search(surrounding)
    )


def _doctorate_has_unassessed_path(sentence: str) -> bool:
    """Do not prove exclusion from only one branch of a qualification union."""

    doctorate = re.search(rf"\b{_DEGREE}\b", sentence)
    if doctorate is None:
        return False
    for union in _ALTERNATIVE_UNION.finditer(sentence):
        if union.end() <= doctorate.start():
            if re.search(r"\b(?:have|hold|possess|titulaire)\b", sentence[: union.start()]):
                return True
        elif union.start() >= doctorate.end():
            between = sentence[doctorate.end() : union.start()].strip()
            if not between or _OTHER_QUALIFICATION_PATH_AFTER_OR.search(sentence[union.end() :]):
                return True
    return False


def _has_following_non_doctoral_path(
    sentences: tuple[str, ...],
    index: int,
) -> bool:
    """Recognize an explicit alternative stated just after a doctorate line."""

    following = " ".join(
        _normalize_degree_punctuation(norm(sentence))
        for sentence in sentences[index + 1 : index + 3]
    )
    return _EXPLICIT_FOLLOWING_NON_DOCTORAL_PATH.search(following) is not None


def _surrounding_qualification_context(
    sentences: tuple[str, ...],
    index: int,
) -> str:
    start = max(0, index - 6)
    end = min(len(sentences), index + 4)
    return norm("\n".join(sentences[start:end]))


def _offer_body(description: str) -> str:
    """Drop recommendation widgets appended after the actual offer."""

    tail = _RECOMMENDED_JOBS_TAIL.search(description)
    return description[: tail.start()] if tail else description


def _postdoctoral_title_is_alternative(title: str) -> bool:
    postdoctoral = _POSTDOCTORAL_TITLE.search(title)
    non_doctoral = _NON_DOCTORAL_TITLE_PATH.search(title)
    if not postdoctoral or not non_doctoral:
        return False
    left = min(postdoctoral.start(), non_doctoral.start())
    right = max(postdoctoral.end(), non_doctoral.end())
    return _ALTERNATIVE_SEPARATOR.search(title[left:right]) is not None


def _normalize_degree_punctuation(text: str) -> str:
    return re.sub(r"\bph\s*\.\s*d\s*\.?", "phd", text)


def _sentences(text: str) -> tuple[str, ...]:
    return tuple(
        sentence.strip()
        for sentence in re.split(
            r"(?:\r?\n|[;!?•]+|(?<!\bph)(?<!\bph\.d)\.(?:\s+|$))",
            text,
            flags=re.IGNORECASE,
        )
        if sentence.strip()
    )


__all__ = ["evaluate_qualification"]
