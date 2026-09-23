"""Independent semantic review of CV title, summary and motivation-letter text."""

from __future__ import annotations

import json
from datetime import date

from smartapply.llm.prompts.document_context import build_document_context
from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.llm.schemas import AdaptedCV, JobAnalysis, MotivationLetter
from smartapply.profile import Profile


def build_system_prompt() -> str:
    """Load the current instructions when preparing a request."""
    return load_prompt("document_review/system.j2")


def build_user_prompt(
    *,
    profile: Profile,
    analysis: JobAnalysis,
    adapted_cv: AdaptedCV,
    job_title: str,
    job_company: str,
    job_description: str,
    letter: MotivationLetter,
    validation_issues: list[str],
    allow_corrections: bool,
    language: str = "fr",
    reference_date: date | None = None,
) -> str:
    # Imported here because CV assembly also imports the prompt builders.
    # These are the validator's exact conventions, not another token estimate.
    from smartapply.cv.motivation_validator import _paragraph_count, _word_count

    context = build_document_context(
        profile=profile,
        analysis=analysis,
        adapted_cv=adapted_cv,
        job_title=job_title,
        job_company=job_company,
        job_description=job_description,
        language=language,
        reference_date=reference_date,
    )
    # The writer sees the CV as read-only. Review may patch its generated text,
    # while every source-backed section and selection remains frozen.
    context["final_cv"].pop("read_only")
    context["final_cv"]["editable_fields"] = (
        ["cv_title", "professional_summary"] if allow_corrections else []
    )
    context["final_cv"]["read_only_fields"] = [
        field
        for field in context["final_cv"]["data"]
        if field not in context["final_cv"]["editable_fields"]
    ]
    fields = {
        "cv_title": adapted_cv.cv_title,
        "professional_summary": adapted_cv.professional_summary,
        "motivation_letter_subject": letter.subject,
        "motivation_letter_body": letter.body,
    }
    metrics = {
        "motivation_letter_body": {
            "word_count": _word_count(letter.body),
            "paragraph_count": _paragraph_count(letter.body),
        },
        "professional_summary": {
            "character_count": len(adapted_cv.professional_summary),
            "nonempty_line_count": len(
                [line for line in adapted_cv.professional_summary.splitlines() if line.strip()]
            ),
        },
    }
    return render_prompt(
        "document_review/user.j2",
        context_json=json.dumps(context, ensure_ascii=False, indent=2),
        text_fields_json=json.dumps(fields, ensure_ascii=False, indent=2),
        metrics_json=json.dumps(metrics, ensure_ascii=False, indent=2),
        validation_issues_json=json.dumps(validation_issues, ensure_ascii=False),
        allow_corrections=allow_corrections,
    )
