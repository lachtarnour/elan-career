"""Prompt builder for CV adaptation — strictly grounded in the profile.

Each bullet exposes:
- ``allowed_claims``: the exhaustive list of claims you may make about it.
  Any sentence that goes beyond these claims is treated as a hallucination.
- ``evidence_level``: how strong the underlying evidence is.
  - ``verified``: reformulate freely while staying faithful.
  - ``self_reported``: keep the claim narrow, don't generalize.
  - ``inferred``: be conservative; avoid asserting it strongly.
"""

from __future__ import annotations

import re

from smartapply.cv.constants import NON_DISPLAY_DOMAIN_TERMS
from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.llm.prompts.skill_profiles import format_skill_profiles
from smartapply.llm.schemas import JobAnalysis
from smartapply.profile import Bullet, Experience, Profile, Project
from smartapply.utils.skill_terms import normalize_skill_term

SYSTEM = load_prompt("cv_adaptation/system.j2")


def _format_bullet(bullet: Bullet) -> str:
    claims = "\n".join(f"      * {c}" for c in bullet.effective_allowed_claims)
    numbers = f" numbers={bullet.numbers}" if bullet.numbers else ""
    link_line = ""
    if bullet.links:
        anchors = ", ".join(f'"{link.anchor}"' for link in bullet.links)
        link_line = f"    keep_anchors_verbatim: {anchors}\n"
    return (
        f"  - id={bullet.id} (evidence={bullet.evidence_level}){numbers}\n"
        f"    source_text: {bullet.text}\n"
        f"{link_line}"
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


def _term_supported_by_allowed_skill(term: str, allowed_skills: set[str]) -> bool:
    normalized = normalize_skill_term(term)
    if not normalized:
        return True
    for skill in allowed_skills:
        skill_norm = normalize_skill_term(skill)
        if not skill_norm:
            continue
        if len(skill_norm) <= 2:
            if normalized == skill_norm:
                return True
            continue
        if normalized == skill_norm:
            return True
        if skill_norm in normalized or normalized in skill_norm:
            return True
        if re.search(rf"(?<![a-z0-9]){re.escape(skill_norm)}(?![a-z0-9])", normalized):
            return True
    return False


def _format_unsupported_offer_terms(profile: Profile, analysis: JobAnalysis) -> str:
    """Terms from the offer that should not become candidate claims."""
    allowed = profile.skills.allowed_skills
    terms: list[str] = []
    seen: set[str] = set()
    for term in list(analysis.required_skills) + list(analysis.cv_keywords_to_include):
        clean = " ".join((term or "").split())
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        if key in NON_DISPLAY_DOMAIN_TERMS:
            continue
        if _term_supported_by_allowed_skill(clean, allowed):
            continue
        # Skip broad education phrases; they are not skills to claim or block.
        if "equivalent" in key or "education" in key or "degree" in key:
            continue
        terms.append(clean)
    if not terms:
        return "- none"
    return "\n".join(f"- {term}" for term in terms)


def build_user_prompt(
    *,
    profile: Profile,
    analysis: JobAnalysis,
    job_title: str,
    job_company: str,
    selected_experiences: list[Experience],
    selected_projects: list[Project],
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
    unsupported_offer_terms = _format_unsupported_offer_terms(profile, analysis)

    return render_prompt(
        "cv_adaptation/user.j2",
        job_title=job_title,
        job_company=job_company,
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
        unsupported_offer_terms=unsupported_offer_terms,
        candidate_title=profile.identity.title,
        summary_source=profile.identity.summary,
        experiences_block=_format_experiences(selected_experiences),
        projects_block=_format_projects(selected_projects),
        tone=style.tone,
        voice=style.voice,
        max_summary_lines=style.max_summary_lines,
        max_summary_length=style.max_summary_length,
        do=do,
        dont=dont,
    )
