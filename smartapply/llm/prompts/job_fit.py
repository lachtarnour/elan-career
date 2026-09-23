"""Candidate fit prompts over an immutable, separately extracted offer."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from smartapply.llm.prompts.loader import load_prompt, render_prompt
from smartapply.llm.schemas import OfferAnalysis
from smartapply.profile import Bullet, Profile


def build_system_prompt() -> str:
    """Load the common assessment rubric for every offer."""
    return load_prompt("job_fit/system.j2")


def _bullet_facts(bullet: Bullet) -> dict[str, Any]:
    return {
        "id": bullet.id,
        "text": bullet.text,
        "evidence_level": bullet.evidence_level,
        "allowed_claims": bullet.allowed_claims,
        "numbers": bullet.numbers,
    }


def _candidate_facts(profile: Profile) -> dict[str, Any]:
    """Keep all factual entries in profile order, without CV selection policies."""
    return {
        "identity": {
            "title": profile.identity.title,
            "summary": profile.identity.summary,
            "residence": profile.identity.location,
        },
        "declared_skills": [
            {"category": category.name, "skills": category.skills}
            for category in profile.skills.categories
        ],
        "preferences": profile.preferences.model_dump(mode="json"),
        "experiences": [
            {
                "id": experience.id,
                "title": experience.title,
                "company": experience.company,
                "location": experience.location,
                "start_date": experience.start_date,
                "end_date": experience.end_date,
                "bullets": [_bullet_facts(bullet) for bullet in experience.bullets],
            }
            for experience in profile.experiences
        ],
        "projects": [
            {
                "id": project.id,
                "name": project.name,
                "status": project.status,
                "bullets": [_bullet_facts(bullet) for bullet in project.bullets],
            }
            for project in profile.projects
        ],
        "education": [
            degree.model_dump(mode="json", exclude={"url"}) for degree in profile.education
        ],
        "languages": [language.model_dump(mode="json") for language in profile.languages],
        "certificates": [
            certificate.model_dump(mode="json", exclude={"url"})
            for certificate in profile.certificates
        ],
    }


def build_user_prompt(
    *,
    profile: Profile,
    offer_analysis: OfferAnalysis,
    reference_date: date | None = None,
) -> str:
    """Assess the same full factual profile for each immutable offer analysis."""
    return render_prompt(
        "job_fit/user.j2",
        candidate_json=json.dumps(_candidate_facts(profile), ensure_ascii=False),
        reference_date=(reference_date or date.today()).isoformat(),
        offer_json=offer_analysis.model_dump_json(),
    )
