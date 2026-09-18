"""Classify a job offer into a role family.

The family id drives a deterministic skill-display contract applied after the
LLM produces its CV draft. It is intentionally local and free (regex over the
analysis + title) so we don't pay an LLM call for a routing decision.

Granularity is coarse on purpose. The contracts only need to distinguish
roles that demand different skill envelopes (e.g. an Analytics Engineer must
not display FAISS or NLP, a Data Scientist must keep a ML/IA baseline).
"""

from __future__ import annotations

import re
from typing import Final

from smartapply.llm.schemas import JobAnalysis

RoleFamily = str

OTHER: Final[RoleFamily] = "other"

KNOWN_ROLE_FAMILIES: Final[set[RoleFamily]] = {
    "analytics_engineer",
    "computer_vision",
    "data_analyst",
    "data_engineer",
    "data_scientist",
    "llm_engineer",
    "medical_ai",
    "ml_engineer",
    "mlops",
    OTHER,
    "reinforcement_learning",
    "software_engineer",
    "speech_audio",
}

# Ordered title/role_type patterns. These are high-confidence signals: when
# the title says "Data Scientist" or "Backend Software Engineer", the family
# should not be overwritten by noisy skills found later in the description.
_TITLE_PATTERNS: Final[list[tuple[RoleFamily, list[str]]]] = [
    (
        "llm_engineer",
        [
            r"\bllm\b",
            r"\bllms\b",
            r"\bgen[- ]?ai\b",
            r"\bgenerative ai\b",
            r"\bia générative\b",
            r"\brag\b",
            r"\bretrieval[- ]augmented\b",
            r"\bvector (db|search|database|store)\b",
            r"\bprompt engineer",
            r"\blangchain\b",
            r"\blanggraph\b",
            r"\bllamaindex\b",
        ],
    ),
    (
        "computer_vision",
        [
            r"\bcomputer vision\b",
            r"\bvision par ordinateur\b",
            r"\bopencv\b",
            r"\bobject detection\b",
            r"\bimage segmentation\b",
            r"\bsemantic segmentation\b",
            r"\binstance segmentation\b",
            r"\bimage processing\b",
            r"\bimage classification\b",
            r"\bvision pipeline",
        ],
    ),
    (
        "speech_audio",
        [
            r"\bspeech[- ]to[- ]text\b",
            r"\bspeech processing\b",
            r"\baudio processing\b",
            r"\btranscription\b",
            r"\bdiarization\b",
            r"\bwhisper\b",
            r"\bpyannote\b",
        ],
    ),
    (
        "reinforcement_learning",
        [
            r"\breinforcement learning\b",
            r"\bapprentissage par renforcement\b",
            r"\bcontrol tasks?\b",
            r"\bpolicy gradient\b",
        ],
    ),
    (
        "mlops",
        [
            r"\bml[- ]?ops\b",
            r"\bdevops/ml\b",
            r"\bdevops\s*/\s*ml\b",
            r"\bml platform\b",
            r"\bml infrastructure\b",
            r"\bmodel monitoring\b",
            r"\bmodel serving\b",
            r"\bplateforme ml\b",
        ],
    ),
    (
        "data_scientist",
        [
            r"\bdata scientist\b",
            r"\bdatascientist\b",
            r"\bdata[- ]scientist\b",
            r"\bscientifique des données\b",
            r"\bdata science\b",
        ],
    ),
    (
        "data_analyst",
        [
            r"\bdata analyst\b",
            r"\bproduct data analyst\b",
            r"\bproduct analyst\b",
            r"\bbusiness analyst\b",
            r"\banalyste (de )?donn",
            r"\bdata miner\b",
        ],
    ),
    (
        "analytics_engineer",
        [
            r"\banalytics engineer\b",
            r"\bbi engineer\b",
        ],
    ),
    (
        "data_engineer",
        [
            r"\bdata engineer\b",
            r"\bcloud data engineer\b",
            r"\bbig data engineer\b",
            r"\bdata platform engineer\b",
            r"\bingénieur data\b",
        ],
    ),
    (
        "software_engineer",
        [
            r"\bsoftware engineer\b",
            r"\bsoftware developer\b",
            r"\bbackend engineer\b",
            r"\bbackend developer\b",
            r"\bfullstack\b",
            r"\bfull[- ]stack\b",
            r"\bfrontend\b",
            r"\bc\+\+",
            r"\bqt framework\b",
        ],
    ),
]


# Generic ML/AI titles are intentionally handled after the locked title
# patterns. A plain "AI Engineer" may become LLM/CV/Speech if the offer body is
# clearly specialised, otherwise it falls back to ml_engineer.
_GENERIC_PRIMARY_PATTERNS: Final[list[tuple[RoleFamily, list[str]]]] = [
    (
        "ml_engineer",
        [
            r"\bml engineer\b",
            r"\bmachine learning engineer\b",
            r"\bmachine learning consultant\b",
            r"\bml consultant\b",
            r"\bjunior ml\b",
            r"\bai engineer\b",
            r"\bia engineer\b",
            r"\bingénieur (en )?ia\b",
            r"\bingénieur (en )?ml\b",
            r"\bml[- ]engineer\b",
            r"\bml specialist\b",
            r"\bmachine learning specialist\b",
            r"\bapplied (ai|ml) engineer\b",
        ],
    ),
]


