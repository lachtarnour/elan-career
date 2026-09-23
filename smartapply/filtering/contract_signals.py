"""Visible contract-context detection for local filtering."""

from __future__ import annotations

import re

from smartapply.filtering.text import contains_any, has_word, matches_any_pattern, norm

STAGE_CONTRACT_MARKERS = {"stage", "stagiaire", "internship", "intern"}
APPRENTICESHIP_CONTRACT_MARKERS = {
    "alternance",
    "alternant",
    "alternante",
    "apprenti",
    "apprentissage",
    "apprentice",
    "apprenticeship",
    "work-study",
    "work study",
    "co-op",
    "co op",
    "traineeship",
}
FREELANCE_CONTRACT_MARKERS = {"freelance", "contractor"}
INDEPENDENT_CONTRACT_MARKERS = {"independant", "independent"}

# A CDD is acceptable for doctoral/research offers when the offer title makes
# that context explicit.  Keep this title-only and word-boundary based: a
# mention in the description must not turn an ordinary CDD into an exception.
RESEARCH_DEGREE_TITLE_MARKERS = (
    "these",
    "thesis",
    "doctorat",
    "doctorant",
    "doctorante",
    "doctorate",
    "doctoral",
    "phd",
    "ph d",
)

_APPRENTICESHIP_SAFE_PHRASES = (
    "apprentissage statistique",
    "apprentissage automatique",
    "machine learning / apprentissage",
    "machine learning et apprentissage",
    "capacite d'apprentissage",
    "culture de l'apprentissage",
    "apprentissage continu",
)
_INDEPENDENT_EMPLOYER_PHRASES = (
    "cabinet independant",
    "groupe independant",
    "institution independante",
    "autorite independante",
    "autorite publique independante",
    "acteur independant",
    "societe independante",
    "entreprise independante",
)
_STAGE_CONTRACT_PATTERNS = (
    r"\bcontrat\s+(?:de\s+)?stage\b",
    r"\bstage\s+conventionne\b",
    r"\boffre\s+(?:de\s+)?stage\b",
    r"\bposte\s+(?:en|de)\s+stage\b",
    r"\brecrut(?:e|ons|ement)\s+(?:un|une)?\s*stagiaire\b",
    r"\bstage\s+(?:de\s+)?fin\s+d[' ]?etudes\b",
    r"\bstage\s+pfe\b",
    r"\binternship\b",
    r"\bintern\s+(?:role|position|job)\b",
)
_STAGE_SAFE_CONTEXT_PATTERNS = (
    r"\bhors\s+stage\b",
    r"\bstage\b.{0,90}\b(?:accepte|acceptes|acceptee|acceptees)\b",
    r"\bstage\b.{0,90}\b(?:apprecie|apprecies|appreciee|appreciees)\b",
    r"\bpremiere\s+experience\b.{0,90}\bstage\b",
    r"\bstage\b.{0,90}\bpremiere\s+experience\b",
    r"\bstage\s+ou\s+projet\s+significatif\b",
    r"\btutorat\s+d[' ]?un\s+stagiaire\b",
    r"\bencadrement\s+d[' ]?un\s+stagiaire\b",
    r"\baccueill(?:e|ent)\b.{0,120}\bstagiaires?\b",
    r"\b(?:previous|prior)\s+internship\s+experience\b.{0,90}"
    r"\b(?:accepted|preferred|appreciated|welcome)\b",
    r"\binternship\s+experience\b.{0,90}"
    r"\b(?:accepted|preferred|appreciated|welcome)\b",
    r"\b(?:mentor|mentoring|supervise|supervising|coach|coaching)\b"
    r".{0,90}\binterns?\b",
)
_APPRENTICESHIP_CONTRACT_PATTERNS = (
    r"\bcontrat\s+d[' ]?apprentissage\b",
    r"\bcontrat\s+de\s+professionnalisation\b",
    r"\bposte\s+en\s+alternance\b",
    r"\boffre\s+en\s+alternance\b",
    r"\brythme\s+d[' ]?alternance\b",
    r"\brecrut(?:e|ons|ement)\s+(?:un|une)?\s*alternant(?:e)?\b",
    r"\brecrut(?:e|ons|ement)\s+(?:un|une)?\s*apprenti(?:e)?\b",
    r"\bapprenticeship\b",
    r"\bwork[- ]study\s+(?:role|position|job|program(?:me)?)\b",
    r"\bco[- ]op\s+(?:role|position|job|program(?:me)?)\b",
    r"\btraineeship\b",
)
_APPRENTICESHIP_SAFE_CONTEXT_PATTERNS = (
    r"\bhors\s+alternance\b",
    r"\balternance\b.{0,90}\b(?:acceptee|acceptees|appreciee|appreciees)\b",
    r"\bpremiere\s+experience\b.{0,90}\balternance\b",
    r"\balternance\b.{0,90}\bpremiere\s+experience\b",
    r"\bexperience\b.{0,90}\balternance\b.{0,90}\b(?:acceptee|appreciee)\b",
    r"\baccueill(?:e|ent)\b.{0,120}\balternants?\b",
    r"\bforme(?:r|nt)?\s+les\s+apprentis\b",
    r"\btutorat\s+d[' ]?un\s+alternant\b",
    r"\bencadrement\s+d[' ]?un\s+alternant\b",
    r"\b(?:previous|prior)\s+(?:apprenticeship|work[- ]study|co[- ]op)\s+"
    r"experience\b.{0,90}\b(?:accepted|preferred|appreciated|welcome)\b",
    r"\b(?:apprenticeship|work[- ]study|co[- ]op)\s+experience\b.{0,90}"
    r"\b(?:accepted|preferred|appreciated|welcome)\b",
    r"\b(?:mentor|mentoring|supervise|supervising|coach|coaching)\b"
    r".{0,90}\b(?:apprentices?|trainees?)\b",
)
_FREELANCE_CONTRACT_PATTERNS = (
    r"\bmission\s+(?:en\s+)?freelance\b",
    r"\bfreelance\s+mission\b",
    r"\bcontrat\s+freelance\b",
    r"\bfreelance\s+uniquement\b",
    r"\bcontractor\s+(?:role|position|job)\b",
    r"\bcontract\s+(?:role|position|job)\b",
    r"\b(?:role|position|job)\s+as\s+(?:a\s+)?contractor\b",
)
_INDEPENDENT_CONTRACT_PATTERNS = (
    r"\bmission\s+independant(?:e)?\b",
    r"\bcontrat\s+independant(?:e)?\b",
    r"\bstatut\s+independant(?:e)?\b",
    r"\btravailleur\s+independant\b",
    r"\btravailleuse\s+independante\b",
    r"\bconsultant\s+independant\b",
    r"\bconsultante\s+independante\b",
    r"\bindependent\s+(?:consultant|contractor)\b",
    r"\bself[- ]employed\s+(?:role|position|job|status|contract)\b",
    r"\b(?:role|position|job)\s+(?:for|as)\s+(?:a\s+)?self[- ]employed\b",
    r"\bauto-?entrepreneur\b",
    r"\bmicro-?entrepreneur\b",
    r"\bportage\s+salarial\b",
)
_CDD_SAFE_CONTEXT_PATTERNS = (
    r"\b(?:nos|les|des)\s+offres?\b.{0,90}\bcdi\b.{0,90}\bcdd\b",
    r"\bnous\s+recrutons\b.{0,90}\bcdi\b.{0,90}\bcdd\b",
    r"\bnos\s+consultants\b.{0,90}\baccompagn(?:e|ent)\b.{0,90}\bcdi\b.{0,90}\bcdd\b",
    r"\bcdi\b.{0,60}\bcdd\b.{0,60}\b(?:interim|freelance|stage|alternance)\b",
)
_CDD_CONTRACT_PATTERNS = (
    r"\btype\s+de\s+contrat\s*:\s*cdd\b",
    r"\btype\s+de\s+contrat\s*:\s*interim\b",
    r"\bcontrat\s+(?:de\s+)?cdd\b",
    r"\bcontrat\s+(?:d[' ]?)?interim\b",
    r"\bposte\s+(?:en|de)\s+cdd\b",
    r"\bposte\s+(?:en|d[' ]?)interim\b",
    r"\boffre\s+(?:en|de)\s+cdd\b",
    r"\boffre\s+(?:en|d[' ]?)interim\b",
    r"\bmission\s+(?:en|de)\s+cdd\b",
    r"\bmission\s+(?:en|d[' ]?)interim\b",
    r"\ben\s+cdd\b",
    r"\ben\s+interim\b",
    r"\bcdd\s+(?:de|d[' ]?une|d[' ]?un|\d{1,2}\s*(?:mois|ans?))\b",
    r"\binterim\s+(?:de|d[' ]?une|d[' ]?un|\d{1,2}\s*(?:mois|ans?))\b",
    r"\bcdd\s+temps\s+plein\b",
    r"\binterim\s+temps\s+plein\b",
    r"\bfixed[- ]term\b",
    r"\btravail\s+temporaire\b",
    r"\bcontrat\s+temporaire\b",
    r"\bposte\s+temporaire\b",
    r"\btemporary\s+(?:contract|role|position|job|employment)\b",
    r"\binterim\s+(?:contract|role|position|job|employment)\b",
    r"\b(?:contract|fixed[- ]term)\s+(?:role|position|job)\b"
    r".{0,60}\b\d{1,2}\s*(?:months?|years?)\b",
    r"\b\d{1,2}[- ]months?\s+(?:fixed[- ]term\s+)?contract\b",
    r"\bcontract\s+(?:for|lasting)\s+\d{1,2}\s*(?:months?|years?)\b",
)

