"""A bounded text review must never reconstruct the approved CV structure."""

from __future__ import annotations

from contextlib import ExitStack
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from smartapply.cv import CvAdapter, CvValidator
from smartapply.cv.assembly import profile_experiences
from smartapply.cv.letter_writer import ApplicationTextWriter
from smartapply.cv.motivation_validator import MotivationLetterValidator
from smartapply.llm import (
    AdaptedCV,
    ApplicationTexts,
    CVSelection,
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
    return "\n\n".join(f"{opening} {' '.join([refrain] * 4)}" for opening in openings)


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


def _issue(field, passage, *, source_ids=None, category="factual", replacement=None):
    return DocumentReviewIssue(
        field=field,
        category=category,
        passage=passage,
        reason="Make the generated text match the cited profile evidence.",
        source_ids=source_ids if source_ids is not None else ["bullet:blt_sillage_data"],
        replacement=replacement,
    )


def _review(*, issues=()):
    return DocumentTextReview(issues=list(issues))


def _run(documents, responses, *, reference_date=None, initial_warnings=()):
    profile, cv, letter, analysis = documents
    provider = _SequentialProvider(responses)
    harness = _ReviewHarness(profile, provider)
    report = ApplyReport(job_id=42, application_id=None)
    report.validation_warnings.extend(initial_warnings)
    with ExitStack() as stack:
        for method in (
            "_ensure_supported_offer_skills",
            "_ensure_summary_skills_visible",
            "_apply_role_family_contract",
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
            reference_date=reference_date,
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
    assert provider.calls[0]["model"] == provider.smart_model
    assert provider.calls[0]["use_cache"] is False
    assert report.document_review[0]["decision"] == "accepted"


def test_accepted_text_corrections_preserve_skills_projects_experiences_and_order(documents):
    profile, cv, letter, _ = documents
    before = profile.model_dump()
    summary = "Data Scientist building Python and SQL data workflows."
    subject = "Application for Data Scientist"
    body = letter.body.replace("dependable evaluation", "reproducible evaluation")
    correction = _review(
        issues=[
            _issue("professional_summary", "Python", replacement="Python and SQL"),
            _issue(
                "motivation_letter_subject",
                " - ",
                source_ids=["document:final_cv"],
                replacement=" for ",
            ),
            _issue(
                "motivation_letter_body",
                "dependable evaluation",
                source_ids=["bullet:proj_evidence_rag_evidence"],
                replacement="reproducible evaluation",
            ),
        ],
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


@pytest.mark.parametrize("confirmation", ["accepted", "unresolved", "unavailable"])
def test_title_correction_requires_confirmation_and_preserves_every_other_field(
    documents, confirmation
):
    profile, cv, letter, analysis = documents
    original = cv.model_copy(update={"cv_title": "Principal Data Scientist"})
    correction = _review(
        issues=[
            _issue(
                "cv_title",
                "Principal ",
                source_ids=["candidate:provided_evidence"],
                replacement="",
            )
        ]
    )
    second = {
        "accepted": _review(),
        "unresolved": _review(
            issues=[_issue("cv_title", cv.cv_title, source_ids=["profile:identity"])]
        ),
        "unavailable": LLMError("Review unavailable"),
    }[confirmation]

    (reviewed_cv, reviewed_letter), report, provider = _run(
        (profile, original, letter, analysis), [correction, second]
    )

    assert len(provider.calls) == 2
    assert '"cv_title": "Principal Data Scientist"' in provider.calls[0]["user"]
    assert '"cv_title": "Data Scientist"' in provider.calls[1]["user"]
    assert "Principal Data Scientist" not in provider.calls[1]["user"]
    assert reviewed_letter == letter
    assert original.cv_title == "Principal Data Scientist"
    if confirmation == "accepted":
        assert reviewed_cv == cv
        assert not report.validation_errors
        assert report.document_review[-1]["decision"] == "accepted"
    else:
        assert reviewed_cv == original
        assert report.validation_errors
        assert any("text_review:factual:cv_title:" in error for error in report.validation_errors)


@pytest.mark.parametrize("failure_stage", ["initial", "recheck"])
def test_provider_failure_keeps_originals_and_prevents_ready_status(documents, failure_stage):
    _, cv, letter, _ = documents
    responses = [LLMError("Sensitive provider context must not be persisted")]
    if failure_stage == "recheck":
        responses.insert(
            0,
            _review(
                issues=[
                    _issue(
                        "professional_summary",
                        cv.professional_summary,
                        replacement="Data Scientist working with Python and SQL.",
                    )
                ],
            ),
        )
    (reviewed_cv, reviewed_letter), report, provider = _run(documents, responses)

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert any(error.startswith("text_review_incomplete") for error in report.validation_errors)
    assert report.document_review[-1]["status"] == "incomplete"
    assert report.document_review[-1]["decision"] == "incomplete"
    assert "Sensitive provider context" not in str(report.document_review)
    assert "Sensitive provider context" not in str(report.validation_errors)
    assert any("text_review:factual:" in error for error in report.validation_errors) is (
        failure_stage == "recheck"
    )
    assert len(provider.calls) == (1 if failure_stage == "initial" else 2)


def test_unresolved_recheck_rolls_back_corrections_without_third_call(documents):
    _, cv, letter, _ = documents
    proposed = "Data Scientist working with Python and SQL."
    correction = _review(
        issues=[_issue("professional_summary", cv.professional_summary, replacement=proposed)],
    )
    unresolved = _review(issues=[_issue("professional_summary", proposed)])

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [correction, unresolved])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "invalid_reason",
    ["absent_passage", "unknown_source", "unknown_aggregate", "empty_sources", "no_reason"],
)
def test_invalid_review_evidence_cannot_authorize_a_correction(documents, invalid_reason):
    _, cv, letter, _ = documents
    issue = _issue(
        "professional_summary",
        cv.professional_summary,
        replacement="Data Scientist working with Python and SQL.",
    )
    if invalid_reason == "absent_passage":
        issue.passage = "A sentence absent from the original summary."
    elif invalid_reason == "unknown_source":
        issue.source_ids = ["invented_profile_source"]
    elif invalid_reason == "unknown_aggregate":
        issue.source_ids = ["candidate:entire_private_profile"]
    elif invalid_reason == "empty_sources":
        issue.source_ids = []
    elif invalid_reason == "no_reason":
        issue.reason = " "
    response = _review(issues=[issue])

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [response])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 1
    assert any(error.startswith("text_review_incomplete") for error in report.validation_errors)
    assert not any(error.startswith("text_review:factual:") for error in report.validation_errors)
    assert report.document_review[-1]["status"] == "incomplete"
    assert report.document_review[-1]["decision"] == "incomplete"