_OFF_TARGET_PRIMARY_PATTERNS: Final[list[str]] = [
    r"\bdevops\b",
    r"\biam\b",
    r"\bidentity and access\b",
    r"\bide\b",
    r"\binfirmier\b",
    r"\binfirmière\b",
    r"\bnurse\b",
    r"\bcybersecurity\b",
    r"\bcybersécurité\b",
    r"\bsécurité\b",
    r"\bresponsable développement\b",
    r"\bjava development manager\b",
]


# Extended patterns are used only when the primary signal is ambiguous or
# absent. They let "AI Engineer" become LLM Engineer when the offer body says
# RAG/NLP, without turning "Data Scientist" into "LLM Engineer".
_SPECIALIST_EXTENDED_PATTERNS: Final[list[tuple[RoleFamily, list[str]]]] = [
    (
        "llm_engineer",
        [
            r"\bllm\b",
            r"\bllms\b",
            r"\bgen[- ]?ai\b",
            r"\bgenerative ai\b",
            r"\bia générative\b",
            r"\brag\b",
            r"\bretrieval[- ]augmented\b",
            r"\bvector (db|search|database|store)\b",
            r"\bprompt engineer",
            r"\blangchain\b",
            r"\blanggraph\b",
            r"\bllamaindex\b",
        ],
    ),
    (
        "computer_vision",
        [
            r"\bcomputer vision\b",
            r"\bvision par ordinateur\b",
            r"\bopencv\b",
            r"\bobject detection\b",
            r"\bimage segmentation\b",
            r"\bsemantic segmentation\b",
            r"\binstance segmentation\b",
            r"\bimage processing\b",
            r"\bimage classification\b",
            r"\bvision pipeline",
        ],
    ),
    (
        "speech_audio",
        [
            r"\bspeech[- ]to[- ]text\b",
            r"\bspeech processing\b",
            r"\btraitement de la voix\b",
            r"\bvoice\b",
            r"\baudio processing\b",
            r"\btranscription\b",
            r"\bdiarization\b",
            r"\bwhisper\b",
            r"\bpyannote\b",
        ],
    ),
    (
        "reinforcement_learning",
        [
            r"\breinforcement learning\b",
            r"\bapprentissage par renforcement\b",
            r"\bcontrol tasks?\b",
            r"\bpolicy gradient\b",
        ],
    ),
    (
        "medical_ai",
        [
            r"\bmedical (ai|machine learning|ml|nlp|imaging|data science)\b",
            r"\bdigital health (ai|platform|data|ml|machine learning)\b",
            r"\bhealthtech\b",
            r"\bbiomark",
            r"\bclinical ai\b",
            r"\bsanté numérique\b",
            r"\bclinical (machine learning|ml|nlp|data science|biomarker)\b",
            r"\bdispositif médical connecté\b",
        ],
    ),
]


_EXTENDED_PATTERNS: Final[list[tuple[RoleFamily, list[str]]]] = [
    (
        "analytics_engineer",
        [
            r"\banalytics engineer\b",
            r"\bdbt\b",
            r"\bdata modeling\b",
            r"\bsnowflake\b",
            r"\banalytics engineering\b",
            r"\bsemantic layer\b",
        ],
    ),
    (
        "data_engineer",
        [
            r"\bdata engineer\b",
            r"\bcloud data engineer\b",
            r"\bbig data engineer\b",
            r"\bdata platform engineer\b",
            r"\bdata pipelines?\b",
            r"\bairflow\b",
            r"\bbigquery\b",
            r"\bdata transformation\b",
        ],
    ),
    (
        "data_analyst",
        [
            r"\bdata analyst\b",
            r"\bproduct analyst\b",
            r"\bbusiness analyst\b",
            r"\banalyste (de )?donn",
            r"\bpower bi\b",
            r"\btableau\b",
            r"\balteryx\b",
            r"\bbi engineer\b",
        ],
    ),
    (
        "data_scientist",
        [
            r"\bdata scientist\b",
            r"\bdatascientist\b",
            r"\bdata[- ]scientist\b",
            r"\bscientifique des données\b",
            r"\bdata science\b",
        ],
    ),
    *_GENERIC_PRIMARY_PATTERNS,
]


# Offer signals that turn on extra ml_ai display for Data Scientist roles
# (NLP / Transformers on top of the base PyTorch + scikit-learn).
_DS_IA_SIGNAL_PATTERNS: Final[list[str]] = [
    r"\bnlp\b",
    r"\bllm\b",
    r"\bllms\b",
    r"\bgen[- ]?ai\b",
    r"\bgenerative ai\b",
    r"\bia\b",
    r"\bartificial intelligence\b",
    r"\bintelligence artificielle\b",
    r"\btransformers?\b",
    r"\bhugging[- ]face\b",
    r"\bfine[- ]tuning\b",
    r"\btextes?\b",
    r"\blanguage models?\b",
    r"\bmodèles? de langue\b",
    r"\btraitement (du|de) langage\b",
]