_PART_TIME_CONTEXT_PATTERNS = (
    r"\b(?:poste|emploi|contrat|role|position|job)\b.{0,70}"
    r"\b(?:a\s+)?(?:temps\s+partiel|mi-?temps|part[- ]time)\b",
    r"\b(?:temps\s+de\s+travail|duree\s+du\s+travail|work\s+schedule|"
    r"working\s+hours|employment\s+type)\s*:\s*.{0,50}"
    r"\b(?:temps\s+partiel|mi-?temps|part[- ]time)\b",
    r"\b(?:a\s+temps\s+partiel|en\s+temps\s+partiel|mi-?temps|"
    r"part[- ]time\s+(?:role|position|job|employment))\b",
)


def has_stage_contract_context(title: str, description: str) -> bool:
    if any(has_word(title, marker) for marker in STAGE_CONTRACT_MARKERS):
        return True
    if matches_any_pattern(description, _STAGE_SAFE_CONTEXT_PATTERNS):
        return False
    return matches_any_pattern(description, _STAGE_CONTRACT_PATTERNS)


def has_apprenticeship_contract_context(title: str, description: str) -> bool:
    title_has_contract_marker = any(
        has_word(title, marker)
        for marker in APPRENTICESHIP_CONTRACT_MARKERS
        if marker != "apprentissage"
    ) or (
        has_word(title, "apprentissage") and not contains_any(title, _APPRENTICESHIP_SAFE_PHRASES)
    )
    if title_has_contract_marker:
        return True
    if matches_any_pattern(description, _APPRENTICESHIP_SAFE_CONTEXT_PATTERNS):
        return False
    return matches_any_pattern(description, _APPRENTICESHIP_CONTRACT_PATTERNS)


