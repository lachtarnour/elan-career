"""Reproduce the CVSelection reference sample and run Élan analysis in an isolated DB.

Run with ELAN_HOME, ELAN_ENV_FILE, DATABASE_URL, and PROFILE_DIR pointing at
an isolated snapshot. This script never opens the live database for writing.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import local

from sqlalchemy import select as sql_select

from smartapply.config import get_llm_settings, get_settings
from smartapply.database import session_scope
from smartapply.database.models import Job, LLMUsage
from smartapply.llm.anthropic_provider import AnthropicProvider
from smartapply.llm.anthropic_transport import build_message_params
from smartapply.llm.prompts import cv_adaptation
from smartapply.llm.schemas import JobAnalysis as JobAnalysisSchema
from smartapply.model_config import model_config_file
from smartapply.offers import build_analyzer_input
from smartapply.pipeline import Pipeline
from smartapply.pipeline.apply.cv_writer import _document_company_label
from smartapply.profile import get_profile

ROOT = Path(__file__).resolve().parent
DATASET = ROOT / "dataset.jsonl"
MANIFEST = ROOT / "manifest.json"
REPORT = ROOT / "diversity-report.md"
SEED = 20261010
QUOTAS = {name: 5 for name in (
    "data_scientist", "ml_engineer", "ai_engineer", "nlp_llm",
    "computer_vision", "data_engineer", "data_analyst", "other",
)}
PRODUCTION_FILES = (
    "smartapply/pipeline/process/analysis.py",
    "smartapply/pipeline/process/scope_screening.py",
    "smartapply/llm/fit_context.py",
    "smartapply/llm/fit_request.py",
    "smartapply/llm/prompts/cv_adaptation.py",
    "smartapply/llm/schemas.py",
)
PATTERNS = (
    ("computer_vision", r"computer vision|vision par ordinateur|vision|image|imagerie|3d|robot"),
    ("nlp_llm", r"nlp|llm|language model|langage|generative ai|genai|générative|rag"),
    ("ml_engineer", r"machine learning engineer|ml engineer|mlops|deep learning engineer"),
    ("ai_engineer", r"ai engineer|ingénieur ia|ingenieur ia|artificial intelligence engineer|agentic|ai research"),
    ("data_scientist", r"data scientist|scientist data|science des données|statisticien"),
    ("data_engineer", r"data engineer|analytics engineer|data platform"),
    ("data_analyst", r"data analyst|analyste data|business intelligence"),
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()


def role_group(job: Job) -> str:
    text = f"{job.title} {job.analysis.role_type if job.analysis else ''}".lower()
    return next((name for name, pattern in PATTERNS if re.search(pattern, text)), "other")


def seniority_group(value: str | None) -> str:
    value = (value or "").lower()
    if any(word in value for word in ("junior", "entry", "intern", "associate")):
        return "junior"
    if any(word in value for word in ("senior", "lead", "expert", "manager")):
        return "senior"
    return "mid_or_unspecified"


def stable_key(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def offer_data(job: Job) -> dict:
    return {
        "id": job.id, "external_id": job.external_id, "title": job.title,
        "company": job.source_company, "location": job.location,
        "contract_type": job.contract_type, "remote_policy": job.remote_policy,
        "description": job.description, "cleaned_description": job.cleaned_description,
        "application_url": job.application_url, "apply_options": job.apply_options,
        "source": job.source, "source_data": job.source_data,
        "display_metadata": job.display_metadata,
        "published_date": job.published_date, "scraped_at": job.scraped_at,
    }


def write_rows(rows: list[dict]) -> None:
    temp = DATASET.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    temp.replace(DATASET)


def read_rows() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def write_manifest(data: dict) -> None:
    MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")


def update_manifest_results(rows: list[dict], manifest: dict, *, finished: bool = False) -> None:
    manifest["analysis_status_counts"] = dict(Counter(r["analysis_status"] for r in rows))
    manifest["exploitable_count"] = sum(r["analysis_status"] == "success" for r in rows)
    manifest["cost_usd_estimated"] = round(sum(
        float(u["cost_usd"] or 0) for r in rows for u in r["usage"]
    ), 8)
    manifest["usage_totals"] = {
        field: sum(int(u[field] or 0) for row in rows for u in row["usage"])
        for field in ("prompt_tokens", "completion_tokens", "cached_prompt_tokens", "cache_write_prompt_tokens")
    }
    manifest["native_response_count"] = sum(
        "native_response" in call for row in rows for call in row["calls"]
    )
    manifest["historical_attempt_count"] = sum(len(row.get("previous_attempts", [])) for row in rows)
    manifest["dataset_sha256"] = digest(DATASET.read_bytes())
    if finished:
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        manifest["run_state"] = "complete"
    write_manifest(manifest)


def select() -> None:
    if DATASET.exists() or MANIFEST.exists():
        raise SystemExit("Dataset already exists; refusing to resample")
    pipeline = Pipeline()
    candidates = [job for job in pipeline.analysis_candidates(
        include_analyzed=True, exclude_applications=True,
    ) if job.analysis is not None and len(job.description or "") > 300]
    pools: dict[str, list[Job]] = {group: [] for group in QUOTAS}
    for job in candidates:
        pools[role_group(job)].append(job)
    rng = random.Random(SEED)
    for group in pools:
        pools[group].sort(key=lambda job: job.id)
        rng.shuffle(pools[group])
        if len(pools[group]) < QUOTAS[group]:
            raise SystemExit(f"Insufficient offers in {group}: {len(pools[group])}")
    chosen: list[Job] = []
    seen: set[str] = set()
    lang_counts: Counter = Counter()
    seniority_counts: Counter = Counter()
    for _round_no in range(5):
        for group in QUOTAS:
            available = []
            for job in pools[group]:
                key = stable_key(f"{job.source_company} {job.title}")
                body_key = digest(stable_key(job.description).encode())
                if key not in seen and body_key not in seen:
                    available.append((job, key, body_key))
            if not available:
                raise SystemExit(f"No distinct offer left for {group}")
            # Random order breaks ties; deficits encourage English, junior and senior cases.
            def priority(item):
                job = item[0]
                language = (job.analysis.raw_response or {}).get("offer_language", "unknown")
                seniority = seniority_group(job.analysis.seniority)
                return (
                    int(language == "en" and lang_counts["en"] < 15) * 3
                    + int(language == "fr" and lang_counts["fr"] < 25) * 2
                    + int(seniority == "junior" and seniority_counts["junior"] < 8) * 2
                    + int(seniority == "senior" and seniority_counts["senior"] < 7) * 2
                )
            job, key, body_key = max(available, key=priority)
            chosen.append(job)
            seen.update((key, body_key))
            lang_counts[(job.analysis.raw_response or {}).get("offer_language", "unknown")] += 1
            seniority_counts[seniority_group(job.analysis.seniority)] += 1
    rows = []
    for job in chosen:
        original = offer_data(job)
        analyzer_input = asdict(build_analyzer_input(job))
        rows.append({
            "job_id": job.id,
            "sample_stratum": {
                "role": role_group(job),
                "language": (job.analysis.raw_response or {}).get("offer_language", "unknown"),
                "seniority": seniority_group(job.analysis.seniority),
            },
            "offer": original,
            "offer_sha256": digest(json_bytes(original)),
            "analyzer_input": analyzer_input,
            "analyzer_input_sha256": digest(json_bytes(analyzer_input)),
            "analysis_status": "pending",
            "analysis": None,
            "cvselection_input": None,
            "calls": [], "usage": [], "errors": [],
        })
    write_rows(rows)
    config_file = model_config_file()
    profile_dir = get_settings().profile_dir
    prompt_files = sorted((Path("smartapply/llm/prompts/templates")).rglob("*.j2"))
    manifest = {
        "version": "dataset-v1", "seed": SEED, "sample_size": len(rows),
        "selection": "Five sampled offers per exclusive title/previous-role stratum; fixed-seed random order with language and seniority deficit preference; previously analyzed, currently admissible, no application, unique normalized company/title and description.",
        "source_database": "/Users/nourlachtar/Library/Application Support/Elan/data/smartapply.db",
        "isolated_database": str(get_settings().database_url),
        "isolated_database_snapshot_pre_run_sha256": digest(Path("/private/tmp/cvselection-dataset-v1/smartapply.db").read_bytes()),
        "selected_ids": [row["job_id"] for row in rows],
        "offer_hashes": {str(row["job_id"]): row["offer_sha256"] for row in rows},
        "analyzer_input_hashes": {str(row["job_id"]): row["analyzer_input_sha256"] for row in rows},
        "profile_file_hashes": {p.name: digest(p.read_bytes()) for p in sorted(profile_dir.glob("*.json"))},
        "model_config": get_llm_settings().calls,
        "model_config_sha256": digest(config_file.read_bytes()),
        "prompt_file_hashes": {str(p): digest(p.read_bytes()) for p in prompt_files},
        "production_file_hashes": {name: digest(Path(name).read_bytes()) for name in PRODUCTION_FILES},
        "runner_sha256": digest(Path(__file__).read_bytes()),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "python": sys.version.split()[0],
        "analysis_workflow": "Pipeline.analyze_jobs(force_reanalyze=True, exclude_applications=True), one offer at a time",
        "cache_policy": "Clear isolated DB LLM cache and analysis/scope/Batch checkpoints before run; verify no returned cache hits.",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_manifest(manifest)
    update_manifest_results(rows, manifest)
    report(rows, manifest)
    print("selected", len(rows), "roles", dict(Counter(r["sample_stratum"]["role"] for r in rows)), "languages", dict(lang_counts), "seniority", dict(seniority_counts), flush=True)


def prepare_isolated_cache() -> int:
    from smartapply.config import get_settings
    db_url = get_settings().database_url
    path = db_url.removeprefix("sqlite:////")
    if db_url == path or not path.startswith("private/tmp/cvselection-dataset-v1/"):
        raise SystemExit(f"Unsafe database path: {db_url}")
    connection = sqlite3.connect("/" + path)
    baseline = connection.execute("select coalesce(max(id),0) from llm_usage").fetchone()[0]
    for table in ("llm_cache", "scope_checkpoints", "analysis_checkpoints",
                  "analysis_batch_waves", "analysis_batch_items", "analysis_batch_runs"):
        connection.execute(f"delete from {table}")
    connection.commit()
    connection.close()
    return baseline


def report(rows: list[dict], manifest: dict) -> None:
    statuses = Counter(row["analysis_status"] for row in rows)
    strata = {field: Counter(row["sample_stratum"][field] for row in rows)
              for field in ("role", "language", "seniority")}
    lines = ["# Diversité du dataset CVSelection v1", "",
             f"Graine : `{SEED}`. Offres distinctes : **{len(rows)}**.", "",
             "Les strates ci-dessous proviennent des analyses historiques, utilisées uniquement pour le tirage aléatoire.", ""]
    labels = {"role": "Métier", "language": "Langue", "seniority": "Séniorité"}
    for field, counts in strata.items():
        lines += [f"## {labels[field]} de la strate", "", *[f"- {name}: {count}" for name, count in sorted(counts.items())], ""]
    source_counts = Counter(row["offer"]["source"] for row in rows)
    lines += ["## Source des annonces", "", *[f"- {name}: {count}" for name, count in sorted(source_counts.items())], ""]
    fresh = [row["analysis"] for row in rows if row["analysis_status"] == "success"]
    if fresh:
        new_languages = Counter(analysis.get("offer_language", "unknown") for analysis in fresh)
        new_seniorities = Counter(analysis.get("seniority", "unknown") for analysis in fresh)
        lines += ["## Métadonnées des nouvelles analyses", "", "Langues :",
                  *[f"- {name}: {count}" for name, count in sorted(new_languages.items())], "",
                  "Séniorité :",
                  *[f"- {name or 'non renseigné'}: {count}" for name, count in sorted(new_seniorities.items())], ""]
    exploitable = statuses["success"]
    lines += ["## Nouvelle analyse", "", *[f"- {name}: {count}" for name, count in sorted(statuses.items())],
              f"- Cas exploitables pour CVSelection : **{exploitable}/{len(rows)}**.", "",
              f"- Réponses natives de l'analyse finale : {manifest.get('native_response_count', 0)}.",
              f"- Tokens d'entrée comptabilisés : {manifest.get('usage_totals', {}).get('prompt_tokens', 0)} ; tokens de sortie : {manifest.get('usage_totals', {}).get('completion_tokens', 0)}.",
              f"- Coût estimé enregistré : {manifest.get('cost_usd_estimated', 0):.4f} USD, reprises incluses.", "",
              "Les strates utilisent les métadonnées d'analyse historiques uniquement pour l'échantillonnage ; les résultats du benchmark proviennent exclusivement des nouveaux appels.", ""]
    REPORT.write_text("\n".join(lines))


def verify_inputs(rows: list[dict], manifest: dict) -> None:
    if len(rows) != 40 or len({row["job_id"] for row in rows}) != 40:
        raise SystemExit("Expected 40 unique offers")
    if [row["job_id"] for row in rows] != manifest["selected_ids"]:
        raise SystemExit("Selected IDs differ from manifest")
    for row in rows:
        job_id = str(row["job_id"])
        if digest(json_bytes(row["offer"])) != manifest["offer_hashes"][job_id]:
            raise SystemExit(f"Offer hash mismatch: {job_id}")
        if digest(json_bytes(row["analyzer_input"])) != manifest["analyzer_input_hashes"][job_id]:
            raise SystemExit(f"Analyzer input hash mismatch: {job_id}")
    if digest(model_config_file().read_bytes()) != manifest["model_config_sha256"]:
        raise SystemExit("Configured model routes changed since sample freeze")
    if get_llm_settings().calls != manifest["model_config"]:
        raise SystemExit("Configured model routes differ from manifest")
    for name, expected in manifest["profile_file_hashes"].items():
        if digest((get_settings().profile_dir / name).read_bytes()) != expected:
            raise SystemExit(f"Candidate profile changed: {name}")
    for name, expected in manifest["prompt_file_hashes"].items():
        if digest(Path(name).read_bytes()) != expected:
            raise SystemExit(f"Prompt changed: {name}")
    for name, expected in manifest["production_file_hashes"].items():
        if digest(Path(name).read_bytes()) != expected:
            raise SystemExit(f"Production file changed: {name}")


def verify_dataset(rows: list[dict], manifest: dict) -> None:
    verify_inputs(rows, manifest)
    if digest(DATASET.read_bytes()) != manifest["dataset_sha256"]:
        raise SystemExit("Dataset hash differs from manifest")
    if digest(Path(__file__).read_bytes()) != manifest["runner_sha256"]:
        raise SystemExit("Runner hash differs from manifest")
    if manifest["analysis_status_counts"] != dict(Counter(row["analysis_status"] for row in rows)):
        raise SystemExit("Analysis status counts differ from manifest")
    purposes = ["job_scope_screening", "job_offer_extraction", "job_fit_assessment"]
    for row in rows:
        if row["analysis_status"] != "success":
            continue
        if [call["purpose"] for call in row["calls"]] != purposes:
            raise SystemExit(f"Incomplete final workflow: {row['job_id']}")
        if any("native_request" not in call or "native_response" not in call for call in row["calls"]):
            raise SystemExit(f"Missing native evidence: {row['job_id']}")
        for call in row["calls"]:
            route = manifest["model_config"][call["purpose"]]
            body = call["request_body"]
            if body["model"] != route["model"] or body["max_completion_tokens"] != route["max_output_tokens"]:
                raise SystemExit(f"Model or token budget differs from route: {row['job_id']}")
            if body.get("output_config", {}).get("effort") != route["reasoning"]:
                raise SystemExit(f"Reasoning effort differs from route: {row['job_id']}")
        if any(usage["cached"] for usage in row["usage"]):
            raise SystemExit(f"Local cache reuse found: {row['job_id']}")
        JobAnalysisSchema.model_validate(row["analysis"])
        cv_input = row["cvselection_input"]
        if not cv_input or digest(cv_input["system_prompt"].encode()) != cv_input["system_sha256"]:
            raise SystemExit(f"Missing CVSelection system prompt: {row['job_id']}")
        if digest(cv_input["user_prompt"].encode()) != cv_input["user_sha256"]:
            raise SystemExit(f"Missing CVSelection user prompt: {row['job_id']}")
    if manifest["exploitable_count"] != sum(row["analysis_status"] == "success" for row in rows):
        raise SystemExit("Exploitable count differs from manifest")
    if manifest["native_response_count"] != sum(
        "native_response" in call for row in rows for call in row["calls"]
    ):
        raise SystemExit("Native response count differs from manifest")
    for field, expected in manifest["usage_totals"].items():
        if expected != sum(int(usage[field] or 0) for row in rows for usage in row["usage"]):
            raise SystemExit(f"Usage total differs from manifest: {field}")
    total_cost = round(sum(float(usage["cost_usd"] or 0) for row in rows for usage in row["usage"]), 8)
    if total_cost != manifest["cost_usd_estimated"]:
        raise SystemExit("Estimated cost differs from manifest")


def run() -> None:
    rows = read_rows()
    manifest = json.loads(MANIFEST.read_text())
    verify_inputs(rows, manifest)
    if len(sys.argv) > 3 and sys.argv[3] == "retry_incomplete_screen":
        incomplete = [
            row for row in rows
            if row["analysis_status"] == "success"
            and row["calls"]
            and not any(call["purpose"] == "job_scope_screening" for call in row["calls"])
        ]
        if len(incomplete) != 1:
            raise SystemExit("Expected exactly one incomplete screening trace")
        row = incomplete[0]
        row.setdefault("previous_attempts", []).append({
            "analysis_status": row["analysis_status"], "calls": row["calls"],
            "usage": row["usage"], "errors": row["errors"],
            "analysis": row["analysis"],
            "reason": "screening fail-open checkpoint from the earlier DNS failure",
        })
        row.update(analysis_status="pending", analysis=None, cvselection_input=None,
                   calls=[], usage=[], errors=[])
        db_url = get_settings().database_url
        if db_url != "sqlite:////private/tmp/cvselection-dataset-v1/smartapply.db":
            raise SystemExit(f"Unsafe database path: {db_url}")
        connection = sqlite3.connect("/private/tmp/cvselection-dataset-v1/smartapply.db")
        connection.execute("delete from llm_cache")
        connection.execute("delete from scope_checkpoints where job_id=?", (row["job_id"],))
        connection.execute("delete from analysis_checkpoints where job_id=?", (row["job_id"],))
        connection.commit()
        connection.close()
        write_rows(rows)
        update_manifest_results(rows, manifest)
    if len(sys.argv) > 3 and sys.argv[3] == "retry_network":
        for row in rows:
            if row["analysis_status"] == "failed" and row["calls"] and all(
                "Connection error" in str(call.get("error", "")) for call in row["calls"]
            ):
                row.setdefault("previous_attempts", []).append({
                    "analysis_status": row["analysis_status"],
                    "calls": row["calls"], "usage": row["usage"], "errors": row["errors"],
                    "reason": "sandbox DNS unavailable; retry with network access",
                })
                row["analysis_status"] = "pending"
                row["calls"] = []
                row["usage"] = []
                row["errors"] = []
        write_rows(rows)
    if "usage_baseline_id" in manifest:
        baseline = manifest["usage_baseline_id"]
    else:
        baseline = prepare_isolated_cache()
        manifest["usage_baseline_id"] = baseline
        write_manifest(manifest)
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else len(rows)
    pipeline = Pipeline()
    profile = get_profile()
    current = local()
    original_generate = AnthropicProvider._generate
    original_call = AnthropicProvider._call_anthropic

    def recorded_call(self, **kwargs):
        try:
            message = original_call(self, **kwargs)
            current.trace[-1]["native_response"] = message.model_dump(mode="json")
            return message
        except Exception as exc:
            current.trace[-1]["transport_error"] = repr(exc)
            raise

    def recorded_generate(self, body):
        trace = {
            "request_body": body,
            "native_request": build_message_params(body),
            "purpose": body["prompt_cache_key"].split(":")[1],
        }
        current.trace.append(trace)
        try:
            completion = original_generate(self, body)
            trace["parsed_transport"] = {
                "content": completion.content, "model": completion.model,
                "prompt_tokens": completion.prompt_tokens,
                "completion_tokens": completion.completion_tokens,
                "cached_prompt_tokens": completion.cached_prompt_tokens,
                "cache_write_prompt_tokens": completion.cache_write_prompt_tokens,
                "finish_reason": completion.finish_reason,
            }
            return completion
        except Exception as exc:
            trace["error"] = repr(exc)
            raise

    AnthropicProvider._call_anthropic = recorded_call
    AnthropicProvider._generate = recorded_generate
    pending = [row for row in rows if row["analysis_status"] == "pending"][:limit]

    def analyze_row(row: dict) -> dict:
        current.trace = []
        try:
            outcome = pipeline.analyze_jobs([row["job_id"]], force_reanalyze=True, exclude_applications=True)
            row["calls"] = current.trace
            row["errors"] = outcome.errors
            row["analysis_status"] = "success" if outcome.analyzed == 1 else ("screened_out" if outcome.screened_out else "failed")
            with session_scope() as session:
                row["usage"] = [
                    {column.name: getattr(usage, column.name) for column in LLMUsage.__table__.columns}
                    for usage in session.scalars(sql_select(LLMUsage).where(
                        LLMUsage.id > baseline, LLMUsage.job_id == row["job_id"]
                    ).order_by(LLMUsage.id))
                ]
                if row["analysis_status"] == "success":
                    job = session.get(Job, row["job_id"])
                    analysis = JobAnalysisSchema.model_validate(job.analysis.raw_response)
                    row["analysis"] = analysis.model_dump(mode="json")
                    row["fit_diagnostics"] = job.analysis.fit_diagnostics
                    company = _document_company_label(job.source_company, analysis.extracted_company_name, analysis.offer_language)
                    user = cv_adaptation.build_user_prompt(
                        profile=profile, analysis=analysis, job_title=job.title,
                        job_company=company,
                        job_description=job.description or job.cleaned_description or "",
                        selected_experiences=list(profile.experiences),
                        selected_projects=list(profile.projects),
                    )
                    system = cv_adaptation.build_system_prompt()
                    row["cvselection_input"] = {
                        "system_prompt": system, "user_prompt": user,
                        "system_sha256": digest(system.encode()),
                        "user_sha256": digest(user.encode()),
                        "job_company": company,
                    }
        except Exception as exc:
            row["analysis_status"] = "failed"
            row["errors"].append({"type": type(exc).__name__, "message": str(exc)})
            row["calls"] = current.trace
        return row

    workers = min(4, len(pending)) if pending else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyze_row, row): row["job_id"] for row in pending}
        for completed, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            write_rows(rows)
            update_manifest_results(rows, manifest)
            report(rows, manifest)
            print(f"{completed}/{len(pending)} job={row['job_id']} {row['analysis_status']} calls={len(row['calls'])}", flush=True)
    update_manifest_results(rows, manifest, finished=all(r["analysis_status"] != "pending" for r in rows))
    report(rows, manifest)


if __name__ == "__main__":
    if sys.argv[1] == "verify":
        verify_dataset(read_rows(), json.loads(MANIFEST.read_text()))
        print("40 offers, new analyses, native responses and hashes verified")
    else:
        {"select": select, "run": run}[sys.argv[1]]()
