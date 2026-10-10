"""Coverage for refreshing real source evidence while retaining application history."""

from __future__ import annotations

import json
import sqlite3

import pytest

from smartapply.filtering.source_fact_builders.wttj import build_wttj_filter_facts
from smartapply.maintenance._sqlite_safety import SQLiteSafetyError
from smartapply.maintenance.wttj_refresh import REFRESH_KEY, refresh_wttj_from_audit
from smartapply.offers.source_metadata_builders.wttj import build_wttj_source_metadata


def _fixture(tmp_path):
    database = tmp_path / "jobs.db"
    audit = tmp_path / "audit"
    responses = audit / "live_api_responses"
    responses.mkdir(parents=True)
    results = []
    jobs = []
    for job_id, raw_contract, status in [(1, "full_time", "published"),
                                         (2, "freelance", "archived"),
                                         (3, None, "not_found")]:
        url = f"https://www.welcometothejungle.com/fr/companies/test/jobs/offer-{job_id}"
        api = f"https://api.welcometothejungle.com/api/v3/organizations/test/jobs/offer-{job_id}"
        payload = {"organization": {"slug": "test"}, "slug": f"offer-{job_id}",
                   "contract_type": raw_contract, "status": status,
                   "wttj_reference": f"reference-{job_id}", "updated_at": "2026-10-09T10:00:00Z"}
        attempt = {"completed_at": "2026-10-10T06:00:00+00:00", "outcome": "success"}
        if raw_contract:
            (responses / f"{job_id}.json").write_text(json.dumps(payload))
        else:
            attempt.update(outcome="http_error", http_status=404)
            (audit / f"html-exception-{job_id}.html").write_text(
                "<html><title>Erreur 404</title><h1>Page introuvable</h1></html>"
            )
        results.append({"id": job_id, "application_url": url,
                        "api_url": api, "attempts": [attempt]})
        saved = {"detail_api": {"contract_type": "full_time",
                                "wttj_reference": f"reference-{job_id}"},
                 "matches_api": {"contract_type": "full_time"},
                 "employment_type": "full_time"}
        jobs.append((job_id, "welcometothejungle", "Full-time", json.dumps(saved), url))
    (audit / "live_api_results.jsonl").write_text("\n".join(map(json.dumps, results)))
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, source TEXT, contract_type TEXT, "
            "source_data TEXT, application_url TEXT, status TEXT, archived_at TEXT, description TEXT)"
        )
        connection.executemany(
            "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, 'sent', NULL, 'Original text')", jobs,
        )
        connection.execute(
            "INSERT INTO jobs VALUES (4, 'linkedin', 'Full-time', '{}', 'https://example.org', "
            "'analyzed', NULL, 'Original LinkedIn text')"
        )
        connection.execute("CREATE TABLE applications (id INTEGER, job_id INTEGER, status TEXT, notes TEXT)")
        connection.execute("INSERT INTO applications VALUES (1, 3, 'sent', 'Keep my follow-up')")
    return database, audit


def _rows(database, table="jobs"):
    with sqlite3.connect(database) as connection:
        return connection.execute(f'SELECT * FROM "{table}" ORDER BY id').fetchall()


def test_refresh_preserves_original_sources_workflow_and_other_sources(tmp_path):
    database, audit = _fixture(tmp_path)
    before = _rows(database)
    applications = _rows(database, "applications")
    backup = tmp_path / "before.bak"

    plan = refresh_wttj_from_audit(database, audit=audit, backup=backup)
    assert not backup.exists()
    assert _rows(database) == before
    assert plan["source_statuses"] == {"published": 1, "archived": 1, "not_found": 1}

    report = refresh_wttj_from_audit(database, audit=audit, backup=backup, apply=True)
    assert report["rows_updated"] == 3
    assert _rows(backup) == before
    assert _rows(database, "applications") == applications
    after = _rows(database)
    assert after[3] == before[3]
    assert [row[2] for row in after[:3]] == ["CDI", "Freelance", "CDI"]
    for original, updated in zip(before[:3], after[:3], strict=True):
        assert original[:2] == updated[:2]
        assert original[4:] == updated[4:]
        original_data = json.loads(original[3])
        updated_data = json.loads(updated[3])
        assert {key: value for key, value in updated_data.items() if key != REFRESH_KEY} == original_data
        assert build_wttj_filter_facts(updated_data).structured_contract_type == updated[2]
    malt = json.loads(after[1][3])
    assert malt[REFRESH_KEY]["available"] is False
    assert malt["matches_api"]["contract_type"] == "full_time"
    metadata = build_wttj_source_metadata(malt, fields={"contract_type"})
    assert "contract_type: Freelance" in metadata
    assert "full_time" not in metadata
    unavailable = json.loads(after[2][3])[REFRESH_KEY]
    assert unavailable["source_status"] == "not_found"
    assert unavailable["contract_type"] is None

    second_backup = tmp_path / "second.bak"
    second = refresh_wttj_from_audit(database, audit=audit, backup=second_backup, apply=True)
    assert second["rows_updated"] == 0
    assert not second_backup.exists()
    assert _rows(database) == after


@pytest.mark.parametrize("failure", ["existing_backup", "uncovered_offer", "identity_changed", "fetch_error"])
def test_refresh_refuses_incomplete_or_unsafe_changes(tmp_path, failure):
    database, audit = _fixture(tmp_path)
    backup = tmp_path / "before.bak"
    if failure == "existing_backup":
        backup.write_bytes(b"keep existing backup")
    elif failure == "uncovered_offer":
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE jobs SET application_url='https://example.org/missing' WHERE id=1")
    elif failure == "identity_changed":
        payload_path = audit / "live_api_responses/1.json"
        payload = json.loads(payload_path.read_text())
        payload["wttj_reference"] = "different-offer"
        payload_path.write_text(json.dumps(payload))
    else:
        results_path = audit / "live_api_results.jsonl"
        results = list(map(json.loads, results_path.read_text().splitlines()))
        results[2]["attempts"][0]["http_status"] = 503
        results_path.write_text("\n".join(map(json.dumps, results)))
    before = _rows(database)
    with pytest.raises(SQLiteSafetyError):
        refresh_wttj_from_audit(database, audit=audit, backup=backup, apply=True)
    assert _rows(database) == before
    if failure == "existing_backup":
        assert backup.read_bytes() == b"keep existing backup"
    else:
        assert not backup.exists()