def test_unsupported_claim_can_use_aggregate_scope_without_a_nearby_source(documents):
    profile, cv, letter, analysis = documents
    invented = " I led a 500-person team at Fictional Meridian."
    ungrounded_letter = letter.model_copy(update={"body": letter.body + invented})
    issue = _issue(
        "motivation_letter_body",
        invented,
        source_ids=["candidate:provided_evidence"],
        replacement="",
    )
    issue.reason = (
        "No supplied candidate declaration or factual record supports this leadership claim."
    )

    (revised_cv, revised_letter), report, provider = _run(
        (profile, cv, ungrounded_letter, analysis), [_review(issues=[issue]), _review()]
    )

    assert revised_cv == cv
    assert revised_letter == letter
    assert report.validation_errors == []
    assert report.document_review[0]["review"]["issues"][0]["source_ids"] == [
        "candidate:provided_evidence"
    ]
    assert report.document_review[-1]["decision"] == "accepted"
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "field",
    ["cv_title", "professional_summary", "motivation_letter_subject", "motivation_letter_body"],
)
def test_empty_correction_is_rejected_without_erasing_document_text(documents, field):
    _, cv, letter, _ = documents
    original = {
        "cv_title": cv.cv_title,
        "professional_summary": cv.professional_summary,
        "motivation_letter_subject": letter.subject,
        "motivation_letter_body": letter.body,
    }[field]
    response = _review(issues=[_issue(field, original, replacement="   ")])

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
        issues=[_issue("professional_summary", cv.professional_summary, replacement=proposed)],
    )
    forbidden_second_correction = _review(
        issues=[_issue("professional_summary", proposed, replacement="A third summary.")]
    )

    (reviewed_cv, reviewed_letter), report, provider = _run(
        documents, [first, forbidden_second_correction]
    )

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 2


