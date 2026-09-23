"""Normalize equivalent skill wording while retaining canonical display labels."""

from __future__ import annotations

import re
import unicodedata

_GRAPH_THEORY_FR = re.compile(r"(?<!\w)th[ée]orie des graphes(?!\w)")

# Only equivalent names belong here. Related tools/model families (for
# example Whisper/STT, LangChain/LangGraph or LSTM/RNN) are not aliases.
_SKILL_ALIASES = (
    (r"graph neural networks?(?:\s*\(gnns?\))?|gnns", "gnn"),
    (r"recurrent neural networks?(?:\s*\(rnns?\))?|rnns", "rnn"),
    (r"convolutional neural networks?(?:\s*\(cnns?\))?|cnns?", "cnn"),
    (r"monte[ -]carlo tree search(?:\s*\(mcts\))?", "mcts"),
    (r"speech[ -]+to[ -]+text|automatic speech recognition|asr", "speech-to-text"),
    (r"scikit[ -]learn|sklearn", "scikit-learn"),
    (r"tree[ -]based & boosting models", "tree-based models & boosting"),
    (r"imputation techniques", "data imputation"),
    (r"ovh", "ovhcloud"),
)
_SKILL_ALIAS_PATTERNS = tuple(
    (re.compile(rf"(?<![\w+#])(?:{pattern})(?![\w+#])"), canonical)
    for pattern, canonical in _SKILL_ALIASES
)

_GENERIC_SKILL_TERMS = frozenset(
    {
        "test",
        "tests",
        "validation",
        "validate",
        "validated",
        "integration",
        "intégration",
        "deployment",
        "déploiement",
        "model",
        "models",
        "modèle",
        "modèles",
        "solution",
        "solutions",
    }
)


def normalize_skill_term(term: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFC", term or "").lower().split())
    normalized = _GRAPH_THEORY_FR.sub("graph theory", normalized)
    for pattern, canonical in _SKILL_ALIAS_PATTERNS:
        normalized = pattern.sub(canonical, normalized)
    return normalized


def skill_term_matches(term: str, skill: str) -> bool:
    """Match names/phrases at word boundaries, never fragments such as Git/digital.

    Partial compound names remain supported (ARIMA in ARIMA/SARIMA, or DDP
    in Distributed training (PyTorch DDP)); generic words are not evidence.
    This only compares wording: offer relevance still comes from the caller.
    """
    term_norm = normalize_skill_term(term)
    skill_norm = normalize_skill_term(skill)
    if not term_norm or not skill_norm:
        return False
    if term_norm == skill_norm:
        return True
    if len(term_norm) <= 2 or len(skill_norm) <= 2 or term_norm in _GENERIC_SKILL_TERMS:
        return False
    return any(
        re.search(rf"(?<![\w+#]){re.escape(needle)}(?![\w+#])", haystack)
        for needle, haystack in ((skill_norm, term_norm), (term_norm, skill_norm))
    )
