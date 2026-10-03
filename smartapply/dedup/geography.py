"""Read-only French commune identities from the bundled official reference.

No lookup downloads or writes data. Ambiguous names remain unresolved unless
department or postal evidence selects exactly one official commune. The caller
can translate regional context to a set of admissible departments.
Municipal arrondissement rows use their containing city's identity; those
aliases are derived from the official rows rather than guessed INSEE codes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

from unidecode import unidecode


@dataclass(frozen=True)
class Commune:
    code: str
    name: str
    department: str
    region: str | None
    postal_codes: frozenset[str]


@dataclass(frozen=True)
class _Reference:
    by_code: dict[str, Commune]
    by_name: dict[str, tuple[Commune, ...]]
    departments_by_name: dict[str, str]
    departments_by_postal_code: dict[str, frozenset[str]]


def commune_name_key(name: str | None) -> str:
    value = unidecode(str(name or "")).casefold()
    value = re.sub(r"[^\w]+", " ", value).strip()
    value = re.sub(r"\bst\b", "saint", value)
    value = re.sub(r"\bste\b", "sainte", value)
    return re.sub(r"\s+", " ", value)


@lru_cache(maxsize=1)
def _reference() -> _Reference:
    try:
        payload = json.loads(
            files("smartapply.dedup").joinpath("data/french_communes.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return _Reference({}, {}, {}, {})
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return _Reference({}, {}, {}, {})

    communes_payload = payload.get("communes", ())
    if not isinstance(communes_payload, (list, tuple)):
        return _Reference({}, {}, {}, {})

    rows: dict[str, Commune] = {}
    names: dict[str, list[Commune]] = {}
    for raw in communes_payload:
        if not isinstance(raw, list) or len(raw) != 5:
            continue
        code, name, department, region, postal_codes = raw
        if not all(isinstance(value, str) for value in (code, name, department)):
            continue
        if region is not None and not isinstance(region, str):
            continue
        if not isinstance(postal_codes, list):
            continue
        row = Commune(
            code.upper(),
            name,
            department.casefold(),
            region,
            frozenset(value for value in postal_codes if isinstance(value, str)),
        )
        rows[row.code] = row
        names.setdefault(commune_name_key(name), []).append(row)

    # The supplied reference includes official municipal arrondissement names.
    # Collapse only those rows whose parent city itself resolves uniquely.
    canonical_by_code: dict[str, Commune] = {}
    for code, row in rows.items():
        district = re.fullmatch(
            r"(.+?) \d{1,2}(?:er|e|eme)? arrondissement", commune_name_key(row.name)
        )
        parents = names.get(district.group(1), ()) if district else ()
        canonical_by_code[code] = (
            parents[0]
            if len(parents) == 1 and parents[0].department == row.department
            else row
        )

    named_identities: dict[str, dict[str, Commune]] = {}
    postal_departments: dict[str, set[str]] = {}
    for code, row in rows.items():
        canonical = canonical_by_code[code]
        named_identities.setdefault(commune_name_key(row.name), {})[canonical.code] = canonical
        for postal_code in row.postal_codes:
            postal_departments.setdefault(postal_code, set()).add(row.department)
    departments: dict[str, str] = {}
    departments_payload = payload.get("departments", ())
    for raw in departments_payload if isinstance(departments_payload, (list, tuple)) else ():
        if isinstance(raw, list) and len(raw) >= 2 and all(
            isinstance(value, str) for value in raw[:2]
        ):
            departments[commune_name_key(raw[1])] = raw[0].casefold()
    return _Reference(
        canonical_by_code,
        {key: tuple(values.values()) for key, values in named_identities.items()},
        departments,
        {key: frozenset(values) for key, values in postal_departments.items()},
    )


def commune_by_code(code: str | None) -> Commune | None:
    return _reference().by_code.get(str(code or "").upper())


def communes_named(name: str | None) -> tuple[Commune, ...]:
    return _reference().by_name.get(commune_name_key(name), ())


def department_codes_named(names: frozenset[str]) -> frozenset[str]:
    index = _reference().departments_by_name
    return frozenset(index[key] for name in names if (key := commune_name_key(name)) in index)


def postal_department_codes(postal_code: str) -> frozenset[str]:
    return _reference().departments_by_postal_code.get(postal_code, frozenset())


def resolve_commune(
    name: str | None,
    *,
    departments: frozenset[str] = frozenset(),
    postal_code: str | None = None,
) -> Commune | None:
    candidates = communes_named(name)
    if departments:
        candidates = tuple(row for row in candidates if row.department in departments)
    if postal_code and postal_department_codes(postal_code):
        candidates = tuple(row for row in candidates if postal_code in row.postal_codes)
    return candidates[0] if len(candidates) == 1 else None
