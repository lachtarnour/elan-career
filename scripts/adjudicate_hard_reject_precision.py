#!/usr/bin/env python3
"""Adjudicate every blocked hard-reject decision against the saved full offer.

The input replay must have been produced by ``audit_all_hard_reject_gates.py``.
This script never writes to the application database. It emits the manually
confirmed exceptions and leaves every other pair explicitly unreviewed. A
precision figure is emitted only when every blocked pair for that gate has an
independent verdict.

``FALSE_BY_GATE`` records cases where a literal trigger is contradicted by the
surrounding offer text. Those overrides are intentionally explicit and auditable
rather than inferred from workflow status, Match score, IA score, or an
archived/sent outcome. Unlisted decisions must never be presumed legitimate.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from smartapply.filtering.hard_reject.input_fingerprint import hard_reject_input_sha256

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = ROOT / "artifacts" / "filter-audit-20261009"
GATE_LABELS = {
    "language": "Langue",
    "contract": "Contrat",
    "experience": "Expérience",
    "qualification": "Qualification",
    "seniority": "Séniorité",
    "role": "Métier",
}


FALSE_BY_GATE: dict[str, dict[int, str]] = {
    "language": {},
    "contract": {
        927: "Le texte décrit une prestation de recrutement et d'intérim du cabinet; l'offre elle-même est un CDI.",
        1198: "Le texte décrit une prestation de recrutement et d'intérim du cabinet; l'offre elle-même est un CDI.",
        1564: "La mention travail temporaire décrit l'agence Adéquat; l'offre elle-même est un CDI.",
        1580: "La mention travail temporaire décrit l'agence Adéquat; l'offre elle-même est un CDI.",
        2122: "La mention travail temporaire décrit l'agence Adéquat; l'offre elle-même est un CDI.",
        2160: "Randstad énumère ses services intérim, CDD et CDI; l'offre elle-même est un CDI.",
        10631: "Randstad énumère ses services intérim, CDD et CDI; l'offre elle-même est un CDI.",
        3241: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        3298: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        3541: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        3645: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        3949: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        4760: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        4807: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        5594: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        5957: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        9322: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        10807: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        12699: "La phrase exclut explicitement les personnes cherchant une alternance; elle ne propose pas une alternance.",
        5285: "Le poste est ouvert en CDI, freelance ou portage; une option CDI compatible est explicitement proposée.",
        11489: "“Recherche contractuelle” décrit la relation avec des partenaires industriels, pas le contrat de travail.",
        11542: "“Cryogenic stage” désigne un étage cryogénique de lanceur, pas un stage étudiant.",
        11820: "L'alternance est citée comme expérience passée acceptable; le poste proposé est à temps plein.",
        12438: "Le déclencheur vient de “international d'audit”; le texte annonce un démarrage en CDI.",
        13152: "L'offre propose explicitement CDI, portage ou freelance; une option CDI compatible existe.",
    },
    "experience": {
        107: "Les 25 ans qualifient l'ancienneté d'une méthode, pas un minimum d'expérience candidat.",
        4543: "La plage commence à 4 ans (4–7+); cinq ans ne sont donc pas un minimum obligatoire.",
        6202: "La plage couvre 3 à 20+ ans et plusieurs niveaux; cinq ans ne sont pas un minimum obligatoire.",
        6203: "La plage couvre 3 à 20+ ans et plusieurs niveaux; cinq ans ne sont pas un minimum obligatoire.",
        6286: "La plage couvre 2 à 10+ ans, du junior au senior; cinq ans ne sont pas un minimum obligatoire.",
        7720: "La plage couvre 2 à 10+ ans, du junior au senior; cinq ans ne sont pas un minimum obligatoire.",
        8917: "La plage commence à 4 ans (4–7+); cinq ans ne sont donc pas un minimum obligatoire.",
        9212: "La plage commence à 4 ans (4–8+); cinq ans ne sont donc pas un minimum obligatoire.",
        9213: "La plage commence à 4 ans (4–8+); cinq ans ne sont donc pas un minimum obligatoire.",
        9766: "La plage commence à 4 ans (4–8+); cinq ans ne sont donc pas un minimum obligatoire.",
        11546: "Les 15 ans qualifient l'historique du programme de recherche, pas le candidat.",
        11601: "Le parcours Master demande seulement 1 à 3 ans; les 10 ans concernent l'alternative Bac+2/3.",
        11857: "La plage commence à 4 ans (4–5+); cinq ans ne sont donc pas un minimum obligatoire.",
        11943: "Les 30 ans qualifient l'ancienneté de SERMA Ingénierie, pas le candidat.",
        12068: "Les 30 ans qualifient l'ancienneté de SERMA Ingénierie, pas le candidat.",
        12195: "Les 115 ans qualifient l'ancienneté du groupe SNEF, pas le candidat.",
        12234: "Les 30 ans qualifient l'ancienneté de SERMA Ingénierie, pas le candidat.",
        12280: "Les 170 ans qualifient l'ancienneté de l'entreprise, pas le candidat.",
        12353: "Le parcours Master demande 3 ans; les 10 ans appartiennent à une autre voie d'accès.",
        12403: "Les 30 ans qualifient l'ancienneté de SERMA Ingénierie, pas le candidat.",
        12694: "Les dix ans décrivent le parcours du fondateur, pas une exigence candidat.",
        12870: "Bac+5 ou 15 ans sont deux voies alternatives; le profil Bac+5 n'est pas soumis au seuil de 15 ans.",
        12871: "Bac+5 ou 15 ans sont deux voies alternatives; le profil Bac+5 n'est pas soumis au seuil de 15 ans.",
        12886: "La plage couvre 2 à 10+ ans, du junior au senior; cinq ans ne sont pas un minimum obligatoire.",
        13005: "Les 60 ans qualifient l'ancienneté de l'entreprise Briand, pas le candidat.",
        13068: "Cinq ans d'expérience seraient seulement un plus; ce n'est pas une obligation.",
        13204: "Bac+5 ou 15 ans sont deux voies alternatives; le profil Bac+5 n'est pas soumis au seuil de 15 ans.",
    },
    "qualification": {
        161: "Le doctorat est préféré, mais l'annonce accepte explicitement des profils Master ou grande école exceptionnels.",
        737: "L'annonce accepte explicitement un Master avec expérience comme alternative au PhD.",
        864: "Le doctorat est préféré, mais l'annonce accepte explicitement des profils Master ou grande école exceptionnels.",
        1067: "L'annonce accepte explicitement des profils de Master ou d'école d'ingénieurs comme alternative au PhD.",
    },
    "seniority": {
        2290: "Le poste coordonne transversalement équipes projet et fournisseurs; aucune responsabilité hiérarchique n'est exigée.",
        2885: "L'encadrement est une possibilité d'évolution future, pas une responsabilité du poste actuel.",
        2886: "L'encadrement est une possibilité d'évolution future, pas une responsabilité du poste actuel.",
        2888: "L'encadrement est une possibilité d'évolution future, pas une responsabilité du poste actuel.",
        2892: "L'encadrement est une possibilité d'évolution future, pas une responsabilité du poste actuel.",
        4210: "Le management est présenté comme une évolution de carrière future, pas comme une exigence actuelle.",
        4308: "L'encadrement est une possibilité d'évolution future, pas une responsabilité du poste actuel.",
        4683: "Tutorat, management ou formation sont seulement un plus; les experts terrain sans management sont acceptés.",
        7038: "Tutorat, management ou formation sont seulement un plus; les experts terrain sans management sont acceptés.",
        8855: "Le management est présenté comme une évolution de carrière future, pas comme une exigence actuelle.",
        9673: "L'annonce demande gestion de projet et/ou management transverse; la responsabilité hiérarchique n'est pas obligatoire.",
        9893: "Le management est une orientation de carrière future, pas une responsabilité du poste actuel.",
    },
    "role": {
        5293: "Malgré le domaine biochimie, la mission structure les données et développe puis valide un modèle prédictif de ML.",
        6828: "Malgré le domaine matériaux, la mission évalue des méthodes IA sur des cartographies Raman et traite les données.",
        10686: "La mission consiste explicitement à préparer les données puis concevoir, entraîner et évaluer un modèle ML.",
        11659: "Le champ titre contient par erreur tout le descriptif et capture Customer Success; la mission construit des modèles statistiques en Python.",
    },
}

TRUE_BY_GATE: dict[str, dict[int, str]] = {
    "language": {
        13089: "L'intégralité de l'offre est rédigée en allemand, langue non acceptée par le profil.",
    },
    "contract": {},
    "experience": {},
    "qualification": {},
    "seniority": {},
    "role": {},
}


def _load_decisions(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _snapshot_jobs(database: Path, ids: list[int]) -> dict[int, dict]:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"""SELECT id, title, company, description, location, contract_type,
                       remote_policy, application_url, source, source_data, status,
                       canonical_job_id
                FROM jobs WHERE id IN ({placeholders}) ORDER BY id""",
            ids,
        ).fetchall()
        connection.execute("ROLLBACK")
        return {row["id"]: dict(row) for row in rows}
    finally:
        connection.close()


def _evidence_text(gate: dict) -> str:
    return " || ".join(
        f"{item.get('rule_id', '')}: {item.get('quote', '')}" for item in gate["evidence"]
    )


def _source_data(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _current_input_sha256(job: dict) -> str:
    return hard_reject_input_sha256(
        SimpleNamespace(
            title=job["title"] or "",
            company=job["company"] or "",
            description=job["description"] or "",
            location=job["location"],
            contract_type=job["contract_type"],
            remote_policy=job["remote_policy"],
            application_url=job["application_url"],
            source=job["source"] or "",
            source_data=_source_data(job["source_data"]),
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--audit-dir", type=Path, default=DEFAULT_AUDIT)
    args = parser.parse_args()

    database = args.database.expanduser().resolve()
    audit_dir = args.audit_dir.expanduser().resolve()
    replay = _load_decisions(audit_dir / "decisions.jsonl")
    blocked_ids = sorted(
        {
            item["id"]
            for item in replay
            if any(gate["status"] == "blocked" for gate in item["gates"])
        }
    )
    jobs = _snapshot_jobs(database, blocked_ids)
    if set(jobs) != set(blocked_ids):
        missing = sorted(set(blocked_ids) - set(jobs))
        raise RuntimeError(f"Offers disappeared since replay: {missing[:20]}")

    verdict_rows: list[dict] = []
    offer_rows: list[dict] = []
    counts: dict[str, Counter[str]] = {gate: Counter() for gate in GATE_LABELS}
    seen_overrides: set[tuple[str, int]] = set()

    for item in replay:
        blocked = [gate for gate in item["gates"] if gate["status"] == "blocked"]
        if not blocked:
            continue
        job = jobs[item["id"]]
        current_sha256 = _current_input_sha256(job)
        if current_sha256 != item["input_sha256"]:
            raise RuntimeError(
                f"Offer {item['id']} changed since replay: "
                f"{item['input_sha256']} != {current_sha256}"
            )
        per_offer: dict[str, str] = {}
        for gate in blocked:
            gate_name = gate["gate"]
            false_reason = FALSE_BY_GATE[gate_name].get(item["id"])
            true_reason = TRUE_BY_GATE[gate_name].get(item["id"])
            if false_reason:
                verdict = "illegitimate"
                rationale = false_reason
                seen_overrides.add((gate_name, item["id"]))
            elif true_reason:
                verdict = "legitimate"
                rationale = true_reason
            else:
                verdict = "not_independently_reviewed"
                rationale = "Aucun verdict indépendant n'a encore été posé sur cette décision."
            counts[gate_name][verdict] += 1
            per_offer[gate_name] = verdict
            verdict_rows.append(
                {
                    "offer_id": item["id"],
                    "status": item["status"],
                    "title": item["title"],
                    "company": item["company"],
                    "gate": gate_name,
                    "gate_label": GATE_LABELS[gate_name],
                    "verdict": verdict,
                    "rationale": rationale,
                    "reason_codes": "|".join(gate["reason_codes"]),
                    "decision_evidence": _evidence_text(gate),
                    "input_sha256": item["input_sha256"],
                }
            )
        offer_rows.append(
            {
                "offer_id": item["id"],
                "status": item["status"],
                "title": item["title"],
                "company": item["company"],
                "source": item["source"],
                "location": job["location"] or "",
                "contract_type": job["contract_type"] or "",
                "canonical_job_id": job["canonical_job_id"] or "",
                "blocked_gates": "|".join(gate["gate"] for gate in blocked),
                **{
                    f"{gate_name}_verdict": per_offer.get(gate_name, "not_blocked")
                    for gate_name in GATE_LABELS
                },
                "description": job["description"] or "",
            }
        )

    expected = {
        (gate_name, offer_id)
        for gate_name, offers in FALSE_BY_GATE.items()
        for offer_id in offers
    }
    if seen_overrides != expected:
        missing = sorted(expected - seen_overrides)
        unexpected = sorted(seen_overrides - expected)
        raise RuntimeError(f"Override mismatch; missing={missing}, unexpected={unexpected}")

    verdict_path = audit_dir / "filter_verdicts.csv"
    with verdict_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(verdict_rows[0]))
        writer.writeheader()
        writer.writerows(verdict_rows)

    offers_path = audit_dir / "blocked_offers_full_text.csv"
    with offers_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(offer_rows[0]))
        writer.writeheader()
        writer.writerows(offer_rows)

    summary_rows = []
    for gate_name, label in GATE_LABELS.items():
        legitimate = counts[gate_name]["legitimate"]
        illegitimate = counts[gate_name]["illegitimate"]
        unreviewed = counts[gate_name]["not_independently_reviewed"]
        total = legitimate + illegitimate + unreviewed
        fully_reviewed = unreviewed == 0
        summary_rows.append(
            {
                "gate": gate_name,
                "gate_label": label,
                "blocked_decisions": total,
                "legitimate": legitimate,
                "illegitimate": illegitimate,
                "not_independently_reviewed": unreviewed,
                "precision_percent": (
                    round(100 * legitimate / total, 2) if total and fully_reviewed else ""
                ),
                "false_positive_rate_percent": (
                    round(100 * illegitimate / total, 2) if total and fully_reviewed else ""
                ),
            }
        )
    summary_path = audit_dir / "precision_by_filter.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)

    false_positive_rows = [
        row for row in verdict_rows if row["verdict"] == "illegitimate"
    ]
    false_positive_path = audit_dir / "false_positives.csv"
    with false_positive_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(false_positive_rows[0]))
        writer.writeheader()
        writer.writerows(false_positive_rows)

    metadata = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "database": str(database),
        "offers_with_at_least_one_block": len(offer_rows),
        "blocked_offer_gate_pairs": len(verdict_rows),
        "false_positive_offer_gate_pairs": len(false_positive_rows),
        "false_positive_offers": len({row["offer_id"] for row in false_positive_rows}),
        "precision_definition": (
            "legitimate / blocked_decisions, only after independent review of every block"
        ),
        "summary": summary_rows,
        "false_positive_offer_ids": {
            gate: sorted(offers) for gate, offers in FALSE_BY_GATE.items()
        },
    }
    (audit_dir / "adjudication_summary.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    table_lines = []
    for row in summary_rows:
        precision = (
            f"{row['precision_percent']:.2f} %"
            if row["precision_percent"] != ""
            else "non calculable"
        )
        table_lines.append(
            "| {gate_label} | {blocked_decisions:,} | {legitimate:,} | "
            "{illegitimate:,} | {not_independently_reviewed:,} | {precision} |".format(
                **row, precision=precision
            ).replace(",", " ")
        )
    table = "\n".join(table_lines)
    false_status = Counter(row["status"] for row in false_positive_rows)
    blocked_count = f"{len(verdict_rows):,}".replace(",", " ")
    false_pair_count = f"{len(false_positive_rows):,}".replace(",", " ")
    false_offer_count = f"{len({row['offer_id'] for row in false_positive_rows}):,}".replace(
        ",", " "
    )
    report = f"""# Correction méthodologique de l'audit des filtres

