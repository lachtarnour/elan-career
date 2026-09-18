"""A bounded text review must never reconstruct the approved CV structure."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from smartapply.cv import CvAdapter, CvValidator
from smartapply.cv.assembly import profile_experiences
from smartapply.cv.motivation_validator import MotivationLetterValidator
from smartapply.llm import (
    AdaptedCV,
    CVPersonalization,
    JobAnalysis,
    LLMError,
    MockLLMProvider,
    MotivationLetter,
    SkillSelectionBlock,
)
from smartapply.llm.schemas import DocumentReviewIssue, DocumentTextReview
from smartapply.pipeline.apply.cv_writer import CvWriterMixin
from smartapply.pipeline.reports import ApplyReport
from smartapply.profile import load_profile
from smartapply.ranking import MockEmbeddingsProvider


class _SequentialProvider(MockLLMProvider):
    def __init__(self, responses):
        super().__init__({})
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("Review exceeded its bounded number of model calls")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, kwargs["schema"])
        return response


class _ReviewHarness(CvWriterMixin):
    def __init__(self, profile, provider):
        self.profile = profile
        self.llm = provider
        self.validator = CvValidator(profile)
        self.letter_validator = MotivationLetterValidator(profile)
        forbidden = Mock(side_effect=AssertionError("Text review must not rebuild selections"))
        self.adapter = SimpleNamespace(
            _ensure_supported_offer_skills=forbidden,
            _ensure_summary_skills_visible=forbidden,
            _apply_role_family_contract=forbidden,
            _ensure_letter_projects_visible=forbidden,
            _enforce_project_count=forbidden,
        )
        self.validator.auto_fix = Mock(
            side_effect=AssertionError("Text review must reject, not auto-fix CV structure")
        )


def _body():
    refrain = (
        "I value careful analysis, clear communication, reliable delivery, and "
        "thoughtful collaboration."
    )
    openings = (
        "The Evidence RAG Verifier project strengthened my approach to grounded "
        "retrieval, evidence review, and dependable evaluation.",
        "This selected work reflects a practical way of connecting technical "
        "decisions with clear product priorities and team needs.",
        "For this role, I would bring the same structured approach, attention "
        "to quality, and focus on useful outcomes.",
    )
    return "\n\n".join(f"{opening} {' '.join([refrain] * 6)}" for opening in openings)


@pytest.fixture
def documents():
    profile = load_profile(Path(__file__).parent / "fixtures" / "profile")
    cv = AdaptedCV(
        cv_title="Data Scientist",
        professional_summary="Data Scientist building Python data workflows.",
        selected_experiences=profile_experiences(profile),
        selected_project_ids=["proj_evidence_rag", "proj_ner_camembert", "proj_smartapply"],
        skills_profile_id="mixed",
        selected_skills=[
            SkillSelectionBlock(category_id="data_analysis", skills=["Python", "SQL"]),
            SkillSelectionBlock(category_id="ml_ai", skills=["PyTorch", "NLP"]),
            SkillSelectionBlock(category_id="data_infra", skills=["Git", "Docker"]),
        ],
        skills_order=["data_analysis", "ml_ai", "data_infra"],
        warnings=["Existing editorial warning"],
    )
    letter = MotivationLetter(subject="Application - Data Scientist", body=_body())
    analysis = JobAnalysis(
        role_type="Data Scientist",
        seniority="mid",
        domain="Technology",
        main_tasks=["Build reliable data workflows"],
        required_skills=["Python", "SQL"],
        nice_to_have=[],
        match_reasons=[],
        risks=[],
        cv_keywords_to_include=["data workflows"],
        offer_language="en",
    )
    assert CvValidator(profile).validate(cv).ok
    result = MotivationLetterValidator(profile).validate(letter, cv=cv, analysis=analysis)
    assert result.ok and not result.warnings
    return profile, cv, letter, analysis


def _issue(field, passage, *, source_ids=None):
    return DocumentReviewIssue(
        field=field,
        passage=passage,
        reason="Make the generated text match the cited profile evidence.",
        source_ids=source_ids if source_ids is not None else ["bullet:blt_sillage_data"],
    )


def _review(*, issues=(), **corrections):
    return DocumentTextReview(
        issues=list(issues),
        professional_summary=corrections.get("professional_summary"),
        motivation_letter_subject=corrections.get("motivation_letter_subject"),
        motivation_letter_body=corrections.get("motivation_letter_body"),
    )


def _run(documents, responses):
    profile, cv, letter, analysis = documents
    provider = _SequentialProvider(responses)
    harness = _ReviewHarness(profile, provider)
    report = ApplyReport(job_id=42, application_id=None)
    with ExitStack() as stack:
        for method in (
            "_ensure_supported_offer_skills",
            "_ensure_summary_skills_visible",
            "_apply_role_family_contract",
            "_ensure_letter_projects_visible",
            "_enforce_project_count",
        ):
            stack.enter_context(
                patch.object(
                    CvAdapter,
                    method,
                    side_effect=AssertionError("Text review must not rebuild selections"),
                )
            )
        stack.enter_context(
            patch.object(
                CvValidator,
                "auto_fix",
                side_effect=AssertionError("Text review must not auto-fix CV structure"),
            )
        )
        result = harness._review_generated_text(
            cv,
            letter,
            analysis,
            job_title="Data Scientist",
            job_company="Example",
            job_description="Build reliable Python and SQL workflows for research teams.",
            language="en",
            job_id=42,
            report=report,
            refresh_cache=True,
        )
    return result, report, provider


def _assert_unchanged_structure(before, after):
    # Compare every field except the one explicitly editable CV field. This also
    # protects category order, skill order, IDs, approved bullets and warnings.
    assert after.model_dump(exclude={"professional_summary"}) == before.model_dump(
        exclude={"professional_summary"}
    )


def test_clean_review_preserves_all_documents_and_profile_exactly(documents):
    profile, cv, letter, _ = documents
    before = profile.model_dump()
    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [_review()])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert profile.model_dump() == before
    assert report.validation_errors == []
    assert len(provider.calls) == 1
    assert provider.calls[0]["refresh_cache"] is True
    assert provider.calls[0]["job_id"] == 42


def test_accepted_text_corrections_preserve_skills_projects_experiences_and_order(documents):
    profile, cv, letter, _ = documents
    before = profile.model_dump()
    summary = "Data Scientist building Python and SQL data workflows."
    subject = "Application for Data Scientist"
    body = letter.body.replace("dependable evaluation", "reproducible evaluation")
    correction = _review(
        issues=[
            _issue("professional_summary", cv.professional_summary),
            _issue("motivation_letter_subject", letter.subject, source_ids=["document:final_cv"]),
            _issue(
                "motivation_letter_body",
                "dependable evaluation",
                source_ids=["bullet:proj_evidence_rag_evidence"],
            ),
        ],
        professional_summary=summary,
        motivation_letter_subject=subject,
        motivation_letter_body=body,
    )

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [correction, _review()])

    assert reviewed_cv.professional_summary == summary
    assert reviewed_letter == MotivationLetter(subject=subject, body=body)
    _assert_unchanged_structure(cv, reviewed_cv)
    assert cv.professional_summary == "Data Scientist building Python data workflows."
    assert letter.body == _body()
    assert profile.model_dump() == before
    assert report.validation_errors == []
    assert len(provider.calls) == 2
    assert summary in provider.calls[1]["user"]
    assert (
        body in provider.calls[1]["user"] or "reproducible evaluation" in provider.calls[1]["user"]
    )


@pytest.mark.parametrize("failure_stage", ["initial", "recheck"])
def test_provider_failure_keeps_originals_and_prevents_ready_status(documents, failure_stage):
    _, cv, letter, _ = documents
    responses = [LLMError("Review unavailable")]
    if failure_stage == "recheck":
        responses.insert(
            0,
            _review(
                issues=[_issue("professional_summary", cv.professional_summary)],
                professional_summary="Data Scientist working with Python and SQL.",
            ),
        )
    (reviewed_cv, reviewed_letter), report, provider = _run(documents, responses)

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == (1 if failure_stage == "initial" else 2)


def test_unresolved_recheck_rolls_back_corrections_without_third_call(documents):
    _, cv, letter, _ = documents
    proposed = "Data Scientist working with Python and SQL."
    correction = _review(
        issues=[_issue("professional_summary", cv.professional_summary)],
        professional_summary=proposed,
    )
    unresolved = _review(issues=[_issue("professional_summary", proposed)])

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [correction, unresolved])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 2


@pytest.mark.parametrize("invalid_reason", ["absent_passage", "unknown_source", "no_issue"])
def test_invalid_review_evidence_cannot_authorize_a_correction(documents, invalid_reason):
    _, cv, letter, _ = documents
    issue = _issue("professional_summary", cv.professional_summary)
    if invalid_reason == "absent_passage":
        issue.passage = "A sentence absent from the original summary."
    elif invalid_reason == "unknown_source":
        issue.source_ids = ["invented_profile_source"]
    response = _review(
        issues=[] if invalid_reason == "no_issue" else [issue],
        professional_summary="Data Scientist working with Python and SQL.",
    )

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [response])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "field", ["professional_summary", "motivation_letter_subject", "motivation_letter_body"]
)
def test_empty_correction_is_rejected_without_erasing_document_text(documents, field):
    _, cv, letter, _ = documents
    original = {
        "professional_summary": cv.professional_summary,
        "motivation_letter_subject": letter.subject,
        "motivation_letter_body": letter.body,
    }[field]
    response = _review(issues=[_issue(field, original)], **{field: "   "})

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [response])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 1


def test_issue_without_correction_is_retained_as_blocking_error(documents):
    _, cv, letter, _ = documents
    response = _review(issues=[_issue("professional_summary", cv.professional_summary)])

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [response])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 1


def test_recheck_cannot_start_another_correction_cycle(documents):
    _, cv, letter, _ = documents
    proposed = "Data Scientist working with Python and SQL."
    first = _review(
        issues=[_issue("professional_summary", cv.professional_summary)],
        professional_summary=proposed,
    )
    forbidden_second_correction = _review(professional_summary="A third summary.")

    (reviewed_cv, reviewed_letter), report, provider = _run(
        documents, [first, forbidden_second_correction]
    )

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 2


def test_letter_correction_cannot_introduce_an_unselected_project(documents):
    _, cv, letter, _ = documents
    bad_body = letter.body.replace("Evidence RAG Verifier", "Geometry Classification")
    response = _review(
        issues=[
            _issue(
                "motivation_letter_body",
                "Evidence RAG Verifier",
                source_ids=["document:final_cv"],
            )
        ],
        motivation_letter_body=bad_body,
    )

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [response])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 1


@pytest.mark.parametrize("field", ["professional_summary", "motivation_letter_body"])
def test_correction_that_breaks_document_limits_is_rejected_without_rewriting(documents, field):
    profile, cv, letter, _ = documents
    if field == "professional_summary":
        passage = cv.professional_summary
        replacement = "Python data workflows. " * profile.style_guide.max_summary_length
    else:
        passage = letter.body
        replacement = "I worked at Aurore Labs."
    response = _review(issues=[_issue(field, passage)], **{field: replacement})

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [response])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 1


def test_legacy_adapt_application_no_longer_generates_a_joint_application_draft(documents):
    profile, cv, letter, analysis = documents
    personalization = CVPersonalization.model_validate(
        cv.model_dump(exclude={"selected_experiences"})
    )
    provider = _SequentialProvider([personalization, letter, _review()])
    adapter = CvAdapter(profile, llm=provider, embeddings=MockEmbeddingsProvider())

    adapted, generated_letter, _ = adapter.adapt_application(
        analysis,
        job_title="Data Scientist",
        job_company="Example",
        language="en",
        job_id=42,
        refresh_cache=True,
    )

    assert generated_letter == letter
    assert adapted.selected_experiences == profile_experiences(profile)
    assert len(provider.calls) == 3
    assert [call["schema"] for call in provider.calls] == [
        CVPersonalization,
        MotivationLetter,
        DocumentTextReview,
    ]
    assert all(call["purpose"] != "application_draft" for call in provider.calls)
    assert all(call["refresh_cache"] is True for call in provider.calls)
    assert adapted.cv_title in provider.calls[1]["user"]
    assert adapted.professional_summary in provider.calls[1]["user"]


def test_clean_llm_review_cannot_approve_a_hard_letter_validation_error(documents):
    profile, cv, letter, analysis = documents
    invalid_letter = letter.model_copy(update={"body": f"{letter.body} Привет"})
    invalid_documents = (profile, cv, invalid_letter, analysis)

    (reviewed_cv, reviewed_letter), report, provider = _run(invalid_documents, [_review()])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == invalid_letter.model_dump()
    assert any("foreign_script_in_letter:cyrillic" in error for error in report.validation_errors)
    assert len(provider.calls) == 1


def test_legacy_adapt_application_rejects_hard_error_despite_clean_llm_review(documents):
    profile, cv, letter, analysis = documents
    personalization = CVPersonalization.model_validate(
        cv.model_dump(exclude={"selected_experiences"})
    )
    invalid_letter = letter.model_copy(update={"body": f"{letter.body} Привет"})
    provider = _SequentialProvider([personalization, invalid_letter, _review()])
    adapter = CvAdapter(profile, llm=provider, embeddings=MockEmbeddingsProvider())

    with pytest.raises(LLMError, match="foreign_script_in_letter:cyrillic"):
        adapter.adapt_application(
            analysis,
            job_title="Data Scientist",
            job_company="Example",
            language="en",
            job_id=42,
        )

    assert len(provider.calls) == 3