def test_letter_correction_cannot_introduce_an_unselected_project(documents):
    _, cv, letter, _ = documents
    response = _review(
        issues=[
            _issue(
                "motivation_letter_body",
                "Evidence RAG Verifier",
                source_ids=["document:final_cv"],
                replacement="Geometry Classification",
            )
        ],
    )

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [response])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == letter.model_dump()
    assert report.validation_errors
    assert len(provider.calls) == 1


@pytest.mark.parametrize("field", ["professional_summary", "motivation_letter_body"])
def test_correction_outside_objective_limits_rolls_back_before_confirmation(documents, field):
    profile, cv, letter, _ = documents
    if field == "professional_summary":
        passage = cv.professional_summary
        replacement = "Python data workflows. " * profile.style_guide.max_summary_length
    else:
        passage = letter.body
        replacement = "I worked at Aurore Labs."
    response = _review(issues=[_issue(field, passage, replacement=replacement)])

    (reviewed_cv, reviewed_letter), report, provider = _run(documents, [response])

    expected = "summary_too_long:" if field == "professional_summary" else "letter_too_short:"
    assert any(expected in error for error in report.validation_errors)
    assert len(provider.calls) == 1
    assert reviewed_cv == cv and reviewed_letter == letter


def test_legacy_adapt_application_no_longer_generates_a_joint_application_draft(documents):
    profile, cv, letter, analysis = documents
    personalization = CVSelection.model_validate(
        {
            **cv.model_dump(include=set(CVSelection.model_fields)),
            "selected_certificate_ids": [],
            "project_selection_reasons": [
                {
                    "project_id": project_id,
                    "reason": "Demonstrates documented data workflow experience.",
                    "offer_evidence": "Build reliable data workflows",
                    "complementary_value": "",
                }
                for project_id in cv.selected_project_ids
            ],
        }
    )
    texts = ApplicationTexts(
        professional_summary=cv.professional_summary,
        motivation_letter_subject=letter.subject,
        motivation_letter_body=letter.body,
    )
    provider = _SequentialProvider([personalization, texts, _review()])
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
        CVSelection,
        ApplicationTexts,
        DocumentTextReview,
    ]
    assert all(call["purpose"] != "application_draft" for call in provider.calls)
    assert all(call["refresh_cache"] is True for call in provider.calls)
    assert adapted.cv_title in provider.calls[1]["user"]
    assert adapted.professional_summary not in provider.calls[1]["user"]
    assert adapted.professional_summary in provider.calls[2]["user"]
    assert "professional_summary" not in provider.calls[0]["schema"].model_fields


def test_clean_llm_review_cannot_approve_a_hard_letter_validation_error(documents):
    profile, cv, letter, analysis = documents
    invalid_letter = letter.model_copy(update={"body": f"{letter.body} Привет"})
    invalid_documents = (profile, cv, invalid_letter, analysis)

    (reviewed_cv, reviewed_letter), report, provider = _run(invalid_documents, [_review()])

    assert reviewed_cv.model_dump() == cv.model_dump()
    assert reviewed_letter.model_dump() == invalid_letter.model_dump()
    assert any("foreign_script_in_letter:cyrillic" in error for error in report.validation_errors)
    assert len(provider.calls) == 1