Le premier calcul de précision était biaisé : toute décision qui n'appartenait
pas à la liste des erreurs repérées avait été considérée comme légitime par
défaut. Cette règle produit mécaniquement des précisions proches de 100 % et ne
constitue pas une annotation indépendante. Les chiffres précédents sont donc
retirés.

La base SQLite a été ouverte en lecture seule et n'a pas été modifiée. Les
statuts de parcours et les scores Match/IA ne servent pas d'étiquettes de vérité.

## État vérifié

| Filtre | Blocages | Légitimes confirmés | Faux positifs confirmés | Non relus indépendamment | Précision |
|---|---:|---:|---:|---:|---:|
{table}

Les six filtres totalisent **{blocked_count} couples offre–filtre bloquants**.
À ce stade, **{false_pair_count} faux positifs sont confirmés** sur
**{false_offer_count} offres distinctes**. Ce sont des minima, pas le nombre
définitif d'erreurs. Les statuts de ces offres sont :
{', '.join(f'{key} {value}' for key, value in sorted(false_status.items()))}.

## Principales causes des faux positifs

- **Contrat** : mentions génériques d'agences de recrutement, négation
  d'alternance, expérience antérieure en alternance, mots anglais comme
  « cryogenic stage », et offres proposant explicitement le CDI parmi plusieurs
  modalités.
