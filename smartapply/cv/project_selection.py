"""Validate project policy without silently rewriting the generated selection.

The model judges semantic relevance and complementarity. Code checks membership,
limits, policy, and that cited offer evidence actually exists. This is not a
semantic entailment check. Legacy CV snapshots remain readable without reasons;
new personalization responses must satisfy the full contract here.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from smartapply.llm.schemas import CVSelection, JobAnalysis
from smartapply.profile import Profile

MIN_PROJECTS = 3
MAX_PROJECTS = 3
MAX_SPECIALIZED_PROJECTS = 1

# Only glyph variants, never dashes/minus signs or punctuation deletion.
_QUOTE_TYPOGRAPHY = str.maketrans({"‘": "'", "’": "'", "ʼ": "'", "‐": "-", "‑": "-"})
_INTERNAL_APOSTROPHE = re.compile(r"(?<=\w)\s*'\s*(?=\w)")


def _normalized_quote(value: str) -> str:
    """Compare offer excerpts modulo typography, without rewriting stored text.

    Scraped line breaks can split an elision (``d’\nAPIs``). Retain the
    apostrophe while removing adjacent layout whitespace inside a word.
    Accents, numbers, word order and other punctuation remain significant.
    This is for evidence matching, not for locating text-replacement offsets.
    """
    normalized = unicodedata.normalize("NFKC", value).translate(_QUOTE_TYPOGRAPHY)
    normalized = _INTERNAL_APOSTROPHE.sub("'", normalized)
    return " ".join(normalized.split()).casefold()


def selectable_project_capacity(profile: Profile) -> int:
    """Upper bound under editorial policy, before offer-specific relevance."""
    primary = sum(p.selection_policy.tier == "primary" for p in profile.projects)
    specialized = sum(
        p.selection_policy.tier == "specialized"
        and any(condition.strip() for condition in p.selection_policy.activation_conditions)
        for p in profile.projects
    )
    return primary + min(specialized, MAX_SPECIALIZED_PROJECTS)


def project_policy_errors(selected_ids: list[str], profile: Profile) -> list[str]:
    """Structural constraints shared by generation and final CV validation."""
    errors: list[str] = []
    projects = {project.id: project for project in profile.projects}
    if len(set(selected_ids)) < MIN_PROJECTS:
        errors.append(f"too_few_projects:min={MIN_PROJECTS}")
    if len(selected_ids) > MAX_PROJECTS:
        errors.append(f"too_many_projects:max={MAX_PROJECTS}")
    for project_id, count in Counter(selected_ids).items():
        if count > 1:
            errors.append(f"duplicate_project_id:{project_id}")
        project = projects.get(project_id)
        if project is None:
            errors.append(f"unknown_project_id:{project_id}")
        elif project.selection_policy.tier == "disabled":
            errors.append(f"disabled_project_id:{project_id}")
    specialized = [
        pid
        for pid in set(selected_ids)
        if pid in projects and projects[pid].selection_policy.tier == "specialized"
    ]
    if len(specialized) > MAX_SPECIALIZED_PROJECTS:
        errors.append(f"too_many_specialized_projects:max={MAX_SPECIALIZED_PROJECTS}")
    return errors


def validate_project_selection(
    personalization: CVSelection,
    profile: Profile,
    analysis: JobAnalysis,
    *,
    job_description: str = "",
) -> list[str]:
    errors = project_policy_errors(personalization.selected_project_ids, profile)
    projects = {project.id: project for project in profile.projects}
    selected = set(personalization.selected_project_ids)
    reasons = personalization.project_selection_reasons
    reason_counts = Counter(reason.project_id for reason in reasons)
    for pid in personalization.selected_project_ids:
        if reason_counts[pid] != 1:
            errors.append(f"project_reason_count:{pid}:expected=1")
    offer_sources = [
        _normalized_quote(source)
        for source in [*analysis.main_tasks, *analysis.required_skills, job_description]
        if source.strip()
    ]
    for reason in reasons:
        pid = reason.project_id
        if pid not in selected:
            errors.append(f"unselected_project_reason:{pid}")
        if not reason.reason.strip():
            errors.append(f"missing_project_reason:{pid}")
        evidence = _normalized_quote(reason.offer_evidence)
        if not evidence or not any(evidence in source for source in offer_sources):
            errors.append(f"ungrounded_project_evidence:{pid}")
        project = projects.get(pid)
        if project and project.selection_policy.tier == "specialized":
            if not any(c.strip() for c in project.selection_policy.activation_conditions):
                errors.append(f"missing_activation_conditions:{pid}")
            if not reason.complementary_value.strip():
                errors.append(f"missing_complementary_value:{pid}")
    return errors


def repair_project_evidence(
    selection: CVSelection,
    analysis: JobAnalysis,
    *,
    job_description: str = "",
) -> CVSelection:
    """Replace only mechanically verifiable citations after the correction call.

    Keep the native response immutable and preserve the initial validation path:
    a saved correction request depends on the initial errors verbatim. This
    fallback never chooses projects or infers relevance. A comma-separated list
    is recoverable only when every entry equals a complete required-skill fact.
    """
    sources = [*analysis.main_tasks, *analysis.required_skills, job_description]
    normalized_sources = [_normalized_quote(source) for source in sources if source.strip()]
    skills = {_normalized_quote(skill): skill for skill in analysis.required_skills if skill.strip()}
    repaired = selection.model_copy(deep=True)
    for reason in repaired.project_selection_reasons:
        evidence = _normalized_quote(reason.offer_evidence)
        if not evidence or any(evidence in source for source in normalized_sources):
            continue
        replacement = None
        # A model sometimes appends a sentence period to a complete task that
        # has none. Compare whole facts, preserving internal punctuation/negation.
        if evidence.endswith("."):
            replacement = next(
                (source for source in sources if _normalized_quote(source) == evidence[:-1]),
                None,
            )
        if replacement is None:
            parts = [part.strip() for part in evidence.split(",")]
            if len(parts) > 1 and all(part in skills for part in parts):
                replacement = skills[parts[0]]
        if replacement is not None:
            reason.offer_evidence = replacement
            repaired.warnings.append(f"auto_fixed:project_offer_evidence:{reason.project_id}")
    return repaired
