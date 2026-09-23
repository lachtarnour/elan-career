"""Generate personalization, then assemble a CV with static profile experiences."""

from __future__ import annotations

import re

from smartapply.cv.assembly import assemble_cv
from smartapply.cv.project_selection import (
    MIN_PROJECTS,
    selectable_project_capacity,
    validate_project_selection,
)
from smartapply.cv.role_contracts import apply_contract
from smartapply.cv.selector import CvBlockSelector, SelectionResult
from smartapply.llm import (
    AdaptedCV,
    AssembledCV,
    CVSelection,
    JobAnalysis,
    LLMError,
    LLMProvider,
    MotivationLetter,
    SkillSelectionBlock,
    get_llm_provider,
)
from smartapply.llm.prompts import cv_adaptation
from smartapply.profile import Profile
from smartapply.ranking.embeddings import (
    EmbeddingsProvider,
    get_embeddings_provider,
)
from smartapply.utils.skill_terms import normalize_skill_term, skill_term_matches


class CvAdapter:
    def __init__(
        self,
        profile: Profile,
        llm: LLMProvider | None = None,
        embeddings: EmbeddingsProvider | None = None,
    ):
        self.profile = profile
        self.llm = llm or get_llm_provider()
        self.embeddings = embeddings or get_embeddings_provider()
        self.selector = CvBlockSelector(self.embeddings)

    def adapt(
        self,
        analysis: JobAnalysis,
        *,
        job_title: str,
        job_company: str,
        job_description: str = "",
        job_id: int | None = None,
        refresh_cache: bool = False,
    ) -> tuple[AssembledCV, SelectionResult]:
        capacity = selectable_project_capacity(self.profile)
        if capacity < MIN_PROJECTS:
            raise LLMError(
                f"Insufficient eligible projects: policy allows {capacity}, "
                f"but each CV requires at least {MIN_PROJECTS}"
            )
        selection = self.selector.select(self.profile, analysis)
        prompt = cv_adaptation.build_user_prompt(
            profile=self.profile,
            analysis=analysis,
            job_title=job_title,
            job_company=job_company,
            job_description=job_description,
            # Keep the profile prefix complete and in canonical order so it is
            # identical across offers and eligible for OpenAI prompt caching.
            selected_experiences=list(self.profile.experiences),
            selected_projects=list(self.profile.projects),
        )
        # Finalize the complete selection before any summary or letter is written.
        for attempt in range(2):
            personalization = self.llm.complete_json(
                system=cv_adaptation.build_system_prompt(),
                user=prompt,
                schema=CVSelection,
                model=self.llm.smart_model,
                temperature=0.3,
                purpose="cv_adaptation",
                job_id=job_id,
                refresh_cache=refresh_cache or attempt > 0,
            )
            errors = validate_project_selection(
                personalization, self.profile, analysis, job_description=job_description
            )
            if not errors:
                break
            if attempt == 1:
                raise LLMError("Project selection could not be finalized: " + "; ".join(errors))
            prompt += (
                "\n\nThe previous personalization violated project selection rules:\n- "
                + "\n- ".join(errors)
                + "\nRegenerate the complete CV selection, without a summary, "
                "using exactly three unique eligible projects and grounded reasons. "
                "Prefer direct matches, then the strongest primary projects with "
                "honest transferable relevance. Never invent a direct specialty match."
            )
        adapted = assemble_cv(personalization, self.profile)
        adapted = self._ensure_supported_offer_skills(adapted, analysis)
        adapted = self._apply_role_family_contract(adapted, analysis, job_title)
        project_by_id = {project.id: project for project in self.profile.projects}
        selection.projects = [project_by_id[pid] for pid in adapted.selected_project_ids]
        return adapted, selection

    def adapt_application(
        self,
        analysis: JobAnalysis,
        *,
        job_title: str,
        job_company: str,
        language: str = "fr",
        job_id: int | None = None,
        refresh_cache: bool = False,
        job_description: str = "",
    ) -> tuple[AdaptedCV, MotivationLetter, SelectionResult]:
        """Compatibility entry point using the same sequential, reviewed flow.

        Text checks remain advisory for document availability. This tuple-only
        API retains diagnostics in CV warnings; the pipeline also persists the
        complete review audit.
        """
        from smartapply.cv.letter_writer import ApplicationTextWriter
        from smartapply.cv.validator import CvValidator

        assembled, selection = self.adapt(
            analysis,
            job_title=job_title,
            job_company=job_company,
            job_description=job_description,
            job_id=job_id,
            refresh_cache=refresh_cache,
        )
        validator = CvValidator(self.profile)
        if not validator.validate(assembled).ok:
            assembled, _ = validator.auto_fix(assembled)
        if not validator.validate(assembled).ok:
            raise LLMError("CV structure could not be finalized before text generation")
        writer = ApplicationTextWriter(self.profile, self.llm)
        context = dict(
            job_title=job_title,
            job_company=job_company,
            job_description=job_description,
            language=language,
            job_id=job_id,
            refresh_cache=refresh_cache,
        )
        adapted, letter = writer.generate_texts(assembled, analysis, **context)
        reviewed = writer.review(adapted, letter, analysis, **context)
        warnings = list(
            dict.fromkeys(
                [
                    *reviewed.cv.warnings,
                    *reviewed.warnings,
                    *(f"validation_error:{error}" for error in reviewed.errors),
                ]
            )
        )
        return reviewed.cv.model_copy(update={"warnings": warnings}), reviewed.letter, selection

    def _ensure_summary_skills_visible(self, adapted: AdaptedCV) -> AdaptedCV:
        """Legacy utility; the two-stage generation never mutates skills from a summary."""
        summary = adapted.professional_summary or ""
        if not summary.strip():
            return adapted

        category_by_skill: dict[str, str] = {}
        canonical_by_skill: dict[str, str] = {}
        for category in self.profile.skills.categories:
            for skill in category.skills:
                key = skill.lower()
                category_by_skill.setdefault(key, category.id)
                canonical_by_skill.setdefault(key, skill)

        selected_by_category: dict[str, list[str]] = {
            block.category_id: list(block.skills) for block in adapted.selected_skills
        }
        category_order = [block.category_id for block in adapted.selected_skills]
        already_selected = {
            skill.lower() for skills in selected_by_category.values() for skill in skills
        }

        for skill_key in sorted(canonical_by_skill, key=len, reverse=True):
            if skill_key in already_selected:
                continue
            canonical = canonical_by_skill[skill_key]
            if not self._summary_mentions_skill(summary, canonical):
                continue
            category_id = category_by_skill[skill_key]
            if category_id not in selected_by_category:
                selected_by_category[category_id] = []
                category_order.append(category_id)
            selected_by_category[category_id].append(canonical)
            already_selected.add(skill_key)

        if not selected_by_category:
            return adapted

        skills_order = list(adapted.skills_order)
        for category_id in category_order:
            if category_id not in skills_order:
                skills_order.append(category_id)

        return adapted.model_copy(
            update={
                "selected_skills": [
                    SkillSelectionBlock(category_id=category_id, skills=skills)
                    for category_id in category_order
                    if (skills := selected_by_category.get(category_id))
                ],
                "skills_order": skills_order,
            }
        )

    def _ensure_supported_offer_skills(
        self,
        adapted: AssembledCV,
        analysis: JobAnalysis,
    ) -> AssembledCV:
        """Deterministically keep supported offer-required skills visible.

        The LLM decides the section, but this guard fixes a common miss:
        offers often phrase skills as "Python programming" or "Experience
        with PyTorch". If the canonical profile skill is whitelisted, it must
        appear in the CV skills block; unsupported terms such as Kubernetes
        remain excluded.
        """
        category_by_skill: dict[str, str] = {}
        canonical_by_skill: dict[str, str] = {}
        for category in self.profile.skills.categories:
            for skill in category.skills:
                key = skill.lower()
                category_by_skill.setdefault(key, category.id)
                canonical_by_skill.setdefault(key, skill)

        selected_by_category: dict[str, list[str]] = {
            block.category_id: list(block.skills) for block in adapted.selected_skills
        }
        category_order = [block.category_id for block in adapted.selected_skills]

        for term in list(analysis.required_skills) + list(analysis.cv_keywords_to_include):
            for skill_key in self._matched_allowed_skill_keys(term, canonical_by_skill):
                category_id = category_by_skill[skill_key]
                canonical = canonical_by_skill[skill_key]
                if category_id not in selected_by_category:
                    selected_by_category[category_id] = []
                    category_order.append(category_id)
                existing = {skill.lower() for skill in selected_by_category[category_id]}
                if canonical.lower() not in existing:
                    selected_by_category[category_id].append(canonical)

        if not selected_by_category:
            return adapted

        skills_order = list(adapted.skills_order)
        for category_id in category_order:
            if category_id not in skills_order:
                skills_order.append(category_id)

        return adapted.model_copy(
            update={
                "selected_skills": [
                    SkillSelectionBlock(category_id=category_id, skills=skills)
                    for category_id in category_order
                    if (skills := selected_by_category.get(category_id))
                ],
                "skills_order": skills_order,
            }
        )

    def _apply_role_family_contract(
        self,
        adapted: AssembledCV,
        analysis: JobAnalysis,
        job_title: str,
    ) -> AssembledCV:
        """Strip off-role skills and anchor a coherent baseline per family."""
        allowed_skills_lower = {s.lower() for s in self.profile.skills.allowed_skills}
        supported_skills_by_category = {
            category.id: list(category.skills) for category in self.profile.skills.categories
        }
        adapted, _family = apply_contract(
            adapted,
            analysis=analysis,
            job_title=job_title,
            allowed_skills_lower=allowed_skills_lower,
            supported_skills_by_category=supported_skills_by_category,
        )
        return adapted

    @staticmethod
    def _summary_mentions_skill(summary: str, skill: str) -> bool:
        normalized_summary = normalize_skill_term(summary)
        normalized_skill = normalize_skill_term(skill)
        if not normalized_summary or not normalized_skill:
            return False
        if len(normalized_skill) <= 2:
            return normalized_skill in re.findall(r"[a-z0-9+#.]+", normalized_summary)
        return bool(
            re.search(
                rf"(?<![a-z0-9+#]){re.escape(normalized_skill)}(?![a-z0-9+#])",
                normalized_summary,
            )
        )

    @staticmethod
    def _matched_allowed_skill_keys(
        term: str,
        canonical_by_skill: dict[str, str],
    ) -> list[str]:
        normalized = normalize_skill_term(term)
        if not normalized:
            return []

        normalized_by_skill = {
            skill_key: normalize_skill_term(skill_key) for skill_key in canonical_by_skill
        }
        exact_matches = [
            skill_key
            for skill_key, skill_norm in normalized_by_skill.items()
            if normalized == skill_norm
        ]
        if exact_matches:
            return sorted(exact_matches, key=len, reverse=True)[:1]

        matches: list[str] = []
        for skill_key in sorted(canonical_by_skill, key=len, reverse=True):
            if skill_term_matches(term, skill_key):
                matches.append(skill_key)

        filtered: list[str] = []
        for skill_key in matches:
            skill_norm = normalized_by_skill[skill_key]
            if any(
                skill_norm != other_norm and skill_norm in other_norm
                for other_key, other_norm in normalized_by_skill.items()
                if other_key in matches
            ):
                continue
            if skill_key not in filtered:
                filtered.append(skill_key)
        return filtered