def has_freelance_contract_context(title: str, description: str) -> bool:
    if any(has_word(title, marker) for marker in FREELANCE_CONTRACT_MARKERS):
        return True
    return matches_any_pattern(description, _FREELANCE_CONTRACT_PATTERNS)


def has_independent_contract_context(title: str, description: str) -> bool:
    """Detect independent/freelance status without flagging ordinary adjectives."""
    independent_re = r"(?<![a-z0-9])(?:independant(?:e|s)?|independent)(?![a-z0-9])"
    if re.search(independent_re, title):
        return True
    if contains_any(description, _INDEPENDENT_EMPLOYER_PHRASES):
        return False
    return matches_any_pattern(description, _INDEPENDENT_CONTRACT_PATTERNS)


def has_research_degree_title(title: str) -> bool:
    """Return whether a title explicitly describes doctoral research."""
    # ``title`` is usually already normalized by the filter, but normalizing
    # punctuation here also catches variants such as ``Ph.D.`` reliably.
    title_words = re.sub(r"[^a-z0-9]+", " ", norm(title)).strip()
    return any(has_word(title_words, marker) for marker in RESEARCH_DEGREE_TITLE_MARKERS)


def has_cdd_contract_context(title: str, description: str) -> bool:
    """Detect visible CDD/fixed-term offers without flagging generic listings."""
    if (
        has_word(title, "cdd")
        or has_word(title, "fixed term")
        or has_word(title, "fixed-term")
        or has_word(title, "interim")
    ):
        return True
    if matches_any_pattern(description, _CDD_SAFE_CONTEXT_PATTERNS):
        return False
    return matches_any_pattern(description, _CDD_CONTRACT_PATTERNS)


