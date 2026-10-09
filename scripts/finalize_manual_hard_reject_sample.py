#!/usr/bin/env python3
"""Write the direct adjudication of the 100-per-filter hard-reject sample.

The exception lists below are the result of reading the sampled offer text and
source facts.  This script only makes that review reproducible and computes the
sample statistics; it does not infer verdicts from keywords.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = ROOT / "artifacts" / "filter-audit-20261009"
SAMPLE = AUDIT_DIR / "manual_sample_100_per_filter.csv"
SUMMARY_CSV = AUDIT_DIR / "manual_precision_sample.csv"
SUMMARY_JSON = AUDIT_DIR / "manual_precision_sample.json"
GATES = ("language", "contract", "experience", "qualification", "seniority", "role")


ILLEGITIMATE: dict[tuple[str, str], str] = {
    (
        "experience",
        "9213",
    ): "La plage demandée est « 4-8+ years » : le minimum admissible est 4 ans, pas 5.",
    (
        "experience",
        "9717",
    ): "Le seuil de 5 à 10 ans appartient aux offres suggérées en bas de page, pas à l'offre de doctorant évaluée.",
    (
        "experience",
        "11716",
    ): "Le texte demande « +/- 5 ans », formulation approximative qui n'impose pas un minimum obligatoire de 5 ans.",
    (
        "qualification",
        "737",
    ): "Le profil accepte explicitement un PhD ou un Master avec au moins cinq ans d'expérience.",
    (
        "qualification",
        "2769",
    ): "L'annonce regroupe stage, thèse et post-doctorat et renvoie notamment vers un stage de Master ; le doctorat n'est pas obligatoire pour toutes les voies proposées.",
    (
        "qualification",
        "7585",
    ): "Le profil accepte explicitement un PhD ou, à défaut, un MSc avec une expérience industrielle substantielle.",
    (
        "qualification",
        "9717",
    ): "L'offre évaluée est un contrat doctoral accessible avec un Master 2 ; les mentions de doctorat viennent des offres suggérées voisines.",
    (
        "role",
        "9284",
    ): "Le poste d'enseignant-chercheur comporte une mission centrale de recherche en IA, science des données, modélisation prédictive et analyse de données ; le titre d'enseignement seul ne suffit pas à l'écarter localement.",
    (
        "role",
        "11554",
    ): "La mission centrale porte sur la conception et l'évaluation de modèles d'interaction ainsi que l'analyse de données multimodales avec Python et PyTorch ; elle contredit le rejet fondé seulement sur le titre enseignant-chercheur.",
}


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
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
    if gate == "language":
        return "Le titre et le descriptif sont en allemand avec une détection confiante ; seules les annonces en français ou en anglais sont acceptées."
    if gate == "contract":
        return "Le type publié ou le texte de l'offre indique un stage, une alternance, un CDD, de l'intérim, du freelance ou un temps partiel, incompatible avec le CDI à temps plein recherché."
    if gate == "experience":
        return "Le texte ou le champ structuré de la source impose un minimum d'au moins cinq ans d'expérience au candidat."
    if gate == "qualification":
        return "Le titre ou le profil exige un doctorat/post-doctorat sans alternative non doctorale accessible au profil."
    if gate == "seniority":
        return "Le titre impose explicitement un niveau senior, lead, staff, principal ou direction, ou les missions imposent l'encadrement hiérarchique d'une équipe."
    if gate == "role":
        return "Le titre et les missions principales appartiennent clairement à une famille hors des métiers IA/ML ciblés, sans mission technique IA/ML centrale qui contredise ce constat."
    raise ValueError(f"Unknown gate: {gate}")


def main() -> None:
    with SAMPLE.open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))

    expected = {"language": 1, **{gate: 100 for gate in GATES if gate != "language"}}
    observed = Counter(row["gate"] for row in rows)
    if dict(observed) != expected:
        raise RuntimeError(f"Unexpected sample sizes: {dict(observed)}")

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
        "method": "direct_review_by_codex_of_reproducible_random_sample",
        "seed": 20261009,
        "external_llm_calls": 0,
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
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
