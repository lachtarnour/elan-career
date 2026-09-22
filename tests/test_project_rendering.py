"""Exports preserve the finalized project selection, including an empty one."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from smartapply.cv import CvDocxRenderer, HtmlApplicationRenderer
from smartapply.llm import AdaptedCV
from smartapply.profile import get_profile


@pytest.fixture(
    params=[
        [],
        ["proj_ner_camembert"],
        ["proj_smartapply", "proj_evidence_rag"],
        ["unknown_project"],
    ],
    ids=["empty", "single", "preserve-order", "unknown-without-fallback"],
)
def adapted(request) -> AdaptedCV:
    return AdaptedCV(
        cv_title="Data Scientist",
        professional_summary="Python, NLP and RAG experience.",
        selected_experiences=[],
        selected_project_ids=request.param,
        skills_order=[],
        warnings=[],
    )


def test_html_preserves_final_project_selection(adapted: AdaptedCV) -> None:
    profile = get_profile()
    by_id = {project.id: project for project in profile.projects}
    expected = [by_id[pid] for pid in adapted.selected_project_ids if pid in by_id]

    html = HtmlApplicationRenderer(profile).render_cv_html(adapted)
    document = BeautifulSoup(html, "html.parser")

    assert [row.get_text(strip=True) for row in document.select(".project-row strong")] == [
        project.name for project in expected
    ]
    assert bool(document.select_one("section.projects")) == bool(expected)


def test_docx_preserves_final_project_selection(adapted: AdaptedCV) -> None:
    profile = get_profile()
    by_id = {project.id: project for project in profile.projects}
    expected = [by_id[pid] for pid in adapted.selected_project_ids if pid in by_id]

    document = CvDocxRenderer(profile).render(adapted)
    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    project_lines = [text for text in paragraphs if text.startswith("• ")]

    assert project_lines == [
        f"• {project.name}  {project.description}" for project in expected
    ]
    if not adapted.selected_project_ids:
        assert "Projects" not in paragraphs
