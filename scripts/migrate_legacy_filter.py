#!/usr/bin/env python3
"""Migrate reviewed historical filter labels without restarting the pipeline.

Run from the repository root with ``python -m scripts.migrate_legacy_filter``.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from contextlib import suppress
from pathlib import Path

from scripts.replay_hard_reject_positive_control import load_manifest
from smartapply.config import get_settings
from smartapply.maintenance._sqlite_safety import file_sha256, require_safe_output_path
from smartapply.maintenance.hard_reject_backfill import build_rejector
from smartapply.maintenance.legacy_filter_migration import (
    LegacyFilterMigrationPreconditionError,
    apply_legacy_filter_migration,
    plan_legacy_filter_migration,
)
from smartapply.profile import load_profile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--review-manifest", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path)
    parser.add_argument(
        "--include-relevant", action="store_true",
        help="Also migrate legacy relevant labels using their reviewed blocker IDs.",
    )
    parser.add_argument("--apply", action="store_true", help="Apply after an online backup.")
    parser.add_argument(
        "--expected-plan", type=Path,
        help="Previously reviewed dry-run JSON; required with --apply.",
    )
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.apply and args.expected_plan is None:
        parser.error("--apply requires --expected-plan from a reviewed dry run")
    profile_dir = args.profile_dir or get_settings().profile_dir
    profile_inputs = tuple(path for path in profile_dir.rglob("*") if path.is_file())
    output = require_safe_output_path(
        args.output,
        protected_paths=(
            *profile_inputs, args.review_manifest,
            *((args.expected_plan,) if args.expected_plan is not None else ()),
        ),
        sqlite_databases=(args.database,),
    )
    review_sha256 = file_sha256(args.review_manifest)
    reviewed = load_manifest(args.review_manifest)
    allowed_ids = reviewed.allowed_uncertain_rejected_ids
    dispositions = ("uncertain",)
    if args.include_relevant:
        allowed_ids |= reviewed.allowed_rejected_ids
        dispositions += ("relevant",)
    rejector = build_rejector(load_profile(args.profile_dir) if args.profile_dir else None)
    plan = plan_legacy_filter_migration(
        args.database, rejector, allowed_rejected_ids=allowed_ids,
        dispositions=dispositions, review_manifest=args.review_manifest,
        expected_review_manifest_sha256=review_sha256,
    )
    if args.apply:
        expected = json.loads(args.expected_plan.read_text(encoding="utf-8"))
        if not isinstance(expected, dict) or expected.get("plan_sha256") != plan.plan_sha256:
            raise LegacyFilterMigrationPreconditionError(
                "Current plan differs from the reviewed dry run; review a fresh plan"
            )
    reserved = _reserve_report(output)
    try:
        payload = (
            apply_legacy_filter_migration(plan, rejector, backup_dir=args.backup_dir).to_dict()
            if args.apply else plan.to_dict()
        )
        if args.apply:
            payload["plan_sha256"] = plan.plan_sha256
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        if output is not None and reserved is not None:
            try:
                _write_report(reserved, output, rendered)
            except OSError as exc:
                if not args.apply:
                    raise
                payload["report_output_error"] = str(exc)
                rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        print(rendered, end="")
    finally:
        if reserved is not None:
            with suppress(OSError):
                reserved.unlink(missing_ok=True)


def _reserve_report(output: Path | None) -> Path | None:
    """Verify the destination and reserve an atomic report before mutation."""
    if output is None:
        return None
    if output.is_dir():
        raise IsADirectoryError(f"Report output is a directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=f".{output.name}.", suffix=".tmp",
        dir=output.parent, delete=False,
    ) as stream:
        stream.write("{}\n")
        stream.flush()
        return Path(stream.name)


def _write_report(reserved: Path, output: Path, rendered: str) -> None:
    reserved.write_text(rendered, encoding="utf-8")
    reserved.replace(output)


if __name__ == "__main__":
    main()
