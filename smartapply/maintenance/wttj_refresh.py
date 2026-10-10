"""Refresh WTTJ contract and source availability from a completed live audit.

Provider snapshots stay intact. Fresh evidence is recorded separately so that
contract consumers can prefer it without falsifying an earlier listing response.
Offer workflow, applications, analyses and documents are preserved.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from smartapply.maintenance._sqlite_safety import (
    SQLiteSafetyError,
    online_backup,
    require_no_table_triggers,
    require_safe_output_path,
    resolve_database,
)
from smartapply.maintenance.wttj_contracts import _source_proves_cdi
from smartapply.scrapers.wttj.offer_parser import _detail_api_url_from_job_url
from smartapply.utils.contracts import normalize_source_contract_type

SOURCE = "welcometothejungle"
REFRESH_KEY = "_smartapply_wttj_refresh"


def refresh_wttj_from_audit(
    database: Path | str,
    *,
    audit: Path | str,
    backup: Path | str,
    apply: bool = False,
) -> dict[str, Any]:
    """Plan or apply an evidence-based refresh, with a backup before any write.

    All WTTJ URLs must be covered by successful API responses or API 404s confirmed
    by the HTML error page. Transient fetch failures cannot mark an offer closed.
    Only jobs.contract_type and jobs.source_data are updated, in one transaction.
    """
    database = resolve_database(database)
    backup = require_safe_output_path(backup, sqlite_databases=(database,))
    assert backup is not None
    evidence = _load_evidence(Path(audit).resolve())
    mode = "rw" if apply else "ro"
    with closing(sqlite3.connect(
        f"{database.as_uri()}?mode={mode}", uri=True, timeout=30, isolation_level=None,
    )) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            changes = []
            contracts = []
            statuses: Counter[str] = Counter()
            rows = connection.execute(
                "SELECT id, contract_type, source_data, application_url "
                "FROM jobs WHERE source = ? ORDER BY id", (SOURCE,),
            ).fetchall()
            for row in rows:
                observation = evidence.get(row["application_url"])
                if observation is None:
                    raise SQLiteSafetyError(f"No current audit evidence for WTTJ job {row['id']}")
                source_data = json.loads(row["source_data"] or "{}")
                if not isinstance(source_data, dict):
                    raise SQLiteSafetyError(f"Invalid source metadata for WTTJ job {row['id']}")
                refresh, reference = observation
                _require_same_offer(source_data, reference, row["id"])
                statuses[refresh["source_status"]] += 1
                contract = row["contract_type"]
                if refresh["contract_type"]:
                    contract = refresh["contract_type"]
                elif (
                    normalize_source_contract_type(contract) == "Full-time"
                    and _source_proves_cdi(row["source_data"])
                ):
                    # Unavailable pages retain their last known, proven contract.
                    contract = "CDI"
                if contract != row["contract_type"]:
                    contracts.append({"id": row["id"], "before": row["contract_type"],
                                      "after": contract})
                if contract != row["contract_type"] or source_data.get(REFRESH_KEY) != refresh:
                    source_data[REFRESH_KEY] = refresh
                    changes.append((contract, json.dumps(source_data, ensure_ascii=False), row["id"]))
            report = {
                "database": str(database), "audit": str(Path(audit).resolve()),
                "applied": apply, "offers_checked": len(rows), "rows_updated": len(changes),
                "contract_changes": contracts, "source_statuses": dict(statuses),
                "backup": str(backup) if apply and changes else None,
                "updated_columns": ["contract_type", "source_data"],
            }
            if apply and changes:
                require_no_table_triggers(connection, tables=("jobs",))
                online_backup(database, backup)
                result = connection.executemany(
                    "UPDATE jobs SET contract_type = ?, source_data = ? WHERE id = ? AND source = 'welcometothejungle'",
                    changes,
                )
                if result.rowcount != len(changes):
                    raise SQLiteSafetyError("WTTJ refresh row count mismatch")
                if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise SQLiteSafetyError("WTTJ refresh failed SQLite quick_check")
                connection.commit()
            else:
                connection.rollback()
            return report
        except Exception:
            connection.rollback()
            raise


def _require_same_offer(source_data: dict[str, Any], reference: str | None, job_id: int) -> None:
    for key in ("detail_api", "matches_api"):
        saved = source_data.get(key)
        if isinstance(saved, dict):
            saved_reference = saved.get("wttj_reference")
            if saved_reference and reference and saved_reference != reference:
                raise SQLiteSafetyError(f"WTTJ identity changed for job {job_id}")


def _load_evidence(audit: Path) -> dict[str, tuple[dict[str, Any], str | None]]:
    evidence = {}
    for line in (audit / "live_api_results.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        url = row["application_url"]
        if row["api_url"] != _detail_api_url_from_job_url(url):
            raise SQLiteSafetyError(f"Audit API URL mismatch for {url}")
        if url in evidence:
            raise SQLiteSafetyError(f"Duplicate audit URL: {url}")
        attempts = row.get("attempts") or []
        if not attempts or not attempts[-1].get("completed_at"):
            raise SQLiteSafetyError(f"Audit request is incomplete for {url}")
        refresh = {
            "schema_version": 1, "checked_at": attempts[-1]["completed_at"],
            "api_url": row["api_url"], "api_http_status": None,
            "source_status": None, "available": None,
            "raw_contract_type": None, "contract_type": None,
            "source_updated_at": None, "source_archived_at": None,
        }
        reference = None
        if attempts[-1].get("outcome") == "success":
            payload = json.loads((audit / "live_api_responses" / f"{int(row['id'])}.json").read_text())
            organization = payload.get("organization") or {}
            expected_api_url = (
                "https://api.welcometothejungle.com/api/v3/organizations/"
                f"{organization.get('slug')}/jobs/{payload.get('slug')}"
            )
            if expected_api_url != row["api_url"]:
                raise SQLiteSafetyError(f"Audit payload URL mismatch for {url}")
            status = payload.get("status")
            raw_contract = payload.get("contract_type")
            if status not in {"published", "archived", "unlisted"} or not isinstance(raw_contract, str):
                raise SQLiteSafetyError(f"Unusable current WTTJ payload for {url}")
            refresh.update(
                api_http_status=200, source_status=status,
                available={"published": True, "archived": False, "unlisted": None}[status],
                raw_contract_type=raw_contract,
                contract_type=normalize_source_contract_type(raw_contract, source=SOURCE),
                source_updated_at=payload.get("updated_at"),
                source_archived_at=payload.get("archived_at"),
            )
            reference = payload.get("wttj_reference")
        elif attempts[-1].get("http_status") == 404:
            html = (audit / f"html-exception-{int(row['id'])}.html").read_text()
            soup = BeautifulSoup(html, "html.parser")
            title = soup.title.get_text(strip=True) if soup.title else ""
            if title != "Erreur 404" or "Page introuvable" not in soup.get_text(" ", strip=True):
                raise SQLiteSafetyError(f"API 404 was not confirmed by HTML for {url}")
            refresh.update(api_http_status=404, source_status="not_found", available=False)
        else:
            raise SQLiteSafetyError(f"WTTJ audit fetch failed for {url}")
        evidence[url] = (refresh, reference)
    return evidence
