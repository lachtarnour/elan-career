"""Uncertain extraction language must not become a document-language request."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from smartapply.database import session_scope
from smartapply.database.models import Application, Job, JobStatus
from smartapply.database.models import JobAnalysis as StoredAnalysis
from smartapply.language import resolve_document_language
from smartapply.llm import JobAnalysis, MotivationLetter
from smartapply.pipeline.applier import Applier
from smartapply.pipeline.apply.cv_writer import CvWriterMixin
from smartapply.pipeline.reports import ApplyReport


@pytest.mark.parametrize("language", ["fr", "en", "de", "es", "it", "nl", "pt", "ja", "English"])
def test_concrete_and_historical_document_languages_are_preserved(language):
    assert resolve_document_language(language, "") == (language, None)


@pytest.mark.parametrize("sentinel", ["other", "unknown"])
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("Vos missions : développement. Profil recherché pour un poste en CDI.", "fr"),
        ("Responsibilities and requirements: apply for this role.", "en"),
        ("Wir suchen. Deine Aufgaben. Berufserfahrung und Kenntnisse.", "de"),
    ],
)
def test_sentinels_use_confident_local_detection_with_observation(sentinel, body, expected):
    assert resolve_document_language(sentinel, body) == (
        expected, f"document_language_detected:{expected}:offer_language_{sentinel}",
    )


@pytest.mark.parametrize("sentinel", ["other", "unknown"])
@pytest.mark.parametrize("body", ["", "Python SQL", "Vos missions. Responsibilities."])
def test_insufficient_or_mixed_language_uses_document_default_with_observation(sentinel, body):
    assert resolve_document_language(sentinel, body) == (
        "fr", f"document_language_default:fr:offer_language_{sentinel}",
    )


class _DraftCaptured(Exception):
    """Stop before any mocked generation or output is attempted."""


@pytest.mark.parametrize(
    "body",
    [
        "Vos missions : développement. Responsibilities.",
        "Vos missions. Responsibilities and requirements: apply for this role.",
        "Vos missions. Responsibilities.",
        "",
    ],
)
def test_mixed_french_english_always_writes_in_french_with_observation(body):
    assert resolve_document_language("mixed_fr_en", body) == (
        "fr", "document_language_selected:fr:offer_language_mixed_fr_en",
    )


@pytest.mark.parametrize(
    ("extracted_language", "body", "expected_language", "warning"),
    [
        (
            "unknown", "Responsibilities and requirements: apply for this role.", "en",
            "document_language_detected:en:offer_language_unknown",
        ),
        ("other", "Python SQL", "fr", "document_language_default:fr:offer_language_other"),
        (
            "mixed_fr_en", "Vos missions. Responsibilities and requirements: apply.", "fr",
            "document_language_selected:fr:offer_language_mixed_fr_en",
        ),
        ("it", "Python SQL", "it", None),
        ("ja", "Python SQL", "ja", None),
    ],
)
def test_actual_apply_path_resolves_document_language_without_changing_extraction(
    isolated_db, extracted_language, body, expected_language, warning,
):
    raw = {"offer_language": extracted_language, "extracted_company_name": "Example"}
    with session_scope() as session:
        job = Job(
            external_id="document-language", title="Software Engineer", company="Example",
            description=body, source="manual", status=JobStatus.ANALYZED,
            analysis=StoredAnalysis(role_type="Software Engineer", seniority="", raw_response=raw),
        )
        session.add(job)
        session.flush()
        application = Application(job_id=job.id, status=JobStatus.ANALYZED)
        session.add(application)
        session.flush()
        job_id, application_id = job.id, application.id

    applier = Applier.__new__(Applier)
    applier.stop_requested = None
    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        raise _DraftCaptured

    applier._generate_draft = capture
    report = ApplyReport(job_id=job_id, application_id=application_id)
    with pytest.raises(_DraftCaptured):
        applier._do_apply(report=report)

    assert captured["offer_language"] == expected_language
    assert captured["analysis"].offer_language == extracted_language
    assert report.validation_warnings == ([warning] if warning else [])
    with session_scope() as session:
        assert session.get(Job, job_id).analysis.raw_response == raw


def test_final_letter_validation_uses_writing_language_without_mutating_offer():
    analysis = JobAnalysis(
        role_type="Software Engineer", seniority="unknown", domain="", main_tasks=[],
        required_skills=[], nice_to_have=[], cv_keywords_to_include=[], fit_score=0.5,
        match_reasons=[], risks=[], offer_language="unknown",
    )
    seen = []

    def validate(*args, **kwargs):
        seen.append(kwargs["analysis"])
        return SimpleNamespace(warnings=["french_elision_missing_apostrophe"], errors=[])

    writer = CvWriterMixin()
    writer.letter_validator = SimpleNamespace(validate=validate)
    report = ApplyReport(job_id=1, application_id=1)
    writer._validate_letter(
        MotivationLetter(subject="Candidature", body="Je souhaite rejoindre votre équipe."),
        object(), analysis, report, language="fr",
    )
    assert seen[0].offer_language == "fr"
    assert analysis.offer_language == "unknown"
    assert report.validation_warnings == ["french_elision_missing_apostrophe"]
