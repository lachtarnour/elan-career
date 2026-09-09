"""CV adapter — produces an AdaptedCV via the LLM, anchored on the profile."""

from __future__ import annotations

import re

from smartapply.cv.motivation_validator import (
    mentioned_project_ids,
    normalize_french_elisions,
)
from smartapply.cv.role_contracts import apply_contract
from smartapply.cv.selector import CvBlockSelector, SelectionResult
from smartapply.llm import (
    AdaptedBullet,
    AdaptedCV,
    AdaptedExperience,
    ApplicationDraft,
    JobAnalysis,
    LLMProvider,
    MotivationLetter,
    SkillSelectionBlock,
    get_llm_provider,
)
from smartapply.llm.prompts import application_draft, cv_adaptation
from smartapply.profile import Profile
from smartapply.ranking.embeddings import (
    EmbeddingsProvider,
    get_embeddings_provider,
)
from smartapply.utils.skill_terms import normalize_skill_term

_CV_HEAD_UNSUPPORTED_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("Databricks-driven", "SQL/Spark"),
    ("Databricks", "SQL and Spark"),
    ("Power BI", "SQL analytics"),
    ("SAP", "data reliability"),
    ("ETL/ELT", "SQL/Spark workflows"),
    ("Data Cloud", "SQL/Spark"),
    ("data engineering", "SQL/Spark workflows"),
    ("Data Engineer", "SQL/Spark Specialist"),
    ("Reporting", "SQL analytics"),
    ("reporting", "SQL analytics"),
    ("GCP", "cloud-adjacent"),
)

