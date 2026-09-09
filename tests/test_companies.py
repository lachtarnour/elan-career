"""Company imports and progress must survive restarts and failed edits."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from smartapply.database import Company, Job, session_scope
from smartapply.database.repository.companies import (
    CSV_FIELDS,
    add_company,
    list_companies,
    seed_companies,
    set_company_checked,
    sync_companies,
)
from smartapply.database.session import reset_engine_cache
from smartapply.desktop.services import DesktopService


def company_csv(tmp_path: Path, rows: list[list[str]]) -> Path:
    path = tmp_path / "companies.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(CSV_FIELDS)
        writer.writerows(rows)
    return path


def test_import_preserves_every_field_and_checked_state_after_reopening(isolated_db):
    path = company_csv(
        isolated_db,
        [
            [
                "1",
                "Élan Data",
                "2",
                "Très bonne cible",
                "Data, IA",
                "Startup",
                "Variable",
                "Modérée",
                "Une équipe data, en France.",
            ],
        ],
    )
    assert seed_companies(path) == 1
    [company] = list_companies()
    assert company == {
        "id": company["id"],
        "sort_order": 1,
        "name": "Élan Data",
        "priority": 2,
        "priority_label": "Très bonne cible",
        "category": "Data, IA",
        "company_type": "Startup",
        "english_level": "Variable",
        "selectivity": "Modérée",
        "ranking_reason": "Une équipe data, en France.",
        "checked": False,
    }
    set_company_checked(company["id"], True)
    reset_engine_cache()
    assert seed_companies(path) == 0
    assert list_companies()[0]["checked"] is True
    set_company_checked(company["id"], False)
    reset_engine_cache()
    assert list_companies()[0]["checked"] is False


def test_manual_add_is_persistent_and_import_keeps_manual_fields(isolated_db):
    company_id = add_company({"name": "  Élan   Data  ", "category": "Conseil", "priority": 3})
    set_company_checked(company_id, True)
    path = company_csv(
        isolated_db,
        [
            ["1", "ÉLAN DATA", "1", "À candidater maintenant", "Autre", "", "", "", ""],
            ["2", "Autre entreprise", "5", "Faible priorité", "", "", "", "", ""],
        ],
    )
    assert seed_companies(path) == 1
    reset_engine_cache()
    first, second = list_companies()
    assert first["name"] == "Élan Data"
    assert first["category"] == "Conseil"
    assert first["priority"] == 3
    assert first["checked"] is True
    assert second["checked"] is False
    added_id = add_company({"name": "Nouvelle entreprise"})
    assert added_id > company_id
    assert list_companies()[-1]["sort_order"] == 3
    assert list_companies()[-1]["priority"] == 0


@pytest.mark.parametrize(
    "values",
    [
        {"name": " "},
        {"name": "x" * 256},
        {"name": "Acme", "priority": 6},
        {"name": "Acme", "priority": "incorrect"},
        {"name": "Acme", "priority": 1.5},
    ],
)
def test_invalid_add_does_not_write_a_row(isolated_db, values):
    with pytest.raises(ValueError):
        add_company(values)
    assert list_companies() == []


def test_duplicate_names_are_rejected_without_changing_progress(isolated_db):
    company_id = add_company({"name": "Élan Data"})
    set_company_checked(company_id, True)
    with pytest.raises(ValueError, match="déjà"):
        add_company({"name": " E\u0301LAN   DATA "})
    assert len(list_companies()) == 1
    assert list_companies()[0]["checked"] is True


def test_missing_company_reports_an_error(isolated_db):
    with pytest.raises(ValueError, match="n’existe plus"):
        set_company_checked(999, True)


def test_invalid_import_rolls_back_all_rows(isolated_db):
    path = company_csv(
        isolated_db,
        [
            ["1", "Entreprise valide", "1", "", "", "", "", "", ""],
            ["2", "Entreprise invalide", "6", "", "", "", "", "", ""],
        ],
    )
    with pytest.raises(ValueError):
        seed_companies(path)
    assert list_companies() == []


def test_startup_keeps_imported_companies_without_touching_existing_jobs(isolated_db):
    with session_scope() as session:
        session.add(
            Job(
                external_id="company-import-test",
                title="Data Engineer",
                company="Existing",
                description="Existing offer",
                source="manual",
            )
        )
    service = DesktopService()
    service.initialize()
    assert service.list_companies() == []
    path = company_csv(
        isolated_db,
        [
            ["1", "Example Company A", "1", "", "", "", "", "", "Fictional target"],
            ["2", "Example Company B", "2", "", "", "", "", "", "Fictional target"],
        ],
    )
    sync_companies(path)
    companies = service.list_companies()
    count = 2
    assert len(companies) == count
    assert [row["sort_order"] for row in companies] == list(range(1, count + 1))
    assert all(not row["checked"] for row in companies)
    service.set_company_checked(companies[0]["id"], True)
    service.add_company({"name": "Company added in test"})
    reset_engine_cache()
    DesktopService().initialize()
    assert len(service.list_companies()) == count + 1
    assert service.list_companies()[0]["checked"] is True
    with session_scope() as session:
        assert session.scalar(select(Job.company)) == "Existing"
        assert len(session.scalars(select(Company)).all()) == count + 1


def test_sync_updates_by_name_after_sort_and_preserves_checked_and_manual_rows(isolated_db):
    path = company_csv(
        isolated_db,
        [
            ["1", "Élan Data", "3", "Bonne cible", "Ancienne", "", "", "", "Ancienne raison"],
            ["2", "Vérifiée", "2", "Très bonne cible", "Privée", "", "", "", "À conserver"],
        ],
    )
    seed_companies(path)
    existing = {row["name"]: row for row in list_companies()}
    set_company_checked(existing["Vérifiée"]["id"], True)
    manual = add_company({"name": "Ajout manuel", "priority": 1})
    before = list_companies()
    with session_scope() as session:
        checked = session.get(Company, existing["Vérifiée"]["id"])
        checked_snapshot = {
            column.name: getattr(checked, column.name) for column in Company.__table__.columns
        }
    path = company_csv(
        isolated_db,
        [
            [
                "1",
                "Nouveau laboratoire",
                "1",
                "À candidater maintenant",
                "Recherche",
                "",
                "",
                "",
                "IA",
            ],
            [
                "2",
                " E\u0301LAN   DATA ",
                "1",
                "À candidater maintenant",
                "R&D",
                "",
                "",
                "",
                "Nouvelle raison",
            ],
            ["3", "Vérifiée", "5", "Faible priorité", "Écrasée", "", "", "", "Non"],
        ],
    )
    expected = dict(added=1, updated=1, unchanged=0, checked_preserved=1, absent_preserved=1)
    assert sync_companies(path, dry_run=True) == expected
    assert list_companies() == before
    assert sync_companies(path) == expected
    reset_engine_cache()
    after = list_companies()
    updated = next(row for row in after if row["id"] == existing["Élan Data"]["id"])
    assert updated["sort_order"] == 2
    assert updated["ranking_reason"] == "Nouvelle raison"
    assert updated["checked"] is False
    assert next(row for row in after if row["id"] == manual) == next(
        row for row in before if row["id"] == manual
    )
    with session_scope() as session:
        checked = session.get(Company, existing["Vérifiée"]["id"])
        assert {
            column.name: getattr(checked, column.name) for column in Company.__table__.columns
        } == checked_snapshot
    assert sync_companies(path) == dict(
        added=0, updated=0, unchanged=2, checked_preserved=1, absent_preserved=1
    )
    assert list_companies() == after


@pytest.mark.parametrize(
    "bad_row",
    [
        ["3", "A", "1", "", "", "", "", "", ""],  # Duplicate name, different order
        ["2", "C", "1", "", "", "", "", "", ""],  # Duplicate order
        ["3", "C", "6", "", "", "", "", "", ""],
        ["3", "C", "1"],  # Truncated row
    ],
)
def test_sync_rejects_invalid_file_before_writing_anything(isolated_db, bad_row):
    company_id = add_company({"name": "A", "priority": 3})
    before = list_companies()
    path = company_csv(
        isolated_db,
        [
            ["1", "A", "1", "", "", "", "", "", "changed"],
            ["2", "B", "2", "", "", "", "", "", ""],
            bad_row,
        ],
    )
    with pytest.raises(ValueError):
        sync_companies(path)
    assert list_companies() == before
    assert before[0]["id"] == company_id


def test_cli_sync_saves_restorable_backup_and_dry_run_is_read_only(isolated_db):
    from smartapply.cli import cli

    company_id = add_company({"name": "A", "priority": 3})
    set_company_checked(company_id, True)
    path = company_csv(
        isolated_db,
        [
            ["1", "A", "1", "", "", "", "", "", ""],
            ["2", "B", "2", "", "", "", "", "", ""],
        ],
    )
    runner = CliRunner()
    preview = runner.invoke(cli, ["sync-companies", "--csv", str(path), "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.output)["added"] == 1
    assert len(list_companies()) == 1
    assert not (isolated_db / "backups").exists()
    result = runner.invoke(cli, ["sync-companies", "--csv", str(path)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["checked_preserved"] == 1
    with sqlite3.connect(report["backup"]) as db:
        assert db.execute("select id, name, checked from companies").fetchall() == [
            (company_id, "A", 1)
        ]
        assert db.execute("pragma integrity_check").fetchone()[0] == "ok"
    result = runner.invoke(cli, ["sync-companies", "--csv", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["backup"] is None
    assert len(list((isolated_db / "backups").glob("*.db"))) == 1


def test_cli_sync_backup_failure_prevents_database_mutation(isolated_db, monkeypatch):
    from smartapply.cli import cli

    add_company({"name": "A", "priority": 3})
    before = list_companies()
    path = company_csv(isolated_db, [["1", "A", "1", "", "", "", "", "", ""]])

    def fail_backup(*args, **kwargs):
        raise OSError("Sauvegarde impossible")

    monkeypatch.setattr(sqlite3, "connect", fail_backup)
    result = CliRunner().invoke(cli, ["sync-companies", "--csv", str(path)])
    assert result.exit_code != 0
    assert "Sauvegarde impossible" in result.output
    assert list_companies() == before
