"""Document identity and publisher-name regression checks."""

from __future__ import annotations

import pytest

from smartapply.company_identity import (
    document_company_name,
    is_known_publisher_name,
    publisher_reintroduced,
)
from smartapply.cv import HtmlApplicationRenderer
from smartapply.database import write_session_scope
from smartapply.database.models import Job, JobAnalysis
from smartapply.desktop.services import DesktopService
from smartapply.llm import MotivationLetter
from smartapply.profile import get_profile


def test_exact_publisher_guard_keeps_other_employers():
    for platform in ("STATION F", "LinkedIn", "Indeed", "France Travail", "APEC",
                     "Welcome to the Jungle", "Forums Talents Handicap"):
        assert is_known_publisher_name(platform)
        assert document_company_name(platform) == ""
        assert publisher_reintroduced(platform, f"Je candidate chez {platform}.")
    for employer in ("Allphins", "Accenture France", "T&S (Technology & Strategy)",
                     "LinkedIn Learning France", "Indeed Consulting", "Capgemini"):
        assert document_company_name(employer) == employer
    assert not publisher_reintroduced("LinkedIn", "Une expérience en communication.")


@pytest.mark.parametrize(
    "unknown_label",
    (
        "LinkedIn",
        "Entreprise non communiquée",
        "Entreprise non reconnue",
        "Non reconnues",
        "Employeur non reconnu",
    ),
)
def test_unknown_company_is_not_rendered_as_a_literal_addressee(unknown_label):
    letter = MotivationLetter(subject="Candidature", body="Un paragraphe.\n\nDeux.\n\nTrois.")
    html = HtmlApplicationRenderer(get_profile()).render_letter_html(
        letter=letter, job_title="Ingénieur IA", job_company=unknown_label
    )
    assert document_company_name(unknown_label) == ""
    assert unknown_label not in html
    assert "Ingénieur IA" in html


def test_known_employer_is_retained_in_letter_addressee():
    letter = MotivationLetter(subject="Candidature", body="Un paragraphe.\n\nDeux.\n\nTrois.")
    html = HtmlApplicationRenderer(get_profile()).render_letter_html(
        letter=letter, job_title="Ingénieur IA", job_company="Société Non Reconnue"
    )
    assert "<div>Société Non Reconnue</div>" in html


def test_desktop_search_prefers_extracted_employer_and_exposes_source(isolated_db):
    with write_session_scope() as session:
        job = Job(external_id="display-identity", title="AI Engineer",
                  source_company="STATION F", description="Rejoignez Allphins.",
                  source="fixture", status="analyzed")
        job.analysis = JobAnalysis(raw_response={"extracted_company_name": "Allphins"})
        session.add(job)
        session.flush()
        job_id = job.id

    service = DesktopService()
    rows = service.list_jobs(search="Allphins")
    row = next(row for row in rows if row.id == job_id)
    assert (row.company, row.source_company, row.extracted_company_name,
            row.document_company) == ("Allphins", "STATION F", "Allphins", "Allphins")
    detail = service.get_job(job_id)
    assert detail.company == "Allphins"
    assert detail.source_company == "STATION F"
