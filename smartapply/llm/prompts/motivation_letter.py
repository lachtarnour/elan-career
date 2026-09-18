"""Write a letter after the CV structure and project selection are final."""

from __future__ import annotations

import json

from smartapply.llm.prompts.document_context import build_document_context
from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.llm.schemas import AdaptedCV, JobAnalysis
from smartapply.profile import Profile

SYSTEM = load_prompt("motivation_letter/system.j2")


def build_user_prompt(
    *,
    profile: Profile,
    analysis: JobAnalysis,
    adapted_cv: AdaptedCV,
    job_title: str,
    job_company: str,
    job_description: str,
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
    # CV-only prohibitions (for example "no first person") must not leak
    # into a letter-writing request. The reviewer receives these separately.
    context.pop("summary_style")
    context["source_index"].remove("style:summary")
    return render_prompt(
        "motivation_letter/user.j2",
        context_json=json.dumps(context, ensure_ascii=False, indent=2),
    )
