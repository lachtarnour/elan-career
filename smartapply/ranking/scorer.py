"""Composite job scoring — combines semantic similarity with cheap signals.

Weights (sum to 1.0):
- 0.30 semantic similarity (embeddings)
- 0.25 skills overlap
- 0.10 title similarity
- 0.25 seniority match
- 0.05 location match
- 0.05 domain match
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from types import SimpleNamespace
from typing import Protocol

from rapidfuzz import fuzz
from unidecode import unidecode

from smartapply.filtering.experience_requirements import assess_candidate_experience
from smartapply.filtering.preferences import ruleset_from_preferences
from smartapply.filtering.seniority import assess_seniority
from smartapply.filtering.source_facts import build_filter_facts
from smartapply.filtering.types import HasJobFields
from smartapply.profile import Profile
from smartapply.ranking.embeddings import (
    EmbeddingsProvider,
    cosine_similarity,
    get_embeddings_provider,
)

WEIGHTS = {
    "semantic": 0.30,
    "skills": 0.25,
    "title": 0.10,
    # Seniority weighs heavy: the candidate is junior-to-mid and senior /
    # lead postings must be pushed down hard, even when other signals look
    # good (ESN job ads often look attractive on paper).
    "seniority": 0.25,
    # Search location is chosen per run in the macOS app. Ranking must remain
    # neutral instead of re-applying the profile's old France/Paris defaults.
    "location": 0.05,
    "domain": 0.05,
}


@dataclass
class ScoreComponents:
    semantic: float
    skills: float
    title: float
    seniority: float
    location: float
    domain: float
    cap: float | None = None

    @property
    def final(self) -> float:
        weighted = (
            WEIGHTS["semantic"] * self.semantic
            + WEIGHTS["skills"] * self.skills
            + WEIGHTS["title"] * self.title
            + WEIGHTS["seniority"] * self.seniority
            + WEIGHTS["location"] * self.location
            + WEIGHTS["domain"] * self.domain
        )
        return min(weighted, self.cap) if self.cap is not None else weighted

    def to_dict(self) -> dict[str, float | None]:
        data: dict[str, float | None] = {
            "semantic": round(self.semantic, 4),
            "skills": round(self.skills, 4),
            "title": round(self.title, 4),
            "seniority": round(self.seniority, 4),
            "location": round(self.location, 4),
            "domain": round(self.domain, 4),
            "final": round(self.final, 4),
        }
        if self.cap is not None:
            data["cap"] = round(self.cap, 4)
        return data


class ScorableJob(Protocol):
    title: str
    description: str
    location: str | None


def _norm(s: str | None) -> str:
    return unidecode((s or "").lower())


def _skill_lexical_form(value: str | None) -> str:
    """Normalize separators without erasing meaningful skill punctuation."""
    normalized = _norm(value)
    normalized = re.sub(r"\s*([/&])\s*", r"\1", normalized)
    normalized = re.sub(r"(?<=[a-z0-9])[-_](?=[a-z0-9])", " ", normalized)
    return " ".join(normalized.split())


@lru_cache(maxsize=512)
def _skill_pattern(skill: str) -> re.Pattern[str]:
    """Compile one catalogue skill as an independent lexical term."""
    body = re.escape(skill)
    if len(skill) == 1 and skill.isalnum():
        # Single-letter languages must not match words, R&D, C++, or C#.
        boundary_chars = "a-z0-9+#&"
        return re.compile(rf"(?<![{boundary_chars}]){body}(?![{boundary_chars}])")

    left = r"(?<![a-z0-9])"
    # Accept version suffixes such as C++17, while rejecting C++Builder.
    right = r"(?![a-z+#])" if skill[-1] in "+#" else r"(?![a-z0-9])"
    return re.compile(f"{left}{body}{right}")


def build_profile_text(profile: Profile) -> str:
    """Aggregate the profile into a single document for embedding."""
    parts = [profile.identity.title, profile.identity.summary]
    for category in profile.skills.categories:
        parts.append(category.name + ": " + ", ".join(category.skills))
    for exp in profile.experiences:
        parts.append(f"{exp.title} at {exp.company}: " + " ".join(b.text for b in exp.bullets))
    for proj in profile.projects:
        parts.append(f"{proj.name}: {proj.description}\nKeywords: {', '.join(proj.keywords)}")
    return "\n".join(parts)


def build_job_text(job: ScorableJob) -> str:
    return f"{job.title}\n{job.description}"


def _analytics_without_python(title: str, description: str) -> bool:
    analytics_title = "data analyst" in title or "analytics" in title or " bi" in f" {title}"
    excludes_python = (
        "pas de developpement python" in description
        or "pas de python" in description
        or "no python" in description
        or "without python" in description
    )
    return analytics_title and excludes_python


class JobScorer:
    """Compute composite scores against a candidate profile."""

    def __init__(
        self,
        profile: Profile,
        embeddings: EmbeddingsProvider | None = None,
    ):
        self.profile = profile
        self.embeddings = embeddings or get_embeddings_provider()
        self._profile_vector: list[float] | None = None
        skill_terms = {
            normalized
            for skill in profile.skills.allowed_skills
            if (normalized := _skill_lexical_form(skill))
        }
        self._allowed_skill_patterns = tuple(
            _skill_pattern(skill) for skill in sorted(skill_terms)
        )
        self._filter_rules = ruleset_from_preferences(profile.preferences)

    # -------------------- score components --------------------

    def _semantic_score(self, job: ScorableJob, vec_cache: dict[int, list[float]]) -> float:
        if self._profile_vector is None:
            self._profile_vector = self.embeddings.embed_one(build_profile_text(self.profile))
        key = id(job)
        if key not in vec_cache:
            vec_cache[key] = self.embeddings.embed_one(build_job_text(job))
        sim = cosine_similarity(self._profile_vector, vec_cache[key])
        # Map [-1, 1] -> [0, 1]
        return max(0.0, (sim + 1.0) / 2.0)

    def _skills_score(self, job: ScorableJob) -> float:
        if not self._allowed_skill_patterns:
            return 0.0
        text = _skill_lexical_form(f"{job.description} {job.title}")
        if _analytics_without_python(_norm(job.title), _norm(job.description)):
            return 0.0
        hits = sum(pattern.search(text) is not None for pattern in self._allowed_skill_patterns)
        # Saturate at 8 matches
        return min(1.0, hits / 8.0)

    def _title_score(self, job: ScorableJob) -> float:
        target_roles = self.profile.preferences.target_roles
        if not target_roles:
            return 0.5
        title = _norm(job.title)
        best = max(fuzz.token_set_ratio(title, _norm(r)) for r in target_roles)
        if _analytics_without_python(title, _norm(job.description)):
            return min(best / 100.0, 0.35)
        return best / 100.0

    def _seniority_score(self, job: ScorableJob) -> float:
        seniority = assess_seniority(
            job.title,
            job.description,
            configured_title_markers=self._filter_rules.seniority_title_hard_reject,
        )
        if seniority.blocked:
            return 0.10

        view = self._job_view(job)
        experience = assess_candidate_experience(
            title=job.title,
            description=job.description,
            facts=build_filter_facts(view),
            max_required_years=self._filter_rules.max_required_years,
        )
        requirements = experience.resolved_mandatory_years
        if requirements:
            required_years = max(requirements)
            if required_years >= 10:
                return 0.10
            if required_years >= 6:
                return 0.20
            if required_years >= 5:
                return 0.25
            if required_years >= 4:
                return 0.40
            if required_years >= 3:
                return 0.90
            if required_years >= 2:
                return 0.95

        title = _norm(job.title)
        description = _norm(job.description)
        if re.search(r"\b(?:junior|debutant)\b", title):
            return 1.0
        if re.search(
            r"\b(?:entry[- ]level|debutant(?:e)?s?\s+accepte(?:e)?s?|"
            r"no\s+experience\s+(?:is\s+)?required|sans\s+experience)\b",
            description,
        ):
            return 1.0
        return 0.7

    @staticmethod
    def _job_view(job: ScorableJob) -> HasJobFields:
        return SimpleNamespace(
            title=job.title,
            company=str(getattr(job, "company", "") or ""),
            description=job.description,
            location=getattr(job, "location", None),
            contract_type=getattr(job, "contract_type", None),
            remote_policy=getattr(job, "remote_policy", None),
            application_url=getattr(job, "application_url", None),
            source=str(getattr(job, "source", "") or ""),
            source_data=getattr(job, "source_data", None),
        )

    def _location_score(self, job: ScorableJob) -> float:
        """Stay neutral: the selected search location is authoritative."""
        return 0.5

    def _domain_score(self, job: ScorableJob) -> float:
        domains = [_norm(d) for d in self.profile.preferences.domains_of_interest]
        if not domains:
            return 0.5
        text = _norm(job.description) + " " + _norm(job.title)
        hits = sum(1 for d in domains if d in text)
        return min(1.0, hits / 3.0)

    # -------------------- public API --------------------

    def score(
        self, job: ScorableJob, vec_cache: dict[int, list[float]] | None = None
    ) -> ScoreComponents:
        cache = vec_cache if vec_cache is not None else {}
        return ScoreComponents(
            semantic=self._semantic_score(job, cache),
            skills=self._skills_score(job),
            title=self._title_score(job),
            seniority=self._seniority_score(job),
            location=self._location_score(job),
            domain=self._domain_score(job),
            cap=None,
        )

    def rank(
        self,
        jobs: list[ScorableJob],
        top_k: int | None = None,
    ) -> list[tuple[ScorableJob, ScoreComponents]]:
        cache: dict[int, list[float]] = {}
        if jobs:
            if self._profile_vector is None:
                vectors = self.embeddings.embed(
                    [build_profile_text(self.profile)] + [build_job_text(job) for job in jobs]
                )
                self._profile_vector = vectors[0]
                job_vectors = vectors[1:]
            else:
                job_vectors = self.embeddings.embed([build_job_text(job) for job in jobs])
            cache.update({id(job): vector for job, vector in zip(jobs, job_vectors, strict=True)})
        results = [(j, self.score(j, cache)) for j in jobs]
        results.sort(key=lambda r: r[1].final, reverse=True)
        if top_k is not None:
            return results[:top_k]
        return results
