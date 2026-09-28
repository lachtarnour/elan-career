"""Tests for the ranking module — embeddings, scoring, rank."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from smartapply.profile import get_profile
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
