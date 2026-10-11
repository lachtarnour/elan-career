"""Retire unused embedding storage while preserving offers and LLM responses."""

from __future__ import annotations

from sqlalchemy.engine import Engine

EMBEDDING_TABLES = ("job_embeddings", "embedding_cache")
EMBEDDING_RECORDS = "purpose = 'embeddings' OR model LIKE 'text-embedding-%'"


def remove_embeddings(engine: Engine) -> dict[str, int]:
    """Atomically remove legacy vectors and their usage/cache rows, once or repeatedly."""
    if engine.dialect.name != "sqlite":
        return {}
    removed: dict[str, int] = {}
    with engine.connect() as connection:
        # Explicit BEGIN includes SQLite DDL in the same rollback boundary.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            tables = set(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).scalars()
            )
            for table in EMBEDDING_TABLES:
                if table in tables:
                    removed[table] = connection.exec_driver_sql(
                        f'SELECT count(*) FROM "{table}"'
                    ).scalar_one()
                    connection.exec_driver_sql(f'DROP TABLE "{table}"')
            for table in ("llm_usage", "llm_cache"):
                if table in tables:
                    result = connection.exec_driver_sql(
                        f'DELETE FROM "{table}" WHERE {EMBEDDING_RECORDS}'
                    )
                    if result.rowcount:
                        removed[table] = result.rowcount
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return removed
