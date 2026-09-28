"""Safety checks for the explicit, transactional database repair tools."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from smartapply.offers import make_external_id
from tools.repair_francetravail_identity import _apply as apply_france_travail_plan
from tools.repair_francetravail_identity import _plan as france_travail_plan
from tools.repair_historical_application_data import apply_plan, build_plan


def test_france_travail_repair_swaps_exact_url_alias_and_preserves_rows() -> None:
    database = sqlite3.connect(":memory:")
    database.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            external_id TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL,
            source_data TEXT,
            canonical_job_id INTEGER,
            duplicate_match_type TEXT
        );
        CREATE TABLE applications (id INTEGER PRIMARY KEY, job_id INTEGER);
        CREATE TABLE llm_usage (
            id INTEGER PRIMARY KEY,
            job_id INTEGER,
            job_external_id TEXT
        );
        """
    )
    native_id = "213HYVJ"
    normalized = make_external_id("francetravail", native_id)
    source_data = json.dumps({"id": native_id})
    database.execute(
        "INSERT INTO jobs VALUES (1, ?, 'francetravail', ?, NULL, NULL)",
        ("francetravail:old-hash", source_data),
    )
    database.execute(
        "INSERT INTO jobs VALUES (2, ?, 'francetravail', ?, 1, 'exact_url')",
        (normalized, source_data),
    )
    database.execute("INSERT INTO applications VALUES (1, 1)")
    database.execute(
        "INSERT INTO llm_usage VALUES (1, 1, 'francetravail:old-hash')"
    )

    plan = france_travail_plan(database)
    assert len(plan) == 1
    assert plan[0].alias_id == 2
    summary = apply_france_travail_plan(database, plan)

    assert summary == {"jobs_normalized": 1, "llm_usage_normalized": 1}
    assert [
        tuple(row)
        for row in database.execute("SELECT id, external_id FROM jobs ORDER BY id")
    ] == [(1, normalized), (2, "francetravail:old-hash")]
    assert database.execute("SELECT count(*) FROM applications").fetchone()[0] == 1
    assert database.execute(
        "SELECT job_external_id FROM llm_usage WHERE id = 1"
    ).fetchone()[0] == normalized


def test_historical_repair_archives_secondary_dossier_repairs_path_and_links_alias(
    tmp_path: Path,
) -> None:
    database = sqlite3.connect(":memory:")
    database.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            canonical_job_id INTEGER,
            status TEXT NOT NULL,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            application_url TEXT,
            possible_duplicate_of_id INTEGER,
            duplicate_review_status TEXT,
            duplicate_match_type TEXT,
            duplicate_confidence REAL
        );
        CREATE TABLE applications (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            cv_docx_path TEXT,
            cv_pdf_path TEXT
        );
        CREATE TABLE generated_documents (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL,
            doc_type TEXT NOT NULL,
            path TEXT
        );
        """
    )
    replacement = tmp_path / "CV - Candidate.docx"
    replacement.write_text("fixture", encoding="utf-8")
    missing = tmp_path / "CV - Ancien.docx"

    # Confirmed group with two live dossiers: the secondary one is archived.
    database.execute(
        "INSERT INTO jobs VALUES (1, NULL, 'sent', 'Acme', 'ML Engineer', NULL, NULL, NULL, NULL, NULL)"
    )
    database.execute(
        "INSERT INTO jobs VALUES (2, 1, 'archived', 'Acme', 'ML Engineer', NULL, NULL, 'confirmed', 'fuzzy', 0.9)"
    )
    database.execute(
        "INSERT INTO applications VALUES (1, 1, 'sent', ?, NULL)", (str(missing),)
    )
    database.execute(
        "INSERT INTO applications VALUES (2, 2, 'ready_for_form_submission', NULL, NULL)"
    )
    database.execute(
        "INSERT INTO generated_documents VALUES (1, 1, 'cv_docx', ?)", (str(missing),)
    )

    # Independent exact-URL pair: the archived row without a dossier becomes an alias.
    database.execute(
        "INSERT INTO jobs VALUES (3, NULL, 'sent', 'Beta SAS', 'Data Scientist', 'https://beta.test/jobs/42/', NULL, NULL, NULL, NULL)"
    )
    database.execute(
        "INSERT INTO jobs VALUES (4, NULL, 'archived', ' beta sas ', 'Data Scientist', 'https://beta.test/jobs/42', NULL, NULL, NULL, NULL)"
    )
    database.execute(
        "INSERT INTO applications VALUES (3, 3, 'sent', NULL, NULL)"
    )

    plan = build_plan(database)
    assert [item.application_id for item in plan.applications] == [2]
    assert [item.document_id for item in plan.documents] == [1]
    assert [(item.alias_job_id, item.canonical_job_id) for item in plan.aliases] == [(4, 3)]

    apply_plan(database, plan)

    assert database.execute(
        "SELECT status FROM applications WHERE id = 2"
    ).fetchone()[0] == "archived"
    assert database.execute(
        "SELECT path FROM generated_documents WHERE id = 1"
    ).fetchone()[0] == str(replacement)
    assert database.execute(
        "SELECT cv_docx_path FROM applications WHERE id = 1"
    ).fetchone()[0] == str(replacement)
    assert tuple(
        database.execute(
            "SELECT canonical_job_id, duplicate_review_status, duplicate_match_type "
            "FROM jobs WHERE id = 4"
        ).fetchone()
    ) == (3, "confirmed", "exact_url")
    assert database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
