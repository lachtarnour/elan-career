"""Scope revisions cannot reuse old classifications or influence fit inputs."""

from __future__ import annotations

import hashlib
import itertools
from typing import get_args

import pytest

from smartapply.llm.fit_context import build_fit_context, build_fit_user
from smartapply.llm.prompts import job_analysis, job_scope_screening
from smartapply.llm.schemas import (
    JobScopeScreening,
    MissionEvidence,
    MissionRelation,
    MissionScreen,
    OfferExtractionOutput,
)
from smartapply.pipeline.process.extraction_screening import extraction_offer_facts
from smartapply.profile import get_profile

# This revision concerns only the screen; retain the recorded instructions for
# all other extraction fields byte for byte, independently of local artifacts.
FACTUAL_INSTRUCTIONS_SHA256 = "77fd0cd96d3d0af90ac4ba568f30188ad03040169ef5957363b878aad5ac8afc"


def _mission_instructions() -> str:
    prompt = job_scope_screening.build_system_prompt()
    section = prompt.split("- Classify the actual responsibilities and deliverables", 1)[1]

    return " ".join(section.split())


def test_scope_requires_concrete_work_and_retains_substantial_mixed_roles() -> None:
    instructions = _mission_instructions()
    assert "General transferable skills are insufficient" in instructions
    assert "do not imagine an unmentioned data/AI component" in instructions
    assert "A mixed role is retained" in instructions
    assert "even if it is not the dominant activity" in instructions
    assert "Incidental use" in instructions and "does not by itself establish" in instructions
    assert "Affirmative evidence of the other work is required" in instructions
    assert "technically transferable or may include" not in instructions
    assert "plausible relevant component" not in instructions


def test_data_ai_scope_includes_non_programming_analysis_and_consulting() -> None:
    instructions = _mission_instructions()
    assert "Programming is not required for data analysis or consulting." in instructions
    assert (
        "Generic software development, cloud/IT administration or cybersecurity work"
        in instructions
    )
    assert "no substantial data/AI responsibility" in instructions


@pytest.mark.parametrize(
    "search_term",
    [
        "AI Engineer",
        "Machine Learning Engineer",
        "Data Scientist",
        "AI Research Engineer",
        "Applied ML Scientist",
        "NLP Engineer",
        "Computer Vision Engineer",
        "Speech AI Engineer",
        "Data Analyst",
        "Analytics Engineer",
        "Technical AI Consultant",
    ],
)
def test_search_terms_define_scope_examples_without_becoming_title_filters(
    search_term: str,
) -> None:
    instructions = _mission_instructions()
    assert search_term in instructions
    assert "not a title whitelist: classify the work described, not the job title" in instructions


def test_mission_proximity_uses_meaning_without_requiring_target_words() -> None:
    instructions = _mission_instructions()
    assert "Assess semantic proximity from the meaning of the assigned activities" in instructions
    assert "Exact search terms, role labels or acronyms are not required" in instructions
    assert (
        "synonymous and functionally equivalent duties, in any language or industry" in instructions
    )
    assert "even when the offer never says AI, ML or data" in instructions
    assert (
        "A keyword alone cannot establish proximity; actual responsibilities must support it"
        in instructions
    )


def test_financial_and_economic_proximity_requires_quantitative_missions_not_a_sector_match() -> (
    None
):
    instructions = _mission_instructions()
    assert "quantitative analysis, quantitative research" in instructions
    assert "Quantitative analysis and quantitative research are in scope" in instructions
    assert "including modelling, forecasting and statistical experimentation" in instructions
    assert "AI or ML terminology is not required for such work" in instructions
    assert "financial and economic" not in instructions
    assert (
        "For financial or economic work, concrete quantitative research or quantitative analysis "
        "responsibilities must be established."
    ) in instructions
    assert (
        "General financial, accounting, tax advisory or budget-tracking duties alone do not establish proximity"
        in instructions
    )
    assert "a distinction between activities, never a blanket sector exclusion" in instructions


