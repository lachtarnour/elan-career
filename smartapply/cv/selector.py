"""Embedding diagnostics for offer relevance, not the final project decision.

Personalization sees the complete profile and its project policies. The adapter
replaces ``SelectionResult.projects`` with the validated final selection.
"""

from __future__ import annotations

from dataclasses import dataclass

from smartapply.llm.schemas import JobAnalysis
from smartapply.profile import Experience, Profile, Project
from smartapply.ranking.embeddings import EmbeddingsProvider, cosine_similarity


@dataclass
class SelectionResult:
    experiences: list[Experience]
    projects: list[Project]
    experience_scores: dict[str, float]
    project_scores: dict[str, float]


class CvBlockSelector:
    def __init__(self, embeddings: EmbeddingsProvider):
        self.embeddings = embeddings

    @staticmethod
    def _experience_text(exp: Experience) -> str:
        bullets = " ".join(b.text for b in exp.bullets)
        return f"{exp.title} at {exp.company}: {bullets}"

    @staticmethod
    def project_text(p: Project) -> str:
        """Return the canonical text used to embed a profile project."""
        keywords = ", ".join(p.keywords)
        return f"{p.name}: {p.description}\nKeywords: {keywords}"

    @staticmethod
    def _analysis_text(analysis: JobAnalysis) -> str:
        return (
            f"Role: {analysis.role_type}. Domain: {analysis.domain}.\n"
            f"Tasks: {' '.join(analysis.main_tasks)}\n"
            f"Required: {', '.join(analysis.required_skills)}\n"
            f"Keywords: {', '.join(analysis.cv_keywords_to_include)}"
        )

    def select(
        self,
        profile: Profile,
        analysis: JobAnalysis,
        *,
        top_k_experiences: int = 5,
        top_k_projects: int = 10,
    ) -> SelectionResult:
        # One batched provider call is both faster and easier to cache than a
        # separate request for every static profile block.
        texts = [self._analysis_text(analysis)]
        texts.extend(self._experience_text(exp) for exp in profile.experiences)
        eligible_projects = [
            project for project in profile.projects if project.selection_policy.tier != "disabled"
        ]
        texts.extend(self.project_text(project) for project in eligible_projects)
        vectors = self.embeddings.embed(texts)
        analysis_vec = vectors[0]
        experience_offset = 1
        project_offset = experience_offset + len(profile.experiences)

        exp_scores: dict[str, float] = {}
        for index, exp in enumerate(profile.experiences):
            v = vectors[experience_offset + index]
            exp_scores[exp.id] = cosine_similarity(analysis_vec, v)

        proj_scores: dict[str, float] = {}
        for index, proj in enumerate(eligible_projects):
            v = vectors[project_offset + index]
            proj_scores[proj.id] = cosine_similarity(analysis_vec, v)

        # Always keep the most recent experience — recency matters even if
        # the semantic match is lower than older roles.
        ordered_experiences = sorted(
            profile.experiences,
            key=lambda e: exp_scores.get(e.id, 0.0),
            reverse=True,
        )
        if profile.experiences:
            most_recent = profile.experiences[0]
            if most_recent not in ordered_experiences[:top_k_experiences]:
                ordered_experiences = [most_recent] + [
                    e for e in ordered_experiences if e.id != most_recent.id
                ]
        selected_experiences = ordered_experiences[:top_k_experiences]

        ordered_projects = sorted(
            eligible_projects,
            key=lambda p: proj_scores.get(p.id, 0.0),
            reverse=True,
        )
        selected_projects = ordered_projects[:top_k_projects]

        return SelectionResult(
            experiences=selected_experiences,
            projects=selected_projects,
            experience_scores=exp_scores,
            project_scores=proj_scores,
        )
