"""Inspect saved schemas and round-trip the current fit fields, entirely offline.

No provider client, API call, submission, score persistence or LLM evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from smartapply.llm.anthropic_fit_transport import encode_fit_transport
from smartapply.llm.anthropic_transport import build_message_params
from smartapply.llm.fit_response import read_fit_response
from smartapply.llm.fit_scoring import calculate_fit_decisions
from smartapply.llm.schemas import JobFitDecisionEvaluation
from smartapply.llm.structured import to_strict_schema


def schema_metrics(schema):
    counts = Counter()

    def walk(node):
        if isinstance(node, dict):
            for keyword in ("$ref", "anyOf", "allOf", "oneOf", "const"):
                counts[keyword] += int(keyword in node)
            counts["type_arrays"] += isinstance(node.get("type"), list)
            if "properties" in node:
                counts["properties"] += len(node["properties"])
                counts["optional_properties"] += len(
                    set(node["properties"]) - set(node.get("required", []))
                )
                counts["objects"] += 1
            counts["enum_values"] += len(node.get("enum", []))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    def depth(node, seen=frozenset()):
        if "$ref" in node:
            ref = node["$ref"]
            if ref in seen:
                raise ValueError("Recursive schema")
            definition = schema
            for key in ref.split("/")[1:]:
                definition = definition[key]
            return depth(definition, seen | {ref})
        children = list(node.get("properties", {}).values())
        if "items" in node:
            children.append(node["items"])
        children += node.get("anyOf", []) + node.get("allOf", [])
        return int(node.get("type") == "object") + max(
            (depth(child, seen) for child in children), default=0
        )

    walk(schema)
    return {
        **counts,
        "$defs": len(schema.get("$defs", {})),
        "root_fields": len(schema.get("properties", {})),
        "expanded_object_depth": depth(schema),
        "utf8_bytes": len(json.dumps(schema, ensure_ascii=False).encode()),
        "sha256": hashlib.sha256(
            json.dumps(schema, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source, output = args.benchmark, args.output
    output.mkdir(parents=True, exist_ok=True)

    def load(name):
        return json.loads((source / name).read_text())

    old = load("baseline/anthropic_requests.json")["requests"][0]["params"]
    failed = load("attempts/anthropic_original_schema/anthropic_requests.json")["requests"][0][
        "params"
    ]
    normalized = load("attempts/anthropic_nullable_schema/anthropic_requests.json")["requests"][0][
        "params"
    ]
    canonical_requests = load("anthropic_canonical.json")
    prepared = []
    prompt_checks = []
    for request in canonical_requests:
        # Archived benchmarks may use a retired contract. Rebuild only the output
        # schema offline, keeping their source prompts and provider settings.
        body = json.loads(json.dumps(request["body"]))
        body["response_format"]["json_schema"]["schema"] = to_strict_schema(
            JobFitDecisionEvaluation
        )
        params = build_message_params(body)
        assert params["output_config"]["effort"] == "high"
        assert not {"tools", "tool_choice", "thinking"}.intersection(params)
        assert params["system"][0]["text"] == body["messages"][0]["content"]
        user = params["messages"][0]["content"]
        if isinstance(user, list):
            user = "".join(block["text"] for block in user)
        assert user == body["messages"][1]["content"]
        prepared.append({"custom_id": request["custom_id"], "params": params})
        prompt_checks.append(request["custom_id"])
    new = prepared[0]["params"]
    schemas = {
        "historical_strict": old["output_config"]["format"]["schema"],
        "current_rejected_strict": failed["output_config"]["format"]["schema"],
        "current_rejected_nullable_normalization": normalized["output_config"]["format"]["schema"],
        "proposed_native_strict": new["output_config"]["format"]["schema"],
    }
    for name, schema in schemas.items():
        (output / f"{name}_schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
        )
    (output / "prepared_native_batch.json").write_text(
        json.dumps({"requests": prepared}, ensure_ascii=False, indent=2) + "\n"
    )
    results = load("results.json")["results"]
    rows = []
    for provider, records in results.items():
        for offer_id, record in records.items():
            original = json.loads(record["response_text"])
            current_fields = {
                key: value
                for key, value in original.items()
                if key in JobFitDecisionEvaluation.model_fields
            }
            try:
                JobFitDecisionEvaluation.model_validate(current_fields)
            except ValidationError as exc:
                rows.append(
                    {
                        "provider": provider,
                        "offer_id": offer_id,
                        "canonical_valid": False,
                        "validation_errors": exc.error_count(),
                        "roundtrip_tested": False,
                    }
                )
                continue
            wire = encode_fit_transport(current_fields)
            Draft202012Validator(schemas["proposed_native_strict"]).validate(wire)
            parsed = read_fit_response(json.dumps(wire, ensure_ascii=False))
            assert parsed.raw == current_fields and not parsed.observations, offer_id
            JobFitDecisionEvaluation.model_validate(parsed.raw)
            assert calculate_fit_decisions(parsed.decisions) == calculate_fit_decisions(
                tuple(
                    original["decision"][axis]
                    for axis in ("missions", "essentials", "responsibilities")
                )
            )
            rows.append(
                {
                    "provider": provider,
                    "offer_id": offer_id,
                    "canonical_valid": True,
                    "roundtrip_tested": True,
                    "exact_roundtrip": True,
                    "archived_fields_excluded": sorted(set(original) - set(current_fields)),
                    "unchanged_score": True,
                }
            )
    failures = {}
    for attempt in ("anthropic_original_schema", "anthropic_nullable_schema"):
        errors = Counter()
        for line in (
            (source / f"attempts/{attempt}/anthropic_results.jsonl").read_text().splitlines()
        ):
            record = json.loads(line)
            result = record["result"]
            assert result["type"] == "errored"
            errors[result["error"]["error"]["message"]] += 1
        failures[attempt] = dict(errors)
    observations = {}
    for label, name in (
        ("historical", "baseline/anthropic_results.jsonl"),
        ("nonstrict_workaround", "anthropic_results.jsonl"),
    ):
        count = Counter()
        for line in (source / name).read_text().splitlines():
            message = json.loads(line)["result"]["message"]
            count["responses"] += 1
            count["thinking_tokens_reported"] += message["usage"]["output_tokens_details"][
                "thinking_tokens"
            ]
            count["output_tokens"] += message["usage"]["output_tokens"]
            count[f"stop_{message['stop_reason']}"] += 1
            for block in message["content"]:
                count[f"blocks_{block['type']}"] += 1
        observations[label] = dict(count)
    report = {
        "api_calls": 0,
        "api_cost_usd": 0,
        "provider_compilation_verified": False,
        "source_benchmark": str(source),
        "schemas": {name: schema_metrics(schema) for name, schema in schemas.items()},
        "saved_compilation_failures": failures,
        "reasoning_observations": observations,
        "prepared_requests": len(prepared),
        "prompts_preserved_exactly": len(prompt_checks),
        "roundtrip_summary": {
            provider: {
                "available": sum(row["provider"] == provider for row in rows),
                "canonical_valid_roundtrips": sum(
                    row["provider"] == provider and row["roundtrip_tested"] for row in rows
                ),
                "existing_invalid_not_repaired": sum(
                    row["provider"] == provider and not row["roundtrip_tested"] for row in rows
                ),
            }
            for provider in results
        },
        "roundtrip_records": rows,
        "limits": [
            "No offline test proves acceptance by the proprietary Anthropic grammar compiler.",
            "The API constrains types/keys/enums, but not reliability maxItems.",
            "Full canonical conformance remains measured separately; defects only warn when decisions are usable.",
            "Only current fields of saved outputs are converted offline; retired fields are excluded from this audit projection.",
            "Prepared requests retain archived prompts; they are offline schema artifacts, not submissions or current prompt examples.",
        ],
        "official_docs": "https://platform.claude.com/docs/en/build-with-claude/structured-outputs",
    }
    (output / "offline_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("schemas", "roundtrip_summary", "prepared_requests", "api_calls")
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
