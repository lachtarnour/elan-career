"""Shared, identified sources for writing and reviewing final document text.

Declared skills support general competence claims. Only experience/project
facts support attributing a tool, result or responsibility to a specific job
or project. The final CV is a frozen document to inspect, not independent
evidence that its generated summary is true.
"""

from __future__ import annotations

from typing import Any

from smartapply.llm.schemas import AdaptedCV, JobAnalysis
from smartapply.profile import Bullet, Profile

_CONTEXT_SOURCE_IDS = {
    "document:final_cv",
    "profile:identity",
    "profile:declared_skills",
    "offer:description",
    "offer:analysis",
    "style:summary",
    "style:letter",
}


def _bullet_evidence(bullet: Bullet) -> dict[str, Any]:
    return {
        "source_id": f"bullet:{bullet.id}",
        "id": bullet.id,
        "text": bullet.text,
        "allowed_claims": list(bullet.effective_allowed_claims),
        "evidence_level": bullet.evidence_level,
        "numbers": list(bullet.numbers),
    }


def allowed_source_ids(profile: Profile, adapted_cv: AdaptedCV) -> set[str]:
    """Exactly the identifiable sources exposed by ``build_document_context``.

    Namespace prefixes distinguish profile objects from generated documents,
    offer material and style rules. Only projects selected in the final CV
    have project/bullet source IDs in this context.
    """
    source_ids = set(_CONTEXT_SOURCE_IDS)
    source_ids.update(f"skills:{category.id}" for category in profile.skills.categories)
    selected_experiences = {experience.source_id for experience in adapted_cv.selected_experiences}
    for experience in profile.experiences:
        if experience.id not in selected_experiences:
            continue
        source_ids.add(f"experience:{experience.id}")
        source_ids.update(f"bullet:{bullet.id}" for bullet in experience.bullets)
    selected_projects = set(adapted_cv.selected_project_ids)
    for project in profile.projects:
        if project.id not in selected_projects:
            continue
        source_ids.add(f"project:{project.id}")
        source_ids.update(f"bullet:{bullet.id}" for bullet in project.bullets)
    source_ids.update(f"education:{degree.id}" for degree in profile.education)
    source_ids.update(f"certificate:{certificate.id}" for certificate in profile.certificates)
    source_ids.update(f"language:{index}" for index, _ in enumerate(profile.languages))
    return source_ids


def build_document_context(
    *,
    profile: Profile,
    analysis: JobAnalysis,
    adapted_cv: AdaptedCV,
    job_title: str,
    job_company: str,
    job_description: str,
    language: str = "fr",
) -> dict[str, Any]:
    """Build read-only final state and sources, without author reasoning."""
    experiences_by_id = {experience.id: experience for experience in profile.experiences}
    projects_by_id = {project.id: project for project in profile.projects}
    experiences = []
    for selected in adapted_cv.selected_experiences:
        source = experiences_by_id.get(selected.source_id)
        if source is None:
            continue
        experiences.append(
            {
                "source_id": f"experience:{source.id}",
                **source.model_dump(mode="json", exclude={"bullets", "keywords"}),
                "bullets": [_bullet_evidence(bullet) for bullet in source.bullets],
            }
        )
    projects = []
    for project_id in dict.fromkeys(adapted_cv.selected_project_ids):
        project = projects_by_id.get(project_id)
        if project is None:
            continue
        projects.append(
            {
                "source_id": f"project:{project.id}",
                **project.model_dump(mode="json", exclude={"bullets", "keywords"}),
                "bullets": [_bullet_evidence(bullet) for bullet in project.bullets],
            }
        )
    style = profile.style_guide
    return {
        "source_index": sorted(allowed_source_ids(profile, adapted_cv)),
        "final_cv": {
            "source_id": "document:final_cv",
            "read_only": True,
            # Warnings can contain the drafting model's reasoning; the reviewer
            # receives final text and source facts, never that self-assessment.
            "data": adapted_cv.model_dump(mode="json", exclude={"warnings"}),
        },
        "candidate_identity": {
            "source_id": "profile:identity",
            "data": profile.identity.model_dump(mode="json"),
        },
        "declared_skills": {
            "source_id": "profile:declared_skills",
            "scope": (
                "All candidate-declared skills, including those not displayed in the final CV. "
                "Valid support for general competence claims without a mandatory project proof; "
                "not proof of a particular realization, employer, product or level of seniority."
            ),
            "categories": [
                {"source_id": f"skills:{category.id}", **category.model_dump(mode="json")}
                for category in profile.skills.categories
            ],
        },
        "experience_evidence": experiences,
        "selected_project_evidence": projects,
        "languages": [
            {"source_id": f"language:{index}", **item.model_dump(mode="json")}
            for index, item in enumerate(profile.languages)
        ],
        "education": [
            {"source_id": f"education:{item.id}", **item.model_dump(mode="json")}
            for item in profile.education
        ],
        "certificates": [
            {"source_id": f"certificate:{item.id}", **item.model_dump(mode="json")}
            for item in profile.certificates
        ],
        "job_offer": {
            "source_id": "offer:description",
            "title": job_title,
            "company": job_company,
            "full_description": job_description,
        },
        "job_analysis": {
            "source_id": "offer:analysis",
            "data": analysis.model_dump(
                mode="json", exclude={"match_reasons", "risks", "fit_score"}
            ),
        },
        "summary_style": {
            "source_id": "style:summary",
            "language": "en",
            "tone": style.tone,
            "voice": style.voice,
            "do": list(style.do),
            "dont": list(style.dont),
            "max_characters": style.max_summary_length,
            "max_lines": style.max_summary_lines,
            "scope": "CV professional_summary only; never apply its voice rules to the letter.",
        },
        "letter_style": {
            "source_id": "style:letter",
            "language": language,
            "voice": "first person: je in French, I in English",
            "paragraphs": 3,
            "min_words": 220,
            "max_words": 300,
            "scope": "Motivation letter only; separate from CV style rules.",
        },
    }
