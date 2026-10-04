"""Apply a role-family contract to an LLM-produced CV.

The contract is a deterministic enrichment step that:
1. Preserves every selected skill present in the candidate's display catalogue,
   even when the detected family would not normally include it.
2. Adds the ``must_show`` skills that are missing, anchoring a coherent
   baseline for the role (e.g. PyTorch + scikit-learn for a Data Scientist).
3. Fills a minimum number of skills from contract-approved fallback skills.
4. Restores the LLM's category and skill order before automatic complements.
   The role contract's ``category_order`` only orders newly added categories.

``forbidden`` and ``allowed_categories`` guide automatic additions only;
they never veto a catalogue skill already selected by the LLM or offer guard.
Only skills in the candidate's ``allowed_skills`` whitelist may survive or be
added. Deduplication can remove repeated occurrences, never the skill itself.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

from smartapply.cv.role_family import KNOWN_ROLE_FAMILIES, classify, has_data_scientist_ia_signal
from smartapply.cv.skill_selection import (
    normalize_skill_selection,
    original_skill_selection,
    restore_skill_selection_order,
)
from smartapply.llm import CVSelection, JobAnalysis, SkillSelectionBlock
from smartapply.utils.skill_terms import skill_term_matches

_CV = TypeVar("_CV", bound=CVSelection)

_CONTRACTS_PATH = Path(__file__).with_name("role_contracts.json")

_DS_IA_EXTRA_SKILLS: tuple[str, ...] = ("NLP", "Transformers")


@lru_cache(maxsize=1)
def load_contracts() -> dict[str, dict[str, Any]]:
    """Read the V1 contracts JSON. Cached across calls."""
    with _CONTRACTS_PATH.open("r", encoding="utf-8") as f:
        contracts = json.load(f)
    missing = sorted(family for family in KNOWN_ROLE_FAMILIES if family not in contracts)
    if missing:
        raise ValueError(
            "role_contracts.json is missing contracts for role families: " + ", ".join(missing)
        )
    return contracts


def _normalize(term: str) -> str:
    return " ".join((term or "").lower().split())


def _is_explicit_offer_skill(term: str, offer_terms: set[str]) -> bool:
    """Use the same name matching as CV selection and prompt validation."""
    return any(skill_term_matches(offer_term, term) for offer_term in offer_terms)


def _augmented_must_show(
    family: str,
    contract: dict[str, Any],
    analysis: JobAnalysis,
    job_title: str,
) -> dict[str, list[str]]:
    """``must_show`` plus the DS-IA conditional augmentation."""
    base: dict[str, list[str]] = {
        cid: list(skills) for cid, skills in contract.get("must_show", {}).items()
    }
    if family == "data_scientist" and has_data_scientist_ia_signal(analysis, job_title):
        ml_ai = base.setdefault("ml_ai", [])
        existing_lower = {s.lower() for s in ml_ai}
        for skill in _DS_IA_EXTRA_SKILLS:
            if skill.lower() not in existing_lower:
                ml_ai.append(skill)
                existing_lower.add(skill.lower())
    return base


def _active_forbidden(
    contract: dict[str, Any],
    analysis: JobAnalysis,
) -> set[str]:
    """Block automatic additions unless the offer explicitly requests them."""
    forbidden = {_normalize(s) for s in contract.get("forbidden", []) if s}
    if not forbidden:
        return set()
    offer_terms = {
        _normalize(term)
        for term in list(analysis.required_skills) + list(analysis.cv_keywords_to_include)
        if term
    }
    return {skill for skill in forbidden if not _is_explicit_offer_skill(skill, offer_terms)}


def _offer_anchored_skills(analysis: JobAnalysis) -> set[str]:
    return {
        _normalize(term)
        for term in list(analysis.required_skills) + list(analysis.cv_keywords_to_include)
        if term
    }


def offer_anchored_categories(
    adapted: _CV,
    analysis: JobAnalysis,
) -> set[str]:
    """Return categories containing skills explicitly requested by the offer."""
    offer_terms = _offer_anchored_skills(analysis)
    return {
        block.category_id
        for block in adapted.selected_skills
        if any(_is_explicit_offer_skill(skill, offer_terms) for skill in block.skills)
    }


def _selected_skill_count(skills_by_category: dict[str, list[str]]) -> int:
    return sum(len(skills) for skills in skills_by_category.values())


def _ordered_unique(items: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        ordered.append(item)
        seen.add(item)
    return ordered


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _add_skill_if_supported(
    skills_by_category: dict[str, list[str]],
    category_order: list[str],
    *,
    category_id: str,
    skill: str,
    forbidden_active: set[str],
    allowed_skills_lower: set[str],
) -> bool:
    norm = _normalize(skill)
    if not norm:
        return False
    if norm in forbidden_active:
        return False
    if norm not in allowed_skills_lower:
        return False
    if category_id not in skills_by_category:
        skills_by_category[category_id] = []
        category_order.append(category_id)
    existing_lower = {s.lower() for s in skills_by_category[category_id]}
    if norm in existing_lower:
        return False
    skills_by_category[category_id].append(skill)
    return True


def _fill_minimum_skills(
    filtered: dict[str, list[str]],
    category_order: list[str],
    *,
    contract: dict[str, Any],
    allowed_categories: set[str],
    forbidden_active: set[str],
    allowed_skills_lower: set[str],
) -> None:
    """Add contract-approved fallback skills until ``min_total_skills`` is met."""
    min_total = int(contract.get("min_total_skills", 0) or 0)
    fill_skills = contract.get("fill_skills", {})
    if min_total <= 0 or not isinstance(fill_skills, dict):
        return

    for cid, skills in fill_skills.items():
        if allowed_categories and cid not in allowed_categories:
            continue
        if not isinstance(skills, list):
            continue
        for skill in skills:
            if _selected_skill_count(filtered) >= min_total:
                return
            if isinstance(skill, str):
                _add_skill_if_supported(
                    filtered,
                    category_order,
                    category_id=cid,
                    skill=skill,
                    forbidden_active=forbidden_active,
                    allowed_skills_lower=allowed_skills_lower,
                )


def _apply_global_baseline(
    filtered: dict[str, list[str]],
    category_order: list[str],
    *,
    contracts: dict[str, dict[str, Any]],
    forbidden_active: set[str],
    allowed_skills_lower: set[str],
) -> list[str]:
    """Always keep the candidate's core positioning visible in the CV."""
    baseline = contracts.get("_global_baseline", {})
    if not baseline.get("enabled", True):
        return []
    skills_by_category = baseline.get("skills", {})
    if not isinstance(skills_by_category, dict):
        return []

    baseline_categories: list[str] = []
    for cid, skills in skills_by_category.items():
        if not isinstance(skills, list):
            continue
        for skill in skills:
            if not isinstance(skill, str):
                continue
            if _normalize(skill) in {
                _normalize(existing)
                for existing_skills in filtered.values()
                for existing in existing_skills
            }:
                continue
            _add_skill_if_supported(
                filtered,
                category_order,
                category_id=cid,
                skill=skill,
                forbidden_active=forbidden_active,
                allowed_skills_lower=allowed_skills_lower,
            )
        if filtered.get(cid) and cid not in baseline_categories:
            baseline_categories.append(cid)
    return baseline_categories