def test_legacy_adapt_application_keeps_documents_and_diagnostics_despite_review_error(documents):
    profile, cv, letter, analysis = documents
    personalization = CVSelection.model_validate(
        {
            **cv.model_dump(include=set(CVSelection.model_fields)),
            "selected_certificate_ids": [],
            "project_selection_reasons": [
                {
                    "project_id": project_id,
                    "reason": "Demonstrates documented data workflow experience.",
                    "offer_evidence": "Build reliable data workflows",
                    "complementary_value": "",
                }
                for project_id in cv.selected_project_ids
            ],
        }
    )
    invalid_letter = letter.model_copy(update={"body": f"{letter.body} Привет"})
    texts = ApplicationTexts(
        professional_summary=cv.professional_summary,
        motivation_letter_subject=invalid_letter.subject,
        motivation_letter_body=invalid_letter.body,
    )
    provider = _SequentialProvider([personalization, texts, _review()])
    adapter = CvAdapter(profile, llm=provider, embeddings=MockEmbeddingsProvider())

    adapted, generated_letter, _ = adapter.adapt_application(
        analysis,
        job_title="Data Scientist",
        job_company="Example",
        language="en",
        job_id=42,
    )

    assert generated_letter == invalid_letter
    assert adapted.professional_summary == cv.professional_summary
    assert "validation_error:foreign_script_in_letter:cyrillic" in adapted.warnings

    assert len(provider.calls) == 3


def test_multiple_local_edits_use_original_offsets_and_preserve_other_text(documents):
    _, cv, letter, _ = documents
    correction = _review(
        issues=[
            _issue("professional_summary", "data workflows", replacement="data systems"),
            _issue("professional_summary", "Python", replacement="Python and SQL"),
            _issue("motivation_letter_subject", "Application - ", replacement="Interest in "),
            _issue(
                "motivation_letter_subject", "Data Scientist", replacement="Data Scientist vacancy"
            ),
        ]
    )

    (revised_cv, revised_letter), report, _ = _run(documents, [correction, _review()])

    assert revised_cv.professional_summary == "Data Scientist building Python and SQL data systems."
    assert revised_letter.subject == "Interest in Data Scientist vacancy"
    assert revised_letter.body == letter.body
    assert report.validation_errors == []
    _assert_unchanged_structure(cv, revised_cv)


@pytest.mark.parametrize("mode", ["ambiguous", "overlapping"])
def test_ambiguous_or_overlapping_patches_never_modify_documents(documents, mode):
    _, cv, letter, _ = documents
    if mode == "ambiguous":
        issues = [
            _issue("motivation_letter_body", "careful analysis", replacement="precise analysis")
        ]
    else:
        issues = [
            _issue("professional_summary", "Python data", replacement="Python workflows"),
            _issue("professional_summary", "data workflows", replacement="data systems"),
        ]

    (revised_cv, revised_letter), report, provider = _run(documents, [_review(issues=issues)])

    assert revised_cv == cv and revised_letter == letter
    assert any(mode in error for error in report.validation_errors)
    assert len(provider.calls) == 1


def test_local_deletion_is_allowed_without_erasing_the_field(documents):
    _, cv, letter, _ = documents
    correction = _review(issues=[_issue("motivation_letter_body", "dependable ", replacement="")])

    (revised_cv, revised_letter), report, _ = _run(documents, [correction, _review()])

    assert revised_letter.body == letter.body.replace("dependable ", "")
    assert revised_letter.subject == letter.subject
    assert revised_cv == cv
    assert report.validation_errors == []


def test_noop_factual_edit_is_unresolved_without_spending_a_confirmation_call(documents):
    _, cv, letter, _ = documents
    correction = _review(
        issues=[
            _issue(
                "professional_summary", cv.professional_summary, replacement=cv.professional_summary
            )
        ]
    )

    (revised_cv, revised_letter), report, provider = _run(documents, [correction])

    assert revised_cv == cv and revised_letter == letter
    assert "text_review_unresolved: relecture nécessaire" in report.validation_errors
    assert len(provider.calls) == 1


