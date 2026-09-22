"""Prompt builder for CV adaptation — strictly grounded in the profile.

Each bullet exposes:
- ``allowed_claims``: the exhaustive list of claims you may make about it.
  Any sentence that goes beyond these claims is treated as a hallucination.
- ``evidence_level``: how strong the underlying evidence is.
  - ``verified``: factual evidence for generated summaries and letters.
  - ``self_reported``: keep the claim narrow, don't generalize.
  - ``inferred``: be conservative; avoid asserting it strongly.
"""

from __future__ import annotations

import json

from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.llm.prompts.skill_profiles import format_skill_profiles
from smartapply.llm.schemas import JobAnalysis
from smartapply.profile import Bullet, Experience, Profile, Project


def build_system_prompt() -> str:
    """Render selection instructions; prose is written after CV assembly."""
    return render_prompt(
        "cv_adaptation/system.j2",
        recruiter_text_rules=load_prompt("shared/recruiter_text_rules.j2"),
        project_selection_rules=load_prompt("cv_adaptation/project_selection_rules.j2"),
    )


def _format_bullet(bullet: Bullet) -> str:
    claims = "\n".join(f"      * {c}" for c in bullet.effective_allowed_claims)
    numbers = f" numbers={bullet.numbers}" if bullet.numbers else ""
    return (
        f"  - id={bullet.id} (evidence={bullet.evidence_level}){numbers}\n"
        f"    source_text: {bullet.text}\n"
        f"    allowed_claims:\n{claims}"
    )


def _format_experiences(experiences: list[Experience]) -> str:
    out = []
    for exp in experiences:
        header = f"- id={exp.id} :: {exp.title} @ {exp.company} ({exp.start_date} → {exp.end_date})"
        out.append(header)
        for b in exp.bullets:
            out.append(_format_bullet(b))
    return "\n".join(out)


def _format_projects(projects: list[Project]) -> str:
    out = []
    for proj in projects:
        header = f"- id={proj.id} :: {proj.name}" + (f" ({proj.status})" if proj.status else "")
        out.append(header)
        out.append(
            "  selection_policy: "
            + json.dumps(proj.selection_policy.model_dump(), ensure_ascii=False)
        )
        if proj.keywords:
            out.append(f"  keywords: {', '.join(proj.keywords)}")
        for b in proj.bullets:
            out.append(_format_bullet(b))
    return "\n".join(out)


def _format_skill_catalog(profile: Profile) -> str:
    lines: list[str] = []
    for category in profile.skills.categories:
        lines.append(f"- {category.id} ({category.name}): {', '.join(category.skills)}")
    return "\n".join(lines)


def _format_core_skills(profile: Profile) -> str:
    core_skills = profile.skills.effective_category_skills(None)
    if not core_skills:
        return "- none"
    return "\n".join(
        f"- {category_id}: {', '.join(skills)}" for category_id, skills in core_skills.items()
    )


def _format_matching_keywords(profile: Profile) -> str:
    if not profile.skills.matching_keywords:
        return "- none"
    return "\n".join(
        f"- {family}: {', '.join(keywords)}"
        for family, keywords in profile.skills.matching_keywords.items()
    )


def build_user_prompt(
    *,
    profile: Profile,
    analysis: JobAnalysis,
    job_title: str,
    job_company: str,
    selected_experiences: list[Experience],
    selected_projects: list[Project],
    job_description: str = "",
) -> str:
    allowed = ", ".join(sorted(profile.skills.allowed_skills))
    style = profile.style_guide
    dont = "\n  - ".join(style.dont)
    do = "\n  - ".join(style.do)
    main_tasks_block = "\n".join(f"- {t}" for t in analysis.main_tasks)
    skill_profiles = format_skill_profiles(profile)
    skill_catalog = _format_skill_catalog(profile)
    core_skills = _format_core_skills(profile)
    matching_keywords = _format_matching_keywords(profile)

    return render_prompt(
        "cv_adaptation/user.j2",
        job_title=job_title,
        job_company=job_company,
        job_description=job_description,
        role_type=analysis.role_type,
        seniority=analysis.seniority,
        domain=analysis.domain,
        main_tasks_block=main_tasks_block,
        required_skills=", ".join(analysis.required_skills),
        keywords_to_surface=", ".join(analysis.cv_keywords_to_include),
        allowed_skills=allowed,
        skill_catalog=skill_catalog,
        core_skills=core_skills,
        skill_profiles=skill_profiles,
        matching_keywords=matching_keywords,
        candidate_title=profile.identity.title,
        summary_source=profile.identity.summary,
        experiences_block=_format_experiences(selected_experiences),
        projects_block=_format_projects(selected_projects),
        tone=style.tone,
        voice=style.voice,
        do=do,
        dont=dont,
    )