def _dedupe_cross_category(
    skills_by_category: dict[str, list[str]],
    category_order: list[str],
) -> None:
    """Ensure one display skill appears in only one category.

    Specific categories are preferred over broad buckets such as ``ml_ai``.
    This avoids visual padding like PyTorch appearing under both ML & CV.
    """
    specific_priority = (
        "computer_vision",
        "generative_agentic_ai",
        "speech_audio",
        "rl",
        "stats_signal",
        "data_analysis",
        "data_infra",
        "ml_ai",
    )
    ordered_categories = [cid for cid in specific_priority if cid in skills_by_category] + [
        cid for cid in category_order if cid in skills_by_category and cid not in specific_priority
    ]

    seen: set[str] = set()
    for cid in ordered_categories:
        unique: list[str] = []
        for skill in skills_by_category.get(cid, []):
            norm = _normalize(skill)
            if norm in seen:
                continue
            seen.add(norm)
            unique.append(skill)
        skills_by_category[cid] = unique

    for cid in list(skills_by_category):
        if not skills_by_category[cid]:
            skills_by_category.pop(cid, None)


def _skill_exists_anywhere(skills_by_category: dict[str, list[str]], skill: str) -> bool:
    norm = _normalize(skill)
    return any(
        _normalize(existing) == norm
        for skills in skills_by_category.values()
        for existing in skills
    )


def _ensure_min_skills_per_category(
    filtered: dict[str, list[str]],
    category_order: list[str],
    *,
    contract: dict[str, Any],
    must_show: dict[str, list[str]],
    contracts: dict[str, dict[str, Any]],
    forbidden_active: set[str],
    allowed_skills_lower: set[str],
    supported_skills_by_category: dict[str, list[str]],
    min_per_category: int = 2,
) -> None:
    """Avoid displaying a skills category with a single lonely skill.

    This is a deterministic post-LLM polish step. It never invents claims: it
    only uses role must-show skills, role fill skills, the global baseline, or
    canonical profile skills from the same category.
    """
    if min_per_category <= 1:
        return

    fill_skills = contract.get("fill_skills", {})
    if not isinstance(fill_skills, dict):
        fill_skills = {}
    baseline = contracts.get("_global_baseline", {})
    baseline_skills = baseline.get("skills", {}) if isinstance(baseline, dict) else {}
    if not isinstance(baseline_skills, dict):
        baseline_skills = {}

    for cid in list(category_order):
        if len(filtered.get(cid, [])) != 1:
            continue
        supported_for_category = supported_skills_by_category.get(cid, [])
        supported_norms = {_normalize(skill) for skill in supported_for_category}
        candidate_groups = (
            must_show.get(cid, []),
            fill_skills.get(cid, []),
            baseline_skills.get(cid, []),
            supported_for_category,
        )
        for candidates in candidate_groups:
            if not isinstance(candidates, list):
                continue
            for skill in candidates:
                if not isinstance(skill, str):
                    continue
                norm = _normalize(skill)
                if supported_norms and norm not in supported_norms:
                    continue
                if _skill_exists_anywhere(filtered, skill):
                    continue
                added = _add_skill_if_supported(
                    filtered,
                    category_order,
                    category_id=cid,
                    skill=skill,
                    forbidden_active=forbidden_active,
                    allowed_skills_lower=allowed_skills_lower,
                )
                if added and len(filtered.get(cid, [])) >= min_per_category:
                    break
            if len(filtered.get(cid, [])) >= min_per_category:
                break


