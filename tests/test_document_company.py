"""Document identity checks over frozen offers and saved V2/V3 extractions."""

from __future__ import annotations

import json
from pathlib import Path

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

CORPUS = Path(__file__).resolve().parents[1] / "benchmarks/elan_200"


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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


def test_saved_benchmark_document_decisions_cover_200_offers_and_400_extractions():
    if not (CORPUS / "corpus/corpus_1/offers.jsonl").is_file():
        pytest.skip("Frozen local benchmark is unavailable")
    offers = {row["offer_id"]: row for row in _jsonl(CORPUS / "corpus/corpus_1/offers.jsonl")}
    assert len(offers) == 200
    runs = [CORPUS / "job_offer_extraction/runs" / run / "results.jsonl"
            for run in ("v2-gpt54mini-medium-16000-20261009",
                        "v3-gpt54mini-medium-16000-20261009")]
    assert all(path.is_file() for path in runs)
    decisions = {}
    for path in runs:
        rows = _jsonl(path)
        assert len(rows) == 200
        assert {row["offer_id"] for row in rows} == set(offers)
        for row in rows:
            result = json.loads(
                row["raw_batch_result"]["response"]["body"]["choices"][0]["message"]["content"]
            )
            extracted = result["extracted_company_name"]
            decision = document_company_name(extracted)
            assert decision in {"", " ".join(extracted.split())}
            if decision:
                assert not is_known_publisher_name(decision)
            decisions[(path.parent.name[:2], row["offer_id"])] = decision

    # Independent offer evidence establishes Allphins, while the V2 source
    # label STATION F is a publisher/location reference. The others have
    # independently documented source metadata; URL alone does not mask them.
    assert decisions[("v2", "ELAN200-H-007")] == ""
    assert decisions[("v3", "ELAN200-H-007")] == "Allphins"
    assert decisions[("v3", "ELAN200-H-096")] == "Accenture France"
    assert decisions[("v3", "ELAN200-N-010")] == "T&S (Technology & Strategy)"
    assert sum(not decisions[("v3", offer_id)] for offer_id in offers) == 32
    independently_supported = {
        "ELAN200-H-045": ("Capgemini", "L'entreprise Capgemini recherche"),
        "ELAN200-H-047": ("DATIVE", "Pourquoi rejoindre DATIVE"),
        "ELAN200-H-051": ("EDF", "groupe EDF"),
        "ELAN200-H-066": ("Sapiens Group", "Sapiens Group, Située"),
        "ELAN200-H-069": ("Natixis", "L'entreprise Natixis recherche"),
        "ELAN200-N-007": ("Framatome", "de Framatome"),
        "ELAN200-N-031": ("JOCAD IT", "En rejoignant JOCAD IT"),
        "ELAN200-N-084": ("Pelagon", "Pelagon is building"),
        "ELAN200-N-099": ("Esri", "Esri\nverifiedVerified Listing"),
    }
    for offer_id, (employer, source_anchor) in independently_supported.items():
        assert source_anchor in offers[offer_id]["original_description"]
        assert decisions[("v3", offer_id)] == employer