def has_part_time_contract_context(title: str, description: str) -> bool:
    """Detect a part-time work schedule in visible FR/EN offer text."""
    if re.search(r"\b(?:temps\s+partiel|mi-?temps|part[- ]time)\b", title):
        return True
    return matches_any_pattern(description, _PART_TIME_CONTEXT_PATTERNS)


def visible_blocked_contract_marker(
    *,
    title: str,
    description: str,
    blocked_contract_types: tuple[str, ...],
) -> str | None:
    visible_contract_text = f"{title}\n{description}"
    enabled_markers = {norm(marker) for marker in blocked_contract_types if norm(marker)}
    for marker in blocked_contract_types:
        normalized = norm(marker)
        if not normalized:
            continue
        if normalized in STAGE_CONTRACT_MARKERS:
            marker_visible = has_word(title, normalized) or has_word(
                description,
                normalized,
            )
            if marker_visible and has_stage_contract_context(title, description):
                return normalized
            continue
        if normalized in APPRENTICESHIP_CONTRACT_MARKERS:
            marker_visible = has_word(title, normalized) or has_word(
                description,
                normalized,
            )
            professionnalisation_context = normalized == "alternance" and re.search(
                r"\bcontrat\s+de\s+professionnalisation\b",
                description,
            )
            if (
                marker_visible or professionnalisation_context
            ) and has_apprenticeship_contract_context(title, description):
                return normalized
            continue
        if normalized in FREELANCE_CONTRACT_MARKERS:
            marker_visible = has_word(title, normalized) or has_word(
                description,
                normalized,
            )
            if marker_visible and has_freelance_contract_context(title, description):
                return normalized
            continue
        if normalized in INDEPENDENT_CONTRACT_MARKERS:
            marker_visible = has_word(title, normalized) or has_word(
                description,
                normalized,
            )
            if marker_visible and has_independent_contract_context(title, description):
                return normalized
            continue
        if has_word(visible_contract_text, normalized):
            return normalized
    if enabled_markers & STAGE_CONTRACT_MARKERS and has_stage_contract_context(
        title,
        description,
    ):
        return "stage"
    if enabled_markers & APPRENTICESHIP_CONTRACT_MARKERS and has_apprenticeship_contract_context(
        title, description
    ):
        return "alternance"
    if enabled_markers & FREELANCE_CONTRACT_MARKERS and has_freelance_contract_context(
        title,
        description,
    ):
        return "freelance"
    if enabled_markers & INDEPENDENT_CONTRACT_MARKERS and has_independent_contract_context(
        title,
        description,
    ):
        return "independant"
    return None
