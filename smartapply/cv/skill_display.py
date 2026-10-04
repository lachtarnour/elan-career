"""Presentation-only composition for compact CV skill sections.

The adaptation and role-contract layers keep their canonical skill categories.
This module only prepares a transient copy for renderers, so compacting a sparse
secondary category cannot alter validation, persistence, or future adaptations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from smartapply.cv.role_contracts import load_contracts
from smartapply.llm import AdaptedCV, SkillSelectionBlock

DEFAULT_SECONDARY_BLOCK_MIN_SKILLS = 4

# Explicit affinities are deterministic and easier to audit than fuzzy text
# similarity. The first category already present in the CV receives the skills.
_DISPLAY_AFFINITIES: dict[str, tuple[str, ...]] = {
    "generative_agentic_ai": ("ml_ai", "data_infra", "data_analysis"),
    "computer_vision": ("ml_ai", "data_analysis", "data_infra"),
    "speech_audio": ("ml_ai", "stats_signal", "data_analysis"),
    "rl": ("ml_ai", "stats_signal", "data_analysis"),
    "ml_ai": ("generative_agentic_ai", "data_analysis", "data_infra"),
    "stats_signal": ("data_analysis", "ml_ai", "data_infra"),
    "data_analysis": ("stats_signal", "ml_ai", "data_infra"),
    "data_infra": ("data_analysis", "ml_ai", "generative_agentic_ai"),
}


@dataclass(frozen=True)
class SkillDisplayMerge:
    """One sparse secondary category folded into an existing display block."""

    source_category: str
    target_category: str
    skills: tuple[str, ...]


def compact_sparse_secondary_categories(
    adapted: AdaptedCV,
    *,
    primary_family: str,
    enabled: bool = True,
    min_standalone_skills: int = DEFAULT_SECONDARY_BLOCK_MIN_SKILLS,
    protected_categories: set[str] | None = None,
    contracts: dict[str, dict[str, Any]] | None = None,
) -> tuple[AdaptedCV, tuple[SkillDisplayMerge, ...]]:
    """Return a render-only CV copy with sparse secondary blocks compacted.

    Original LLM categories remain standalone to preserve their display order.
    A code-added category is secondary when it is outside the primary role contract's
    ``allowed_categories``. Categories containing at least
    ``min_standalone_skills`` remain standalone. Smaller ones are folded into
    the nearest compatible category already present. The canonical ``adapted``
    object is never mutated.
    """
    if not enabled or min_standalone_skills <= 1 or not adapted.selected_skills:
        return adapted, ()

    contracts = contracts or load_contracts()
    contract = contracts.get(primary_family)
    if not isinstance(contract, dict):
        return adapted, ()

    allowed_categories = {
        category_id
        for category_id in contract.get("allowed_categories", [])
        if isinstance(category_id, str)
    }
    protected_categories = set(protected_categories or ())
    if adapted.llm_selected_skills is not None:
        protected_categories.update(block.category_id for block in adapted.llm_selected_skills)
    if not allowed_categories:
        return adapted, ()

    skills_by_category = {
        block.category_id: list(block.skills) for block in adapted.selected_skills
    }
    category_order = [
        cid for cid in dict.fromkeys([*adapted.skills_order, *skills_by_category])
        if cid in skills_by_category
    ]
    merges: list[SkillDisplayMerge] = []

    for source_category in list(category_order):
        source_skills = skills_by_category.get(source_category, [])
        if source_category in allowed_categories or source_category in protected_categories:
            continue
        if len(source_skills) >= min_standalone_skills:
            continue

        target_category = next(
            (
                candidate
                for candidate in _DISPLAY_AFFINITIES.get(source_category, ())
                if candidate in skills_by_category and candidate != source_category
            ),
            None,
        )
        if target_category is None:
            continue

        target_skills = skills_by_category[target_category]
        seen = {skill.casefold() for skill in target_skills}
        moved: list[str] = []
        for skill in source_skills:
            key = skill.casefold()
            if key in seen:
                continue
            target_skills.append(skill)
            seen.add(key)
            moved.append(skill)

        category_order.remove(source_category)
        skills_by_category.pop(source_category, None)
        merges.append(
            SkillDisplayMerge(
                source_category=source_category,
                target_category=target_category,
                skills=tuple(moved),
            )
        )

    if not merges:
        return adapted, ()

    return (
        adapted.model_copy(
            update={
                "selected_skills": [
                    SkillSelectionBlock(
                        category_id=category_id,
                        skills=skills_by_category[category_id],
                    )
                    for category_id in category_order
                ],
                "skills_order": list(category_order),
            }
        ),
        tuple(merges),
    )
