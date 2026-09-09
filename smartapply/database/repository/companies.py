"""Import and maintain the company prospecting list without resetting progress."""

from __future__ import annotations

import csv
import unicodedata
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, inspect, select
from sqlalchemy.exc import IntegrityError

from smartapply.database.models import Company
from smartapply.database.session import get_engine, session_scope

PRIORITY_LABELS = {
    0: "Non définie",
    1: "À candidater maintenant",
    2: "Très bonne cible",
    3: "Bonne cible",
    4: "Secondaire",
    5: "Faible priorité",
}
CSV_FIELDS = {
    "Ordre": "sort_order",
    "Entreprise": "name",
    "Priorité": "priority",
    "Niveau de priorité": "priority_label",
    "Catégorie": "category",
    "Type": "company_type",
    "Anglais estimé": "english_level",
    "Sélectivité estimée": "selectivity",
    "Raison du classement": "ranking_reason",
}


def company_name_key(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).split()).casefold()


def read_company_csv(path: Path) -> list[dict[str, Any]]:
    """Validate the entire file before any database write."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if set(CSV_FIELDS) - set(reader.fieldnames or []):
            raise ValueError(
                "Le fichier entreprises ne contient pas toutes les colonnes attendues."
            )
        rows = []
        known: set[str] = set()
        orders: set[int] = set()
        for line, row in enumerate(reader, start=2):
            if None in row or any(row.get(header) is None for header in CSV_FIELDS):
                raise ValueError(f"Ligne {line} : nombre de colonnes invalide.")
            values: dict[str, Any] = {
                field: row[header].strip() for header, field in CSV_FIELDS.items()
            }
            key = company_name_key(values["name"])
            if not key or len(values["name"]) > 255:
                raise ValueError(f"Ligne {line} : nom obligatoire, limité à 255 caractères.")
            if key in known:
                raise ValueError(f"Ligne {line} : entreprise en double : {values['name']}.")
            try:
                values["sort_order"] = int(values["sort_order"])
                values["priority"] = int(values["priority"])
            except ValueError as exc:
                raise ValueError(f"Ligne {line} : ordre ou priorité invalide.") from exc
            if values["priority"] not in PRIORITY_LABELS or values["sort_order"] < 1:
                raise ValueError(f"Ligne {line} : ordre ou priorité invalide.")
            if values["sort_order"] in orders:
                raise ValueError(f"Ligne {line} : ordre en double.")
            known.add(key)
            orders.add(values["sort_order"])
            rows.append({**values, "name_key": key})
    if not rows:
        raise ValueError("Le fichier entreprises est vide.")
    return rows


def seed_companies(path: Path) -> int:
    """Add missing CSV entries atomically; existing fields and checks stay intact."""
    rows = read_company_csv(path)
    with session_scope() as session:
        known = set(session.scalars(select(Company.name_key)))
        added = 0
        for values in rows:
            key = values["name_key"]
            if key in known:
                continue
            session.add(Company(**values, checked=False))
            known.add(key)
            added += 1
        return added


def sync_companies(path: Path, *, dry_run: bool = False) -> dict[str, int]:
    """Upsert CSV fields; checked rows and rows absent from the CSV are untouched.

    Names (Unicode/case/space normalized) are stable keys, never CSV row numbers.
    No checked value is imported, and no row is deleted or recreated.
    """
    rows = read_company_csv(path)
    engine = get_engine()
    report = dict(added=0, updated=0, unchanged=0, checked_preserved=0, absent_preserved=0)
    if not inspect(engine).has_table(Company.__tablename__):
        if dry_run:
            return {**report, "added": len(rows)}
        Company.__table__.create(engine, checkfirst=True)
    with session_scope() as session:
        # Serialize with UI checkbox writes, including changes during a CSV sync.
        if not dry_run and engine.dialect.name == "sqlite":
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        query = select(Company)
        if not dry_run and engine.dialect.name != "sqlite":
            query = query.with_for_update()
        known = {company.name_key: company for company in session.scalars(query)}
        csv_keys = {values["name_key"] for values in rows}
        report["absent_preserved"] = len(known.keys() - csv_keys)
        for values in rows:
            company = known.get(values["name_key"])
            if company is None:
                report["added"] += 1
                if not dry_run:
                    session.add(Company(**values, checked=False))
            elif company.checked:
                report["checked_preserved"] += 1
            elif any(getattr(company, field) != value for field, value in values.items()):
                report["updated"] += 1
                if not dry_run:
                    for field, value in values.items():
                        setattr(company, field, value)
            else:
                report["unchanged"] += 1
    return report


def list_companies() -> list[dict[str, Any]]:
    with session_scope() as session:
        companies = session.scalars(
            select(Company).order_by(
                case((Company.priority == 0, 6), else_=Company.priority),
                Company.sort_order,
                Company.id,
            )
        )
        return [
            {
                "id": company.id,
                **{field: getattr(company, field) for field in CSV_FIELDS.values()},
                "checked": company.checked,
            }
            for company in companies
        ]


def set_company_checked(company_id: int, checked: bool) -> None:
    with session_scope() as session:
        company = session.get(Company, company_id)
        if company is None:
            raise ValueError("Cette entreprise n’existe plus.")
        company.checked = checked


def add_company(values: dict[str, Any]) -> int:
    name = " ".join(str(values.get("name") or "").split())
    if not name:
        raise ValueError("Le nom de l’entreprise est obligatoire.")
    if len(name) > 255:
        raise ValueError("Le nom doit contenir au maximum 255 caractères.")
    try:
        priority = int(str(values.get("priority", 0)))
    except (ValueError, TypeError) as exc:
        raise ValueError("La priorité doit être comprise entre 1 et 5, ou non définie.") from exc
    if priority not in PRIORITY_LABELS:
        raise ValueError("La priorité doit être comprise entre 1 et 5, ou non définie.")
    details = {
        field: str(values.get(field) or "").strip()
        for field in ("category", "company_type", "english_level", "selectivity", "ranking_reason")
    }
    try:
        with session_scope() as session:
            key = company_name_key(name)
            if session.scalar(select(Company.id).where(Company.name_key == key)) is not None:
                raise ValueError("Cette entreprise figure déjà dans la liste.")
            last_order = session.scalar(select(func.max(Company.sort_order))) or 0
            company = Company(
                name=name,
                name_key=key,
                sort_order=last_order + 1,
                priority=priority,
                priority_label=PRIORITY_LABELS[priority],
                **details,
                checked=False,
            )
            session.add(company)
            session.flush()
            return company.id
    except IntegrityError as exc:
        raise ValueError("Cette entreprise figure déjà dans la liste.") from exc
