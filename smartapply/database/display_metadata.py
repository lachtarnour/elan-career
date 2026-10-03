"""Small derived offer fields used without loading descriptions in desktop lists."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import bindparam, select, update

from smartapply.database.models import Job
from smartapply.utils.experience import required_min_years

DISPLAY_METADATA_VERSION = 1


def build_display_metadata(description: str | None, cleaned_description: str | None) -> dict:
    return {
        "version": DISPLAY_METADATA_VERSION,
        "required_min_years": required_min_years(cleaned_description or description),
    }


def display_metadata_current(value) -> bool:
    return isinstance(value, dict) and value.get("version") == DISPLAY_METADATA_VERSION


def backfill_display_metadata(
    stop_requested: Callable[[], bool] | None = None,
    batch_size: int = 100,
) -> int:
    """Fill legacy metadata in short batches, rechecking the source text before writing."""
    from smartapply.database import session_scope, write_session_scope

    updated = 0
    last_id = 0
    statement = (
        update(Job.__table__)
        .where(
            Job.id == bindparam("previous_id"),
            Job.description == bindparam("previous_description"),
            Job.cleaned_description.is_not_distinct_from(bindparam("previous_cleaned_description")),
            Job.display_metadata.is_not_distinct_from(
                bindparam("previous_metadata", type_=Job.display_metadata.type)
            ),
        )
        .values(display_metadata=bindparam("computed_metadata", type_=Job.display_metadata.type))
    )
    while not (stop_requested and stop_requested()):
        with session_scope() as session:
            batch = session.execute(
                select(Job.id, Job.description, Job.cleaned_description, Job.display_metadata)
                .where(Job.id > last_id)
                .order_by(Job.id)
                .limit(max(1, batch_size))
            ).all()
        if not batch:
            break
        values = [
            (row, build_display_metadata(row.description, row.cleaned_description))
            for row in batch
            if not display_metadata_current(row.display_metadata)
        ]
        if stop_requested and stop_requested():
            break
        if values:
            parameters = [
                {
                    "previous_id": row.id,
                    "previous_description": row.description,
                    "previous_cleaned_description": row.cleaned_description,
                    "previous_metadata": row.display_metadata,
                    "computed_metadata": metadata,
                }
                for row, metadata in values
            ]
            with write_session_scope(notify=False) as session:
                result = session.execute(statement, parameters)
                updated += int(result.rowcount or 0)
        last_id = int(batch[-1].id)
    return updated
