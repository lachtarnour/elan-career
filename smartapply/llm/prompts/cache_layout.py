"""Locate reusable candidate prefixes without changing canonical prompt text.

Only known layouts are eligible. Unknown or ambiguous historical requests keep
their original single user block and the existing system-only cache boundary.
"""

from __future__ import annotations

import json

_TEXT_BOUNDARIES = {
    "JobFitAssessment": "\n=== IMMUTABLE OFFER ANALYSIS JSON ===\n",
    "JobFitDecisionEvaluation": "\n=== IMMUTABLE OFFER ANALYSIS JSON ===\n",
    "CVSelection": "\n=== JOB ===\n",
}
_DOCUMENT_HEADERS = {
    "ApplicationTexts": "OPPORTUNITY CONTEXT AND READ-ONLY CANDIDATE SOURCES\n",
    "DocumentTextReview": "CURRENT CV SNAPSHOT, EDIT BOUNDARIES AND IDENTIFIED SOURCES\n",
}
_STABLE_DOCUMENT_FIELDS = {
    "candidate_identity",
    "declared_skills",
    "languages",
    "education",
    "certificates",
    "recruiter_text_style",
    "title_style",
    "summary_style",
}


def _skip_space(text: str, offset: int) -> int:
    while offset < len(text) and text[offset] in " \t\r\n":
        offset += 1
    return offset


def _document_prefix_end(user: str, header: str) -> int | None:
    if user.count(header) != 1:
        return None
    start = _skip_space(user, user.index(header) + len(header))
    decoder = json.JSONDecoder()
    try:
        context, _ = decoder.raw_decode(user, start)
        if not isinstance(context, dict) or next(iter(context), None) != "candidate_identity":
            return None
        offset = start + 1  # Opening object brace, already validated by the decoder.
        seen = set()
        while True:
            field_start = _skip_space(user, offset)
            key, offset = decoder.raw_decode(user, field_start)
            if not isinstance(key, str) or key in seen:
                return None
            if key not in _STABLE_DOCUMENT_FIELDS:
                # Selection-scoped evidence, letter language/target role, dates,
                # offers and generated documents must all remain in the suffix.
                return field_start
            seen.add(key)
            offset = _skip_space(user, offset)
            if user[offset] != ":":
                return None
            offset = _skip_space(user, offset + 1)
            _, offset = decoder.raw_decode(user, offset)
            offset = _skip_space(user, offset)
            if user[offset] != ",":
                return None  # No variable suffix: keep the original user block.
            offset += 1
    except (ValueError, IndexError):
        return None


def candidate_cache_prefix_end(user: str, *, schema_name: str) -> int | None:
    """Return a safe split offset in the original text, or keep its old layout.

    JSON is decoded only to locate top-level fields: source strings may contain
    lookalike markers, and reserializing would change frozen request bytes.
    """
    boundary = _TEXT_BOUNDARIES.get(schema_name)
    if boundary is not None:
        if user.count(boundary) != 1:
            return None
        offset = user.index(boundary)
        return offset if user[:offset].strip() and user[offset:].strip() else None
    header = _DOCUMENT_HEADERS.get(schema_name)
    return _document_prefix_end(user, header) if header is not None else None
