"""Assemble generated personalization with the approved, static experience block."""

from __future__ import annotations

from typing import overload

from smartapply.llm.schemas import (
    AdaptedBullet,
    AdaptedCV,
    AdaptedExperience,
    AssembledCV,
    CVPersonalization,
    CVSelection,
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


@overload
def assemble_cv(personalization: CVPersonalization, profile: Profile) -> AdaptedCV: ...


@overload
def assemble_cv(personalization: CVSelection, profile: Profile) -> AssembledCV: ...


def assemble_cv(
    personalization: CVSelection, profile: Profile
) -> AssembledCV | AdaptedCV:
    """Assemble static experiences; a new selection has no summary to seed."""
    if isinstance(personalization, CVPersonalization):
        return AdaptedCV(
            **personalization.model_dump(include=set(CVPersonalization.model_fields)),
            selected_experiences=profile_experiences(profile),
        )
    return AssembledCV(
        **personalization.model_dump(),
        selected_experiences=profile_experiences(profile),
    )
