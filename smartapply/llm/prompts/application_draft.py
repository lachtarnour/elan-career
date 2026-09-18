"""Prompt builder for a combined CV adaptation + motivation-letter call."""

from __future__ import annotations

from smartapply.llm.prompts.cv_adaptation import (
    _format_core_skills,
    _format_experiences,
    _format_matching_keywords,
    _format_projects,
    _format_skill_catalog,
)
from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.llm.prompts.skill_profiles import format_skill_profiles
from smartapply.llm.schemas import JobAnalysis
from smartapply.profile import Experience, Profile, Project

SYSTEM = load_prompt("application_draft/system.j2")

_SUPPORTED_CANDIDATE_EVIDENCE = (
    "the PROFILE section above: summary_source, read-only profile experiences, "
    "selected projects and each bullet's allowed_claims and evidence_level"
)


def _format_offer_interest_anchors(analysis: JobAnalysis) -> str:
    lines: list[str] = []
    if analysis.company_context.strip():
        lines.append(f"Company/context: {analysis.company_context.strip()}")
    for point in analysis.offer_interest_points:
        point = " ".join((point or "").split())
        if point:
            lines.append(f"- {point}")
    if lines:
        return "\n".join(lines)
    return (
        "No reliable company/about-us anchor extracted. Use only the visible "
        "domain, missions, responsibilities and required skills; do not invent "
        "company facts."
    )


def build_user_prompt(
    *,
    profile: Profile,
    analysis: JobAnalysis,
    job_title: str,
    job_company: str,
    selected_experiences: list[Experience],
    selected_projects: list[Project],
    language: str = "fr",
) -> str:
    allowed = ", ".join(sorted(profile.skills.allowed_skills))
    style = profile.style_guide
    dont = "\n  - ".join(style.dont)
    do = "\n  - ".join(style.do)
    main_tasks_block = "\n".join(f"- {t}" for t in analysis.main_tasks)
    project_hint = ", ".join(p.name for p in selected_projects[:4])
    skill_profiles = format_skill_profiles(profile)
    skill_catalog = _format_skill_catalog(profile)
    core_skills = _format_core_skills(profile)
    matching_keywords = _format_matching_keywords(profile)
    offer_interest_anchors = _format_offer_interest_anchors(analysis)

    return render_prompt(
        "application_draft/user.j2",
        job_title=job_title,
        job_company=job_company,
        role_title=job_title,
        company_name=job_company,
        role_type=analysis.role_type,
        seniority=analysis.seniority,
        domain=analysis.domain,
        offer_interest_anchors=offer_interest_anchors,
        main_tasks_block=main_tasks_block,
        required_skills=", ".join(analysis.required_skills),
        keywords_to_surface=", ".join(analysis.cv_keywords_to_include),
        match_reasons=", ".join(analysis.match_reasons),
        risks_to_avoid=", ".join(analysis.risks),
        candidate_name=profile.identity.full_name,
        candidate_title=profile.identity.title,
        summary_source=profile.identity.summary,
        allowed_skills=allowed,
        skill_catalog=skill_catalog,
        core_skills=core_skills,
        skill_profiles=skill_profiles,
        matching_keywords=matching_keywords,
        supported_candidate_evidence=_SUPPORTED_CANDIDATE_EVIDENCE,
        experiences_block=_format_experiences(selected_experiences),
        projects_block=_format_projects(selected_projects),
        letter_language="French" if language == "fr" else "English",
        target_word_count="220-300",
        project_hint=project_hint,
        tone=style.tone,
        voice=style.voice,
        max_summary_lines=style.max_summary_lines,
        max_summary_length=style.max_summary_length,
        do=do,
        dont=dont,
    )
