"""Assemble generated personalization with the approved, static experience block."""

from __future__ import annotations

from smartapply.llm.schemas import (
    AdaptedBullet,
    AdaptedCV,
    AdaptedExperience,
    CVPersonalization,
)
from smartapply.profile import Profile


def profile_experiences(profile: Profile) -> list[AdaptedExperience]:
    """Take a fresh, verbatim snapshot in profile order, preserving source IDs."""
    return [
        AdaptedExperience(
            source_id=experience.id,
            bullets=[
                AdaptedBullet(source_id=bullet.id, text=bullet.text)
                for bullet in experience.bullets
            ],
        )
        for experience in profile.experiences
    ]


def assemble_cv(personalization: CVPersonalization, profile: Profile) -> AdaptedCV:
    """Both generation paths use the same assembly before downstream processing."""
    return AdaptedCV(
        **personalization.model_dump(include=set(CVPersonalization.model_fields)),
        selected_experiences=profile_experiences(profile),
    )
