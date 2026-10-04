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
    resolved_categories: dict[str, list[str]] = {}
    for block in selected.selected_skills:
        for skill in block.skills:
            key = skill.strip().casefold()
            if not key or key not in allowed:
                continue
            category_id = block.category_id
            if supported_skills_by_category and category_id not in supported_skills_by_category:
                category_id = category_by_skill[key]
            destinations = resolved_categories.setdefault(block.category_id, [])
            if category_id not in destinations:
                destinations.append(category_id)
            seen = seen_by_category.setdefault(category_id, set())
            if key in seen:
                continue
            seen.add(key)
            by_category.setdefault(category_id, []).append(canonical.get(key, skill.strip()))

    order = list(dict.fromkeys(
        [cid for source in selected.skills_order for cid in resolved_categories.get(source, [])]
        + list(by_category)
    ))
    update = {
        # Preserve processing order until enrichment finishes: changing it can
        # alter which automatic complements satisfy the existing minimums.
        "selected_skills": [
            SkillSelectionBlock(category_id=cid, skills=skills)
            for cid, skills in by_category.items()
        ],
        "skills_order": order,
    }
    if (
        "llm_selected_skills" in type(selected).model_fields
        and selected.llm_selected_skills is None
    ):
        # A repaired category may merge into another one. Capture its skills
        # in the source category's explicit position, before that information
        # is lost in the processing map above.
        source_order = dict.fromkeys([
            *selected.skills_order, *(block.category_id for block in selected.selected_skills),
        ])
        source_rank = {cid: rank for rank, cid in enumerate(source_order)}
        original: dict[str, dict[str, str]] = {}
        for block in sorted(selected.selected_skills, key=lambda b: source_rank[b.category_id]):
            for skill in block.skills:
                key = skill.strip().casefold()
                if not key or key not in allowed:
                    continue
                cid = block.category_id
                if supported_skills_by_category and cid not in supported_skills_by_category:
                    cid = category_by_skill[key]
                original.setdefault(cid, {}).setdefault(key, canonical.get(key, skill.strip()))
        update["llm_selected_skills"] = [
            SkillSelectionBlock(category_id=cid, skills=list(skills.values()))
            for cid, skills in original.items()
        ]
    return selected.model_copy(update=update)


def original_skill_selection(selected: CVSelection) -> list[SkillSelectionBlock]:
    """Read the original order, including an intentionally empty LLM selection."""
    original = getattr(selected, "llm_selected_skills", None)
    if original is not None:
        return original
    by_category = {block.category_id: block for block in selected.selected_skills}
    order = dict.fromkeys([*selected.skills_order, *by_category])
    return [by_category[cid] for cid in order if cid in by_category]


def restore_skill_selection_order(
    enriched: _CV,
    original: list[SkillSelectionBlock],
) -> _CV:
    """Place original skills first, then complements, without changing the skill set.

    Resolve duplicate placement in favour of the first original occurrence.
    This happens after every addition so display order cannot affect selection.
    """
    available = {
        skill.strip().casefold(): skill
        for block in enriched.selected_skills
        for skill in block.skills
    }
    by_category: dict[str, list[str]] = {}
    seen: set[str] = set()
    for block in [*original, *enriched.selected_skills]:
        for skill in block.skills:
            key = skill.strip().casefold()
            if key in seen or key not in available:
                continue
            by_category.setdefault(block.category_id, []).append(available[key])
            seen.add(key)
    return enriched.model_copy(update={
        "selected_skills": [
            SkillSelectionBlock(category_id=cid, skills=skills)
            for cid, skills in by_category.items()
        ],
        "skills_order": list(by_category),
    })