def test_unrelated_patch_cannot_resolve_a_blocking_finding_without_a_correction(documents):
    _, cv, letter, _ = documents
    review = _review(
        issues=[
            _issue("professional_summary", cv.professional_summary),
            _issue(
                "motivation_letter_body",
                "dependable evaluation",
                replacement="reproducible evaluation",
                category="style",
            ),
        ]
    )

    (revised_cv, revised_letter), report, provider = _run(documents, [review])

    assert revised_cv == cv and revised_letter == letter
    assert any("text_review:factual:" in error for error in report.validation_errors)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("replacement", ["reproducible evaluation", "Привет", "", "x" * 1000])
def test_style_replacements_are_never_applied_or_confirmed(documents, replacement):
    _, cv, letter, _ = documents
    correction = _review(
        issues=[
            _issue(
                "motivation_letter_body",
                "dependable evaluation",
                category="style",
                source_ids=["style:letter"],
                replacement=replacement,
            )
        ]
    )
    (revised_cv, revised_letter), report, provider = _run(documents, [correction])

    assert revised_cv == cv and revised_letter == letter
    assert report.validation_errors == []
    assert any(
        "text_review:style:motivation_letter_body:" in warning
        for warning in report.validation_warnings
    )
    assert report.document_review[-1]["decision"] == "accepted"
    assert len(provider.calls) == 1


def test_new_valid_factual_finding_cannot_be_downgraded_after_structure_edit(documents):
    _, cv, letter, _ = documents
    correction = _review(
        issues=[
            _issue(
                "motivation_letter_body",
                "dependable evaluation",
                category="structure",
                source_ids=["style:letter"],
                replacement="reproducible evaluation",
            )
        ]
    )
    unresolved = _review(issues=[_issue("professional_summary", cv.professional_summary)])

    (revised_cv, revised_letter), report, _ = _run(documents, [correction, unresolved])

    assert revised_cv == cv and revised_letter == letter
    assert any("text_review:factual:" in error for error in report.validation_errors)
    assert report.document_review[-1]["decision"] == "rejected"


def test_factual_repair_crossing_word_limit_rolls_back_without_confirmation(documents):
    from smartapply.cv.motivation_validator import _word_count

    profile, cv, letter, analysis = documents
    # Construct a synthetic boundary case; the exact count is the program's,
    # not a guessed count returned by the model.
    padding = " ".join(["context"] * (240 - _word_count(letter.body)))
    original = letter.model_copy(update={"body": f"{letter.body} {padding}".rstrip()})
    assert _word_count(original.body) == 240
    correction = _review(
        issues=[
            _issue(
                "motivation_letter_body",
                "dependable evaluation",
                replacement="separately measured evaluation",
            )
        ]
    )

    (_, revised_letter), report, provider = _run((profile, cv, original, analysis), [correction])

    assert revised_letter == original
    assert _word_count(revised_letter.body) == 240
    assert any("letter_too_long:241" in error for error in report.validation_errors)
    assert len(provider.calls) == 1


@pytest.mark.parametrize("field", ["cv_title", "professional_summary", "subject", "body"])
def test_empty_generated_fields_are_blocking_even_with_clean_review(documents, field):
    profile, cv, letter, analysis = documents
    if field in {"cv_title", "professional_summary"}:
        cv = cv.model_copy(update={field: " "})
    else:
        letter = letter.model_copy(update={field: " "})

    _, report, _ = _run((profile, cv, letter, analysis), [_review()])

    assert any("empty_field:" in error for error in report.validation_errors)


def test_reference_date_and_smart_model_are_fixed_across_both_reviews(documents):
    _, cv, _, _ = documents
    correction = _review(
        issues=[_issue("professional_summary", "Python", replacement="Python and SQL")]
    )

    _, report, provider = _run(documents, [correction, _review()], reference_date=date(2031, 7, 14))

    assert all('"date": "2031-07-14"' in call["user"] for call in provider.calls)
    assert len(provider.calls) == 2
    assert all(call["model"] == provider.smart_model for call in provider.calls)
    assert all(trace["model"] == provider.smart_model for trace in report.document_review)
    assert all(trace["reference_date"] == "2031-07-14" for trace in report.document_review)
    assert cv.professional_summary == "Data Scientist building Python data workflows."


