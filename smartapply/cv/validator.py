"""Anti-hallucination validator for adapted CVs.

Hard rules (errors → reject / auto-fix):
- Experiences and bullets must exactly match the profile, including their order.
- Every experience source_id must exist in the profile.
- Every bullet source_id must exist in the profile (experiences OR projects).
- Every bullet must belong to the experience it's nested under.
- Every selected project id must exist in the profile.
- Skill category ids in ``skills_order`` must exist.
- ``skills_profile_id`` should be one of the profile skill presets when provided.
- ``selected_skills`` may only contain known category ids and whitelisted skills.

Soft rules (warnings → keep but flag):
- Numbers in output bullets must appear in source bullet (years excluded).
- Output bullet should preserve some content from source (rapidfuzz >= 35).
- Output bullet should map to the source's ``allowed_claims`` (fuzzy match
  >= 40 against at least one allowed claim).
- For non-verified evidence (``self_reported``, ``inferred``), require a
  stricter overlap to avoid over-claiming.
- Approved source bullets are never shortened to meet generated-text limits.
- Summary length above style_guide.max_summary_length is reported, never truncated.
- Summary line count above style_guide.max_summary_lines is reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TypeVar

from rapidfuzz import fuzz

from smartapply.cv.assembly import profile_experiences
from smartapply.cv.project_selection import project_policy_errors
from smartapply.cv.text_hygiene import evidence_metadata_warnings
from smartapply.llm import AdaptedCV, AssembledCV, SkillSelectionBlock
from smartapply.profile import Profile

_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_CV = TypeVar("_CV", AssembledCV, AdaptedCV)


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok


def _extract_numbers(text: str) -> set[str]:
    return {m.group(0).replace(",", ".") for m in _NUMBER_RE.finditer(text)}


def _is_year_like(token: str) -> bool:
    try:
        n = float(token)
    except ValueError:
        return False
    return 1900 <= n <= 2099


def _best_claim_score(text: str, claims: list[str]) -> int:
    """Best fuzzy match between ``text`` and any allowed claim."""
    if not claims:
        return 100
    return max(fuzz.token_set_ratio(text.lower(), c.lower()) for c in claims)


class CvValidator:
    def __init__(
        self,
        profile: Profile,
        min_text_overlap: int = 35,
        min_claim_overlap: int = 40,
        min_claim_overlap_inferred: int = 60,
    ):
        self.profile = profile
        self.bullet_index = profile.bullet_index()
        self.exp_index = {e.id: e for e in profile.experiences}
        self.skill_category_ids = {c.id for c in profile.skills.categories}
        self.skill_profile_ids = profile.skills.profile_ids
        self.allowed_skills_lower = {s.lower() for s in profile.skills.allowed_skills}
        self.allowed_skills_by_lower = {s.lower(): s for s in profile.skills.allowed_skills}
        self.min_text_overlap = min_text_overlap
        self.min_claim_overlap = min_claim_overlap
        self.min_claim_overlap_inferred = min_claim_overlap_inferred
        self.max_bullet_len = profile.style_guide.max_bullet_length
        self.max_summary_len = profile.style_guide.max_summary_length
        self.max_summary_lines = profile.style_guide.max_summary_lines

    def validate(self, cv: AssembledCV | AdaptedCV) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        # ---- Experiences and bullets ----
        if [exp.source_id for exp in cv.selected_experiences] != list(self.exp_index):
            errors.append("experience_order_mismatch:expected complete profile order")
        for exp in cv.selected_experiences:
            if exp.source_id not in self.exp_index:
                errors.append(f"unknown_experience_id:{exp.source_id}")
                continue
            source_experience = self.exp_index[exp.source_id]
            if [b.source_id for b in exp.bullets] != [b.id for b in source_experience.bullets]:
                errors.append(f"experience_bullets_mismatch:{exp.source_id}")
            for blt in exp.bullets:
                src_bullet = self.bullet_index.get(blt.source_id)
                if src_bullet is None:
                    errors.append(f"unknown_bullet_id:{blt.source_id}")
                    continue
                src_exp = self.profile.experience_for_bullet(blt.source_id)
                if src_exp is None or src_exp.id != exp.source_id:
                    errors.append(
                        f"bullet_wrong_parent:{blt.source_id} "
                        f"expected {src_exp.id if src_exp else 'profile experience'} "
                        f"got {exp.source_id}"
                    )
                if blt.text == src_bullet.text:
                    # Static, approved source content is not subject to LLM
                    # paraphrase checks or generated-bullet length limits.
                    continue
                errors.append(f"experience_text_mismatch:{blt.source_id}")

                # Numbers preservation
                out_numbers = _extract_numbers(blt.text)
                src_numbers = _extract_numbers(src_bullet.text) | set(src_bullet.numbers)
                hallucinated = {
                    n for n in out_numbers if n not in src_numbers and not _is_year_like(n)
                }
                for n in hallucinated:
                    warnings.append(f"hallucinated_number:{n} in {blt.source_id}")

                # Text overlap with source
                overlap = fuzz.token_set_ratio(blt.text.lower(), src_bullet.text.lower())
                if overlap < self.min_text_overlap:
                    warnings.append(f"low_text_overlap:{blt.source_id} score={overlap}")

                # Allowed-claims overlap (the soft anti-hallucination check)
                claim_score = _best_claim_score(blt.text, src_bullet.effective_allowed_claims)
                threshold = (
                    self.min_claim_overlap
                    if src_bullet.evidence_level == "verified"
                    else self.min_claim_overlap_inferred
                )
                if claim_score < threshold:
                    warnings.append(
                        f"off_allowed_claims:{blt.source_id} score={claim_score} "
                        f"(evidence={src_bullet.evidence_level})"
                    )

                # Bullet length
                if len(blt.text) > self.max_bullet_len:
                    warnings.append(f"bullet_too_long:{blt.source_id} len={len(blt.text)}")

        # ---- Projects ----
        errors.extend(project_policy_errors(cv.selected_project_ids, self.profile))

        # ---- Skill categories ----
        if (
            cv.skills_profile_id
            and self.skill_profile_ids
            and cv.skills_profile_id not in self.skill_profile_ids
        ):
            warnings.append(f"unknown_skill_profile:{cv.skills_profile_id}")
        for cid in cv.skills_order:
            if cid not in self.skill_category_ids:
                warnings.append(f"unknown_skill_category:{cid}")
        for block in cv.selected_skills:
            cid = block.category_id
            skills = block.skills
            if cid not in self.skill_category_ids:
                warnings.append(f"unknown_selected_skill_category:{cid}")
                continue
            for skill in skills:
                if skill.lower() not in self.allowed_skills_lower:
                    warnings.append(f"unknown_selected_skill:{skill}")

        fields = {"cv_title": cv.cv_title}
        if isinstance(cv, AdaptedCV):
            fields["professional_summary"] = cv.professional_summary
        warnings.extend(evidence_metadata_warnings(fields))

        # Summary checks apply only after the second call; never seed or slice it.
        if isinstance(cv, AdaptedCV):
            if len(cv.professional_summary) > self.max_summary_len:
                warnings.append(f"summary_too_long:len={len(cv.professional_summary)}")
            summary_lines = [line for line in cv.professional_summary.splitlines() if line.strip()]
            if len(summary_lines) > self.max_summary_lines:
                warnings.append(f"summary_too_many_lines:lines={len(summary_lines)}")

        return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings)

    def auto_fix(self, cv: _CV) -> tuple[_CV, list[str]]:
        """Restore static experiences and skills; project failures require a new draft."""
        removed: list[str] = []
        cleaned_experiences = profile_experiences(self.profile)
        if cv.selected_experiences != cleaned_experiences:
            removed.append("experience_content:restored_from_profile")

        valid_skills = [cid for cid in cv.skills_order if cid in self.skill_category_ids]
        valid_selected_skills = []
        for block in cv.selected_skills:
            cid = block.category_id
            skills = block.skills
            if cid not in self.skill_category_ids:
                removed.append(f"selected_skill_category:{cid}")
                continue
            cleaned_skills: list[str] = []
            seen: set[str] = set()
            for skill in skills:
                canonical = self.allowed_skills_by_lower.get(skill.lower())
                if not canonical:
                    removed.append(f"selected_skill:{skill}")
                    continue
                if canonical not in seen:
                    cleaned_skills.append(canonical)
                    seen.add(canonical)
            if cleaned_skills:
                valid_selected_skills.append(
                    SkillSelectionBlock(category_id=cid, skills=cleaned_skills)
                )
        skill_profile_id = (
            cv.skills_profile_id
            if not self.skill_profile_ids or cv.skills_profile_id in self.skill_profile_ids
            else "mixed"
        )

        cleaned = cv.model_copy(
            update={
                "selected_experiences": cleaned_experiences,
                "skills_profile_id": skill_profile_id,
                "selected_skills": valid_selected_skills,
                "skills_order": valid_skills,
            }
        )
        return cleaned, removed
