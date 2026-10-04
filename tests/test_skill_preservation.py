"""Known LLM skills survive enrichment, regrouping and both document formats."""

from __future__ import annotations

import re
from html import unescape

import pytest
from docx import Document

from smartapply.cv import CvAdapter, CvDocxRenderer, HtmlApplicationRenderer
from smartapply.cv.assembly import assemble_cv
from smartapply.cv.role_family import classify
from smartapply.cv.skill_display import compact_sparse_secondary_categories
from smartapply.llm import AdaptedCV, CVSelection, JobAnalysis, SkillSelectionBlock
from smartapply.profile import get_profile
from smartapply.ranking import MockEmbeddingsProvider


class _NoLLM:
    def complete_json(self, **kwargs):
        raise AssertionError("Skill preservation must not need a model call")


@pytest.mark.parametrize("role", ["Data Analyst", "Software Engineer", "LLM Engineer"])
def test_selected_catalogue_skills_survive_malformed_blocks_and_document_rendering(role):
    profile = get_profile()
    raw = CVSelection(
        cv_title="Applied AI Engineer",
        selected_project_ids=[],
        selected_certificate_ids=[],
        skills_profile_id="mixed",
        selected_skills=[
            SkillSelectionBlock(category_id="data_analysis", skills=["Python"]),
            SkillSelectionBlock(category_id="unknown_category", skills=["git", "Invented tool"]),
            SkillSelectionBlock(category_id="speech_audio", skills=["Pyannote"]),
            SkillSelectionBlock(category_id="data_analysis", skills=["SQL", "Python"]),
            SkillSelectionBlock(category_id="stats_signal", skills=["Mathematical optimization"]),
        ],
        skills_order=["data_analysis", "unknown_category", "speech_audio", "stats_signal"],
        warnings=[],
    )
    original = raw.model_dump()
    analysis = JobAnalysis(
        role_type=role,
        seniority="mid",
        domain="Technology",
        main_tasks=[],
        required_skills=[],
        nice_to_have=[],
        cv_keywords_to_include=[],
        match_reasons=[],
        risks=[],
    )
    adapter = CvAdapter(profile, llm=_NoLLM(), embeddings=MockEmbeddingsProvider())
    selected = adapter._ensure_supported_offer_skills(assemble_cv(raw, profile), analysis)
    selected = adapter._apply_role_family_contract(selected, analysis, role)
    cv = AdaptedCV(**selected.model_dump(), professional_summary="Applied AI experience.")
    displayed, _merges = compact_sparse_secondary_categories(
        cv, primary_family=classify(analysis, title=role)
    )

    expected = {"Python", "SQL", "Git", "Pyannote", "Mathematical optimization"}
    canonical = {skill for block in selected.selected_skills for skill in block.skills}
    assert expected <= canonical <= profile.skills.allowed_skills
    assert {skill for block in displayed.selected_skills for skill in block.skills} == canonical
    assert all(block.category_id != "unknown_category" for block in selected.selected_skills)
    assert raw.model_dump() == original

    html = HtmlApplicationRenderer(profile).render_cv_html(displayed)
    html_skills = [
        unescape(skill.strip())
        for span in re.findall(r'<span class="skill-list">(.*?)</span>', html, re.S)
        for skill in span.split("·")
    ]
    document = Document()
    CvDocxRenderer(profile)._write_skills(document, displayed)
    docx_skills = [
        skill.strip()
        for paragraph in document.paragraphs
        for skill in paragraph.text.split(": ", 1)[1].split(", ")
    ]
    assert set(html_skills) == set(docx_skills) == canonical
    assert len(html_skills) == len(set(html_skills))
    assert len(docx_skills) == len(set(docx_skills))
