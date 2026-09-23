"""Prompt builder for a single, targeted motivation-letter repair."""

from __future__ import annotations

import json
from typing import Any

from smartapply.llm import AdaptedCV, JobAnalysis, MotivationLetter
from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.profile import Profile


def build_system_prompt() -> str:
    """Load the current instructions when preparing a request."""
    return load_prompt("motivation_letter_repair/system.j2")


def _selected_evidence(profile: Profile, adapted_cv: AdaptedCV) -> dict[str, Any]:
    experiences_by_id = {experience.id: experience for experience in profile.experiences}
    projects_by_id = {project.id: project for project in profile.projects}

    experiences: list[dict[str, Any]] = []
    for adapted_experience in adapted_cv.selected_experiences:
        source = experiences_by_id.get(adapted_experience.source_id)
        experiences.append(
            {
                "id": adapted_experience.source_id,
                "company": source.company if source else "",
                "title": source.title if source else "",
                "validated_cv_bullets": [bullet.text for bullet in adapted_experience.bullets],
            }
        )

    projects: list[dict[str, Any]] = []
    for project_id in adapted_cv.selected_project_ids:
        project = projects_by_id.get(project_id)
        if project is None:
            continue
        projects.append(
            {
                "id": project.id,
                "name": project.name,
                "allowed_high_level_claims": [
                    claim
                    for bullet in project.bullets[:2]
                    for claim in bullet.effective_allowed_claims[:1]
                ],
            }
        )

    return {"experiences": experiences, "projects": projects}


def build_user_prompt(
    *,
    profile: Profile,
    defect: str,
    language: str,
    job_title: str,
    job_company: str,
    analysis: JobAnalysis,
    adapted_cv: AdaptedCV,
    letter: MotivationLetter,
) -> str:
    """Render a compact repair prompt containing only approved evidence."""
    offer_context = {
        "job_title": job_title,
        "job_company": job_company,
        "role_type": analysis.role_type,
        "main_tasks": list(analysis.main_tasks[:4]),
        "match_reasons": list(analysis.match_reasons[:4]),
        "offer_interest_points": list(analysis.offer_interest_points[:4]),
    }
    return render_prompt(
        "motivation_letter_repair/user.j2",
        defect=defect,
        language=language,
        current_body=letter.body,
        offer_context_json=json.dumps(
            offer_context,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        selected_evidence_json=json.dumps(
            _selected_evidence(profile, adapted_cv),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