@pytest.mark.parametrize(
    "field",
    [
        "cv_title",
        "professional_summary",
        "motivation_letter_subject",
        "motivation_letter_body",
    ],
)
def test_metadata_hints_reach_reviewer_and_survive_a_missed_finding(documents, field):
    profile, cv, letter, analysis = documents
    if field in {"cv_title", "professional_summary"}:
        setattr(cv, field, "Verified experience in data analysis.")
    elif field == "motivation_letter_body":
        letter.body = letter.body.replace("This selected work", "This verified work", 1)
    else:
        setattr(
            letter, field.removeprefix("motivation_letter_"), "Verified experience in analysis."
        )
    before = (cv.model_dump(), letter.model_dump(), profile.model_dump())
    (result_cv, result_letter), report, provider = _run(documents, [_review()])
    prefix = f"internal_evidence_metadata:{field}:"
    assert prefix in provider.calls[0]["user"]
    assert any(warning.startswith(prefix) for warning in report.validation_warnings)
    assert not report.validation_errors  # Editorial warning, not a fabricated factual finding.
    assert (result_cv.model_dump(), result_letter.model_dump(), profile.model_dump()) == before


@pytest.mark.parametrize("replacement", ["experience", "source-grounded experience"])
def test_metadata_style_suggestions_preserve_original_text_and_warning(documents, replacement):
    _, cv, _, _ = documents
    cv.professional_summary = "Data Scientist with verified work in Python data workflows."
    prefix = "internal_evidence_metadata:professional_summary:"
    correction = _review(
        issues=[
            _issue(
                "professional_summary",
                "verified work",
                replacement=replacement,
                category="style",
                source_ids=["style:recruiter_text"],
            )
        ]
    )
    (revised_cv, _), report, provider = _run(
        documents,
        [correction],
        initial_warnings=[prefix + "verified work", "unrelated_warning"],
    )
    assert len(provider.calls) == 1
    assert revised_cv == cv
    final_hints = [w for w in report.validation_warnings if w.startswith(prefix)]
    assert final_hints
    assert all("verified work" in w for w in final_hints)
    assert "unrelated_warning" in report.validation_warnings
    _assert_unchanged_structure(cv, revised_cv)


def test_metadata_warning_remains_when_review_is_unavailable(documents):
    _, cv, _, _ = documents
    cv.professional_summary = "Data Scientist with verified work in Python."
    (revised_cv, _), report, _ = _run(documents, [LLMError("unavailable")])
    assert revised_cv == cv
    assert any(
        w.startswith("internal_evidence_metadata:professional_summary:")
        for w in report.validation_warnings
    )
    assert report.validation_errors


def test_metadata_hints_do_not_scan_or_rewrite_static_experiences(documents):
    profile, cv, _, _ = documents
    profile.experiences[0].bullets[0].text = "Verified work in an approved static source."
    cv.selected_experiences = profile_experiences(profile)
    before = cv.model_dump()
    (revised_cv, _), report, _ = _run(documents, [_review()])
    assert revised_cv.model_dump() == before
    assert not any(w.startswith("internal_evidence_metadata:") for w in report.validation_warnings)


@pytest.mark.parametrize("category", ["factual", "language", "structure"])
def test_mixed_review_applies_only_non_style_corrections_and_keeps_style_diagnostic(
    documents, category
):
    profile, cv, letter, _ = documents
    before_profile = profile.model_dump()
    correction = _review(
        issues=[
            _issue(
                "professional_summary", "Python", replacement="Python and SQL", category=category
            ),
            _issue(
                "motivation_letter_body",
                "dependable evaluation",
                category="style",
                source_ids=["style:letter"],
                replacement="a completely different account",
            ),
        ]
    )

    (revised_cv, revised_letter), report, provider = _run(documents, [correction, _review()])

    assert revised_cv.professional_summary == cv.professional_summary.replace(
        "Python", "Python and SQL"
    )
    assert revised_letter == letter
    assert profile.model_dump() == before_profile
    _assert_unchanged_structure(cv, revised_cv)
    assert not report.validation_errors
    assert any(
        "text_review:style:motivation_letter_body:" in warning
        for warning in report.validation_warnings
    )
    assert "a completely different account" not in provider.calls[1]["user"]
    assert len(provider.calls) == 2