def test_recentring_does_not_change_factual_extraction_instructions() -> None:
    factual_instructions = job_analysis.build_system_prompt().split(
        "- Before producing the final JSON",
        1,
    )[1]
    assert (
        hashlib.sha256(factual_instructions.encode("utf-8")).hexdigest()
        == FACTUAL_INSTRUCTIONS_SHA256
    )


def test_evidence_uncertainty_concerns_missions_and_preserves_short_specific_offers() -> None:
    instructions = _mission_instructions()
    assert "A short but specific description can be sufficient" in instructions
    assert "actual responsibilities are too vague, fragmentary or ambiguous" in instructions
    assert "conflict in a way that prevents establishing their relation" in instructions
    assert "A mixed role is not a contradiction" in instructions
    assert "salary, experience or contract information alone does not determine" in instructions


def test_routing_judgement_stays_in_screen_without_requiring_citations() -> None:
    instructions = _mission_instructions()
    assert "Keep every routing judgement exclusively in mission_screen" in instructions
    assert "candidate suitability into offer_risks or any other factual field" in instructions
    assert (
        "All other fields independently describe the offer facts and uncertainties" in instructions
    )
    assert "citation" not in instructions.lower()
    assert "source_quote" not in instructions


def _factual_extraction() -> dict:
    return {
        "role_type": "Data Engineer",
        "seniority": "mid",
        "domain": "Santé",
        "main_tasks": ["Construire des pipelines de données cliniques"],
        "required_skills": ["Python", "SQL"],
        "nice_to_have": ["Airflow"],
        "cv_keywords_to_include": ["Python", "SQL", "Airflow"],
        "offer_language": "fr",
        "extracted_company_name": "Clinical Data",
        "extracted_location": "Paris",
        "company_context": "Plateforme de données cliniques",
        "offer_interest_points": ["Construire une nouvelle plateforme clinique"],
        "constraints": ["Trois ans d'expérience demandés"],
        "supporting_excerpts": ["Construire des pipelines de données cliniques"],
        "offer_risks": ["Le nombre de jours de télétravail n'est pas précisé"],
    }


def test_recentered_scope_preserves_the_two_field_closed_contract() -> None:
    assert set(MissionScreen.model_fields) == {"mission_relation", "mission_evidence"}
    assert get_args(MissionRelation) == ("direct", "adjacent", "clearly_outside", "unknown")
    assert get_args(MissionEvidence) == ("sufficient", "insufficient", "contradictory")
    schema = MissionScreen.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"mission_relation", "mission_evidence"}


@pytest.mark.parametrize(
    ("relation", "evidence"),
    list(itertools.product(get_args(MissionRelation), get_args(MissionEvidence))),
)
@pytest.mark.parametrize("as_mapping", [False, True])
def test_screen_decisions_leave_every_offer_fact_and_fit_input_unchanged(
    relation: str,
    evidence: str,
    as_mapping: bool,
) -> None:
    facts_data = _factual_extraction()
    screen = JobScopeScreening(
        mission_screen={"mission_relation": relation, "mission_evidence": evidence}
    )
    extraction = OfferExtractionOutput.model_validate(facts_data)
    source = extraction.model_dump(mode="json") if as_mapping else extraction
    projected = extraction_offer_facts(source)
    assert projected.model_dump(mode="json") == facts_data

    context = build_fit_context(get_profile(), "mock-cheap", provider="mock", effort=None)
    user = build_fit_user(context, projected)
    baseline = extraction_offer_facts(facts_data)
    assert user == build_fit_user(context, baseline)
    assert "mission_screen" not in user
    assert "mission_relation" not in user
    assert "mission_evidence" not in user
    assert screen.mission_screen.mission_relation == relation
    assert screen.mission_screen.mission_evidence == evidence
