#!/usr/bin/env python3
"""Dry-run or apply the guarded hard-reject-v2 historical backfill."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from smartapply.config import get_settings
from smartapply.maintenance._sqlite_safety import require_safe_output_path
from smartapply.maintenance.hard_reject_backfill import (
    apply_backfill,
    build_rejector,
    plan_backfill,
)
from smartapply.profile import load_profile


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Replay hard-reject-v2 on safe historical local-filter rejects. "
            "The default is a read-only dry run."
        )
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the planned metadata changes after creating an online backup.",
    )
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    profile_dir = args.profile_dir or get_settings().profile_dir
    profile_inputs = tuple(
        path for path in profile_dir.rglob("*") if path.is_file()
    ) if profile_dir.is_dir() else ()
    output = require_safe_output_path(
        args.output,
        protected_paths=profile_inputs,
        sqlite_databases=(args.database,),
    )

    profile = load_profile(args.profile_dir) if args.profile_dir else None
    rejector = build_rejector(profile)
    plan = plan_backfill(args.database, rejector)
    payload = (
        apply_backfill(plan, rejector, backup_dir=args.backup_dir).to_dict()
        if args.apply
        else plan.to_dict()
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
