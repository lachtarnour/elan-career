"""Tests for the ranking module — embeddings, scoring, rank."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from smartapply.profile import get_profile
from smartapply.profile.schema import SkillCategory, Skills
from smartapply.ranking import (
    WEIGHTS,
    JobScorer,
    MockEmbeddingsProvider,
    ScoreComponents,
)


@dataclass
class FakeJob:
    title: str
    description: str
    location: str | None = None


def _scorer_with_allowed_skills(*skills: str) -> JobScorer:
    profile = get_profile().model_copy(deep=True)
    profile.skills = Skills(
        categories=[
            SkillCategory(id="test", name="Test skills", skills=list(skills))
        ]
    )
    return JobScorer(profile, embeddings=MockEmbeddingsProvider())


# ----------------- Embeddings -----------------


def test_openai_embeddings_are_persistently_cached_and_usage_is_recorded(
    isolated_db,
    monkeypatch,
) -> None:
    from sqlalchemy import func, select

    from smartapply.database import session_scope
    from smartapply.database.models import EmbeddingCache, LLMUsage
    from smartapply.ranking.embeddings import OpenAIEmbeddingsProvider

    calls: list[list[str]] = []
    provider = OpenAIEmbeddingsProvider(model="text-embedding-3-small", batch_size=8)
    provider._client = object()

    def fake_create(client, batch):  # noqa: ARG001
        calls.append(list(batch))
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=index, embedding=[float(index), 1.0])
                for index, _ in enumerate(batch)
            ],
            usage=SimpleNamespace(prompt_tokens=9, total_tokens=9),
        )

    monkeypatch.setattr(provider, "_create_embeddings", fake_create)
    first = provider.embed(["alpha", "beta", "alpha"])

    second_provider = OpenAIEmbeddingsProvider(model="text-embedding-3-small")
    second = second_provider.embed(["alpha", "beta", "alpha"])

    assert calls == [["alpha", "beta"]]
    assert first == second
    assert first[0] == first[2]
    with session_scope() as session:
        assert session.scalar(select(func.count(EmbeddingCache.id))) == 2
        usage = session.scalar(select(LLMUsage))
        assert usage is not None
        assert usage.purpose == "embeddings"
        assert usage.prompt_tokens == 9
        assert usage.cost_usd > 0


def test_local_embeddings_are_persistently_cached(isolated_db) -> None:
    from sqlalchemy import func, select

    from smartapply.database import session_scope
    from smartapply.database.models import EmbeddingCache
    from smartapply.ranking.embeddings import LocalEmbeddingsProvider

    calls: list[list[str]] = []

    class FakeVector:
        def __init__(self, values: list[float]):
            self.values = values

        def tolist(self) -> list[float]:
            return self.values

    class FakeModel:
        def encode(self, texts, *, normalize_embeddings):  # noqa: ANN001
            assert normalize_embeddings is True
            calls.append(list(texts))
            return [FakeVector([float(index), 1.0]) for index, _ in enumerate(texts)]

    provider = LocalEmbeddingsProvider(model="local-test")
    provider._model = FakeModel()
    first = provider.embed(["alpha", "beta", "alpha"])

    second_provider = LocalEmbeddingsProvider(model="local-test")
    second_provider._model = FakeModel()
    second = second_provider.embed(["alpha", "beta", "alpha"])

    assert calls == [["alpha", "beta"]]
    assert first == second
    assert first[0] == first[2]
    with session_scope() as session:
        assert session.scalar(select(func.count(EmbeddingCache.id))) == 2


# ----------------- Scoring -----------------


def test_score_components_final_uses_documented_weights() -> None:
    comp = ScoreComponents(
        semantic=1.0,
        skills=1.0,
        title=1.0,
        seniority=1.0,
        location=1.0,
        domain=1.0,
    )
    assert abs(comp.final - sum(WEIGHTS.values())) < 1e-9
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


@pytest.mark.parametrize(
    ("skill", "description"),
    [
        ("R", "Statistical modelling in R, with reproducible reports."),
        ("C++", "Build low-latency services in C++17."),
        (".NET", "Maintain .NET 8 services and APIs."),
        ("Machine learning", "Deploy machine-learning pipelines."),
    ],
)
def test_skills_score_matches_lexical_skill_terms(
    skill: str, description: str
) -> None:
    scorer = _scorer_with_allowed_skills(skill)

    score = scorer._skills_score(FakeJob("Engineer", description))

    assert score == 1 / 8


@pytest.mark.parametrize(
    ("skill", "description"),
    [
        ("R", "Research on Transformers and R&D tooling."),
        ("C++", "Maintain C and C# services."),
        (".NET", "Operate internet and network services."),
        ("Machine learning", "Combine machine vision with a learning culture."),
    ],
)
def test_skills_score_rejects_fragments_and_split_phrases(
    skill: str, description: str
) -> None:
    scorer = _scorer_with_allowed_skills(skill)

    score = scorer._skills_score(FakeJob("Engineer", description))

    assert score == 0.0


def test_scorer_uses_selected_location_neutrally() -> None:
    profile = get_profile()
    provider = MockEmbeddingsProvider()
    scorer = JobScorer(profile, embeddings=provider)

    great = FakeJob(
        title="Data Scientist NLP",
        description=(
            "Build RAG pipelines with PyTorch, Hugging Face and FAISS. "
            "Work on multimodal AI for HealthTech. Junior welcome."
        ),
        location="Paris, France",
    )
    bad = FakeJob(
        title="Sales Manager",
        description="Hit revenue quotas. 10 years of B2B experience required.",
        location="Berlin, Germany",
    )

    s_great = scorer.score(great)
    s_bad = scorer.score(bad)
    assert s_great.final > s_bad.final
    assert s_great.title > s_bad.title
    assert s_great.skills > s_bad.skills
    assert s_great.location == s_bad.location == 0.5


def test_scorer_rank_orders_top_k() -> None:
    profile = get_profile()
    scorer = JobScorer(profile, embeddings=MockEmbeddingsProvider())
    jobs = [
        FakeJob("Sales Manager", "Quotas, B2B", "Berlin"),
        FakeJob("Data Scientist", "PyTorch RAG FAISS multimodal AI", "Paris"),
        FakeJob("ML Engineer", "MLOps on AWS with Docker", "Lyon"),
        FakeJob("Receptionist", "Greet visitors", "Paris"),
    ]
    ranked = scorer.rank(jobs, top_k=2)
    assert len(ranked) == 2
    titles = [j.title for j, _ in ranked]
    assert "Data Scientist" in titles
    assert "Sales Manager" not in titles


def test_scorer_seniority_penalizes_15_years() -> None:
    profile = get_profile()
    scorer = JobScorer(profile, embeddings=MockEmbeddingsProvider())
    senior_only = FakeJob(
        title="Data Scientist",
        description="15+ years of experience required.",
        location="Paris",
    )
    s = scorer.score(senior_only)
    # seniority component should be small
    assert s.seniority < 0.3


def test_seniority_ranking_uses_candidate_requirements_not_raw_tokens() -> None:
    scorer = JobScorer(get_profile(), embeddings=MockEmbeddingsProvider())
    neutral_descriptions = (
        "Previous internship experience is accepted.",
        "Our company has 20 years of experience in machine learning.",
        "Five years of experience would be preferred.",
        "You may grow into a team lead role in the future.",
    )

    scores = [
        scorer.score(FakeJob("Data Scientist", description, "Paris")).seniority
        for description in neutral_descriptions
    ]

    assert scores == [0.7, 0.7, 0.7, 0.7]


def test_seniority_ranking_still_penalizes_real_mandatory_requirement() -> None:
    scorer = JobScorer(get_profile(), embeddings=MockEmbeddingsProvider())
    score = scorer.score(
        FakeJob(
            "Data Scientist",
            "Candidates must have at least 4 years of professional experience.",
            "Paris",
        )
    )

    assert score.seniority == 0.4


def test_ranking_replaces_scores_and_preserves_audit_namespaces(isolated_db) -> None:
    from datetime import datetime, timezone

    from smartapply.database import session_scope
    from smartapply.database.models import Job
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider())
    ingested = pipeline.ingest_text(
        text=(
            "Construire des modèles de machine learning avec Python. "
            "Deux ans d'expérience. CDI à Paris."
        ),
        title="Machine Learning Engineer",
        company="TraceCo",
        location="Paris",
    )
    job_id = ingested.job_ids[0]
    pipeline.filter_pending(job_ids=[job_id])

    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None and job.score is not None
        components = dict(job.score.components)
        components.update(
            {
                "rule_based": -1.0,
                "semantic": -1.0,
                "final": -1.0,
                "cap": 0.1,
                "hard_reject_backfill": {"schema_version": 1},
                "confirmed_false_rejection_recovery": {"schema_version": 1},
                "future_audit_namespace": {"source": "test"},
            }
        )
        job.score.components = components
        job.score.rule_based_score = 0.7
        job.score.computed_at = datetime(2000, 1, 1, tzinfo=timezone.utc)

    report = pipeline.rank_pending(job_ids=[job_id])

    assert report.ranked_ids == [job_id]
    with session_scope() as session:
        job = session.get(Job, job_id)
        assert job is not None and job.score is not None
        assert job.score.rule_based_score is None
        computed_at = job.score.computed_at
        if computed_at.tzinfo is None:
            computed_at = computed_at.replace(tzinfo=timezone.utc)
        assert computed_at > datetime(2000, 1, 1, tzinfo=timezone.utc)
        components = job.score.components
        assert "rule_based" not in components
        assert components["semantic"] != -1.0
        assert components["final"] != -1.0
        assert "cap" not in components
        assert components["hard_reject_backfill"] == {"schema_version": 1}
        assert components["confirmed_false_rejection_recovery"] == {
            "schema_version": 1
        }
        assert components["future_audit_namespace"] == {"source": "test"}
        assert components["filter_disposition"] == "pending_relevance"
        assert components["hard_reject"] == {
            "skipped": True, "skip_reason": "manual_addition", "gates": [],
        }
        assert components["manual_filter_bypass"]["origin"] == "manual_addition"