_MIN_PROJECT_COUNT = 2
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
        adapted = self.llm.complete_json(
            system=cv_adaptation.SYSTEM,
            user=prompt,
            schema=AdaptedCV,
            model=self.llm.smart_model,
            temperature=0.3,
            purpose="cv_adaptation",
            job_id=job_id,
        )
        adapted = self._ensure_supported_offer_skills(adapted, analysis)
        adapted = self._ensure_summary_skills_visible(adapted)
        adapted = self._apply_role_family_contract(adapted, analysis, job_title)
        adapted = self._avoid_unsupported_cv_head_terms(adapted, analysis)
        adapted = self._enforce_complete_experiences(adapted)
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
    ) -> tuple[AdaptedCV, MotivationLetter, SelectionResult]:
        selection = self.selector.select(self.profile, analysis)
        prompt = application_draft.build_user_prompt(
            profile=self.profile,
            analysis=analysis,
            job_title=job_title,
            job_company=job_company,
            # The offer is the dynamic suffix; the complete profile stays an
            # identical, cacheable prefix for every application.
            selected_experiences=list(self.profile.experiences),
            selected_projects=list(self.profile.projects),
            language=language,
        )
        draft = self.llm.complete_json(
            system=application_draft.SYSTEM,
            user=prompt,
            schema=ApplicationDraft,
            model=self.llm.smart_model,
            temperature=0.3,
            purpose="application_draft",
            job_id=job_id,
            refresh_cache=refresh_cache,
        )
        adapted = draft.to_cv()
        adapted = self._ensure_supported_offer_skills(adapted, analysis)
        adapted = self._ensure_summary_skills_visible(adapted)
        adapted = self._apply_role_family_contract(adapted, analysis, job_title)
        adapted = self._avoid_unsupported_cv_head_terms(adapted, analysis)
        adapted = self._enforce_complete_experiences(adapted)
        adapted = self._enforce_project_count(adapted)
        adapted = self._enforce_summary_length(adapted)
        letter = draft.to_motivation_letter()
        letter = letter.model_copy(
            update={
                "subject": normalize_french_elisions(letter.subject, language=language),
                "body": normalize_french_elisions(letter.body, language=language),
            }
        )
        adapted = self._ensure_letter_projects_visible(adapted, letter)
        adapted = self._enforce_project_count(adapted)
        return adapted, letter, selection

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

    def _enforce_complete_experiences(self, adapted: AdaptedCV) -> AdaptedCV:
        """Keep the professional experience block complete and source-grounded.

        The LLM may rewrite a bullet, but it must not decide that the main
        experience suddenly has one bullet. Missing bullets are restored from
        the source profile, in profile order.
        """
        existing_by_exp = {exp.source_id: exp for exp in adapted.selected_experiences}
        restored_experiences = 0
        restored_bullets = 0
        complete_experiences: list[AdaptedExperience] = []

        for source_exp in self.profile.experiences:
            existing_exp = existing_by_exp.get(source_exp.id)
            existing_bullets = {
                bullet.source_id: bullet
                for bullet in (existing_exp.bullets if existing_exp else [])
            }
            if existing_exp is None:
                restored_experiences += 1

            bullets: list[AdaptedBullet] = []
            for source_bullet in source_exp.bullets:
                bullet = existing_bullets.get(source_bullet.id)
                if bullet is None:
                    restored_bullets += 1
                    bullet = AdaptedBullet(
                        source_id=source_bullet.id,
                        text=source_bullet.text,
                    )
                bullets.append(bullet)

            complete_experiences.append(AdaptedExperience(source_id=source_exp.id, bullets=bullets))

        warnings = list(adapted.warnings)
        if restored_experiences:
            warnings.append(f"experience_sections_restored:{restored_experiences}")
        if restored_bullets:
            warnings.append(f"experience_bullets_restored:{restored_bullets}")

        return adapted.model_copy(
            update={
                "selected_experiences": complete_experiences,
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

    def _avoid_unsupported_cv_head_terms(
        self,
        adapted: AdaptedCV,
        analysis: JobAnalysis,
    ) -> AdaptedCV:
        """Keep unsupported offer/vendor terms out of the CV header.

        The CV title and summary read as candidate claims. When the offer
        mentions a vendor/tool or broad family that the profile does not
        support directly, replace only those headline occurrences with a
        supported adjacent phrase. Bullets and skills remain governed by the
        existing validator/contract path.
        """
        offer_terms: set[str] = set()
        for raw_term in (
            list(analysis.required_skills)
            + list(analysis.cv_keywords_to_include)
            + [analysis.role_type]
        ):
            term = str(raw_term).strip()
            if term:
                offer_terms.add(term.lower())
        if not offer_terms:
            return adapted

        title = adapted.cv_title
        summary = adapted.professional_summary
        changed: list[str] = []
        for raw, replacement in _CV_HEAD_UNSUPPORTED_REPLACEMENTS:
            raw_lower = raw.lower()
            if not any(raw_lower in term or term in raw_lower for term in offer_terms):
                continue
            pattern = re.compile(re.escape(raw), flags=re.IGNORECASE)
            if pattern.search(title) or pattern.search(summary):
                changed.append(raw)
            title = pattern.sub(replacement, title)
            summary = pattern.sub(replacement, summary)

        if not changed:
            return adapted

        warnings = list(adapted.warnings)
        warnings.append("unsupported_cv_head_terms_replaced:" + ",".join(sorted(set(changed))))
        return adapted.model_copy(
            update={
                "cv_title": self._tidy_headline_text(title),
                "professional_summary": self._tidy_headline_text(summary),
                "warnings": warnings,
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
    def _tidy_headline_text(text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        cleaned = re.sub(r"\s+([,;:|])", r"\1", cleaned)
        cleaned = re.sub(r"([|,;:])\s*([|,;:])+", r"\1", cleaned)
        return cleaned

    @staticmethod
    def _summary_mentions_skill(summary: str, skill: str) -> bool:
        normalized_summary = " ".join((summary or "").lower().split())
        normalized_skill = " ".join((skill or "").lower().split())
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
            skill_norm = normalized_by_skill[skill_key]
            if len(skill_norm) <= 2:
                if normalized == skill_norm:
                    matches.append(skill_key)
                continue
            if normalized == skill_norm or skill_norm in normalized or normalized in skill_norm:
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
