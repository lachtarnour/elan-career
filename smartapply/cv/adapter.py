"""Generate personalization, then assemble a CV with static profile experiences."""

from __future__ import annotations

import re

from smartapply.cv.assembly import assemble_cv
from smartapply.cv.motivation_validator import mentioned_project_ids
from smartapply.cv.role_contracts import apply_contract
from smartapply.cv.selector import CvBlockSelector, SelectionResult
from smartapply.llm import (
    AdaptedCV,
    CVPersonalization,
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

_MIN_PROJECT_COUNT = 3
_MAX_PROJECT_COUNT = 4


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
        job_id: int | None = None,
        refresh_cache: bool = False,
    ) -> tuple[AdaptedCV, SelectionResult]:
        selection = self.selector.select(self.profile, analysis)
        prompt = cv_adaptation.build_user_prompt(
            profile=self.profile,
            analysis=analysis,
            job_title=job_title,
            job_company=job_company,
            # Keep the profile prefix complete and in canonical order so it is
            # identical across offers and eligible for OpenAI prompt caching.
            selected_experiences=list(self.profile.experiences),
            selected_projects=list(self.profile.projects),
        )
        personalization = self.llm.complete_json(
            system=cv_adaptation.SYSTEM,
            user=prompt,
            schema=CVPersonalization,
            model=self.llm.smart_model,
            temperature=0.3,
            purpose="cv_adaptation",
            job_id=job_id,
            refresh_cache=refresh_cache,
        )
        adapted = assemble_cv(personalization, self.profile)
        adapted = self._ensure_supported_offer_skills(adapted, analysis)
        adapted = self._ensure_summary_skills_visible(adapted)
        adapted = self._apply_role_family_contract(adapted, analysis, job_title)
        adapted = self._enforce_project_count(adapted)
        adapted = self._enforce_summary_length(adapted)
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

        The application pipeline additionally persists review diagnostics. This
        tuple-only API raises rather than returning an unreviewed application.
        """
        from smartapply.cv.letter_writer import ApplicationTextWriter
        from smartapply.cv.validator import CvValidator

        adapted, selection = self.adapt(
            analysis,
            job_title=job_title,
            job_company=job_company,
            job_id=job_id,
            refresh_cache=refresh_cache,
        )
        validator = CvValidator(self.profile)
        if not validator.validate(adapted).ok:
            adapted, _ = validator.auto_fix(adapted)
        if not validator.validate(adapted).ok:
            raise LLMError("CV structure could not be finalized before letter generation")
        writer = ApplicationTextWriter(self.profile, self.llm)
        context = dict(
            job_title=job_title,
            job_company=job_company,
            job_description=job_description,
            language=language,
            job_id=job_id,
            refresh_cache=refresh_cache,
        )
        letter = writer.generate_letter(adapted, analysis, **context)
        reviewed = writer.review(adapted, letter, analysis, **context)
        if reviewed.errors:
            raise LLMError("Application text review failed: " + "; ".join(reviewed.errors))
        return reviewed.cv, reviewed.letter, selection

    def _ensure_letter_projects_visible(
        self,
        adapted: AdaptedCV,
        letter: MotivationLetter,
        *,
        max_projects: int = _MAX_PROJECT_COUNT,
    ) -> AdaptedCV:
        """Keep the CV project list consistent with projects named in the letter."""
        mentioned = mentioned_project_ids(letter.body, self.profile)
        if not mentioned:
            return adapted

        selected = list(adapted.selected_project_ids)
        priority: list[str] = []
        for project_id in mentioned + selected:
            if project_id not in priority:
                priority.append(project_id)

        aligned = priority[:max_projects]
        if aligned == selected:
            return adapted
        return adapted.model_copy(update={"selected_project_ids": aligned})

    def _enforce_project_count(
        self,
        adapted: AdaptedCV,
        *,
        min_count: int = _MIN_PROJECT_COUNT,
        max_count: int = _MAX_PROJECT_COUNT,
    ) -> AdaptedCV:
        """Keep the rendered Projects section within the configured range."""
        project_by_id = {project.id: project for project in self.profile.projects}
        max_count = min(max_count, len(project_by_id))
        min_count = min(min_count, max_count)
        if max_count <= 0:
            return adapted

        selected: list[str] = []
        seen: set[str] = set()
        for project_id in adapted.selected_project_ids:
            if project_id in project_by_id and project_id not in seen:
                selected.append(project_id)
                seen.add(project_id)

        if len(selected) < min_count:
            context = self._adapted_project_context(adapted)
            remaining = [project for project in self.profile.projects if project.id not in seen]
            remaining.sort(
                key=lambda project: self._project_relevance(project, context),
                reverse=True,
            )
            for project in remaining:
                selected.append(project.id)
                seen.add(project.id)
                if len(selected) >= min_count:
                    break

        selected = selected[:max_count]
        if selected == adapted.selected_project_ids:
            return adapted

        warnings = list(adapted.warnings)
        marker = f"project_count_enforced:{len(selected)}"
        if marker not in warnings:
            warnings.append(marker)
        return adapted.model_copy(
            update={
                "selected_project_ids": selected,
                "warnings": warnings,
            }
        )

    def _adapted_project_context(self, adapted: AdaptedCV) -> str:
        return " ".join(
            [
                adapted.cv_title,
                adapted.professional_summary,
                " ".join(adapted.selected_project_ids),
                " ".join(
                    bullet.text for exp in adapted.selected_experiences for bullet in exp.bullets
                ),
            ]
        ).lower()

    @staticmethod
    def _project_relevance(project, context: str) -> tuple[int, int]:
        keywords = [kw.lower() for kw in getattr(project, "keywords", [])]
        name_tokens = re.findall(r"[a-z0-9]+", getattr(project, "name", "").lower())
        description_tokens = re.findall(
            r"[a-z0-9]+",
            getattr(project, "description", "").lower(),
        )
        score = sum(1 for keyword in keywords if keyword and keyword in context)
        score += sum(1 for token in name_tokens if len(token) > 2 and token in context)
        score += sum(1 for token in description_tokens if len(token) > 4 and token in context)
        return (score, len(keywords))

    def _enforce_summary_length(self, adapted: AdaptedCV) -> AdaptedCV:
        """Trim overlong summaries deterministically at a word boundary."""
        limit = self.profile.style_guide.max_summary_length
        summary = (adapted.professional_summary or "").strip()
        if len(summary) <= limit:
            return adapted

        truncated = summary[:limit].rstrip()
        for separator in (". ", "; ", ", "):
            idx = truncated.rfind(separator)
            if idx >= max(60, int(limit * 0.55)):
                truncated = truncated[: idx + 1].rstrip()
                break
        else:
            idx = truncated.rfind(" ")
            if idx > 0:
                truncated = truncated[:idx].rstrip()

        if not truncated.endswith((".", "!", "?")):
            truncated = truncated.rstrip(" ,;:") + "."
        warnings = [
            warning for warning in adapted.warnings if not warning.startswith("summary_too_long")
        ]
        warnings.append(f"summary_trimmed:len={len(summary)}")
        return adapted.model_copy(
            update={
                "professional_summary": truncated,
                "warnings": warnings,
            }
        )

    def _ensure_summary_skills_visible(self, adapted: AdaptedCV) -> AdaptedCV:
        """If a whitelisted skill is named in the summary, show it in Skills too."""
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
        adapted: AdaptedCV,
        analysis: JobAnalysis,
    ) -> AdaptedCV:
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
        adapted: AdaptedCV,
        analysis: JobAnalysis,
        job_title: str,
    ) -> AdaptedCV:
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
