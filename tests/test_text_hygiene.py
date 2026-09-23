"""Internal evidence labels must not be confused with professional vocabulary."""

import pytest

from smartapply.cv.text_hygiene import evidence_metadata_warnings


@pytest.mark.parametrize(
    "text",
    [
        "with verified work on transcription and diarization",
        "with source-grounded experience in hybrid RAG",
        "grounded in profile evidence",
        "evidence-grounded workflows",
        "Profile-grounded contributions to ML",
        "Self-reported contribution to research",
        "self_reported experience in ML",
        "VERIFIED EXPERIENCE with classification",
        "source\u2011grounded experience in RAG",
        "Evidence_level: verified",
        "allowed_claims support these skills",
    ],
)
def test_internal_qualifiers_are_editorial_hints(text):
    fields = {"professional_summary": text}
    warnings = evidence_metadata_warnings(fields)
    assert warnings and all(
        w.startswith("internal_evidence_metadata:professional_summary:") for w in warnings
    )
    assert fields == {"professional_summary": text}


@pytest.mark.parametrize(
    "text",
    [
        "Verified model outputs against reference annotations.",
        "Built a pipeline generating source-grounded answers with citations.",
        "Analyzed self-reported clinical data.",
        "Evaluated evidence-grounded answers in a retrieval system.",
        "Experience building transcription and diarization pipelines.",
        "Contributed to an ongoing research project.",
        "Worked on verification, evaluation and grounding of model outputs.",
        "Unverified work and unverified experience.",
    ],
)
def test_legitimate_terms_and_word_boundaries_are_preserved(text):
    assert evidence_metadata_warnings({"professional_summary": text}) == []
