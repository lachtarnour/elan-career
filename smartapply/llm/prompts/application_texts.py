"""Write the summary and letter after the CV structure is assembled."""

from __future__ import annotations

import json
from datetime import date

from smartapply.llm.prompts.document_context import build_document_context
from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.llm.schemas import AssembledCV, JobAnalysis
from smartapply.profile import Profile


def build_system_prompt() -> str:
    """Share summary rules with review while keeping letter instructions separate."""
    return render_prompt(
        "application_texts/system.j2",
        summary_rules=load_prompt("cv_adaptation/summary_rules.j2"),
    )


def build_user_prompt(
    *,
    profile: Profile,
    analysis: JobAnalysis,
    adapted_cv: AssembledCV,
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
    # The headline has already been selected and is not an output of this call.
    # Retain summary_style and its scope so CV voice cannot leak into the letter.
    context.pop("title_style")
    context["source_index"].remove("style:title")
    return render_prompt(
        "application_texts/user.j2",
        context_json=json.dumps(context, ensure_ascii=False, indent=2),
        summary_instructions=render_prompt(
            "shared/summary_instructions.j2",
            max_summary_lines=profile.style_guide.max_summary_lines,
            max_summary_length=profile.style_guide.max_summary_length,
        ),
    )
