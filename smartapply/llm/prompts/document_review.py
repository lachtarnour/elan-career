"""Independent semantic review of final summary and motivation-letter text."""

from __future__ import annotations

import json

from smartapply.llm.prompts.document_context import build_document_context
from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.llm.schemas import AdaptedCV, JobAnalysis, MotivationLetter
from smartapply.profile import Profile

SYSTEM = load_prompt("document_review/system.j2")


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
) -> str:
    context = build_document_context(
        profile=profile,
        analysis=analysis,
        adapted_cv=adapted_cv,
        job_title=job_title,
        job_company=job_company,
        job_description=job_description,
        language=language,
    )
    fields = {
        "professional_summary": adapted_cv.professional_summary,
        "motivation_letter_subject": letter.subject,
        "motivation_letter_body": letter.body,
    }
    return render_prompt(
        "document_review/user.j2",
        context_json=json.dumps(context, ensure_ascii=False, indent=2),
        text_fields_json=json.dumps(fields, ensure_ascii=False, indent=2),
        validation_issues_json=json.dumps(validation_issues, ensure_ascii=False),
        allow_corrections=allow_corrections,
    )