def _contract_category_priority(
    contract: dict[str, Any],
    *,
    must_show: dict[str, list[str]],
    baseline_order: list[str],
) -> list[str]:
    configured = _string_list(contract.get("category_order"))
    if configured:
        return configured

    fill_skills = contract.get("fill_skills", {})
    fill_order = list(fill_skills) if isinstance(fill_skills, dict) else []
    return _ordered_unique(
        list(must_show)
        + fill_order
        + baseline_order
        + _string_list(contract.get("allowed_categories"))
    )


def _final_category_order(
    filtered: dict[str, list[str]],
    *,
    contract: dict[str, Any],
    must_show: dict[str, list[str]],
    baseline_order: list[str],
    observed_order: list[str],
) -> list[str]:
    """Order automatic complements before restoring the original LLM selection.

    ``category_order`` in the JSON is the role-specific presentation strategy.
    The final restoration puts original LLM categories first. This priority
    therefore only determines the relative order of entirely added categories.
    """
    priority = _contract_category_priority(
        contract,
        must_show=must_show,
        baseline_order=baseline_order,
    )
    candidates = _ordered_unique(priority + observed_order + list(filtered))
    return [cid for cid in candidates if filtered.get(cid)]


def apply_contract(
    adapted: _CV,
    *,
    analysis: JobAnalysis,
    job_title: str,
    allowed_skills_lower: set[str],
    supported_skills_by_category: dict[str, list[str]] | None = None,
    contracts: dict[str, dict[str, Any]] | None = None,
) -> tuple[_CV, str]:
    """Apply the role-family contract for ``analysis``.

    Returns the updated CV plus the family id used.
    """
    family = classify(analysis, title=job_title)
    contracts = contracts or load_contracts()
    supported_skills_by_category = supported_skills_by_category or {}
    adapted = normalize_skill_selection(
        adapted,
        allowed_skills_lower=allowed_skills_lower,
        supported_skills_by_category=supported_skills_by_category,
    )
    original = original_skill_selection(adapted)
    contract = contracts.get(family)
    if contract is None:
        return restore_skill_selection_order(adapted, original), family

    allowed_categories = set(contract.get("allowed_categories", []))
    forbidden_active = _active_forbidden(contract, analysis)
    must_show = _augmented_must_show(family, contract, analysis, job_title)

    # Family rules guide additions, not deletion of an existing valid selection.
    filtered = {block.category_id: list(block.skills) for block in adapted.selected_skills}
    category_order = list(filtered)

    # Add must_show skills that are missing (must already exist in the
    # candidate's profile whitelist — never invent).
    for cid, skills in must_show.items():
        if cid not in filtered:
            filtered[cid] = []
            category_order.append(cid)
        for skill in skills:
            _add_skill_if_supported(
                filtered,
                category_order,
                category_id=cid,
                skill=skill,
                forbidden_active=forbidden_active,
                allowed_skills_lower=allowed_skills_lower,
            )

    _fill_minimum_skills(
        filtered,
        category_order,
        contract=contract,
        allowed_categories=allowed_categories,
        forbidden_active=forbidden_active,
        allowed_skills_lower=allowed_skills_lower,
    )
    baseline_order = _apply_global_baseline(
        filtered,
        category_order,
        contracts=contracts,
        forbidden_active=forbidden_active,
        allowed_skills_lower=allowed_skills_lower,
    )
    _dedupe_cross_category(filtered, category_order)
    _ensure_min_skills_per_category(
        filtered,
        category_order,
        contract=contract,
        must_show=must_show,
        contracts=contracts,
        forbidden_active=forbidden_active,
        allowed_skills_lower=allowed_skills_lower,
        supported_skills_by_category=supported_skills_by_category,
    )

    final_order = _final_category_order(
        filtered,
        contract=contract,
        must_show=must_show,
        baseline_order=baseline_order,
        observed_order=category_order,
    )

    new_selected = [
        SkillSelectionBlock(category_id=cid, skills=filtered[cid]) for cid in final_order
    ]
    enriched = adapted.model_copy(
        update={
            "selected_skills": new_selected,
            "skills_order": list(final_order),
        }
    )
    return restore_skill_selection_order(enriched, original), family
