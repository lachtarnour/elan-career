"""Normalize equivalent skill wording while retaining canonical display labels."""

from __future__ import annotations

import re
import unicodedata

_GRAPH_THEORY_FR = re.compile(r"(?<!\w)th[ée]orie des graphes(?!\w)")


def normalize_skill_term(term: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFC", term or "").lower().split())
    return _GRAPH_THEORY_FR.sub("graph theory", normalized)
