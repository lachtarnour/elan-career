"""Validate the self-contained public metadata release without private inputs."""

from __future__ import annotations

import collections
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def verify() -> dict:
    checksums = json.loads((ROOT / "checksums.json").read_text())["sha256"]
    assert set(checksums) == {"corpora.json", "runs.json", "coverage.jsonl", "benchmark_overview.html"}
    for filename, expected in checksums.items():
        assert hashlib.sha256((ROOT / filename).read_bytes()).hexdigest() == expected
    corpora = json.loads((ROOT / "corpora.json").read_text())
    runs = json.loads((ROOT / "runs.json").read_text())["runs"]
    coverage = [json.loads(line) for line in (ROOT / "coverage.jsonl").read_text().splitlines()]
    assert [(item["corpus_id"], item["offers"]) for item in corpora["corpora"]] == [
        ("corpus_1", 200), ("corpus_2", 100)
    ]
    assert len(runs) == 7 and len({run["run_id"] for run in runs}) == 7
    assert len(coverage) == 640
    expected_corpus = {"job_offer_extraction": "corpus_2",
                       "job_scope_screening": "corpus_1", "job_fit_assessment": "corpus_1"}
    assert collections.Counter(run["task"] for run in runs) == {
        "job_offer_extraction": 2, "job_scope_screening": 2, "job_fit_assessment": 3
    }
    assert all(run["corpus_id"] == expected_corpus[run["task"]] for run in runs)
    assert all(row["corpus_id"] == expected_corpus[row["run_id"].split("/", 1)[0]]
               and row["offer_id"].startswith("EB1-" if row["corpus_id"] == "corpus_2" else "ELAN200-H-")
               for row in coverage)
    assert len({(row["run_id"], row["offer_id"]) for row in coverage}) == 640
    grouped = collections.defaultdict(list)
    for row in coverage:
        grouped[row["run_id"]].append(row)
    assert set(grouped) == {run["run_id"] for run in runs}
    for run in runs:
        rows = grouped[run["run_id"]]
        assert len(rows) == run["requests"]
        assert all(row["corpus_id"] == run["corpus_id"] for row in rows)
        identifiers = {row["offer_id"] for row in rows}
        if run["task"] == "job_offer_extraction":
            assert identifiers == {f"EB1-{number:03d}" for number in range(1, 101)}
        elif run["task"] == "job_scope_screening":
            assert identifiers == {f"ELAN200-H-{number:03d}" for number in range(1, 101)}
        else:
            assert len(identifiers) == 80
        assert dict(collections.Counter(row["provider_result"] for row in rows)) == run["statuses"]
        assert run["native_responses"] == (run["statuses"].get("completed", 0)
                                            + run["statuses"].get("succeeded", 0))
    assert sum(run["native_responses"] for run in runs) == 636
    assert collections.Counter(row["corpus_id"] for row in coverage) == {
        "corpus_1": 440, "corpus_2": 200
    }
    fit_sets = [{row["offer_id"] for row in grouped[run["run_id"]]}
                for run in runs if run["task"] == "job_fit_assessment"]
    assert fit_sets[0] == fit_sets[1] == fit_sets[2]
    return {"status": "ok", "corpora": 2, "historical_runs": 7,
            "offer_run_rows": 640, "native_responses": 636,
            "not_completed": 4}


if __name__ == "__main__":
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
