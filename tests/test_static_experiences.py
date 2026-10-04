"""Experiences are approved profile content, never an LLM output field."""

from pathlib import Path
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from docx import Document
from pydantic import ValidationError

from smartapply.cv import CvAdapter, CvDocxRenderer, CvValidator, HtmlApplicationRenderer
from smartapply.cv.assembly import assemble_cv
from smartapply.cv.letter_writer import ApplicationTextWriter
from smartapply.cv.motivation_validator import _paragraph_count, _word_count
from smartapply.llm import (
    AdaptedCV,
    ApplicationDraft,
    ApplicationTexts,
    AssembledCV,
    CVPersonalization,
    CVSelection,
    DocumentTextReview,
    JobAnalysis,
    MockLLMProvider,
)
from smartapply.profile import BulletLink, get_profile
from smartapply.ranking import MockEmbeddingsProvider


def _personalization() -> dict:
    return {
        "cv_title": "Data Scientist",
        "professional_summary": "Data Scientist working on model evaluation and data analysis.",
        "selected_project_ids": ["proj_evidence_rag", "proj_ner_camembert", "proj_smartapply"],
        "project_selection_reasons": [
            {
                "project_id": project_id,
                "reason": "Demonstrates model evaluation on labeled data.",
                "offer_evidence": "Analyze data",
                "complementary_value": "",
            }
            for project_id in ("proj_evidence_rag", "proj_ner_camembert", "proj_smartapply")
        ],
        "skills_profile_id": "mixed",
        "selected_skills": [],
        "skills_order": [],
        "warnings": [],
    }


def _payload(schema: type[CVSelection]) -> dict:
    data = _personalization()
    if schema is CVSelection:
        data.pop("professional_summary")
        data["selected_certificate_ids"] = []
    if schema is ApplicationDraft:
        data.update(
            motivation_letter_subject="Application — Data Scientist",
            motivation_letter_body="I would like to discuss the role and its data priorities.",
        )
    return data


def _expected(profile) -> list[dict]:
    return [
        {
            "source_id": exp.id,
            "bullets": [{"source_id": bullet.id, "text": bullet.text} for bullet in exp.bullets],
        }
        for exp in profile.experiences
    ]


def _letter_body() -> str:
    paragraphs = (
        "I am interested in this role because it connects careful data analysis with practical "
        "decisions. The opportunity to understand a research question, prepare the available "
        "data and explain the results clearly matches the kind of work I want to continue. "
        "I would enjoy contributing to a team that values reliable evaluation and useful outcomes.",
        "At Aurore Labs, I developed model evaluation workflows and worked with data requiring "
        "thoughtful preparation. Those experiences taught me to connect technical choices with "
        "the purpose of an analysis and to make limitations understandable. I would bring "
        "that approach to the responsibilities described in this position, while taking time "
        "to understand the team's existing methods and expectations.",
        "I would welcome the chance to discuss how my background could support your projects. "
        "Working with colleagues on meaningful questions, sharing findings clearly and improving "
        "the quality of an analysis are especially appealing to me. I am motivated by the "
        "opportunity to contribute useful work and continue developing my skills through "
        "collaboration with people who bring different perspectives. I also appreciate opportunities "
        "to compare approaches and explain the reasoning behind technical decisions.",
    )
    body = "\n\n".join(paragraphs)
    assert 180 <= _word_count(body) <= 240
    assert _paragraph_count(body) == 3
    return body


@pytest.mark.parametrize("schema", [CVSelection, CVPersonalization, ApplicationDraft])
def test_llm_schemas_neither_request_nor_accept_experiences(schema) -> None:
    json_schema = schema.model_json_schema()
    assert "selected_experiences" not in json_schema["properties"]
    assert "AdaptedExperience" not in json_schema.get("$defs", {})
    assert json_schema["additionalProperties"] is False
    assert schema.model_validate(_payload(schema))
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        schema.model_validate({**_payload(schema), "selected_experiences": []})


class _RecordingProvider(MockLLMProvider):
    def __init__(self, *, summary: str | None = None):
        personalization = _personalization()
        personalization["selected_certificate_ids"] = []
        if summary is not None:
            personalization["professional_summary"] = summary
        super().__init__(
            {
                "cv_adaptation": CVSelection.model_validate(
                    {
                        key: value
                        for key, value in personalization.items()
                        if key != "professional_summary"
                    }
                ),
                "application_texts": ApplicationTexts(
                    professional_summary=personalization["professional_summary"],
                    motivation_letter_subject="Application — Data Scientist",
                    motivation_letter_body=_letter_body(),
                ),
                "document_review": DocumentTextReview(
                    issues=[],
                ),
            }
        )
        self.calls: list[dict] = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        return super().complete_json(**kwargs)


