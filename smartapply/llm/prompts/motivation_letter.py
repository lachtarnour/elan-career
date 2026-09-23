"""Write a letter after the CV structure and project selection are final."""

from __future__ import annotations

import json
from datetime import date

from smartapply.llm.prompts.document_context import build_document_context
from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.llm.schemas import AdaptedCV, JobAnalysis
from smartapply.profile import Profile


def build_system_prompt() -> str:
    """Load the current instructions when preparing a request."""
    return load_prompt("motivation_letter/system.j2")


def build_user_prompt(
    *,
    profile: Profile,
    analysis: JobAnalysis,
    adapted_cv: AdaptedCV,
    job_title: str,
    job_company: str,
    job_description: str,
    language: str = "fr",
    reference_date: date | None = None,
) -> str:
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
    # CV-only prohibitions (for example "no first person") must not leak
    # into a letter-writing request. The reviewer receives these separately.
    for field, source_id in (("title_style", "style:title"), ("summary_style", "style:summary")):
        context.pop(field)
        context["source_index"].remove(source_id)
    return render_prompt(
        "motivation_letter/user.j2",
        context_json=json.dumps(context, ensure_ascii=False, indent=2),
    )