def test_ignored_style_patch_does_not_overlap_an_authorized_patch(documents):
    _, cv, letter, _ = documents
    review = _review(
        issues=[
            _issue("professional_summary", "Python", replacement="Python and SQL"),
            _issue(
                "professional_summary",
                "Python data",
                category="style",
                source_ids=["style:summary"],
                replacement="a completely different stack",
            ),
        ]
    )

    (revised_cv, revised_letter), report, provider = _run(documents, [review, _review()])

    assert revised_cv.professional_summary == cv.professional_summary.replace(
        "Python", "Python and SQL"
    )
    assert revised_letter == letter
    assert not report.validation_errors
    assert any(
        "text_review:style:professional_summary:" in warning
        for warning in report.validation_warnings
    )
    assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "violation",
    [
        "summary_too_long",
        "summary_too_many_lines",
        "letter_too_short",
        "letter_too_long",
        "letter_not_3_paragraphs",
    ],
)
def test_clean_llm_review_cannot_approve_an_initial_objective_limit_violation(documents, violation):
    profile, cv, letter, analysis = documents
    if violation == "summary_too_long":
        cv = cv.model_copy(
            update={"professional_summary": "x" * (profile.style_guide.max_summary_length + 1)}
        )
    elif violation == "summary_too_many_lines":
        cv = cv.model_copy(
            update={
                "professional_summary": "Data analysis.\nClinical signals.\nReliable workflows."
            }
        )
    elif violation in {"letter_too_short", "letter_too_long"}:
        count = 179 if violation == "letter_too_short" else 241
        letter = letter.model_copy(
            update={
                "body": "\n\n".join(
                    [
                        " ".join(["analysis"] * 60),
                        " ".join(["analysis"] * 60),
                        " ".join(["analysis"] * (count - 120)),
                    ]
                )
            }
        )
    else:
        letter = letter.model_copy(update={"body": letter.body.replace("\n\n", " ")})

    (revised_cv, revised_letter), report, provider = _run(
        (profile, cv, letter, analysis), [_review()]
    )

    assert revised_cv == cv and revised_letter == letter
    assert any(violation + ":" in error for error in report.validation_errors)
    assert report.document_review[-1]["decision"] != "accepted"
    assert len(provider.calls) == 1


@pytest.mark.parametrize("letter_words", [180, 240])
def test_exact_objective_boundaries_remain_acceptable_without_truncation(documents, letter_words):
    from smartapply.cv.motivation_validator import _word_count

    profile, cv, letter, analysis = documents
    summary = "x" * profile.style_guide.max_summary_length
    cv = cv.model_copy(update={"professional_summary": summary})
    letter = letter.model_copy(
        update={
            "body": "\n\n".join(
                [
                    " ".join(["analysis"] * 60),
                    " ".join(["analysis"] * 60),
                    " ".join(["analysis"] * (letter_words - 120)),
                ]
            )
        }
    )
    assert _word_count(letter.body) == letter_words

    (revised_cv, revised_letter), report, provider = _run(
        (profile, cv, letter, analysis), [_review()]
    )

    assert revised_cv == cv and revised_letter == letter
    assert revised_cv.professional_summary == summary
    assert not report.validation_errors
    assert len(provider.calls) == 1


def test_structure_repair_can_resolve_an_initial_objective_violation(documents):
    from smartapply.cv.motivation_validator import _word_count

    profile, cv, letter, analysis = documents
    original = letter.model_copy(update={"body": letter.body + " extra" * 100})
    assert _word_count(original.body) > 240
    correction = _review(
        issues=[
            _issue(
                "motivation_letter_body",
                original.body,
                category="structure",
                source_ids=["style:letter"],
                replacement=letter.body,
            )
        ]
    )

    (revised_cv, revised_letter), report, provider = _run(
        (profile, cv, original, analysis), [correction, _review()]
    )

    assert revised_cv == cv and revised_letter == letter
    assert original.body.endswith(" extra" * 100)
    assert not report.validation_errors
    assert len(provider.calls) == 2


