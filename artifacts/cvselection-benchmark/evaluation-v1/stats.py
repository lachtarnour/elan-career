"""Recompute the CVSelection comparison from frozen outputs and blind ratings.

This script is read-only with respect to source datasets and model results. It
writes only statistics.json and comparison-40.md next to itself.
"""

import json
import re
from collections import Counter, defaultdict
from hashlib import sha256
from itertools import combinations
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parent
BENCH = ROOT.parent
MODELS = ("gpt-5.4", "claude-haiku-5-5", "gpt-5.4-mini")
LABEL = {"gpt-5.4": "GPT-5.4", "claude-haiku-5-5": "Haiku 5.5", "gpt-5.4-mini": "GPT-5.4 mini"}
SOURCE = {
    "dataset": BENCH / "dataset-v1/dataset.jsonl",
    "dual_results": BENCH / "runs/dual-batch-v1/results.jsonl",
    "mini_results": BENCH / "runs/mini-high-batch-v1/results.jsonl",
}


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def usage(row):
    u = row.get("usage") or {}
    if row["model_requested"] == "claude-haiku-5-5":
        return {
            "input": u.get("input_tokens", 0),
            "cache_read": u.get("cache_read_input_tokens", 0),
            "cache_write": u.get("cache_creation_input_tokens", 0),
            "output": u.get("output_tokens", 0),
            "reasoning": (u.get("output_tokens_details") or {}).get("thinking_tokens", 0),
        }
    return {
        "input": u.get("prompt_tokens", 0),
        "cache_read": (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
        "cache_write": 0,
        "output": u.get("completion_tokens", 0),
        "reasoning": (u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
    }


def main():
    blind = json.loads((ROOT / "blind_map.json").read_text())
    for path in SOURCE.values():
        key = str(path.relative_to(BENCH.parent.parent))
        expected = blind["source_sha256"][key]
        actual = sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Source changed: {path}")
    dataset = jsonl(SOURCE["dataset"])
    ratings = jsonl(ROOT / "ratings.jsonl")
    results = jsonl(SOURCE["dual_results"]) + jsonl(SOURCE["mini_results"])
    offers = {r["job_id"]: r for r in dataset}
    by_result = {(r["job_id"], r["model_requested"]): r for r in results}
    assert len(offers) == len(ratings) == 40
    assert len(by_result) == len(results) == 120
    assert {r["job_id"] for r in ratings} == set(offers) == {int(i) for i in blind["mapping"]}
    assert all(set(blind["mapping"][str(j)]) == {"A", "B", "C"} for j in offers)
    assert all(set(blind["mapping"][str(j)].values()) == set(MODELS) for j in offers)

    scores = defaultdict(list)
    defects = defaultdict(Counter)
    offers_with = defaultdict(Counter)
    top_win = Counter()
    top_tie = Counter()
    pairs = defaultdict(Counter)
    rows = []
    for n, rating in enumerate(ratings, 1):
        job_id = rating["job_id"]
        mapping = blind["mapping"][str(job_id)]
        model_ratings = {mapping[letter]: rating[letter] for letter in "ABC"}
        max_score = max(v["score"] for v in model_ratings.values())
        winners = {m for m, v in model_ratings.items() if v["score"] == max_score}
        assert winners == {mapping[x] for x in rating["winner"]}, job_id
        for model, assessment in model_ratings.items():
            scores[model].append(assessment["score"])
            for severity in ("minor", "major", "critical"):
                defects[model][severity] += len(assessment[severity])
                if assessment[severity]:
                    offers_with[model][severity] += 1
            if len(winners) == 1 and model in winners:
                top_win[model] += 1
            elif model in winners:
                top_tie[model] += 1
        for first, second in combinations(MODELS, 2):
            a = model_ratings[first]["score"]
            b = model_ratings[second]["score"]
            pairs[f"{first} vs {second}"]["first_wins" if a > b else "second_wins" if b > a else "tie"] += 1
        title = offers[job_id]["offer"]["title"].replace("|", "/").replace("\n", " ")
        summaries = {}
        for model, assessment in model_ratings.items():
            tags = []
            for severity, marker in (("critical", "C"), ("major", "M"), ("minor", "m")):
                if assessment[severity]:
                    tags.append(f"{marker}{len(assessment[severity])}")
            summaries[model] = f"{assessment['score']} ({', '.join(tags) or '—'})"
        rows.append({
            "number": n,
            "job_id": job_id,
            "title": title,
            "ratings": model_ratings,
            "summary": summaries,
            "winner": sorted(winners),
            "note": rating["note"],
        })

    models = {}
    for model in MODELS:
        subset = [r for r in results if r["model_requested"] == model]
        assert len(subset) == 40
        tokens = Counter()
        for row in subset:
            tokens.update(usage(row))
        models[model] = {
            "n": len(subset),
            "valid_structured": sum(r.get("error") is None and r.get("structured_response") is not None for r in subset),
            "provider_errors": sum(r.get("error") is not None for r in subset),
            "scores": {"mean": round(mean(scores[model]), 3), "median": median(scores[model]), "distribution": dict(sorted(Counter(scores[model]).items()))},
            "defects": dict(defects[model]),
            "offers_with_defect": dict(offers_with[model]),
            "top_wins_sole": top_win[model],
            "top_ties": top_tie[model],
            "tokens": dict(tokens),
            "estimated_batch_cost_usd": round(sum(r.get("estimated_batch_cost_usd") or 0 for r in subset), 8),
            "per_request_latency_available": sum(r.get("per_request_latency_ms") is not None for r in subset),
        }
    output = {
        "method": "One manual blind assessment per original offer and output, labels randomized per offer; scores ordinal 0–4.",
        "source_sha256": blind["source_sha256"],
        "ratings_sha256": sha256((ROOT / "ratings.jsonl").read_bytes()).hexdigest(),
        "models": models,
        "pairwise": {k: dict(v) for k, v in pairs.items()},
        "offers": rows,
    }
    (ROOT / "statistics.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    lines = ["# Comparaison détaillée des 40 offres", "", "Scores ordinaux : 4 très bon ; 3 bon avec réserves ; 2 défaut métier majeur ou couverture faible ; 1 plusieurs défauts majeurs ; 0 défaut critique. Les marqueurs indiquent le nombre de défauts : C critique, M majeur, m mineur. Les égalités sont conservées.", "", "| # | Offre | GPT-5.4 | Haiku 5.5 | GPT-5.4 mini | Meilleur(s) | Observation |", "|---:|---|---:|---:|---:|---|---|"]
    for row in rows:
        winners = ", ".join(LABEL[m] for m in MODELS if m in row["winner"])
        mapping = blind["mapping"][str(row["job_id"])]
        note = re.sub(
            r"\b([ABC])\b",
            lambda match, mapping=mapping: LABEL[mapping[match.group(1)]],
            row["note"],
        )
        note = note.replace("|", "/")
        lines.append(f"| {row['number']} | {row['job_id']} — {row['title']} | {row['summary']['gpt-5.4']} | {row['summary']['claude-haiku-5-5']} | {row['summary']['gpt-5.4-mini']} | {winners} | {note} |")
    lines += ["", "Les jugements par sortie, avec la preuve textuelle de chaque défaut, sont dans `ratings.jsonl`; `statistics.json` associe chaque jugement au modèle après levée de l'aveugle.", ""]
    (ROOT / "comparison-40.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
