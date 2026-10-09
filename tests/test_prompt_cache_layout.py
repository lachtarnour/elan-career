"""Regression tests for OpenAI prompt-cache prefix stability."""

from __future__ import annotations

from smartapply.llm import JobAnalysis
from smartapply.llm.prompts import cv_adaptation
from smartapply.profile import get_profile


def _analysis(
    *,
    role: str,
    domain: str,
    company_context: str,
    task: str,
    required_skill: str,
) -> JobAnalysis:
    return JobAnalysis(
        role_type=role,
        seniority="mid",
        domain=domain,
        company_context=company_context,
        offer_interest_points=[task],
        main_tasks=[task],
        required_skills=["Python", required_skill],
        nice_to_have=[],
        match_reasons=[f"Relevant experience for {role}"],
        risks=[f"Do not overclaim {domain} experience"],
        cv_keywords_to_include=[required_skill],
    )


def _assert_stable_prefix(first: str, second: str, boundary: str) -> None:
    """Offer changes must leave all candidate context and instructions cacheable."""
    first_prefix, first_context = first.split(boundary, 1)
    second_prefix, second_context = second.split(boundary, 1)
    assert first_prefix == second_prefix
    assert first_prefix
    assert first_context != second_context


def _different_analyses() -> tuple[JobAnalysis, JobAnalysis]:
    return (
        _analysis(
            role="Data Scientist NLP",
            domain="HealthTech",
            company_context="Clinical AI platform",
            task="Build reliable clinical RAG pipelines",
            required_skill="RAG",
        ),
        _analysis(
            role="ML Engineer",
            domain="FinTech",
            company_context="Fraud prevention platform",
            task="Deploy and monitor fraud-detection APIs",
            required_skill="Kubernetes",
        ),
    )


def test_cv_adaptation_keeps_profile_and_instructions_in_cacheable_prefix() -> None:
    profile = get_profile()
    first_analysis, second_analysis = _different_analyses()
    shared = {
        "profile": profile,
        "selected_experiences": list(profile.experiences),
        "selected_projects": list(profile.projects),
    }
    first = cv_adaptation.build_user_prompt(
        analysis=first_analysis,
        job_title="Data Scientist NLP",
        job_company="Acme Health",
        **shared,
    )
    second = cv_adaptation.build_user_prompt(
        analysis=second_analysis,
        job_title="ML Engineer",
        job_company="Beta Finance",
        **shared,
    )

    assert first.index("=== PROFILE") < first.index("=== STYLE")
    assert first.index("=== STYLE") < first.index("=== JOB")
    _assert_stable_prefix(
        f"{cv_adaptation.build_system_prompt()}\n{first}",
        f"{cv_adaptation.build_system_prompt()}\n{second}",
        "=== JOB ===",
    )
