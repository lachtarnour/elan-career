"""Freeze original-offer review packets and randomized anonymous model outputs."""

from __future__ import annotations

import hashlib
import json
import random
import secrets
from pathlib import Path

from smartapply.profile import get_profile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DATASET = ROOT / "artifacts/cvselection-benchmark/dataset-v1/dataset.jsonl"
DUAL = ROOT / "artifacts/cvselection-benchmark/runs/dual-batch-v1/results.jsonl"
MINI = ROOT / "artifacts/cvselection-benchmark/runs/mini-high-batch-v1/results.jsonl"


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def main() -> None:
    if (HERE / "blind_map.json").exists():
        raise RuntimeError("Blind map already exists; review packets are immutable")
    offers = records(DATASET)
    dual = records(DUAL)
    mini = records(MINI)
    if len(offers) != 40 or len(dual) != 80 or len(mini) != 40:
        raise RuntimeError("Expected 40 offers and 120 results")
    by_key = {(row["job_id"], row["model_requested"]): row for row in dual + mini}
    if len(by_key) != 120:
        raise RuntimeError("Missing or duplicate model results")
    p = get_profile()
    catalog = ["# Candidate profile used for the blind review", "", f"Candidate title: {p.identity.title}", "", "## Projects", ""]
    for project in p.projects:
        catalog += [f"### {project.id} — {project.name} ({project.selection_policy.tier})", "",
                    f"Status: {project.status}. Activation: {', '.join(project.selection_policy.activation_conditions) or 'none'}.", ""]
        catalog += [f"- {bullet.text}" for bullet in project.bullets]
        catalog.append("")
    catalog += ["## Certificates", ""]
    catalog += [f"- {cert.id}: {cert.name} — {cert.description}" for cert in p.certificates]
    catalog += ["", "## Allowed skills", ""]
    catalog += [f"- {category.id}: {', '.join(category.skills)}" for category in p.skills.categories]
    (HERE / "profile_catalog.md").write_text("\n".join(catalog) + "\n")
    seed = secrets.token_hex(16)
    rng = random.Random(int(seed, 16))
    mapping = {}
    for index, row in enumerate(offers, 1):
        job_id = row["job_id"]
        offer = row["offer"]
        title = f"{index:02d}-{job_id}"
        source = offer["description"] or offer["cleaned_description"] or ""
        offer_packet = [f"# Offer {index:02d} — {job_id}", "",
                        f"Title: {offer['title']}", f"Company: {offer['company']}",
                        f"Location: {offer['location']}", f"Contract: {offer['contract_type']}",
                        f"Source: {offer['source']}", "", "## Original posting", "", source, ""]
        offer_dir = HERE / "offers"
        offer_dir.mkdir(parents=True, exist_ok=True)
        (offer_dir / f"{title}.md").write_text("\n".join(offer_packet))
        models = ["gpt-5.4", "claude-haiku-5-5", "gpt-5.4-mini"]
        rng.shuffle(models)
        mapping[str(job_id)] = dict(zip("ABC", models, strict=True))
        out = [f"# Anonymous CVSelection outputs — offer {index:02d} / {job_id}", ""]
        for label, model in mapping[str(job_id)].items():
            result = by_key[(job_id, model)]
            content = result["structured_response"]
            if content is None:
                out += [f"## {label}", "", f"ERROR: {result['error']}", ""]
                continue
            out += [f"## {label}", "", f"Title: {content['cv_title']}",
                    f"Projects: {', '.join(content['selected_project_ids'])}",
                    f"Certificates: {', '.join(content['selected_certificate_ids']) or 'none'}",
                    f"Skill profile: {content['skills_profile_id']}", "", "### Project reasons", ""]
            for reason in content["project_selection_reasons"]:
                out += [f"- {reason['project_id']}: {reason['reason']}",
                        f"  Evidence: {reason['offer_evidence']}",
                        f"  Complement: {reason['complementary_value'] or '(empty)'}"]
            out += ["", "### Selected skills", ""]
            out += [f"- {block['category_id']}: {', '.join(block['skills'])}" for block in content["selected_skills"]]
            out += ["", f"Category order: {', '.join(content['skills_order'])}",
                    f"Warnings: {' | '.join(content['warnings']) or 'none'}", ""]
        blind_dir = HERE / "blind"
        blind_dir.mkdir(parents=True, exist_ok=True)
        (blind_dir / f"{title}.md").write_text("\n".join(out))
    identity = {"seed": seed, "mapping": mapping,
                "source_sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in (DATASET, DUAL, MINI)}}
    (HERE / "blind_map.json").write_text(json.dumps(identity, ensure_ascii=False, indent=2) + "\n")
    print("Created 40 source-only offers, 40 anonymous output packets, and a sealed model map")


if __name__ == "__main__":
    main()
