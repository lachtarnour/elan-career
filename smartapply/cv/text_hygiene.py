"""Narrow editorial hints for leakage of the application's evidence vocabulary.

These checks neither infer factual truth nor rewrite text. Ambiguous professional
uses remain a contextual decision for the reviewer, not a global word blacklist.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

EVIDENCE_METADATA_PREFIX = "internal_evidence_metadata:"

# Catch our own identifiers and experience qualifiers, not arbitrary uses of
# "verified", "evidence", "grounded" or "self-reported" in technical work/data.
_EVIDENCE_METADATA = re.compile(
    r"\b(?:evidence_level|allowed_claims)\b"
    r"|\bprofile[\s_-]+grounded\b"
    r"|\bgrounded\s+in\s+(?:the\s+)?profile\s+evidence\b"
    r"|\b(?:verified|self[\s_-]+reported|(?:source|evidence)[\s_-]+grounded)"
    r"\s+(?:work(?:flows)?|experience|contributions?|achievements?|expertise|skills?)\b",
    re.IGNORECASE,
)
_DASHES = str.maketrans({char: "-" for char in "\u2010\u2011\u2012\u2013\u2014\u2212"})


def evidence_metadata_warnings(fields: Mapping[str, str]) -> list[str]:
    """Flag recognizable internal markers only in the supplied generated fields."""
    warnings = []
    for field, text in fields.items():
        normalized = unicodedata.normalize("NFKC", text).translate(_DASHES)
        markers = dict.fromkeys(
            match.group().casefold() for match in _EVIDENCE_METADATA.finditer(normalized)
        )
        warnings.extend(f"{EVIDENCE_METADATA_PREFIX}{field}:{marker}" for marker in markers)
    return warnings