@pytest.mark.parametrize("method", ["adapt", "adapt_application"])
def test_different_offers_keep_identical_complete_experiences(method) -> None:
    profile = get_profile().model_copy(deep=True)
    before = profile.model_dump()
    provider = _RecordingProvider()
    adapter = CvAdapter(profile, llm=provider, embeddings=MockEmbeddingsProvider())
    results = []
    for role, skill in [("Data Scientist NLP", "RAG"), ("Data Analyst", "SQL")]:
        analysis = JobAnalysis(
            role_type=role,
            seniority="mid",
            domain="Data",
            main_tasks=["Analyze data"],
            required_skills=[skill],
            nice_to_have=[],
            match_reasons=[],
            risks=[],
            cv_keywords_to_include=[skill],
        )
        cv, *_ = getattr(adapter, method)(analysis, job_title=role, job_company="Example")
        results.append(cv)
        if method == "adapt":
            assert type(cv) is AssembledCV
            assert "professional_summary" not in cv.model_dump()
        assert cv.model_dump()["selected_experiences"] == _expected(profile)
        assert CvValidator(profile).validate(cv).ok
    for call in provider.calls:
        assert (
            call["schema"]
            is {
                "cv_adaptation": CVSelection,
                "application_texts": ApplicationTexts,
                "document_review": DocumentTextReview,
            }[call["purpose"]]
        )
        assert "selected_experiences" not in call["schema"].model_fields
        if call["purpose"] == "cv_adaptation":
            assert "Experiences are read-only context" in call["user"]
        assert "Rephrase only when faithful" not in call["user"]
        for exp in profile.experiences:
            for bullet in exp.bullets:
                assert bullet.text in call["user"]
    assert len(provider.calls) == (2 if method == "adapt" else 6)
    assert all(call["purpose"] != "application_draft" for call in provider.calls)
    # Neither post-processing nor a later edit to a snapshot can alter the profile.
    results[0].selected_experiences[0].bullets[0].text = "Changed snapshot"
    assert results[1].model_dump()["selected_experiences"] == _expected(profile)
    assert profile.model_dump() == before


@pytest.mark.parametrize("separator", [". ", "; ", ", ", " "])
def test_overlong_second_call_summary_is_returned_intact_with_review_diagnostics(separator) -> None:
    profile = get_profile().model_copy(deep=True)
    profile.style_guide.max_summary_length = 180
    profile_before = profile.model_dump()
    critical_tail = (
        "with validation against clinical assessments to support longitudinal follow-up."
    )
    summary = (
        "Data Scientist analyzing clinical time series and evaluating machine learning models "
        "against documented reference datasets"
        + separator
        + "Developed data preparation and model comparison pipelines "
        "for heterogeneous health observations, " + critical_tail
    )
    # The contribution at the end must survive every former cutting boundary.
    assert summary.index(critical_tail) > profile.style_guide.max_summary_length
    provider = _RecordingProvider(summary=summary)
    adapter = CvAdapter(profile, llm=provider, embeddings=MockEmbeddingsProvider())
    analysis = JobAnalysis(
        role_type="Data Scientist",
        seniority="mid",
        domain="Data",
        main_tasks=["Analyze data"],
        required_skills=["Python"],
        nice_to_have=[],
        match_reasons=[],
        risks=[],
        cv_keywords_to_include=["Python"],
    )

    expected_warning = f"summary_too_long:len={len(summary)}"
    reviewed = []
    actual_review = ApplicationTextWriter.review

    def capture_review(writer, *args, **kwargs):
        result = actual_review(writer, *args, **kwargs)
        reviewed.append(result)
        return result

    with patch.object(ApplicationTextWriter, "review", capture_review):
        cv, _, _ = adapter.adapt_application(
            analysis, job_title="Data Scientist", job_company="Example"
        )

    assert len(reviewed) == 1
    assert cv.professional_summary == summary
    assert f"validation_error:{expected_warning}" in cv.warnings
    assert not any(warning.startswith("summary_trimmed") for warning in cv.warnings)
    assert cv.model_dump()["selected_experiences"] == _expected(profile)
    assert profile.model_dump() == profile_before
    assert any(expected_warning in error for error in reviewed[0].errors)
    assert reviewed[0].attempts[-1]["decision"] == "rejected"
    validation = CvValidator(profile).validate(cv)
    assert validation.ok
    assert expected_warning in validation.warnings
    assert summary not in provider.calls[0]["user"]
    assert summary not in provider.calls[1]["user"]
    review_calls = [call for call in provider.calls if call["purpose"] == "document_review"]
    assert len(review_calls) == 1
    assert summary in review_calls[0]["user"]
    assert expected_warning in review_calls[0]["user"]


