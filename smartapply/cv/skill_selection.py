"""Normalize selected skills without making role-relevance decisions."""

from __future__ import annotations

from typing import TypeVar

from smartapply.llm import CVSelection, SkillSelectionBlock

_CV = TypeVar("_CV", bound=CVSelection)


def normalize_skill_selection(
    selected: _CV,
    *,
    allowed_skills_lower: set[str],
    supported_skills_by_category: dict[str, list[str]],
) -> _CV:
    """Keep catalogue skills, merge repeated blocks and repair unknown categories.

    Known categories retain their placement, including intentional cross-category
    grouping. An unknown heading must not hide a valid skill from the renderers:
    relocate that skill to its first canonical profile category instead.
    """
    allowed = {skill.strip().casefold() for skill in allowed_skills_lower}
    canonical: dict[str, str] = {}
    category_by_skill: dict[str, str] = {}
    for category_id, skills in supported_skills_by_category.items():
        for skill in skills:
            key = skill.strip().casefold()
            canonical.setdefault(key, skill)
            category_by_skill.setdefault(key, category_id)

    by_category: dict[str, list[str]] = {}
    seen_by_category: dict[str, set[str]] = {}
    for block in selected.selected_skills:
        for skill in block.skills:
            key = skill.strip().casefold()
            if not key or key not in allowed:
                continue
            category_id = block.category_id
            if supported_skills_by_category and category_id not in supported_skills_by_category:
                category_id = category_by_skill[key]
            seen = seen_by_category.setdefault(category_id, set())
            if key in seen:
                continue
            seen.add(key)
            by_category.setdefault(category_id, []).append(canonical.get(key, skill.strip()))

    order = list(dict.fromkeys(
        [cid for cid in selected.skills_order if cid in by_category] + list(by_category)
    ))
    return selected.model_copy(
        update={
            "selected_skills": [
                SkillSelectionBlock(category_id=cid, skills=skills)
                for cid, skills in by_category.items()
            ],
            "skills_order": order,
        }
    )