def test_invalid_confirmation_retains_only_previously_valid_factual_findings(documents):
    _, cv, letter, _ = documents
    correction = _review(
        issues=[_issue("professional_summary", "Python", replacement="Python and SQL")]
    )
    invalid_confirmation = _review(
        issues=[_issue("cv_title", cv.cv_title, source_ids=["selected_project_evidence"])]
    )

    (revised_cv, revised_letter), report, provider = _run(
        documents, [correction, invalid_confirmation]
    )

    assert revised_cv == cv and revised_letter == letter
    assert any(error.startswith("text_review_incomplete") for error in report.validation_errors)
    assert any("text_review_invalid_source:cv_title" in error for error in report.validation_errors)
    assert any(
        "text_review:factual:professional_summary:" in error for error in report.validation_errors
    )
    assert not any("text_review:factual:cv_title:" in error for error in report.validation_errors)
    assert report.document_review[-1]["status"] == "incomplete"
    assert report.document_review[-1]["decision"] == "incomplete"
    assert len(provider.calls) == 2


def test_initial_invalid_structure_citation_is_incomplete_not_a_factual_finding(documents):
    _, cv, letter, _ = documents
    invalid = _review(
        issues=[
            _issue(
                "motivation_letter_body",
                "dependable evaluation",
                category="structure",
                source_ids=["selected_project_evidence"],
                replacement="reproducible evaluation",
            )
        ]
    )

    (revised_cv, revised_letter), report, provider = _run(documents, [invalid])

    assert revised_cv == cv and revised_letter == letter
    assert any(error.startswith("text_review_incomplete") for error in report.validation_errors)
    assert "text_review_invalid_source:motivation_letter_body" in report.validation_errors
    assert not any(error.startswith("text_review:factual:") for error in report.validation_errors)
    assert report.document_review[-1]["status"] == "incomplete"
    assert report.document_review[-1]["decision"] == "incomplete"
    assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "field", ["selected_skills", "selected_project_ids", "selected_experiences"]
)
def test_review_schema_rejects_corrections_to_frozen_cv_fields(field):
    payload = _issue("professional_summary", "Python", replacement="SQL").model_dump()
    payload["field"] = field
    with pytest.raises(ValidationError):
        DocumentTextReview.model_validate({"issues": [payload]})


@pytest.mark.parametrize(
    "field", ["selected_skills", "selected_project_ids", "selected_experiences"]
)
def test_proposed_patch_cannot_change_frozen_nested_cv_fields(documents, field):
    _, cv, letter, _ = documents
    original_snapshot = cv.model_dump()
    correction = _review(
        issues=[_issue("professional_summary", "Python", replacement="Python and SQL")]
    )
    apply_edits = ApplicationTextWriter._apply_edits

    def corrupt_proposed_copy(writer, review, original_cv, original_letter):
        proposed_cv, proposed_letter = apply_edits(writer, review, original_cv, original_letter)
        if field == "selected_skills":
            proposed_cv.selected_skills[0].skills.reverse()
        elif field == "selected_project_ids":
            proposed_cv.selected_project_ids.reverse()
        else:
            proposed_cv.selected_experiences[0].bullets[0].text = "Changed static experience."
        return proposed_cv, proposed_letter

    with patch.object(ApplicationTextWriter, "_apply_edits", corrupt_proposed_copy):
        (revised_cv, revised_letter), report, provider = _run(documents, [correction])

    assert cv.model_dump() == original_snapshot
    assert revised_cv.model_dump() == original_snapshot
    assert revised_letter == letter
    assert any("text_review_frozen_cv_changed" in error for error in report.validation_errors)
    assert len(provider.calls) == 1