def _primary_haystack(analysis: JobAnalysis, title: str) -> str:
    """Title + role_type only — what the role IS, not what it touches."""
    parts: list[str] = [title or "", analysis.role_type or ""]
    return " ".join(p for p in parts if p).lower()


def _haystack(analysis: JobAnalysis, title: str) -> str:
    """Full extended haystack used by broad families.

    Intentionally drops ``nice_to_have``: bonus skills are too noisy to drive
    a role classification (e.g. a Data Scientist offer that lists Power BI as
    nice-to-have should not be tagged as a Data Analyst).
    """
    parts: list[str] = [
        title or "",
        analysis.role_type or "",
        analysis.domain or "",
        " ".join(analysis.main_tasks),
        " ".join(analysis.required_skills),
        " ".join(analysis.cv_keywords_to_include),
    ]
    return " ".join(p for p in parts if p).lower()


def _matches(patterns: list[str], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _match_family(
    patterns_by_family: list[tuple[RoleFamily, list[str]]],
    text: str,
) -> RoleFamily | None:
    for family_id, patterns in patterns_by_family:
        if _matches(patterns, text):
            return family_id
    return None


def classify(analysis: JobAnalysis, *, title: str = "") -> RoleFamily:
    """Return the role family id, or ``other`` when no pattern matches."""
    primary = _primary_haystack(analysis, title)
    extended = _haystack(analysis, title)

    locked_family = _match_family(_TITLE_PATTERNS, primary)
    if locked_family:
        return locked_family

    if _matches(_OFF_TARGET_PRIMARY_PATTERNS, primary):
        return OTHER

    generic_family = _match_family(_GENERIC_PRIMARY_PATTERNS, primary)
    if generic_family:
        specialist_family = _match_family(_SPECIALIST_EXTENDED_PATTERNS, extended)
        return specialist_family or generic_family

    specialist_family = _match_family(_SPECIALIST_EXTENDED_PATTERNS, extended)
    if specialist_family:
        return specialist_family

    extended_family = _match_family(_EXTENDED_PATTERNS, extended)
    if extended_family:
        return extended_family

    return OTHER


def classify_title(title: str) -> RoleFamily:
    """Classify a generated CV headline from title words only.

    Unlike :func:`classify`, this deliberately ignores tasks, skills and other
    offer details. A CV headline is a broad professional position, not a
    keyword-coverage field.
    """
    title_text = (title or "").lower()
    locked_family = _match_family(_TITLE_PATTERNS, title_text)
    if locked_family:
        return locked_family
    generic_family = _match_family(_GENERIC_PRIMARY_PATTERNS, title_text)
    return generic_family or OTHER


_COMPATIBLE_CV_TITLE_FAMILIES: Final[dict[RoleFamily, set[RoleFamily]]] = {
    "analytics_engineer": {"analytics_engineer", "data_engineer"},
    "computer_vision": {"computer_vision", "ml_engineer"},
    "data_analyst": {"data_analyst"},
    "data_engineer": {"data_engineer", "analytics_engineer"},
    "data_scientist": {"data_scientist"},
    "llm_engineer": {"llm_engineer", "ml_engineer"},
    "medical_ai": {
        "medical_ai",
        "computer_vision",
        "data_scientist",
        "llm_engineer",
        "ml_engineer",
        "speech_audio",
    },
    "ml_engineer": {"ml_engineer"},
    "mlops": {"mlops", "ml_engineer"},
    "reinforcement_learning": {"reinforcement_learning", "ml_engineer"},
    "software_engineer": {"software_engineer"},
    "speech_audio": {"speech_audio", "ml_engineer"},
}


def cv_title_family_is_compatible(
    offer_family: RoleFamily,
    title_family: RoleFamily,
) -> bool:
    """Return whether a CV-title family is compatible with an offer family.

    ``other`` means that the local classifier is uncertain. Ambiguous cases
    are accepted instead of producing a misleading warning.
    """
    if OTHER in {offer_family, title_family}:
        return True
    allowed = _COMPATIBLE_CV_TITLE_FAMILIES.get(offer_family, {offer_family})
    return title_family in allowed


def has_data_scientist_ia_signal(
    analysis: JobAnalysis,
    title: str = "",
) -> bool:
    """True when a Data Scientist offer also asks for AI/NLP/LLM/GenAI.

    Used to augment the data_scientist contract with NLP + Transformers
    on the ml_ai block. A vanilla DS role (forecasting or
    classical statistics) gets a leaner ml_ai baseline.
    """
    haystack = _haystack(analysis, title)
    return any(re.search(p, haystack) for p in _DS_IA_SIGNAL_PATTERNS)
