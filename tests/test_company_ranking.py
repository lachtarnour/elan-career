"""Check ranking invariants using fictional targets, independent of private files."""

import copy
import csv
import io

import pytest

from smartapply.database.repository.companies import CSV_FIELDS
from tools.rank_companies import render


@pytest.fixture
def reviewed_data():
    actions = ["apply", "qualify_offer", "targeted_watch", "broaden", "historical_or_unverified"]
    entries = []
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for index, action in enumerate(actions, start=1):
        company = f"Example Company {index}"
        writer.writerow(
            {
                **dict.fromkeys(CSV_FIELDS, "Synthetic fixture"),
                "Ordre": str(index),
                "Entreprise": company,
                "Priorité": "3",
            }
        )
        entries.append(
            {
                "company": company,
                "action": action,
                "constraint": "none_established",
                "fit": "direct",
                "fit_basis": "Fictional skills match for testing.",
                "work": "applied_ml",
                "maturity": "documented_structure",
                "evidence": "offer_read",
                "reason": "Synthetic ranking justification.",
                "sources": [f"https://example.com/jobs/{index}"],
                "eligibility": dict.fromkeys(
                    ["open", "france", "permanent", "experience_compatible", "no_required_phd"],
                    True,
                ),
            }
        )
    return output.getvalue(), {"as_of": "2025-01-01", "companies": entries}


def test_ranking_preserves_all_companies_and_non_ranking_fields(reviewed_data):
    source, audit = reviewed_data
    result = render(source, audit)
    before = list(csv.DictReader(io.StringIO(source)))
    after = list(csv.DictReader(io.StringIO(result.decode("utf-8-sig"))))
    ranking_fields = {"Ordre", "Priorité", "Niveau de priorité", "Raison du classement"}

    def unchanged(rows):
        return {
            row["Entreprise"]: {
                key: value for key, value in row.items() if key not in ranking_fields
            }
            for row in rows
        }

    assert unchanged(before) == unchanged(after)
    assert [int(row["Ordre"]) for row in after] == list(range(1, len(after) + 1))
    assert result.startswith(b"\xef\xbb\xbf")
    assert b"\n" not in result.replace(b"\r\n", b"")
    assert render(result.decode("utf-8-sig"), audit) == result


def test_input_order_does_not_determine_priority(reviewed_data):
    source, audit = reviewed_data
    reversed_audit = copy.deepcopy(audit)
    reversed_audit["companies"].reverse()
    reader = csv.DictReader(io.StringIO(source))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
    writer.writeheader()
    writer.writerows(reversed(list(reader)))
    assert render(output.getvalue(), reversed_audit) == render(source, audit)


@pytest.mark.parametrize("problem", ["missing", "duplicate", "unknown"])
def test_incomplete_or_ambiguous_audit_is_rejected(reviewed_data, problem):
    source, audit = reviewed_data
    if problem == "missing":
        audit["companies"].pop()
    elif problem == "duplicate":
        audit["companies"].append(copy.deepcopy(audit["companies"][0]))
    else:
        audit["companies"][0]["company"] = "Not in the CSV"
    with pytest.raises(ValueError):
        render(source, audit)


@pytest.mark.parametrize(
    "criterion", ["open", "france", "permanent", "experience_compatible", "no_required_phd"]
)
def test_immediate_priority_requires_each_eligibility_criterion(reviewed_data, criterion):
    source, audit = reviewed_data
    candidate = next(entry for entry in audit["companies"] if entry["action"] == "apply")
    candidate["eligibility"][criterion] = None
    with pytest.raises(ValueError, match="Unverified immediate priority"):
        render(source, audit)


def test_search_result_alone_cannot_establish_immediate_priority(reviewed_data):
    source, audit = reviewed_data
    candidate = next(entry for entry in audit["companies"] if entry["action"] == "apply")
    candidate["evidence"] = "search_only"
    with pytest.raises(ValueError, match="Unverified immediate priority"):
        render(source, audit)