- **Expérience** : ancienneté de l'entreprise ou d'une méthode, plage dont la
  borne basse est inférieure à cinq ans, exigence optionnelle, ou voie de diplôme
  alternative qui ne demande pas le seuil élevé.
- **Qualification** : doctorat présenté avec une vraie alternative Master ou
  école d'ingénieurs.
- **Séniorité** : management futur, optionnel ou uniquement transverse, sans
  responsabilité hiérarchique actuelle.
- **Métier** : mission ML explicite cachée derrière un titre de domaine et un
  champ titre corrompu par le descriptif complet.

## Traçabilité offre par offre

- `filter_verdicts.csv` : une ligne par couple offre–filtre bloquant. Les cas non
  encore relus portent explicitement `not_independently_reviewed`.
- `false_positives.csv` : les faux positifs déjà confirmés.
- `blocked_offers_full_text.csv` : une ligne par offre bloquée, le texte intégral
  et les verdicts des six filtres.
- `precision_by_filter.csv` : les agrégats reproduisant le tableau ci-dessus.
- `adjudication_summary.json` : les totaux et les identifiants exacts des faux
  positifs.

## Mesure encore nécessaire

Une précision fiable exige une lecture aveugle du verdict actuel : l'évaluateur
reçoit uniquement le profil, la politique des six filtres et le texte complet de
l'offre, puis rend six verdicts avec citation. Les résultats doivent ensuite être
comparés au rejeu. Pour mesurer aussi le rappel, cette annotation doit couvrir les
13 024 offres, y compris les 6 616 offres sans blocage.
"""
    (audit_dir / "rapport_precision.md").write_text(
        report + "\n", encoding="utf-8"
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