@pytest.mark.parametrize(
    "change",
    [
        "rewrite",
        "missing_experience",
        "duplicate_experience",
        "experience_order",
        "missing_bullet",
        "duplicate_bullet",
        "bullet_order",
        "wrong_parent",
    ],
)
def test_validator_rejects_and_restores_altered_experiences(change) -> None:
    profile = get_profile()
    cv = assemble_cv(CVPersonalization(**_personalization()), profile)
    experiences = cv.selected_experiences
    bullets = experiences[0].bullets
    if change == "rewrite":
        bullets[0].text += " Revised."
    elif change == "missing_experience":
        experiences.pop()
    elif change == "duplicate_experience":
        experiences.append(experiences[0].model_copy(deep=True))
    elif change == "experience_order":
        experiences.reverse()
    elif change == "missing_bullet":
        bullets.pop()
    elif change == "duplicate_bullet":
        bullets.append(bullets[0].model_copy(deep=True))
    elif change == "bullet_order":
        bullets.reverse()
    elif change == "wrong_parent":
        bullets[0] = experiences[1].bullets[0].model_copy(deep=True)
    validator = CvValidator(profile)
    assert not validator.validate(cv).ok
    fixed, corrections = validator.auto_fix(cv)
    assert corrections == ["experience_content:restored_from_profile"]
    assert fixed.model_dump()["selected_experiences"] == _expected(profile)
    assert validator.validate(fixed).ok
    assert validator.auto_fix(fixed)[1] == []


def test_long_approved_text_and_links_survive_assembly_html_and_docx(tmp_path: Path) -> None:
    profile = get_profile().model_copy(deep=True)
    bullet = profile.experiences[0].bullets[0]
    bullet.text = (
        "Evaluated a multimodal model against a documented reference dataset, "
        "preserving the study context, quantitative results and evaluation methodology. "
        "The approved description deliberately includes all of this context, "
        "even when it exceeds the generated-text length limit."
    )
    bullet.links = [BulletLink(anchor="reference dataset", url="https://example.com/study")]
    assert len(bullet.text) > profile.style_guide.max_bullet_length
    cv = assemble_cv(CVPersonalization(**_personalization()), profile)
    validation = CvValidator(profile).validate(cv)
    assert validation.ok
    assert not any("bullet_too_long" in warning for warning in validation.warnings)

    html = HtmlApplicationRenderer(profile).render_cv_html(cv)
    soup = BeautifulSoup(html, "html.parser")
    section = soup.find("h2", string="Professional Experience").parent
    expected_texts = [b.text for exp in profile.experiences for b in exp.bullets]
    assert [li.get_text() for li in section.find_all("li")] == expected_texts
    assert section.find("a", href="https://example.com/study").get_text() == "reference dataset"
    for exp in profile.experiences:
        for value in (exp.title, exp.company, exp.location, exp.start_date, exp.end_date):
            assert value in section.get_text()

    path = tmp_path / "cv.docx"
    CvDocxRenderer(profile).save(cv, path)
    doc = Document(path)
    paragraphs = ["".join(p.xpath(".//w:t/text()")) for p in doc.element.xpath(".//w:p")]
    rendered_bullets = [
        p.removeprefix("• ").replace(" ↗", "") for p in paragraphs if p.startswith("• ")
    ]
    assert rendered_bullets[: len(expected_texts)] == expected_texts
    assert any(rel.target_ref == "https://example.com/study" for rel in doc.part.rels.values())
    for exp in profile.experiences:
        for value in (exp.title, exp.company, exp.location, exp.start_date, exp.end_date):
            assert any(value in p for p in paragraphs)


def test_final_cv_snapshot_loads_legacy_storage_and_preserves_new_audit() -> None:
    profile = get_profile()
    legacy_payload = {**_personalization(), "selected_experiences": _expected(profile)}
    legacy_payload.pop("project_selection_reasons")
    cv = AdaptedCV.model_validate(legacy_payload)
    assert cv.project_selection_reasons == []
    assert cv.model_dump() == {
        **legacy_payload,
        "project_selection_reasons": [],
        "selected_certificate_ids": None,
    }
    draft = ApplicationDraft(**_payload(ApplicationDraft))
    assembled = assemble_cv(draft, profile)
    assert (
        assembled.model_dump(exclude={"project_selection_reasons", "selected_certificate_ids"})
        == legacy_payload
    )
    assert assembled.project_selection_reasons == draft.project_selection_reasons
    assert AdaptedCV.model_validate_json(assembled.model_dump_json()) == assembled
    assert "motivation_letter_body" not in assembled.model_dump()
