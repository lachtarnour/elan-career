"""Run one frozen CVSelection Batch on gpt-5.4-mini with high reasoning.

Commands: prepare, submit, poll, summarize. Submission is never retried after
an unknown response; all provider payloads and replies are saved verbatim.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from smartapply.config import get_llm_settings
from smartapply.llm.batch import build_batch_request, encode_requests
from smartapply.llm.schemas import CVSelection
from smartapply.llm.usage import estimate_cost_usd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
REFERENCE = HERE.parent / "dual-batch-v1"
DATASET = ROOT / "artifacts/cvselection-benchmark/dataset-v1/dataset.jsonl"
REQUEST = HERE / "requests/openai.jsonl"
MANIFEST = HERE / "manifest.json"
STATE = HERE / "state.json"
RAW = HERE / "raw"
MODEL = "gpt-5.4-mini"
EFFORT = "high"
MAX_OUTPUT_TOKENS = 32_000


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    temporary.replace(path)


def write_once(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"Existing artifact differs: {path}")
        return
    path.write_bytes(content)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def prepare() -> None:
    if MANIFEST.exists() or STATE.exists():
        raise RuntimeError("Run already prepared; use its saved state")
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip()
    if branch != "codex/evidence-fit-v4":
        raise RuntimeError(f"Wrong branch: {branch}")
    prior = read_json(REFERENCE / "manifest.json")
    prior_state = read_json(REFERENCE / "state.json")
    if any(prior_state["providers"][name]["status"] not in {"completed", "ended"}
           for name in ("openai", "anthropic")):
        raise RuntimeError("Reference run is not complete")
    if sha(DATASET.read_bytes()) != prior["source_dataset_sha256"]:
        raise RuntimeError("Reference dataset changed")
    rows = read_jsonl(DATASET)
    old_requests = read_jsonl(REFERENCE / "requests/openai.jsonl")
    if len(rows) != 40 or len(old_requests) != 40:
        raise RuntimeError("Expected exactly 40 paired offers")
    if len({row["job_id"] for row in rows}) != 40 or [row["job_id"] for row in rows] != prior["dataset_offer_ids"]:
        raise RuntimeError("Frozen offer IDs differ")
    requests = []
    for row, old in zip(rows, old_requests, strict=True):
        job_id = row["job_id"]
        if row["analysis_status"] != "success" or row["errors"] or not row["cvselection_input"]:
            raise RuntimeError(f"Frozen analysis unusable: {job_id}")
        entry = row["cvselection_input"]
        request = build_batch_request(
            custom_id=f"CVS_{job_id}", system=entry["system_prompt"],
            user=entry["user_prompt"], schema=CVSelection, model=MODEL,
            max_completion_tokens=MAX_OUTPUT_TOKENS, purpose="cv_adaptation",
            temperature=0.3, provider="openai", effort=EFFORT,
        )
        if request.custom_id != old["custom_id"] or request.body["messages"] != old["body"]["messages"]:
            raise RuntimeError(f"Prompt differs from reference: {job_id}")
        if request.body["response_format"] != old["body"]["response_format"]:
            raise RuntimeError(f"Schema differs from reference: {job_id}")
        if request.body.get("reasoning_effort") != EFFORT or "temperature" in request.body:
            raise RuntimeError(f"High-reasoning transport invalid: {job_id}")
        if request.body["max_completion_tokens"] != old["body"]["max_completion_tokens"]:
            raise RuntimeError(f"Output cap differs from reference: {job_id}")
        requests.append(request)
    content = encode_requests(requests)
    write_once(REQUEST, content)
    run_id = uuid.uuid4().hex
    write_json(MANIFEST, {
        "run_id": run_id, "created_at": now(), "source_branch": branch,
        "source_git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "reference_run_id": prior["run_id"],
        "reference_manifest_sha256": sha((REFERENCE / "manifest.json").read_bytes()),
        "reference_openai_requests_sha256": sha((REFERENCE / "requests/openai.jsonl").read_bytes()),
        "dataset_sha256": sha(DATASET.read_bytes()),
        "offer_ids": prior["dataset_offer_ids"],
        "system_prompt_sha256": prior["system_prompt_sha256"],
        "user_prompt_sha256_by_offer": prior["user_prompt_sha256_by_offer"],
        "canonical_schema_sha256": prior["canonical_schema_sha256"],
        "request_file": "requests/openai.jsonl", "request_sha256": sha(content),
        "provider": "openai", "model": MODEL, "reasoning_effort": EFFORT,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "canonical_production_temperature": 0.3,
        "wire_temperature": "omitted because reasoning is high",
        "schema": "CVSelection", "request_count": 40,
    })
    write_json(STATE, {"run_id": run_id, "status": "prepared", "batch_id": None,
                       "input_file_id": None, "created_at": now()})
    print(f"Prepared 40 CVSelection requests for {MODEL}/{EFFORT}: {run_id}")


def checked_state() -> tuple[dict, dict]:
    manifest, state = read_json(MANIFEST), read_json(STATE)
    if manifest["run_id"] != state["run_id"]:
        raise RuntimeError("Run identity differs")
    checks = (
        (DATASET, "dataset_sha256"),
        (REQUEST, "request_sha256"),
        (REFERENCE / "manifest.json", "reference_manifest_sha256"),
        (REFERENCE / "requests/openai.jsonl", "reference_openai_requests_sha256"),
    )
    for path, key in checks:
        if sha(path.read_bytes()) != manifest[key]:
            raise RuntimeError(f"Saved input differs: {path}")
    return manifest, state


def submit() -> None:
    manifest, state = checked_state()
    if state["status"] not in {"prepared", "uploaded"}:
        raise RuntimeError(f"Submission already attempted: {state['status']}")
    from openai import OpenAI

    client = OpenAI(api_key=get_llm_settings().openai_api_key, timeout=60.0, max_retries=0)
    if state["status"] == "prepared":
        state.update(status="uploading", upload_attempted_at=now())
        write_json(STATE, state)
        try:
            uploaded = client.files.create(file=("cvselection-mini-high.jsonl", REQUEST.read_bytes(), "application/jsonl"),
                                           purpose="batch")
        except Exception as exc:
            state.update(status="upload_unknown", error=repr(exc))
            write_json(STATE, state)
            raise
        raw = uploaded.model_dump(mode="json")
        write_json(RAW / "upload.json", raw)
        state.update(status="uploaded", input_file_id=raw["id"])
        write_json(STATE, state)
    metadata = {"elan_benchmark": "cvselection-mini-high-v1", "run_id": manifest["run_id"]}
    state.update(status="submitting", submission_attempted_at=now(), metadata=metadata)
    write_json(STATE, state)
    try:
        batch = client.batches.create(input_file_id=state["input_file_id"], endpoint="/v1/chat/completions",
                                      completion_window="24h", metadata=metadata)
    except Exception as exc:
        state.update(status="submission_unknown", error=repr(exc))
        write_json(STATE, state)
        raise
    raw = batch.model_dump(mode="json")
    write_json(RAW / "submission.json", raw)
    state.update(status=raw["status"], batch_id=raw["id"], submitted_at=now())
    write_json(STATE, state)
    print(f"Submitted {raw['id']}: {raw['status']}")


def poll() -> None:
    _, state = checked_state()
    if not state.get("batch_id"):
        raise RuntimeError(f"No confirmed Batch ID: {state['status']}")
    from openai import OpenAI

    client = OpenAI(api_key=get_llm_settings().openai_api_key, timeout=60.0, max_retries=0)
    raw = client.batches.retrieve(state["batch_id"]).model_dump(mode="json")
    write_json(RAW / "snapshot.json", raw)
    with (RAW / "poll_history.jsonl").open("ab") as stream:
        stream.write(canonical({"observed_at": now(), "snapshot": raw}) + b"\n")
    state.update(status=raw["status"], last_polled_at=now())
    write_json(STATE, state)
    print(f"{raw['id']}: {raw['status']} {raw.get('request_counts')}")
    if raw["status"] in {"completed", "failed", "expired", "cancelled"}:
        for key, filename in (("output_file_id", "output.jsonl"), ("error_file_id", "errors.jsonl")):
            if raw.get(key):
                write_once(RAW / filename, client.files.content(raw[key]).content)
        summarize()


def summarize() -> None:
    manifest, state = checked_state()
    output, errors = RAW / "output.jsonl", RAW / "errors.jsonl"
    if not output.exists() and not errors.exists():
        return
    output_records = read_jsonl(output) if output.exists() else []
    error_records = read_jsonl(errors) if errors.exists() else []
    records = output_records + error_records
    error_ids = {record["custom_id"] for record in error_records}
    by_id = {}
    expected = {f"CVS_{offer_id}" for offer_id in manifest["offer_ids"]}
    for record in records:
        custom_id = record["custom_id"]
        if custom_id not in expected or custom_id in by_id:
            raise RuntimeError(f"Unexpected or duplicate result: {custom_id}")
        by_id[custom_id] = record
    results = []
    for job_id in manifest["offer_ids"]:
        custom_id = f"CVS_{job_id}"
        record = by_id.get(custom_id)
        result = {"job_id": job_id, "custom_id": custom_id, "batch_id": state["batch_id"],
                  "model_requested": MODEL, "model_returned": None, "response_id": None,
                  "request_id": None, "finish_reason": None, "structured_response": None,
                  "error": None, "usage": None, "estimated_batch_cost_usd": None,
                  "per_request_latency_ms": None,
                  "raw_file": "raw/errors.jsonl" if custom_id in error_ids else "raw/output.jsonl"}
        if record is None:
            result["error"] = "missing_provider_result"
        else:
            envelope = record.get("response") or {}
            body = envelope.get("body") or {}
            usage = body.get("usage")
            result.update(model_returned=body.get("model"), response_id=body.get("id"),
                          request_id=envelope.get("request_id"), usage=usage)
            choice = (body.get("choices") or [{}])[0]
            result["finish_reason"] = choice.get("finish_reason")
            message = choice.get("message") or {}
            if record.get("error") or envelope.get("status_code") != 200:
                result["error"] = record.get("error") or body.get("error") or f"HTTP {envelope.get('status_code')}"
            elif message.get("refusal") or choice.get("finish_reason") != "stop":
                result["error"] = message.get("refusal") or f"finish_reason={choice.get('finish_reason')}"
            else:
                try:
                    result["structured_response"] = CVSelection.model_validate_json(message.get("content")).model_dump(mode="json")
                except Exception as exc:
                    result["error"] = f"CVSelection validation: {exc}"
            if usage:
                result["estimated_batch_cost_usd"] = estimate_cost_usd(
                    MODEL, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0),
                    cached_prompt_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                ) * 0.5
        results.append(result)
    content = b"".join(canonical(result) + b"\n" for result in results)
    (HERE / "results.jsonl").write_bytes(content)
    snapshot = read_json(RAW / "snapshot.json") if (RAW / "snapshot.json").exists() else {}
    usages = [r["usage"] or {} for r in results]
    write_json(HERE / "summary.json", {
        "updated_at": now(), "status": state["status"], "batch_id": state["batch_id"],
        "received": len(records), "valid": sum(r["structured_response"] is not None for r in results),
        "errors": sum(r["error"] is not None for r in results),
        "returned_models": sorted({r["model_returned"] for r in results if r["model_returned"]}),
        "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usages),
        "cached_prompt_tokens": sum((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0) for u in usages),
        "output_tokens": sum(u.get("completion_tokens", 0) for u in usages),
        "reasoning_tokens": sum((u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0) for u in usages),
        "cost_usd_estimated": round(sum(r["estimated_batch_cost_usd"] or 0 for r in results), 8),
        "batch_latency_seconds": (snapshot["completed_at"] - snapshot["created_at"]
                                  if snapshot.get("created_at") and snapshot.get("completed_at") else None),
        "raw_sha256": {path.name: sha(path.read_bytes()) for path in (output, errors) if path.exists()},
        "results_sha256": sha(content),
    })


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "prepare":
        prepare()
    elif command == "submit":
        submit()
    elif command == "poll":
        poll()
    elif command == "summarize":
        summarize()
    else:
        raise SystemExit("Expected prepare, submit, poll, or summarize")
