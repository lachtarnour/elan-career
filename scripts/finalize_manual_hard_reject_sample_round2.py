#!/usr/bin/env python3
"""Persist the direct review of the second hard-reject sample.

The verdicts below come from reading each sampled offer.  The script records
those decisions and computes per-filter precision; it performs no model call
and does not derive verdicts from keyword rules.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "artifacts" / "filter-audit-20261009"
FIRST_SAMPLE = AUDIT_DIR / "manual_sample_100_per_filter.csv"
SAMPLE = AUDIT_DIR / "manual_sample_100_per_filter_round2.csv"
SUMMARY_CSV = AUDIT_DIR / "manual_precision_sample_round2.csv"
SUMMARY_JSON = AUDIT_DIR / "manual_precision_sample_round2.json"
COMBINED_CSV = AUDIT_DIR / "manual_precision_combined.csv"
COMBINED_JSON = AUDIT_DIR / "manual_precision_combined.json"
GATES = ("contract", "experience", "qualification", "seniority", "role")
ALL_GATES = ("language", *GATES)


ILLEGITIMATE: dict[tuple[str, str], str] = {
    (
        "contract",
        "5285",
    ): "L'offre laisse explicitement le choix entre CDI, freelance et portage salarial ; une modalité compatible est donc disponible.",
    (
        "experience",
        "790",
    ): "Les cinq ans sont indiqués comme « idéalement requis » ; la formulation n'en fait pas un minimum obligatoire.",
    (
        "experience",
        "12195",
    ): "Les trente ans décrivent l'expérience collective de l'équipe ; l'offre est un stage destiné à un étudiant.",
    (
        "experience",
        "12234",
    ): "Les trente ans décrivent l'ancienneté de SERMA Ingénierie et non l'expérience exigée du candidat.",
    (
        "experience",
        "13126",
    ): "Les cinq ans concernent un palier d'ancienneté donnant des congés supplémentaires, pas un prérequis candidat.",
    (
        "qualification",
        "161",
    ): "Le profil accepte explicitement des candidats exceptionnels issus d'un Master ou d'une grande école d'ingénieurs.",
    (
        "qualification",
        "864",
    ): "Le profil accepte explicitement des candidats exceptionnels issus d'un Master ou d'une grande école d'ingénieurs.",
    (
        "qualification",
        "1067",
    ): "Le profil accepte explicitement des candidats exceptionnels issus d'un Master ou d'une grande école d'ingénieurs.",
    (
        "qualification",
        "2691",
    ): "Le poste est ouvert soit à un ingénieur de recherche, soit à un post-doctorant ; le doctorat n'est donc pas obligatoire dans tous les cas.",
    (
        "qualification",
        "9570",
    ): "L'offre doctorale demande un Master 2 ; la mention de doctorat détectée appartient à une autre offre affichée dans la page.",
    (
        "seniority",
        "8662",
    ): "Le management d'équipe est présenté comme une évolution possible après environ un an, pas comme une responsabilité du poste proposé.",
    (
        "seniority",
        "10298",
    ): "Il s'agit d'un stage étudiant de trois mois sans encadrement hiérarchique ; « IA Manager » est seulement l'intitulé du stage.",
}


def _wilson(
    successes: int, total: int, z: float = 1.959963984540054
) -> tuple[float, float]:
    if total == 0:
        return (math.nan, math.nan)
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    spread = (
        z
        * math.sqrt(
            proportion * (1 - proportion) / total + z * z / (4 * total * total)
        )
        / denominator
    )
    return centre - spread, centre + spread


def _legitimate_rationale(row: dict[str, str]) -> str:
    gate = row["gate"]
    if gate == "contract":
        return "Le type publié ou le texte impose un stage, une alternance, un CDD, de l'intérim, du freelance ou un temps partiel, incompatible avec le CDI à temps plein recherché."
    if gate == "experience":
        return "Le texte ou le champ structuré de la source impose au candidat un minimum d'au moins cinq ans d'expérience."
    if gate == "qualification":
        return "Le titre ou le profil exige un doctorat/post-doctorat sans voie non doctorale accessible au profil."
    if gate == "seniority":
        return "Le titre impose un niveau senior, lead, staff, principal ou direction, ou les missions imposent l'encadrement hiérarchique d'une équipe."
    if gate == "role":
        return "Le titre et les missions principales appartiennent clairement à une famille hors des métiers IA/ML ciblés, sans mission technique IA/ML centrale qui contredise ce constat."
    raise ValueError(f"Unknown gate: {gate}")


def main() -> None:
    with FIRST_SAMPLE.open(encoding="utf-8") as stream:
        first_rows = list(csv.DictReader(stream))
    with SAMPLE.open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))

    observed = Counter(row["gate"] for row in rows)
    expected = {gate: 100 for gate in GATES}
    if dict(observed) != expected:
        raise RuntimeError(f"Unexpected sample sizes: {dict(observed)}")

    first_keys = {(row["gate"], row["offer_id"]) for row in first_rows}
    second_keys = {(row["gate"], row["offer_id"]) for row in rows}
    overlap = first_keys & second_keys
    if overlap:
        raise RuntimeError(f"The two samples overlap: {sorted(overlap)}")

    for row in rows:
        key = (row["gate"], row["offer_id"])
        if key in ILLEGITIMATE:
            row["manual_verdict"] = "illegitimate"
            row["manual_rationale"] = ILLEGITIMATE[key]
        else:
            row["manual_verdict"] = "legitimate"
            row["manual_rationale"] = _legitimate_rationale(row)

    with SAMPLE.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    by_gate: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_gate[row["gate"]].append(row)

    summary_rows = []
    for gate in GATES:
        gate_rows = by_gate[gate]
        counts = Counter(row["manual_verdict"] for row in gate_rows)
        denominator = counts["legitimate"] + counts["illegitimate"]
        low, high = _wilson(counts["legitimate"], denominator)
        summary_rows.append(
            {
                "gate": gate,
                "population_blocked": gate_rows[0]["population_size"],
                "eligible_after_first_sample": gate_rows[0][
                    "eligible_population_size"
                ],
                "sample_size": len(gate_rows),
                "legitimate": counts["legitimate"],
                "illegitimate": counts["illegitimate"],
                "uncertain": counts["uncertain"],
                "sample_precision": counts["legitimate"] / denominator,
                "false_reject_rate": counts["illegitimate"] / denominator,
                "wilson_95_low": low,
                "wilson_95_high": high,
            }
        )

    with SUMMARY_CSV.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)

    status_counts = {
        gate: dict(Counter(row["status"] for row in gate_rows))
        for gate, gate_rows in by_gate.items()
    }
    payload = {
        "method": "direct_review_by_codex_of_disjoint_reproducible_random_sample",
        "seed": 20261010,
        "external_llm_calls": 0,
        "excluded_sample": FIRST_SAMPLE.name,
        "overlap_offer_filter_pairs": 0,
        "sample_sizes": dict(observed),
        "status_counts": status_counts,
        "filters": summary_rows,
        "false_positive_keys": [
            {"gate": gate, "offer_id": int(offer_id), "rationale": rationale}
            for (gate, offer_id), rationale in ILLEGITIMATE.items()
        ],
    }
    SUMMARY_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    combined_by_gate: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in [*first_rows, *rows]:
        combined_by_gate[row["gate"]].append(row)

    combined_rows = []
    for gate in ALL_GATES:
        gate_rows = combined_by_gate[gate]
        counts = Counter(row["manual_verdict"] for row in gate_rows)
        denominator = counts["legitimate"] + counts["illegitimate"]
        population = int(gate_rows[0]["population_size"])
        low, high = _wilson(counts["legitimate"], denominator)
        combined_rows.append(
            {
                "gate": gate,
                "population_blocked": population,
                "sample_size": len(gate_rows),
                "population_coverage": len(gate_rows) / population,
                "is_census": len(gate_rows) == population,
                "legitimate": counts["legitimate"],
                "illegitimate": counts["illegitimate"],
                "uncertain": counts["uncertain"],
                "sample_precision": counts["legitimate"] / denominator,
                "false_reject_rate": counts["illegitimate"] / denominator,
                "wilson_95_low": low,
                "wilson_95_high": high,
            }
        )

    with COMBINED_CSV.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(combined_rows[0]))
        writer.writeheader()
        writer.writerows(combined_rows)

    combined_payload = {
        "method": "combined_direct_review_of_two_disjoint_random_samples",
        "external_llm_calls": 0,
        "seeds": [20261009, 20261010],
        "overlap_offer_filter_pairs": 0,
        "filters": combined_rows,
    }
    COMBINED_JSON.write_text(
        json.dumps(combined_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
