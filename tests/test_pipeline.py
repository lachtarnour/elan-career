"""Integration tests for the end-to-end pipeline.

Every external dependency is mocked: scrapers, LLM and embeddings. The
pipeline is exercised against fictional profile evidence so the
anti-hallucination contract is validated too.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from smartapply.llm import (
    ApplicationTexts,
    CVSelection,
    DocumentTextReview,
    JobFitAssessment,
    MockLLMProvider,
    MotivationLetter,
    OfferAnalysis,
)
from smartapply.offers import RawJob
from smartapply.ranking import MockEmbeddingsProvider


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "test.db"
    output_dir = tmp_path / "output"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("OUTPUT_DIR", str(output_dir))

    from smartapply.config import get_settings

    get_settings.cache_clear()
    from smartapply.database.session import reset_engine_cache

    reset_engine_cache()
    from smartapply.database.session import init_db

    init_db()
    yield


def _register_llm_responses() -> None:
    MockLLMProvider.clear()
    MockLLMProvider.register(
        "job_offer_analysis",
        OfferAnalysis(
            role_type="Data Scientist NLP",
            seniority="mid",
            domain="HealthTech",
            main_tasks=["Build RAG pipelines", "Productionize ML services"],
            required_skills=["Python", "PyTorch", "RAG"],
            nice_to_have=["AWS"],
            cv_keywords_to_include=["PyTorch", "RAG", "FAISS"],
            extracted_location="Paris",
            company_context=("Acme AI builds HealthTech tools for clinical NLP workflows."),
            offer_interest_points=[
                "Build RAG pipelines for healthcare teams",
                "Productionize ML services in a HealthTech context",
            ],
        ),
    )
    MockLLMProvider.register(
        "job_fit_assessment",
        JobFitAssessment(
            fit_score=0.5,
            match_reasons=["Strong NLP background", "Multimodal AI experience"],
            risks=["No prior healthtech experience"],
        ),
    )
    MockLLMProvider.register(
        "cv_adaptation",
        CVSelection(
            cv_title="Data Scientist – NLP & Multimodal AI",
            selected_certificate_ids=[],
            selected_project_ids=["proj_evidence_rag", "proj_ner_camembert", "proj_smartapply"],
            project_selection_reasons=[
                {
                    "project_id": project_id,
                    "reason": "Demonstrates documented NLP model and pipeline work.",
                    "offer_evidence": "Build RAG pipelines",
                    "complementary_value": "",
                }
                for project_id in ["proj_evidence_rag", "proj_ner_camembert", "proj_smartapply"]
            ],
            skills_order=["ml_ai", "data_infra", "stats_signal"],
            warnings=[],
        ),
    )
    MockLLMProvider.register(
        "application_texts",
        ApplicationTexts(
            professional_summary=(
                "Data Scientist with 2 years in NLP and machine learning. "
                "Built RAG and speech pipelines on synthetic datasets."
            ),
            motivation_letter_subject="Candidature - Data Scientist NLP - Camille Martin",
            motivation_letter_body=(
                "La conception de systèmes de recherche documentaire pour des équipes de santé "
                "m'intéresse parce qu'elle relie la qualité des données à des usages concrets. "
                "Je souhaite travailler dans une équipe où la préparation des sources, "
                "l'évaluation des réponses et la compréhension du besoin sont traitées ensemble. "
                "Le rôle proposé offre un cadre pertinent pour approfondir cette démarche et "
                "contribuer à des outils utiles aux utilisateurs.\n\n"
                "Mon expérience chez Aurore Labs en biomarqueurs cliniques et NLP, ainsi que mon "
                "projet Evidence RAG, correspondent bien aux pipelines que vous décrivez. "
                "Ces travaux m'ont appris à préparer les données avec soin et à examiner "
                "séparément les différentes étapes d'un traitement. Je souhaite continuer à "
                "développer cette attention aux sources et aux résultats dans un environnement "
                "où les choix techniques peuvent être discutés avec les équipes concernées. "
                "Cette continuité entre expérimentation et utilisation concrète nourrit mon "
                "intérêt pour votre proposition.\n\n"
                "Je serais heureux de participer à la construction de pipelines lisibles et "
                "à leur évaluation, tout en progressant au contact de profils complémentaires. "
                "Je souhaite apporter une méthode attentive aux données disponibles et aux "
                "limites des modèles, sans perdre de vue les attentes des utilisateurs. "
                "Je serai ravi d'échanger sur la suite."
            ),
        ),
    )
    MockLLMProvider.register(
        "document_review",
        DocumentTextReview(
            issues=[],
        ),
    )


def test_ingest_text_persists_job() -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job
    from smartapply.offers import ManualOfferInput
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    report = p.ingest_manual_offer(
        ManualOfferInput(
            company="Acme",
            title="Data Scientist",
            description="Build ML pipelines.",
            location="Paris",
        )
    )
    assert report.persisted == 1
    assert len(report.job_ids) == 1
    with session_scope() as s:
        job = s.get(Job, report.job_ids[0])
        assert job is not None
        assert job.application_url is None
        assert job.description == "Build ML pipelines."
        assert job.source_data == {"input": "text"}


def test_manual_offer_reuses_existing_url_for_regeneration() -> None:
    from datetime import datetime, timezone

    from smartapply.database import session_scope
    from smartapply.database.models import Job, JobStatus
    from smartapply.offers import ManualOfferInput
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    first = p.ingest_manual_offer(
        ManualOfferInput(
            company="Old Company",
            title="AI Engineering & Research",
            description="Build AI pricing systems.",
            location="Remote",
            application_url="https://www.linkedin.com/jobs/view/4425564009/",
        )
    )
    job_id = first.job_ids[0]
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        job.status = JobStatus.READY_FOR_FORM_SUBMISSION
        job.analyzed_at = datetime.now(timezone.utc)

    second = p.ingest_manual_offer(
        ManualOfferInput(
            company="Old Company",
            title="AI Engineering & Research",
            description="Build reinforcement-learning pricing systems.",
            location="Remote Europe",
            application_url=(
                "https://www.linkedin.com/jobs/view/4425564009/"
                "?alternateChannel=search&trackingId=abc"
            ),
        )
    )

    assert second.persisted == 1
    assert second.job_ids == [job_id]
    with session_scope() as s:
        job = s.get(Job, job_id)
        assert job is not None
        assert job.company == "Old Company"
        assert job.location == "Remote Europe"
        assert job.analyzed_at is None
        assert job.status == JobStatus.SCRAPED


def test_manual_offers_with_shared_url_keep_separate_job_rows() -> None:
    """A generic pasted URL must not make a different offer reuse its row."""
    from smartapply.database import session_scope
    from smartapply.database.models import Job
    from smartapply.offers import ManualOfferInput
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    shared_url = "https://jobs.example.com/search?ref=semantic_search_landing_page"
    first = p.ingest_manual_offer(
        ManualOfferInput(
            company="Example Vision",
            title="Ingénieur Vision par Ordinateur",
            description="Développer des modèles de vision par ordinateur.",
            location="Paris",
            application_url=shared_url,
        )
    )
    second = p.ingest_manual_offer(
        ManualOfferInput(
            company="Exemple Systèmes",
            title="Ingénieur en développement IA",
            description="Développer et déployer des composants IA.",
            location="Lyon",
            application_url=shared_url,
        )
    )

    assert first.job_ids and second.job_ids
    assert first.job_ids != second.job_ids
    with session_scope() as s:
        assert s.get(Job, first.job_ids[0]).company == "Example Vision"
        assert s.get(Job, second.job_ids[0]).company == "Exemple Systèmes"


def test_ingestor_keeps_paid_serpapi_boolean_query_atomic(monkeypatch: pytest.MonkeyPatch) -> None:
    from smartapply.pipeline.ingest import build_source_queries
    from smartapply.pipeline.ingestor import (
        Ingestor,
        expand_query_for_source,
        split_or_query,
    )

    assert split_or_query("Data Scientist OR Machine Learning Engineer OR AI Engineer") == [
        "Data Scientist",
        "Machine Learning Engineer",
        "AI Engineer",
    ]
    ml_aliases = expand_query_for_source(
        "serpapi",
        "Machine Learning Engineer CDI",
    )
    assert ml_aliases[0] == "Machine Learning Engineer"
    assert "ML Engineer" in ml_aliases
    assert "Ingénieur Machine Learning" in ml_aliases
    assert all(not alias.endswith(" CDI") for alias in ml_aliases)

    analyst_aliases = expand_query_for_source("serpapi", "Data Analyst")
    assert analyst_aliases[0] == "Data Analyst"
    assert "Analyste Data" in analyst_aliases

    ft_ml_aliases = expand_query_for_source(
        "francetravail",
        "ML Engineer",
    )
    assert ft_ml_aliases[0] == "ML Engineer"
    assert "ML Engineer" in ft_ml_aliases
    assert "Ingénieur Machine Learning" in ft_ml_aliases

    calls: list[tuple[str, int | None]] = []

    class FakeScraper:
        name = "fake"

        def is_available(self) -> bool:
            return True

        def search(self, query, location=None, *, max_results=None, **kwargs):  # noqa: ANN001, ARG002
            calls.append((query, max_results))
            for i in range(max_results or 1):
                yield RawJob(
                    external_id=f"fake:{query}:{i}",
                    title=query,
                    company=f"Company {i}",
                    location="Paris",
                    description="Build ML systems.",
                    source="fake",
                )

    monkeypatch.setattr("smartapply.pipeline.ingestor.get_scraper", lambda source: FakeScraper())

    report = Ingestor().from_source(
        "serpapi",
        "Data Scientist OR Machine Learning Engineer OR AI Engineer",
        max_results=11,
    )

    boolean_query = build_source_queries(
        "serpapi",
        "Data Scientist OR Machine Learning Engineer OR AI Engineer",
    )[0]
    assert calls == [(boolean_query, 11)]
    # The paid source receives one request lane for all roles.
    assert report.fetched == 11
    assert report.persisted == 11
    from smartapply.database import session_scope
    from smartapply.database.models import Job

    with session_scope() as s:
        titles = [
            job.title
            for job in s.query(Job).filter(Job.id.in_(report.job_ids)).order_by(Job.id.asc()).all()
        ]
    assert Counter(titles) == {boolean_query: 11}

    calls.clear()
    all_roles = [
        "Data Scientist",
        "Machine Learning Engineer",
        "AI Engineer",
        "Research Engineer",
        "Applied Scientist",
        "Computer Vision Engineer",
        "Speech AI Engineer",
        "NLP Engineer",
        "Data & AI Consultant",
        "Data Analyst",
        "Analytics Engineer",
    ]
    expanded_roles = build_source_queries(
        "francetravail",
        " OR ".join(all_roles),
    )
    # Exact user terms remain first, then every intersecting family contributes
    # its English/French alternatives.
    assert expanded_roles[: len(all_roles)] == all_roles
    assert "Scientifique des données" in expanded_roles
    assert "Ingénieur Machine Learning" in expanded_roles
    assert "Ingénieur IA Vision" in expanded_roles
    assert "Ingénieur reconnaissance vocale" in expanded_roles
    assert "Consultant Data Science" in expanded_roles
    assert "Ingénieur modélisation des données" in expanded_roles


def test_ingestor_collects_all_france_travail_aliases_after_primary_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from smartapply.pipeline.ingest import build_source_queries
    from smartapply.pipeline.ingestor import Ingestor

    calls: list[str] = []

    class FakeScraper:
        name = "francetravail"

        def is_available(self) -> bool:
            return True

        def search(self, query, location=None, *, max_results=None, **kwargs):  # noqa: ANN001, ARG002
            calls.append(query)
            for index in range(3):
                yield RawJob(
                    external_id=f"francetravail:{query}:{index}",
                    title=query,
                    company=f"Company {query} {index}",
                    location="Paris",
                    description="Build applied AI systems.",
                    source="francetravail",
                )

    monkeypatch.setattr(
        "smartapply.pipeline.ingestor.get_scraper",
        lambda source: FakeScraper(),
    )

    report = Ingestor().from_source(
        "francetravail",
        "Data Scientist OR NLP Engineer",
        max_results=4,
    )

    assert calls == build_source_queries("francetravail", "Data Scientist OR NLP Engineer")
    assert report.persisted == len(calls) * 3


def test_ingestor_collects_all_france_travail_aliases_after_empty_primary_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from smartapply.pipeline.ingest import build_source_queries
    from smartapply.pipeline.ingestor import Ingestor

    primary = {"Data Scientist", "NLP Engineer"}
    calls: list[str] = []

    class FakeScraper:
        name = "francetravail"

        def is_available(self) -> bool:
            return True

        def search(self, query, location=None, *, max_results=None, **kwargs):  # noqa: ANN001, ARG002
            calls.append(query)
            if query in primary:
                return
            yield RawJob(
                external_id=f"francetravail:{query}",
                title=query,
                company=f"Company {query}",
                location="Paris",
                description="Build applied AI systems.",
                source="francetravail",
            )

    monkeypatch.setattr(
        "smartapply.pipeline.ingestor.get_scraper",
        lambda source: FakeScraper(),
    )

    report = Ingestor().from_source(
        "francetravail",
        "Data Scientist OR NLP Engineer",
        max_results=4,
    )

    assert calls == build_source_queries("francetravail", "Data Scientist OR NLP Engineer")
    assert report.persisted == len(calls) - len(primary)


# ---- max_results semantics: "new offers", not "raw fetched" -----------------
# Regression guard for the bug where re-running a France Travail search
# returned ``0 nouvelle(s)`` because the most recent offers in the API were
# already in the DB and consumed the whole ``max_results`` quota before any
# new offer could surface.


def _yielding_scraper(yields: list[RawJob]) -> type:
    """Build a deterministic scraper class that yields the same list each call."""

    class _FakeScraper:
        name = "fake"

        def is_available(self) -> bool:  # noqa: D401
            return True

        def search(self, query, location=None, *, max_results=None, **kwargs):  # noqa: ANN001, ARG002
            limit = max_results if max_results is not None else len(yields)
            yield from yields[:limit]

    return _FakeScraper


def _fake_raw_jobs(n: int) -> list[RawJob]:
    return [
        RawJob(
            external_id=f"fake:{i:03d}",
            title=f"Data Scientist {i}",
            company=f"Acme {i}",
            location="Paris",
            description="Build ML pipelines.",
            source="fake",
        )
        for i in range(n)
    ]


def test_ingestor_reports_cancelled_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    from smartapply.pipeline.ingestor import Ingestor

    jobs = _fake_raw_jobs(5)
    stop_checks = 0

    class FakeScraper:
        name = "fake"

        def is_available(self) -> bool:
            return True

        def search(self, query, location=None, *, max_results=None, **kwargs):  # noqa: ANN001, ARG002
            assert "stop_requested" in kwargs
            yield from jobs

    def stop_requested() -> bool:
        nonlocal stop_checks
        stop_checks += 1
        return stop_checks >= 3

    monkeypatch.setattr("smartapply.pipeline.ingestor.get_scraper", lambda source: FakeScraper())

    report = Ingestor().from_source(
        "fake",
        "Data Scientist",
        max_results=5,
        stop_requested=stop_requested,
    )

    assert report.cancelled is True
    assert report.fetched == 1
    assert report.persisted == 1


def test_pipeline_rejects_unbounded_serpapi_ingest() -> None:
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())

    with pytest.raises(ValueError, match="serpapi requires"):
        pipeline.ingest("serpapi", "Data Scientist", max_results=None)


def test_pipeline_uses_configured_linkedin_default_when_unbounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from smartapply.config import get_settings
    from smartapply.pipeline import Pipeline

    monkeypatch.setenv("LINKEDIN_MAX_RESULTS", "3")
    get_settings.cache_clear()
    calls: list[int | None] = []

    class FakeScraper:
        name = "linkedin"

        def is_available(self) -> bool:
            return True

        def search(self, query, location=None, *, max_results=None, **kwargs):  # noqa: ANN001, ARG002
            calls.append(max_results)
            for i in range(max_results or 0):
                yield RawJob(
                    external_id=f"linkedin:{query}:{i}",
                    title=f"Data Scientist {i}",
                    company="Acme",
                    location="Paris",
                    description="Build ML pipelines.",
                    source="linkedin",
                )

    monkeypatch.setattr("smartapply.pipeline.ingestor.get_scraper", lambda source: FakeScraper())
    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())

    report = pipeline.ingest("linkedin", "Unmapped Specialist", max_results=None)

    # Research CDD support adds dedicated doctoral-title lanes alongside the
    # normal role lane, while preserving the configured per-call limit.
    assert calls == [3, 3, 3, 3]
    assert report.fetched == 12
    assert report.persisted == 12


def test_pipeline_rejects_linkedin_limit_above_configured_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from smartapply.config import get_settings
    from smartapply.pipeline import Pipeline

    monkeypatch.setenv("LINKEDIN_MAX_RESULTS", "1")
    get_settings.cache_clear()
    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())

    with pytest.raises(ValueError, match="LINKEDIN_MAX_RESULTS"):
        pipeline.ingest("linkedin", "Data Scientist", max_results=3)


def test_process_pending_can_analyze_only_selected_jobs() -> None:
    _register_llm_responses()
    from smartapply.database import session_scope
    from smartapply.database.models import Job, JobStatus
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    first = p.ingest_text(
        text="Build RAG pipelines with Python.",
        title="Data Scientist NLP",
        company="SelectedCo",
        location="Paris",
    )
    second = p.ingest_text(
        text="Build RAG pipelines with Python.",
        title="Data Scientist NLP",
        company="LaterCo",
        location="Paris",
    )

    report = p.process_pending(top_k_analyze=1, job_ids=first.job_ids)

    assert report.total == 1
    assert report.analyzed == 1
    with session_scope() as s:
        selected = s.get(Job, first.job_ids[0])
        later = s.get(Job, second.job_ids[0])
        assert selected.status == JobStatus.ANALYZED
        assert later.status == JobStatus.SCRAPED


def test_analysis_failure_is_returned_in_report(monkeypatch: pytest.MonkeyPatch) -> None:
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = pipeline.ingest_text(
        text="Build RAG pipelines with Python.",
        title="Data Scientist NLP",
        company="FailureCo",
        location="Paris",
    )

    def fail_analysis(*, job_id: int, allow_existing: bool = False) -> None:  # noqa: ARG001
        raise RuntimeError(f"provider unavailable for {job_id}")

    monkeypatch.setattr(pipeline._processor, "_analyze_one", fail_analysis)

    report = pipeline.analyze_jobs(ingested.job_ids)

    assert report.analyzed == 0
    assert report.already_analyzed == 0
    assert report.errors == [
        {
            "job_id": ingested.job_ids[0],
            "title": "Data Scientist NLP",
            "company": "FailureCo",
            "message": f"provider unavailable for {ingested.job_ids[0]}",
        }
    ]


def test_analysis_replaces_job_board_company_with_extracted_employer() -> None:
    MockLLMProvider.clear()
    MockLLMProvider.register(
        "job_offer_analysis",
        OfferAnalysis(
            role_type="Data Scientist IA",
            seniority="mid",
            domain="Industrie",
            main_tasks=["Construire des maquettes IA"],
            required_skills=["Python", "Machine Learning"],
            nice_to_have=[],
            cv_keywords_to_include=["Python", "Machine Learning"],
            extracted_company_name="EXAMPLE INDUSTRY",
            offer_language="fr",
        ),
    )
    MockLLMProvider.register(
        "job_fit_assessment",
        JobFitAssessment(
            fit_score=0.5,
            match_reasons=["Expérience IA alignée"],
            risks=[],
        ),
    )

    from smartapply.database import session_scope
    from smartapply.database.models import Job
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = p.ingest_text(
        text=(
            "En tant qu'organisateur de forums de recrutement, Talents Handicap "
            "accompagne des entreprises. L'entreprise EXAMPLE INDUSTRY recherche un "
            "Data Scientist pour sa BU Industrie."
        ),
        title="Ingénieur(e) Data Scientist confirmé(e)",
        company="Forums Talents Handicap",
        location="92 - Nanterre",
    )

    p.process_pending(
        top_k_analyze=1,
        job_ids=ingested.job_ids,
        local_filter_override_ids=ingested.job_ids,
    )

    with session_scope() as s:
        job = s.get(Job, ingested.job_ids[0])
        assert job is not None
        assert job.company == "EXAMPLE INDUSTRY"


def test_rank_pending_scores_without_llm_analysis() -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job, JobStatus
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    first = p.ingest_text(
        text="Construire des modèles Machine Learning avec Python, SQL et PyTorch. CDI Paris.",
        title="Data Scientist",
        company="ScoreCo",
        location="Paris",
    )
    second = p.ingest_text(
        text="Analyser des données produit avec Python, SQL et expérimentation. CDI Lyon.",
        title="Product Data Analyst",
        company="RankCo",
        location="Lyon",
    )

    report = p.rank_pending(
        top_k_ranked=1,
        job_ids=[first.job_ids[0], second.job_ids[0]],
    )

    assert report.total == 2
    assert report.ranked == 2
    assert report.shortlisted == 0
    with session_scope() as s:
        jobs = s.query(Job).filter(Job.id.in_(report.ranked_ids)).all()
        assert all(job.score and job.score.final_score is not None for job in jobs)
        assert all(job.analyzed_at is None for job in jobs)
        assert sum(job.status == JobStatus.SHORTLISTED for job in jobs) == 0
        assert sum(job.shortlisted_at is not None for job in jobs) == 0
        assert sum(job.status == JobStatus.FILTERED for job in jobs) == 2
        assert all(job.archived_at is None for job in jobs)


def test_automatic_shortlist_averages_matching_and_llm_scores() -> None:
    from smartapply.pipeline.process.ranking import RankingMixin

    candidates = [
        SimpleNamespace(
            id=1, score=SimpleNamespace(final_score=0.99), analysis=SimpleNamespace(fit_score=0.10)
        ),
        SimpleNamespace(
            id=2, score=SimpleNamespace(final_score=0.98), analysis=SimpleNamespace(fit_score=0.20)
        ),
        SimpleNamespace(
            id=3, score=SimpleNamespace(final_score=0.97), analysis=SimpleNamespace(fit_score=0.30)
        ),
        SimpleNamespace(
            id=4, score=SimpleNamespace(final_score=0.40), analysis=SimpleNamespace(fit_score=0.99)
        ),
        SimpleNamespace(
            id=5, score=SimpleNamespace(final_score=0.30), analysis=SimpleNamespace(fit_score=0.98)
        ),
        SimpleNamespace(
            id=6, score=SimpleNamespace(final_score=0.20), analysis=SimpleNamespace(fit_score=0.97)
        ),
    ]

    selected = RankingMixin._mixed_score_shortlist(candidates, 5)

    assert [job.id for job in selected] == [4, 5, 3, 2, 6]


def test_each_ranking_run_replaces_the_previous_automatic_top() -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    first = pipeline.ingest_text(
        text="Machine learning, Python, PyTorch et modèles prédictifs.",
        title="Data Scientist",
        company="Alpha",
        location="Paris",
    )
    second = pipeline.ingest_text(
        text="Analyse produit, Python, SQL et expérimentation.",
        title="Data Analyst",
        company="Beta",
        location="Paris",
    )
    ids = [first.job_ids[0], second.job_ids[0]]

    initial = pipeline.rank_pending(top_k_ranked=2, job_ids=ids)
    refreshed = pipeline.rank_pending(top_k_ranked=1)

    assert initial.shortlisted == 0
    assert refreshed.shortlisted == 0
    with session_scope() as session:
        jobs = session.query(Job).filter(Job.id.in_(ids)).all()
        assert sum(job.shortlisted_at is not None for job in jobs) == 0
        assert all(job.archived_at is None for job in jobs)


def test_analysis_top_k_ignores_already_analyzed_higher_scored_offers() -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    first = pipeline.ingest_text(
        text="Machine learning, Python, PyTorch et modèles prédictifs.",
        title="Data Scientist",
        company="AlreadyAnalyzedCo",
        location="Paris",
    )
    second = pipeline.ingest_text(
        text="Analyse produit, Python, SQL et expérimentation.",
        title="Data Analyst",
        company="PendingAnalysisCo",
        location="Paris",
    )
    first_id = first.job_ids[0]
    second_id = second.job_ids[0]

    pipeline.rank_pending(top_k_ranked=1, job_ids=[first_id, second_id])
    pipeline.analyze_jobs([first_id])
    report = pipeline.process_pending(top_k_analyze=1)

    assert report.analyzed == 1
    with session_scope() as session:
        analyzed = session.get(Job, second_id)
        assert analyzed is not None and analyzed.analyzed_at is not None


def test_automatic_refresh_preserves_manually_pinned_offer() -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job, JobStatus, ShortlistOrigin
    from smartapply.database.repository import mark_analyzed, set_shortlisted
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    first = pipeline.ingest_text(
        text="Machine learning, Python, PyTorch et modèles prédictifs.",
        title="Data Scientist",
        company="Alpha",
        location="Paris",
    )
    second = pipeline.ingest_text(
        text="Analyse produit, Python, SQL et expérimentation.",
        title="Data Analyst",
        company="Beta",
        location="Paris",
    )
    ids = [first.job_ids[0], second.job_ids[0]]
    initial = pipeline.rank_pending(top_k_ranked=1, job_ids=ids)
    manual_id = next(job_id for job_id in ids if job_id not in initial.shortlisted_ids)
    with session_scope() as session:
        mark_analyzed(session, manual_id)
        set_shortlisted(
            session,
            manual_id,
            selected=True,
            origin=ShortlistOrigin.MANUAL,
        )

    refreshed = pipeline.rank_pending(top_k_ranked=1)

    assert set(refreshed.shortlisted_ids) == {manual_id}
    with session_scope() as session:
        manual = session.get(Job, manual_id)
        assert manual is not None
        assert manual.shortlist_origin == ShortlistOrigin.MANUAL
        assert manual.shortlisted_at is not None
        assert manual.status == JobStatus.SHORTLISTED
        assert manual.archived_at is None


def test_filter_pending_archives_internships_before_llm() -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job, JobStatus
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    stage = p.ingest_text(
        text="Stage de six mois en data science.",
        title="Data Scientist Stage",
        company="StageCo",
        location="Paris",
    )
    cdi = p.ingest_text(
        text="Build ML pipelines with Python and PyTorch.",
        title="Data Scientist",
        company="GoodCo",
        location="Paris",
    )

    report = p.filter_pending()

    assert report.total == 2
    assert report.kept == 1
    assert report.rejected == 1
    assert cdi.job_ids[0] in report.kept_ids
    assert stage.job_ids[0] in report.rejected_ids
    with session_scope() as s:
        stage_job = s.get(Job, stage.job_ids[0])
        cdi_job = s.get(Job, cdi.job_ids[0])
        assert stage_job.status == JobStatus.ARCHIVED
        assert stage_job.score.components["rejection_stage"] == "local_filter"
        assert any(
            "blocked_contract_visible_text:stage" in reason
            for reason in stage_job.score.components["rejection_reasons"]
        )
        assert cdi_job.status == JobStatus.SCRAPED


def test_filter_pending_persists_uncertain_offer_for_semantic_ranking() -> None:
    from smartapply.database import session_scope
    from smartapply.database.models import Job
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = p.ingest_text(
        text="Concevoir des architectures neurosymboliques pour l'aide à la décision.",
        title="Ingénieur en systèmes décisionnels avancés",
        company="DecisionCo",
        location="Paris",
    )

    report = p.filter_pending(job_ids=ingested.job_ids)

    assert report.kept == 1
    assert report.rejected == 0
    assert report.uncertain == 1
    assert report.uncertain_ids == ingested.job_ids
    ranking_report = p.rank_pending(job_ids=ingested.job_ids)
    assert ranking_report.ranked == 1
    assert ranking_report.shortlisted == 0
    with session_scope() as session:
        job = session.get(Job, ingested.job_ids[0])
        assert job is not None
        assert job.archived_at is None
        assert job.filtered_at is not None
        assert job.score.components["filter_disposition"] == "uncertain"


def test_full_pipeline_with_mocked_dependencies() -> None:
    _register_llm_responses()
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())

    # Manually ingest two different jobs (skipping the scrapers HTTP layer)
    p.ingest_text(
        text=(
            "Construire des pipelines RAG avec PyTorch et Hugging Face. "
            "Travailler sur du multimodal AI pour HealthTech. CDI Paris."
        ),
        title="Data Scientist NLP",
        company="Acme AI",
        location="Paris, France",
        application_url="https://example.com/jobs/42",
    )
    p.ingest_text(
        text="Vous gérez l'équipe commerciale. Atteindre 200% des quotas.",
        title="Sales Director",
        company="BetaSales",
        location="Paris, France",
    )

    # Process — filter + rank + analyze the relevant job
    process = p.process_pending()
    assert process.total == 2
    assert process.kept_after_filter == 1  # sales should be filtered out
    assert process.analyzed == 1

    # Apply
    from smartapply.database import session_scope
    from smartapply.database.repository import top_jobs_by_score

    with session_scope() as s:
        top = list(top_jobs_by_score(s, 1))
        assert top, "Expected at least one ranked job"
        assert top[0].analysis is not None
        raw = top[0].analysis.raw_response
        assert raw["company_context"] == (
            "Acme AI builds HealthTech tools for clinical NLP workflows."
        )
        assert raw["extracted_location"] == "Paris"
        assert "Build RAG pipelines for healthcare teams" in raw["offer_interest_points"]
    top_id = top[0].id

    report = p.apply_to(top_id)
    assert report.application_id is not None
    assert report.docx_path and Path(report.docx_path).exists()
    assert report.cv_pdf_path and Path(report.cv_pdf_path).exists()
    assert report.letter_pdf_path and Path(report.letter_pdf_path).exists()
    assert Path(report.docx_path).parent.name == str(report.application_id)
    assert not report.validation_errors


def test_run_manual_offer_executes_direct_manual_pipeline() -> None:
    _register_llm_responses()
    from smartapply.database import session_scope
    from smartapply.database.models import Job
    from smartapply.offers import ManualOfferInput
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    report = p.run_manual_offer(
        ManualOfferInput(
            company="Acme AI",
            title="Data Scientist NLP",
            description="Build RAG pipelines with Python and PyTorch.",
            location="Paris",
            application_url="https://jobs.acme.ai/42",
        ),
    )

    assert report["ingest"].persisted == 1
    assert report["process"].analyzed == 1
    assert len(report["applications"]) == 1
    with session_scope() as s:
        job = s.get(Job, report["ingest"].job_ids[0])
        assert job is not None
        assert job.filtered_at is None
        assert job.ranked_at is None
        assert job.score is None
    application = report["applications"][0]
    assert application.application_id is not None
    assert application.form_url == "https://jobs.acme.ai/42"
    assert application.status == "ready_for_form_submission"
    assert application.cv_pdf_path and Path(application.cv_pdf_path).exists()
    assert application.letter_pdf_path and Path(application.letter_pdf_path).exists()


def test_apply_to_generates_only_application_documents() -> None:
    _register_llm_responses()
    from sqlalchemy import select

    from smartapply.database import session_scope
    from smartapply.database.models import Application, GeneratedDocument, JobStatus
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = p.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Machine Learning Engineer",
        company="DocumentOnlyCo",
        location="Paris",
        application_url="https://example.com/jobs/document-only",
    )
    p.process_pending(top_k_analyze=1, job_ids=ingested.job_ids)

    report = p.apply_to(ingested.job_ids[0])

    assert report.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert report.cv_pdf_path and Path(report.cv_pdf_path).exists()
    assert report.letter_pdf_path and Path(report.letter_pdf_path).exists()
    with session_scope() as s:
        app = s.get(Application, report.application_id)
        assert app is not None
        document_types = set(
            s.scalars(
                select(GeneratedDocument.doc_type).where(
                    GeneratedDocument.application_id == report.application_id
                )
            )
        )
        assert "email" not in document_types
        assert "eml" not in document_types

    from smartapply.pipeline import ApplicationAlreadyExistsError

    def fail_if_called(**kwargs):  # noqa: ARG001
        raise AssertionError("duplicate generation reached the LLM")

    p.llm.complete_json = fail_if_called
    with pytest.raises(ApplicationAlreadyExistsError):
        p.apply_to(ingested.job_ids[0])


def test_force_regenerate_refreshes_the_application_llm_cache() -> None:
    _register_llm_responses()

    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = p.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Machine Learning Engineer",
        company="RefreshCo",
        location="Paris",
        application_url="https://example.com/jobs/refresh",
    )
    job_id = ingested.job_ids[0]
    p.process_pending(top_k_analyze=1, job_ids=[job_id])
    first = p.apply_to(job_id)

    calls: list[dict] = []
    original_complete_json = p.llm.complete_json

    def capture_cache_mode(**kwargs):
        calls.append(kwargs)
        return original_complete_json(**kwargs)

    p.llm.complete_json = capture_cache_mode
    second = p.apply_to(
        job_id,
        force_regenerate=True,
    )

    assert second.application_id == first.application_id
    assert [call.get("purpose") for call in calls] == [
        "cv_adaptation",
        "application_texts",
        "document_review",
    ]
    assert all(call["refresh_cache"] is True for call in calls)


def test_application_selects_cv_then_generates_summary_and_letter_then_persists_review() -> None:
    _register_llm_responses()

    from sqlalchemy import select

    from smartapply.database import session_scope
    from smartapply.database.models import Application, GeneratedDocument, Job, JobStatus
    from smartapply.pipeline import Pipeline
    from smartapply.profile import get_profile

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    offer_description = (
        "Build RAG pipelines with Python and PyTorch for a product team. "
        "The product must retain source citations for clinical teams."
    )
    ingested = pipeline.ingest_text(
        text=offer_description,
        title="Data Scientist NLP",
        company="ReviewedDocumentsCo",
        location="Paris",
    )
    job_id = ingested.job_ids[0]
    calls: list[dict] = []
    original_complete_json = pipeline.llm.complete_json

    def record_generation(**kwargs):
        calls.append(kwargs)
        return original_complete_json(**kwargs)

    pipeline.llm.complete_json = record_generation
    pipeline.process_pending(top_k_analyze=1, job_ids=[job_id])
    assert pipeline.llm.cheap_model != pipeline.llm.smart_model
    assert [call["purpose"] for call in calls] == [
        "job_offer_analysis",
        "job_fit_assessment",
    ]
    assert [call["schema"] for call in calls] == [OfferAnalysis, JobFitAssessment]
    assert all(call["model"] == pipeline.llm.cheap_model for call in calls)
    calls.clear()
    with session_scope() as session:
        stored_analysis = session.get(Job, job_id).analysis
        assert stored_analysis.model_used == pipeline.llm.cheap_model
        analysis_before = (
            stored_analysis.id,
            stored_analysis.created_at,
            stored_analysis.raw_response,
        )
    report = pipeline.apply_to(job_id)

    assert report.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert [call["purpose"] for call in calls] == [
        "cv_adaptation",
        "application_texts",
        "document_review",
    ]
    assert [call["schema"] for call in calls] == [
        CVSelection,
        ApplicationTexts,
        DocumentTextReview,
    ]
    assert [call["model"] for call in calls] == [
        pipeline.llm.smart_model,
        pipeline.llm.smart_model,
        pipeline.llm.smart_model,
    ]
    # This sentence is absent from the canned OfferAnalysis: both later stages
    # must receive the original offer, not merely its extracted keywords.
    for call in calls[1:]:
        assert "The product must retain source citations for clinical teams." in call["user"]

    assert report.document_review
    assert report.document_review[0]["model"] == pipeline.llm.smart_model
    assert report.document_review[0]["schema_version"] == 2
    assert report.document_review[0]["decision"] == "accepted"
    assert report.document_review[0]["reference_date"]
    with session_scope() as session:
        stored_analysis = session.get(Job, job_id).analysis
        assert (
            stored_analysis.id,
            stored_analysis.created_at,
            stored_analysis.raw_response,
        ) == analysis_before
        application = session.get(Application, report.application_id)
        assert application is not None
        assert application.cv_json["selected_skills"]
        assert application.cv_json["llm_selected_skills"] == []
        assert [reason["project_id"] for reason in application.cv_json["project_selection_reasons"]] == (
            application.cv_json["selected_project_ids"]
        )
        final_cv = {
            key: value
            for key, value in application.cv_json.items()
            if key not in {"warnings", "project_selection_reasons", "llm_selected_skills"}
        }
        profile = get_profile()
        for call in calls[1:]:
            # Locate the structured source context independently of the prompt's
            # introductory prose or the review's preceding editable-field JSON.
            source_position = call["user"].index('"source_index"')
            context_start = call["user"].rfind("{", 0, source_position)
            context, _ = json.JSONDecoder().raw_decode(call["user"][context_start:])
            expected_cv = (
                {key: value for key, value in final_cv.items() if key != "professional_summary"}
                if call["purpose"] == "application_texts"
                else final_cv
            )
            assert context["final_cv"]["data"] == expected_cv
            assert context["job_offer"]["full_description"] == offer_description
            assert {
                category["id"]: category["skills"]
                for category in context["declared_skills"]["categories"]
            } == {category.id: category.skills for category in profile.skills.categories}
            assert [project["id"] for project in context["selected_project_evidence"]] == (
                final_cv["selected_project_ids"]
            )
            assert [experience["id"] for experience in context["experience_evidence"]] == [
                experience.id for experience in profile.experiences
            ]
            assert context["experience_evidence"][0]["bullets"][0]["allowed_claims"] == list(
                profile.experiences[0].bullets[0].effective_allowed_claims
            )
        review = session.scalar(
            select(GeneratedDocument).where(
                GeneratedDocument.application_id == application.id,
                GeneratedDocument.doc_type == "document_review",
            )
        )
        assert review is not None
        assert json.loads(review.content) == report.document_review


@pytest.mark.parametrize("category", ["style", "structure"])
def test_advisory_review_does_not_block_or_rewrite_application(category: str) -> None:
    _register_llm_responses()
    reason = "Optional improvement: make the transition between these sentences more direct."
    MockLLMProvider.register(
        "document_review",
        DocumentTextReview(
            issues=[
                {
                    "field": "motivation_letter_body",
                    "passage": "Je serai ravi d'échanger sur la suite.",
                    "reason": reason,
                    "source_ids": ["style:letter"],
                    "category": category,
                    "replacement": None,
                }
            ]
        ),
    )

    from smartapply.database import session_scope
    from smartapply.database.models import Application, JobStatus
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = pipeline.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Data Scientist NLP",
        company="AdvisoryReviewCo",
        location="Paris",
    )
    job_id = ingested.job_ids[0]
    pipeline.process_pending(top_k_analyze=1, job_ids=[job_id])
    calls: list[dict] = []
    original_complete_json = pipeline.llm.complete_json

    def record_generation(**kwargs):
        calls.append(kwargs)
        return original_complete_json(**kwargs)

    pipeline.llm.complete_json = record_generation
    report = pipeline.apply_to(job_id)

    assert report.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert not report.validation_errors
    assert any(reason in warning for warning in report.validation_warnings)
    assert [call["purpose"] for call in calls] == [
        "cv_adaptation",
        "application_texts",
        "document_review",
    ]
    with session_scope() as session:
        application = session.get(Application, report.application_id)
        assert application.status == JobStatus.READY_FOR_FORM_SUBMISSION
        letter = next(
            document
            for document in application.documents
            if document.doc_type == "motivation_letter"
        )
        assert "Je serai ravi d'échanger sur la suite." in letter.content
        review = next(
            document for document in application.documents if document.doc_type == "document_review"
        )
        assert json.loads(review.content) == report.document_review


def test_confirmed_local_correction_survives_advisory_only_confirmation() -> None:
    _register_llm_responses()

    from smartapply.database import session_scope
    from smartapply.database.models import Application, JobStatus
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = pipeline.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Data Scientist NLP",
        company="LocalCorrectionCo",
        location="Paris",
    )
    job_id = ingested.job_ids[0]
    pipeline.process_pending(top_k_analyze=1, job_ids=[job_id])
    original_complete_json = pipeline.llm.complete_json
    calls: list[dict] = []
    generated_letter: MotivationLetter | None = None
    reviews = iter(
        [
            DocumentTextReview(
                issues=[
                    {
                        "field": "motivation_letter_body",
                        "passage": "Je souhaites échanger",
                        "reason": "The first-person singular verb must be souhaite.",
                        "source_ids": ["style:letter"],
                        "category": "language",
                        "replacement": "Je souhaite échanger",
                    }
                ]
            ),
            DocumentTextReview(
                issues=[
                    {
                        "field": "motivation_letter_body",
                        "passage": "correspondent bien aux pipelines que vous décrivez",
                        "reason": "Optional style preference: a more direct transition is possible.",
                        "source_ids": ["style:letter"],
                        "category": "style",
                        "replacement": None,
                    }
                ]
            ),
        ]
    )

    def respond(**kwargs):
        nonlocal generated_letter
        calls.append(kwargs)
        if kwargs["purpose"] == "document_review":
            return next(reviews)
        response = original_complete_json(**kwargs)
        if kwargs["purpose"] == "application_texts":
            response = response.model_copy(
                update={
                    "motivation_letter_body": response.motivation_letter_body.replace(
                        "Je serai ravi d'échanger", "Je souhaites échanger"
                    )
                }
            )
            generated_letter = MotivationLetter(
                subject=response.motivation_letter_subject,
                body=response.motivation_letter_body,
            )
            return response
        return response

    pipeline.llm.complete_json = respond
    report = pipeline.apply_to(job_id)

    assert generated_letter is not None
    assert report.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert not report.validation_errors
    assert any("Optional style preference" in warning for warning in report.validation_warnings)
    assert [call["purpose"] for call in calls] == [
        "cv_adaptation",
        "application_texts",
        "document_review",
        "document_review",
    ]
    assert [call["model"] for call in calls] == [
        pipeline.llm.smart_model,
        pipeline.llm.smart_model,
        pipeline.llm.smart_model,
        pipeline.llm.smart_model,
    ]
    with session_scope() as session:
        application = session.get(Application, report.application_id)
        letter = next(
            document
            for document in application.documents
            if document.doc_type == "motivation_letter"
        )
        assert letter.content == generated_letter.body.replace(
            "Je souhaites échanger", "Je souhaite échanger"
        )
        assert len(report.document_review) == 2
        assert [attempt["decision"] for attempt in report.document_review] == [
            "revision_proposed",
            "accepted",
        ]
        review = next(
            document for document in application.documents if document.doc_type == "document_review"
        )
        assert json.loads(review.content) == report.document_review


def test_confirmed_cv_title_correction_is_persisted_and_rendered_without_reassembly() -> None:
    _register_llm_responses()

    from html import escape

    from smartapply.database import session_scope
    from smartapply.database.models import Application, JobStatus
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = pipeline.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Data Scientist NLP",
        company="TitleCorrectionCo",
        location="Paris",
    )
    job_id = ingested.job_ids[0]
    pipeline.process_pending(top_k_analyze=1, job_ids=[job_id])
    corrected_title = "Data Scientist – NLP & Multimodal AI"
    original_title = f"Principal {corrected_title}"
    original_complete_json = pipeline.llm.complete_json
    calls: list[dict] = []
    generated_letter: MotivationLetter | None = None
    reviews = iter(
        [
            DocumentTextReview(
                issues=[
                    {
                        "field": "cv_title",
                        "passage": "Principal ",
                        "reason": "The supplied candidate evidence does not support this seniority.",
                        "source_ids": ["candidate:provided_evidence"],
                        "category": "factual",
                        "replacement": "",
                    }
                ]
            ),
            DocumentTextReview(issues=[]),
        ]
    )

    def respond(**kwargs):
        nonlocal generated_letter
        calls.append(kwargs)
        if kwargs["purpose"] == "document_review":
            return next(reviews)
        response = original_complete_json(**kwargs)
        if kwargs["purpose"] == "cv_adaptation":
            return response.model_copy(update={"cv_title": original_title})
        if kwargs["purpose"] == "application_texts":
            generated_letter = MotivationLetter(
                subject=response.motivation_letter_subject,
                body=response.motivation_letter_body,
            )
        return response

    pipeline.llm.complete_json = respond
    report = pipeline.apply_to(job_id)

    assert report.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert not report.validation_errors
    assert [call["purpose"] for call in calls] == [
        "cv_adaptation",
        "application_texts",
        "document_review",
        "document_review",
    ]
    assert [call["model"] for call in calls] == [
        pipeline.llm.smart_model,
        pipeline.llm.smart_model,
        pipeline.llm.smart_model,
        pipeline.llm.smart_model,
    ]
    assert [attempt["decision"] for attempt in report.document_review] == [
        "revision_proposed",
        "accepted",
    ]
    source_contexts = []
    for call in calls[1:]:
        source_position = call["user"].index('"source_index"')
        context_start = call["user"].rfind("{", 0, source_position)
        context, _ = json.JSONDecoder().raw_decode(call["user"][context_start:])
        source_contexts.append(context)
    selected_cv = source_contexts[0]["final_cv"]["data"]
    assert "professional_summary" not in selected_cv
    original_cv = source_contexts[1]["final_cv"]["data"]
    assert {
        key: value for key, value in original_cv.items() if key != "professional_summary"
    } == selected_cv
    assert original_cv["cv_title"] == original_title
    expected_cv = {**original_cv, "cv_title": corrected_title}
    # The confirmation sees the corrected title and exactly the same summary,
    # selected Skills, projects, static experiences, identifiers and order.
    assert source_contexts[2]["final_cv"]["data"] == expected_cv

    with session_scope() as session:
        application = session.get(Application, report.application_id)
        assert {
            key: value
            for key, value in application.cv_json.items()
            if key not in {"warnings", "project_selection_reasons", "llm_selected_skills"}
        } == expected_cv
        assert [reason["project_id"] for reason in application.cv_json["project_selection_reasons"]] == (
            application.cv_json["selected_project_ids"]
        )
        cv_document = next(
            document for document in application.documents if document.doc_type == "cv_json"
        )
        assert json.loads(cv_document.content) == application.cv_json
        letter_document = next(
            document
            for document in application.documents
            if document.doc_type == "motivation_letter"
        )
        assert generated_letter is not None
        assert letter_document.content == generated_letter.body
        assert letter_document.extra["subject"] == generated_letter.subject
        review_document = next(
            document for document in application.documents if document.doc_type == "document_review"
        )
        assert json.loads(review_document.content) == report.document_review
        assert review_document.extra["scope"] == "cv_title_professional_summary_and_motivation_letter"

    headline = f'<p class="headline">{escape(corrected_title)}</p>'
    assert headline in Path(report.cv_html_path).read_text(encoding="utf-8")
    assert headline in Path(report.letter_html_path).read_text(encoding="utf-8")
    assert escape(original_title) not in Path(report.letter_html_path).read_text(encoding="utf-8")


def test_unavailable_review_keeps_dossier_available_with_incomplete_control() -> None:
    _register_llm_responses()

    from sqlalchemy import select

    from smartapply.database import session_scope
    from smartapply.database.models import Application, Job, JobStatus
    from smartapply.llm import LLMError
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = pipeline.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Data Scientist NLP",
        company="UnavailableReviewCo",
        location="Paris",
    )
    job_id = ingested.job_ids[0]
    pipeline.process_pending(top_k_analyze=1, job_ids=[job_id])
    original_complete_json = pipeline.llm.complete_json
    purposes: list[str] = []

    def fail_review(**kwargs):
        purposes.append(kwargs["purpose"])
        if kwargs["purpose"] == "document_review":
            raise LLMError("semantic review unavailable")
        return original_complete_json(**kwargs)

    pipeline.llm.complete_json = fail_review
    report = pipeline.apply_to(job_id)

    assert purposes == ["cv_adaptation", "application_texts", "document_review"]
    assert report.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert any(error.startswith("text_review_unavailable:") for error in report.validation_errors)
    assert "text_review_incomplete: contrôle non terminé" in report.validation_errors
    assert not any(error.startswith("text_review:factual:") for error in report.validation_errors)
    assert report.document_review[-1]["status"] == "incomplete"
    assert report.document_review[-1]["decision"] == "incomplete"
    assert report.cv_pdf_path and Path(report.cv_pdf_path).exists()
    assert report.letter_pdf_path and Path(report.letter_pdf_path).exists()
    with session_scope() as session:
        application = session.scalar(select(Application).where(Application.job_id == job_id))
        assert application is not None
        assert application.status == JobStatus.READY_FOR_FORM_SUBMISSION
        assert (
            "validation_error:text_review_incomplete: contrôle non terminé"
            in application.validation_warnings
        )
        assert session.get(Job, job_id).status in (
            JobStatus.READY_FOR_FORM_SUBMISSION,
            JobStatus.SHORTLISTED,
        )
        reviews = [
            document for document in application.documents if document.doc_type == "document_review"
        ]
        assert len(reviews) == 1
        assert json.loads(reviews[0].content) == report.document_review


@pytest.mark.parametrize("field", ["professional_summary", "motivation_letter_body"])
def test_overlimit_documents_remain_available_with_diagnostics_without_truncation(field: str) -> None:
    _register_llm_responses()

    from smartapply.cv.motivation_validator import _word_count
    from smartapply.database import session_scope
    from smartapply.database.models import Application, JobStatus
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = pipeline.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Data Scientist NLP",
        company="OverlimitReviewCo",
        location="Paris",
    )
    job_id = ingested.job_ids[0]
    pipeline.process_pending(top_k_analyze=1, job_ids=[job_id])
    original_complete_json = pipeline.llm.complete_json
    generated_text = ""
    purposes: list[str] = []

    def respond(**kwargs):
        nonlocal generated_text
        purposes.append(kwargs["purpose"])
        response = original_complete_json(**kwargs)
        if kwargs["purpose"] == "application_texts":
            if field == "professional_summary":
                generated_text = "x" * (pipeline.profile.style_guide.max_summary_length + 1)
            else:
                body = response.motivation_letter_body
                generated_text = body + " context" * (241 - _word_count(body))
                assert _word_count(generated_text) == 241
            return response.model_copy(update={field: generated_text})
        return response

    pipeline.llm.complete_json = respond
    report = pipeline.apply_to(job_id)

    defect = "summary_too_long:" if field == "professional_summary" else "letter_too_long:241"
    assert report.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert any(defect in error for error in report.validation_errors)
    assert not any(error.startswith("text_review:factual:") for error in report.validation_errors)
    assert purposes == ["cv_adaptation", "application_texts", "document_review"]
    assert report.document_review[-1]["status"] == "completed"
    assert report.document_review[-1]["decision"] == "rejected"
    with session_scope() as session:
        application = session.get(Application, report.application_id)
        assert application.status == JobStatus.READY_FOR_FORM_SUBMISSION
        assert any(defect in warning for warning in application.validation_warnings)
        if field == "professional_summary":
            assert application.cv_json["professional_summary"] == generated_text
        else:
            letter = next(
                document
                for document in application.documents
                if document.doc_type == "motivation_letter"
            )
            assert letter.content == generated_text
        review = next(
            document for document in application.documents if document.doc_type == "document_review"
        )
        assert json.loads(review.content) == report.document_review


def test_invalid_review_source_persists_incomplete_control_without_factual_accusation() -> None:
    _register_llm_responses()
    allegation = "Unsupported allegation from an invalid reviewer response."
    MockLLMProvider.register(
        "document_review",
        DocumentTextReview(
            issues=[
                {
                    "field": "professional_summary",
                    "passage": "Data Scientist with 2 years in NLP and machine learning.",
                    "reason": allegation,
                    "source_ids": ["project:does_not_exist"],
                    "category": "factual",
                    "replacement": "Data scientist working on language systems.",
                }
            ]
        ),
    )

    from smartapply.database import session_scope
    from smartapply.database.models import Application, JobStatus
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = pipeline.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Data Scientist NLP",
        company="InvalidReviewSourceCo",
        location="Paris",
    )
    job_id = ingested.job_ids[0]
    pipeline.process_pending(top_k_analyze=1, job_ids=[job_id])
    report = pipeline.apply_to(job_id)

    assert report.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert "text_review_invalid_source:professional_summary" in report.validation_errors
    assert "text_review_incomplete: contrôle non terminé" in report.validation_errors
    assert not any(error.startswith("text_review:factual:") for error in report.validation_errors)
    assert allegation not in " ".join(report.validation_errors)
    assert len(report.document_review) == 1
    assert report.document_review[-1]["status"] == "incomplete"
    assert report.document_review[-1]["decision"] == "incomplete"
    assert allegation in json.dumps(report.document_review)
    with session_scope() as session:
        application = session.get(Application, report.application_id)
        assert application.status == JobStatus.READY_FOR_FORM_SUBMISSION
        assert application.cv_json["professional_summary"].startswith(
            "Data Scientist with 2 years in NLP and machine learning."
        )
        assert (
            "validation_error:text_review_incomplete: contrôle non terminé"
            in application.validation_warnings
        )
        assert not any(
            warning.startswith("validation_error:text_review:factual:")
            for warning in application.validation_warnings
        )
        review = next(
            document for document in application.documents if document.doc_type == "document_review"
        )
        assert json.loads(review.content) == report.document_review


def test_unresolved_document_review_is_preserved_without_blocking_the_dossier() -> None:
    _register_llm_responses()
    MockLLMProvider.register(
        "document_review",
        DocumentTextReview(
            issues=[
                {
                    "field": "professional_summary",
                    "passage": "Data Scientist with 2 years in NLP and machine learning.",
                    "reason": "Unresolved claim: the wording needs qualification against the final CV.",
                    "source_ids": ["document:final_cv"],
                    "category": "factual",
                    "replacement": None,
                }
            ],
        ),
    )

    from smartapply.database import session_scope
    from smartapply.database.models import Application, JobStatus
    from smartapply.pipeline import Pipeline

    pipeline = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = pipeline.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Data Scientist NLP",
        company="UnresolvedReviewCo",
        location="Paris",
    )
    job_id = ingested.job_ids[0]
    pipeline.process_pending(top_k_analyze=1, job_ids=[job_id])

    report = pipeline.apply_to(job_id)

    assert report.status == JobStatus.READY_FOR_FORM_SUBMISSION
    assert report.validation_errors
    assert "Unresolved claim" in json.dumps(report.document_review)
    with session_scope() as session:
        application = session.get(Application, report.application_id)
        assert application is not None
        assert application.status == JobStatus.READY_FOR_FORM_SUBMISSION
        assert application.cv_json["selected_experiences"]
        reviews = [
            document for document in application.documents if document.doc_type == "document_review"
        ]
        assert len(reviews) == 1
        assert json.loads(reviews[0].content) == report.document_review


def test_failed_generation_releases_reservation_so_retry_is_possible() -> None:
    _register_llm_responses()
    from sqlalchemy import select

    from smartapply.database import session_scope
    from smartapply.database.models import Application
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = p.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Machine Learning Engineer",
        company="RetryCo",
        location="Paris",
        application_url="https://example.com/jobs/retry",
    )
    job_id = ingested.job_ids[0]
    p.process_pending(top_k_analyze=1, job_ids=[job_id])

    working_complete_json = p.llm.complete_json

    def fail_generation(**kwargs):  # noqa: ARG001
        raise RuntimeError("simulated provider failure")

    p.llm.complete_json = fail_generation
    with pytest.raises(RuntimeError, match="simulated provider failure"):
        p.apply_to(job_id)

    with session_scope() as session:
        assert session.scalar(select(Application).where(Application.job_id == job_id)) is None

    p.llm.complete_json = working_complete_json
    report = p.apply_to(job_id)
    assert report.application_id is not None


def test_atomic_output_removes_partial_files_and_releases_reservation() -> None:
    _register_llm_responses()
    from sqlalchemy import select

    from smartapply.config import get_settings
    from smartapply.database import session_scope
    from smartapply.database.models import Application
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = p.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Machine Learning Engineer",
        company="AtomicFailureCo",
        location="Paris",
        application_url="https://example.com/jobs/atomic-failure",
    )
    job_id = ingested.job_ids[0]
    p.process_pending(top_k_analyze=1, job_ids=[job_id])

    def leave_partial_file_then_fail(adapted, path):  # noqa: ARG001
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"partial-docx")
        raise RuntimeError("simulated renderer crash")

    p.renderer.docx.save = leave_partial_file_then_fail
    with pytest.raises(RuntimeError, match="simulated renderer crash"):
        p.apply_to(job_id)

    output_dir = get_settings().output_dir
    assert not list(output_dir.glob("*"))
    with session_scope() as session:
        assert session.scalar(select(Application).where(Application.job_id == job_id)) is None


def test_atomic_force_regeneration_restores_previous_complete_directory() -> None:
    _register_llm_responses()

    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = p.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Machine Learning Engineer",
        company="AtomicRestoreCo",
        location="Paris",
        application_url="https://example.com/jobs/atomic-restore",
    )
    job_id = ingested.job_ids[0]
    p.process_pending(top_k_analyze=1, job_ids=[job_id])
    first = p.apply_to(job_id)
    first_docx = Path(first.docx_path)
    previous_bytes = first_docx.read_bytes()

    def leave_partial_file_then_fail(adapted, path):  # noqa: ARG001
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"partial-replacement")
        raise RuntimeError("simulated replacement crash")

    p.renderer.docx.save = leave_partial_file_then_fail
    with pytest.raises(RuntimeError, match="simulated replacement crash"):
        p.apply_to(job_id, force_regenerate=True)

    assert first_docx.read_bytes() == previous_bytes
    assert not list(first_docx.parent.parent.glob(f".{first.application_id}.*"))


def test_old_empty_reservation_is_reclaimed_after_a_crash() -> None:
    _register_llm_responses()
    from datetime import datetime, timedelta, timezone

    from smartapply.database import session_scope
    from smartapply.database.repository import create_or_get_application
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    ingested = p.ingest_text(
        text="Build RAG pipelines with Python and PyTorch for a product team.",
        title="Machine Learning Engineer",
        company="StaleReservationCo",
        location="Paris",
        application_url="https://example.com/jobs/stale-reservation",
    )
    job_id = ingested.job_ids[0]
    p.process_pending(top_k_analyze=1, job_ids=[job_id])
    with session_scope() as session:
        reservation = create_or_get_application(session, job_id)
        session.flush()
        reserved_id = reservation.id
        reservation.updated_at = datetime.now(timezone.utc) - timedelta(minutes=31)

    report = p.apply_to(job_id)

    assert report.application_id == reserved_id
    assert report.docx_path and Path(report.docx_path).is_file()


def test_pipeline_invalid_source_raises() -> None:
    from smartapply.pipeline import Pipeline

    p = Pipeline(embeddings=MockEmbeddingsProvider(), llm=MockLLMProvider())
    with pytest.raises(RuntimeError):
        p.ingest("serpapi", "data scientist")  # no API key in tests


def test_offer_language_detection_fr_and_en() -> None:
    from smartapply.language import detect_offer_language

    assert detect_offer_language("Vos missions: analyser des donnees. CDI Paris.") == "fr"
    assert detect_offer_language("Poste en télétravail avec CDI et profil recherché data.") == "fr"
    assert detect_offer_language("Responsibilities: build ML models. English required.") == "en"
