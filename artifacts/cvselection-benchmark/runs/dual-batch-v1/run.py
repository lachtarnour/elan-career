"""Run the frozen 40-offer CVSelection comparison through provider Batch APIs.

Usage: PYTHONPATH=. .venv/bin/python .../run.py prepare|submit-openai|submit-anthropic|poll|summarize
No command retries a Batch submission with an unknown outcome.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from smartapply.config import get_llm_settings, get_settings
from smartapply.llm.anthropic_transport import build_message_params
from smartapply.llm.batch import build_batch_request, encode_requests
from smartapply.llm.prompts import cv_adaptation
from smartapply.llm.schemas import CVSelection, JobAnalysis
from smartapply.llm.structured import to_strict_schema
from smartapply.llm.usage import estimate_cost_usd
from smartapply.profile import get_profile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SOURCE = ROOT / "artifacts/cvselection-benchmark/dataset-v1"
DATASET = SOURCE / "dataset.jsonl"
SOURCE_MANIFEST = SOURCE / "manifest.json"
MANIFEST = HERE / "manifest.json"
STATE = HERE / "state.json"
REQUESTS = HERE / "requests"
RAW = HERE / "raw"
RESULTS = HERE / "results.jsonl"
MODELS = {"openai": ("gpt-5.4", "none"), "anthropic": ("claude-haiku-5-5", "high")}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    temporary.replace(path)


def write_once(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise RuntimeError(f"Existing artifact differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(canonical(value) + b"\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def rows() -> list[dict]:
    return [json.loads(line) for line in DATASET.read_text().splitlines() if line.strip()]


def check_source() -> tuple[list[dict], dict]:
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip()
    if branch != "codex/evidence-fit-v4":
        raise RuntimeError(f"Wrong branch: {branch}")
    source = read_json(SOURCE_MANIFEST)
    if sha(DATASET.read_bytes()) != source["dataset_sha256"]:
        raise RuntimeError("Frozen dataset checksum differs")
    data = rows()
    if len(data) != 40 or [r["job_id"] for r in data] != source["selected_ids"]:
        raise RuntimeError("Expected the same 40 distinct offers in frozen order")
    if len({r["job_id"] for r in data}) != 40 or source["exploitable_count"] != 40:
        raise RuntimeError("Dataset does not have 40 exploitable offers")
    current_config = get_llm_settings().calls
    previous_config = json.loads(json.dumps(source["model_config"]))
    previous_config["job_fit_assessment"]["max_output_tokens"] = 32000
    if current_config != previous_config:
        raise RuntimeError("Model routes changed beyond the authorized fit output cap")
    profile_dir = get_settings().profile_dir
    for name, expected in source["profile_file_hashes"].items():
        if sha((profile_dir / name).read_bytes()) != expected:
            raise RuntimeError(f"Candidate profile changed: {name}")
    for name, expected in source["prompt_file_hashes"].items():
        if sha((ROOT / name).read_bytes()) != expected:
            raise RuntimeError(f"Production prompt changed: {name}")
    for name in ("smartapply/llm/prompts/cv_adaptation.py", "smartapply/llm/schemas.py"):
        if sha((ROOT / name).read_bytes()) != source["production_file_hashes"][name]:
            raise RuntimeError(f"CVSelection production builder/schema changed: {name}")
    profile = get_profile()
    system = cv_adaptation.build_system_prompt()
    for row in data:
        job_id = str(row["job_id"])
        if row["analysis_status"] != "success" or row["errors"]:
            raise RuntimeError(f"Analysis not exploitable: {job_id}")
        if sha(canonical(row["offer"])) != source["offer_hashes"][job_id]:
            raise RuntimeError(f"Offer hash differs: {job_id}")
        if [c["purpose"] for c in row["calls"]] != [
            "job_scope_screening", "job_offer_extraction", "job_fit_assessment"
        ] or any("native_response" not in c for c in row["calls"]):
            raise RuntimeError(f"Incomplete fresh analysis: {job_id}")
        analysis = JobAnalysis.model_validate(row["analysis"])
        entry = row["cvselection_input"]
        offer = row["offer"]
        user = cv_adaptation.build_user_prompt(
            profile=profile, analysis=analysis, job_title=offer["title"],
            job_company=entry["job_company"],
            job_description=offer["description"] or offer["cleaned_description"] or "",
            selected_experiences=list(profile.experiences), selected_projects=list(profile.projects),
        )
        if entry["system_prompt"] != system or entry["user_prompt"] != user:
            raise RuntimeError(f"Frozen CVSelection prompt differs from production: {job_id}")
        if sha(system.encode()) != entry["system_sha256"] or sha(user.encode()) != entry["user_sha256"]:
            raise RuntimeError(f"Frozen CVSelection prompt hash differs: {job_id}")
    return data, source


def prepare() -> None:
    data, source = check_source()
    if MANIFEST.exists() or STATE.exists():
        raise RuntimeError("Run already prepared; use its saved state instead of replacing it")
    run_id = uuid.uuid4().hex
    batches = {}
    first_schema = None
    for provider, (model, effort) in MODELS.items():
        requests = [build_batch_request(
            custom_id=f"CVS_{row['job_id']}",
            system=row["cvselection_input"]["system_prompt"],
            user=row["cvselection_input"]["user_prompt"],
            schema=CVSelection, model=model, max_completion_tokens=32000,
            purpose="cv_adaptation", temperature=0.3, provider=provider, effort=effort,
        ) for row in data]
        schema = requests[0].body["response_format"]["json_schema"]["schema"]
        if first_schema is None:
            first_schema = schema
        elif schema != first_schema:
            raise RuntimeError("Canonical CVSelection schemas differ between providers")
        for request, row in zip(requests, data, strict=True):
            messages = request.body["messages"]
            if messages[0]["content"] != row["cvselection_input"]["system_prompt"] or (
                messages[1]["content"] != row["cvselection_input"]["user_prompt"]
            ):
                raise RuntimeError("Provider prompt differs from frozen input")
        canonical_file = REQUESTS / f"{provider}.jsonl"
        encoded = encode_requests(requests)
        write_once(canonical_file, encoded)
        record = {
            "model": model, "reasoning": effort, "max_output_tokens": 32000,
            "request_count": len(requests), "canonical_requests_sha256": sha(encoded),
            "canonical_requests_file": str(canonical_file.relative_to(HERE)),
        }
        if provider == "anthropic":
            wire = [
                {"custom_id": f"{run_id}_{request.custom_id}",
                 "params": build_message_params(request.body)}
                for request in requests
            ]
            wire_file = REQUESTS / "anthropic_wire.json"
            wire_bytes = canonical({"requests": wire})
            write_once(wire_file, wire_bytes)
            record.update(wire_file=str(wire_file.relative_to(HERE)), wire_sha256=sha(wire_bytes))
            if any(w["params"]["system"][0]["text"] != d["cvselection_input"]["system_prompt"]
                   or w["params"]["messages"][0]["content"] != d["cvselection_input"]["user_prompt"]
                   or w["params"]["max_tokens"] != 32000 or
                   w["params"]["output_config"].get("effort") != "high" or
                   "temperature" in w["params"]
                   for w, d in zip(wire, data, strict=True)):
                raise RuntimeError("Anthropic wire contract differs")
        batches[provider] = record
    manifest = {
        "run_id": run_id, "created_at": now(), "source_dataset_sha256": source["dataset_sha256"],
        "source_manifest_sha256": sha(SOURCE_MANIFEST.read_bytes()),
        "source_git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "dataset_offer_ids": [r["job_id"] for r in data],
        "system_prompt_sha256": data[0]["cvselection_input"]["system_sha256"],
        "user_prompt_sha256_by_offer": {str(r["job_id"]): r["cvselection_input"]["user_sha256"] for r in data},
        "canonical_schema_sha256": sha(canonical(to_strict_schema(CVSelection))),
        "schema_name": "CVSelection", "canonical_temperature": 0.3,
        "anthropic_temperature": "omitted by production wire adapter for claude-haiku-5-5",
        "profile_file_hashes": source["profile_file_hashes"],
        "prompt_file_hashes": source["prompt_file_hashes"], "batches": batches,
    }
    write_json(MANIFEST, manifest)
    write_json(STATE, {"run_id": run_id, "created_at": now(), "providers": {
        provider: {"status": "prepared", "batch_id": None, "input_file_id": None}
        for provider in MODELS
    }})
    print(f"Prepared 40 paired CVSelection requests for {run_id}")


def checked_state() -> tuple[dict, dict]:
    manifest = read_json(MANIFEST)
    state = read_json(STATE)
    if manifest["run_id"] != state["run_id"] or sha(DATASET.read_bytes()) != manifest["source_dataset_sha256"]:
        raise RuntimeError("Saved run identity or source dataset differs")
    for provider in MODELS:
        info = manifest["batches"][provider]
        if sha((HERE / info["canonical_requests_file"]).read_bytes()) != info["canonical_requests_sha256"]:
            raise RuntimeError(f"Frozen {provider} request file differs")
        if provider == "anthropic" and sha((HERE / info["wire_file"]).read_bytes()) != info["wire_sha256"]:
            raise RuntimeError("Frozen Anthropic wire file differs")
    return manifest, state


def submit(provider: str) -> None:
    manifest, state = checked_state()
    entry = state["providers"][provider]
    if entry["status"] not in {"prepared", "uploaded"}:
        raise RuntimeError(f"Submission already attempted or complete: {provider} {entry['status']}")
    settings = get_llm_settings()
    if provider == "openai":
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key, timeout=60.0, max_retries=0)
        if entry["status"] == "prepared":
            entry.update(status="uploading", upload_attempted_at=now())
            write_json(STATE, state)
            try:
                uploaded = client.with_options(max_retries=0).files.create(
                    file=("cvselection-benchmark-openai.jsonl", (REQUESTS / "openai.jsonl").read_bytes(), "application/jsonl"),
                    purpose="batch",
                )
            except Exception as exc:
                entry.update(status="upload_unknown", error=repr(exc))
                write_json(STATE, state)
                raise
            raw = uploaded.model_dump(mode="json")
            write_json(RAW / "openai_upload.json", raw)
            entry.update(status="uploaded", input_file_id=raw["id"])
            write_json(STATE, state)
        metadata = {"elan_benchmark": "cvselection-v1", "run_id": manifest["run_id"]}
        entry.update(status="submitting", submission_attempted_at=now(), metadata=metadata)
        write_json(STATE, state)
        try:
            batch = client.with_options(max_retries=0).batches.create(
                input_file_id=entry["input_file_id"], endpoint="/v1/chat/completions",
                completion_window="24h", metadata=metadata,
            )
        except Exception as exc:
            entry.update(status="submission_unknown", error=repr(exc))
            write_json(STATE, state)
            raise
    else:
        from anthropic import Anthropic

        client = Anthropic(api_key=settings.anthropic_api_key, timeout=60.0, max_retries=0)
        wire = read_json(REQUESTS / "anthropic_wire.json")["requests"]
        entry.update(status="submitting", submission_attempted_at=now())
        write_json(STATE, state)
        try:
            batch = client.with_options(max_retries=0).messages.batches.create(requests=wire)
        except Exception as exc:
            entry.update(status="submission_unknown", error=repr(exc))
            write_json(STATE, state)
            raise
    raw = batch.model_dump(mode="json")
    write_json(RAW / f"{provider}_submission.json", raw)
    entry.update(status=raw.get("status", raw.get("processing_status")), batch_id=raw["id"], submitted_at=now())
    write_json(STATE, state)
    print(f"{provider}: {entry['batch_id']} ({entry['status']})")


def poll() -> None:
    manifest, state = checked_state()
    settings = get_llm_settings()
    for provider in MODELS:
        entry = state["providers"][provider]
        if not entry.get("batch_id"):
            print(f"{provider}: {entry['status']} (no confirmed batch ID)")
            continue
        if provider == "openai":
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key, timeout=60.0, max_retries=0)
            raw = client.batches.retrieve(entry["batch_id"]).model_dump(mode="json")
            status = raw["status"]
        else:
            from anthropic import Anthropic

            client = Anthropic(api_key=settings.anthropic_api_key, timeout=60.0, max_retries=0)
            raw = client.messages.batches.retrieve(entry["batch_id"]).model_dump(mode="json")
            status = raw["processing_status"]
        write_json(RAW / f"{provider}_snapshot.json", raw)
        append_jsonl(RAW / f"{provider}_poll_history.jsonl", {"observed_at": now(), "snapshot": raw})
        entry.update(status=status, last_polled_at=now())
        write_json(STATE, state)
        print(f"{provider}: {entry['batch_id']} {status} {raw.get('request_counts')}")
        if provider == "openai" and status in {"completed", "failed", "expired", "cancelled"}:
            for key, target in (("output_file_id", "openai_output.jsonl"), ("error_file_id", "openai_errors.jsonl")):
                if raw.get(key):
                    write_once(RAW / target, client.files.content(raw[key]).content)
        if provider == "anthropic" and status == "ended":
            data = [item.model_dump(mode="json") for item in client.messages.batches.results(entry["batch_id"])]
            content = b"".join(canonical(item) + b"\n" for item in data)
            write_once(RAW / "anthropic_results.jsonl", content)
    collect(manifest, state)


def collect(manifest: dict, state: dict) -> None:
    expected = [f"CVS_{offer_id}" for offer_id in manifest["dataset_offer_ids"]]
    all_results = []
    for provider, (model, _) in MODELS.items():
        path = RAW / ("openai_output.jsonl" if provider == "openai" else "anthropic_results.jsonl")
        error_path = RAW / "openai_errors.jsonl" if provider == "openai" else None
        if not path.exists() and (error_path is None or not error_path.exists()):
            continue
        raw_records = [json.loads(s) for s in path.read_text().splitlines() if s.strip()] if path.exists() else []
        if error_path is not None and error_path.exists():
            raw_records += [json.loads(s) for s in error_path.read_text().splitlines() if s.strip()]
        by_id = {}
        for record in raw_records:
            wire_id = record["custom_id"]
            if provider == "anthropic" and not wire_id.startswith(manifest["run_id"] + "_"):
                raise RuntimeError(f"Unexpected Anthropic run ID in result: {wire_id}")
            custom_id = wire_id if provider == "openai" else wire_id.removeprefix(manifest["run_id"] + "_")
            if custom_id in by_id or custom_id not in expected:
                raise RuntimeError(f"Unexpected or duplicate {provider} result ID: {wire_id}")
            by_id[custom_id] = record
        for custom_id in expected:
            record = by_id.get(custom_id)
            result = {"provider": provider, "model_requested": model, "custom_id": custom_id,
                      "job_id": int(custom_id.removeprefix("CVS_")), "batch_id": state["providers"][provider]["batch_id"],
                      "raw_file": str((path if path.exists() else error_path).relative_to(HERE)),
                      "raw_response_present": record is not None,
                      "structured_response": None, "error": None, "usage": None,
                      "estimated_batch_cost_usd": None, "per_request_latency_ms": None}
            if record is None:
                result["error"] = "pending_or_missing_result"
            elif provider == "openai":
                envelope = record.get("response") or {}
                body = envelope.get("body") or {}
                usage = body.get("usage")
                result["usage"] = usage
                result["model_returned"] = body.get("model")
                result["response_id"] = body.get("id")
                result["request_id"] = envelope.get("request_id")
                result["finish_reason"] = ((body.get("choices") or [{}])[0]).get("finish_reason")
                if record.get("error") or envelope.get("status_code") != 200:
                    result["error"] = record.get("error") or body.get("error") or f"HTTP {envelope.get('status_code')}"
                else:
                    choice = (body.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    if message.get("refusal") or choice.get("finish_reason") != "stop":
                        result["error"] = message.get("refusal") or f"finish_reason={choice.get('finish_reason')}"
                    else:
                        content = message.get("content")
                        try:
                            result["structured_response"] = CVSelection.model_validate_json(content).model_dump(mode="json")
                        except Exception as exc:
                            result["error"] = f"CVSelection validation: {exc}"
                if usage:
                    detail = usage.get("prompt_tokens_details") or {}
                    result["estimated_batch_cost_usd"] = estimate_cost_usd(
                        model, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                        cached_prompt_tokens=detail.get("cached_tokens", 0) or 0,
                    ) * 0.5
            else:
                outcome = record.get("result") or {}
                message = outcome.get("message") or {}
                usage = message.get("usage")
                result["usage"] = usage
                result["model_returned"] = message.get("model")
                result["response_id"] = message.get("id")
                result["finish_reason"] = message.get("stop_reason")
                if outcome.get("type") != "succeeded":
                    result["error"] = outcome.get("error") or outcome.get("type")
                elif message.get("stop_reason") != "end_turn":
                    result["error"] = f"stop_reason={message.get('stop_reason')}"
                else:
                    content = "".join(block.get("text", "") for block in message.get("content", []) if block.get("type") == "text")
                    try:
                        result["structured_response"] = CVSelection.model_validate_json(content).model_dump(mode="json")
                    except Exception as exc:
                        result["error"] = f"CVSelection validation: {exc}"
                if usage:
                    prompt_tokens = sum(usage.get(key, 0) or 0 for key in
                                        ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                    result["estimated_batch_cost_usd"] = estimate_cost_usd(
                        model, prompt_tokens, usage.get("output_tokens", 0),
                        cached_prompt_tokens=usage.get("cache_read_input_tokens", 0) or 0,
                        cache_write_prompt_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
                    ) * 0.5
            all_results.append(result)
    if all_results:
        content = b"".join(canonical(result) + b"\n" for result in all_results)
        RESULTS.write_bytes(content)
        counts = {provider: {"received": sum(r["provider"] == provider and r["raw_response_present"] for r in all_results),
                             "valid": sum(r["provider"] == provider and r["structured_response"] is not None for r in all_results),
                             "errors": sum(r["provider"] == provider and r["error"] not in (None, "pending_or_missing_result") for r in all_results),
                             "cost_usd_estimated": round(sum(r["estimated_batch_cost_usd"] or 0 for r in all_results if r["provider"] == provider), 8)}
                  for provider in MODELS}
        usage_totals = {}
        for provider in MODELS:
            provider_results = [r for r in all_results if r["provider"] == provider]
            if not provider_results:
                continue
            if provider == "openai":
                usage_totals[provider] = {
                    "prompt_tokens": sum((r["usage"] or {}).get("prompt_tokens", 0) for r in provider_results),
                    "cached_prompt_tokens": sum(((r["usage"] or {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0) for r in provider_results),
                    "output_tokens": sum((r["usage"] or {}).get("completion_tokens", 0) for r in provider_results),
                    "reasoning_tokens": sum(((r["usage"] or {}).get("completion_tokens_details") or {}).get("reasoning_tokens", 0) for r in provider_results),
                }
            else:
                usage_totals[provider] = {
                    "input_tokens": sum((r["usage"] or {}).get("input_tokens", 0) for r in provider_results),
                    "cache_read_input_tokens": sum((r["usage"] or {}).get("cache_read_input_tokens", 0) for r in provider_results),
                    "cache_creation_input_tokens": sum((r["usage"] or {}).get("cache_creation_input_tokens", 0) for r in provider_results),
                    "output_tokens": sum((r["usage"] or {}).get("output_tokens", 0) for r in provider_results),
                    "thinking_tokens": sum(((r["usage"] or {}).get("output_tokens_details") or {}).get("thinking_tokens", 0) for r in provider_results),
                }
        batch_latencies = {}
        for provider in MODELS:
            path = RAW / f"{provider}_snapshot.json"
            if not path.exists():
                batch_latencies[provider] = None
                continue
            snapshot = read_json(path)
            if provider == "openai":
                start = snapshot.get("created_at")
                end = snapshot.get("completed_at") or snapshot.get("failed_at") or snapshot.get("expired_at")
                batch_latencies[provider] = end - start if start and end else None
            else:
                start = snapshot.get("created_at")
                end = snapshot.get("ended_at")
                batch_latencies[provider] = (
                    (datetime.fromisoformat(end.replace("Z", "+00:00")) -
                     datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
                    if start and end else None
                )
        write_json(HERE / "summary.json", {"updated_at": now(), "counts": counts,
                                           "usage_totals": usage_totals,
                                           "returned_models": {provider: sorted({r.get("model_returned") for r in all_results if r["provider"] == provider and r.get("model_returned")}) for provider in MODELS},
                                           "raw_result_sha256": {
                                               provider: sha((RAW / filename).read_bytes())
                                               for provider, filename in (("openai", "openai_output.jsonl"),
                                                                          ("anthropic", "anthropic_results.jsonl"))
                                               if (RAW / filename).exists()
                                           },
                                           "result_file_sha256": sha(content),
                                           "batch_latency_seconds": batch_latencies,
                                           "batch_statuses": {p: state["providers"][p]["status"] for p in MODELS}})


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "prepare":
        prepare()
    elif command == "submit-openai":
        submit("openai")
    elif command == "submit-anthropic":
        submit("anthropic")
    elif command == "poll":
        poll()
    elif command == "summarize":
        collect(*checked_state())
    else:
        raise SystemExit("Expected prepare, submit-openai, submit-anthropic, poll, or summarize")
