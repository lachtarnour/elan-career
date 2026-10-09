"""Retire the obsolete pre-analysis Match score from existing SQLite databases."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

MATCH_COLUMNS = (
    "rule_based_score",
    "semantic_score",
    "skill_score",
    "title_score",
    "seniority_score",
    "location_score",
    "domain_score",
    "final_score",
)
MATCH_COMPONENTS = (
    "rule_based",
    "semantic",
    "skills",
    "title",
    "seniority",
    "location",
    "domain",
    "final",
    "cap",
)


def remove_match_score(engine: Engine) -> None:
    """Remove score values and columns; keep filter metadata in job_scores."""
    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    if not inspector.has_table("job_scores"):
        return
    existing = {column["name"] for column in inspector.get_columns("job_scores")}
    has_legacy_table = inspector.has_table("job_embeddings")
    if not existing.intersection(MATCH_COLUMNS) and not has_legacy_table:
        return
    json_paths = ", ".join(f"'$.{key}'" for key in MATCH_COMPONENTS)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE job_scores SET components = json_remove(components, " + json_paths + ") "
            "WHERE components IS NOT NULL AND json_valid(components)"
        ))
        connection.execute(text("DROP INDEX IF EXISTS ix_job_scores_final_score"))
        for column in MATCH_COLUMNS:
            if column in existing:
                connection.execute(text(f'ALTER TABLE job_scores DROP COLUMN "{column}"'))
        if has_legacy_table:
            connection.execute(text("DROP TABLE job_embeddings"))
