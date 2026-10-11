"""Legacy vector retirement preserves dossiers and remains atomic on failure."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from smartapply.database.embedding_migration import remove_embeddings


@pytest.fixture
def legacy_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        for table in ("jobs", "job_analyses", "applications", "generated_documents"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, data TEXT)")
            connection.exec_driver_sql(f"INSERT INTO {table} VALUES (1, 'keep exactly')")
        connection.exec_driver_sql("CREATE TABLE job_embeddings (job_id INTEGER, vector JSON)")
        connection.exec_driver_sql("INSERT INTO job_embeddings VALUES (1, '[0.1, 0.2]')")
        connection.exec_driver_sql("CREATE TABLE embedding_cache (id INTEGER, vector JSON)")
        connection.exec_driver_sql("INSERT INTO embedding_cache VALUES (1, '[0.3, 0.4]')")
        for table in ("llm_cache", "llm_usage"):
            connection.exec_driver_sql(
                f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, purpose TEXT, model TEXT, data TEXT)"
            )
            connection.exec_driver_sql(
                f"INSERT INTO {table} VALUES "
                "(1, 'embeddings', 'custom-embedding-model', 'remove'), "
                "(2, 'generic', 'text-embedding-3-small', 'remove'), "
                "(3, 'cv_adaptation', 'gpt-5.4', 'keep exactly'), "
                "(4, 'job_fit_assessment', 'gpt-5.4-mini', 'keep exactly')"
            )
    yield engine
    engine.dispose()


def test_retirement_preserves_all_unrelated_data_and_is_idempotent(legacy_engine):
    assert remove_embeddings(legacy_engine) == {
        "job_embeddings": 1,
        "embedding_cache": 1,
        "llm_usage": 2,
        "llm_cache": 2,
    }
    assert remove_embeddings(legacy_engine) == {}
    assert not {"job_embeddings", "embedding_cache"} & set(inspect(legacy_engine).get_table_names())
    with legacy_engine.connect() as connection:
        for table in ("jobs", "job_analyses", "applications", "generated_documents"):
            assert connection.exec_driver_sql(f"SELECT * FROM {table}").all() == [
                (1, "keep exactly")
            ]
        for table in ("llm_cache", "llm_usage"):
            assert connection.exec_driver_sql(f"SELECT * FROM {table} ORDER BY id").all() == [
                (3, "cv_adaptation", "gpt-5.4", "keep exactly"),
                (4, "job_fit_assessment", "gpt-5.4-mini", "keep exactly"),
            ]


def test_retirement_rolls_back_dropped_tables_if_usage_cleanup_fails(legacy_engine):
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_cleanup BEFORE DELETE ON llm_usage BEGIN "
            "SELECT RAISE(ABORT, 'cleanup refused'); END"
        )
    with pytest.raises(IntegrityError, match="cleanup refused"):
        remove_embeddings(legacy_engine)
    with legacy_engine.connect() as connection:
        for table in ("job_embeddings", "embedding_cache"):
            assert connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 1
        for table in ("llm_usage", "llm_cache"):
            assert connection.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 4


def test_startup_retires_old_tables_without_recreating_them(isolated_db):
    from smartapply.database.session import get_engine, init_db

    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE embedding_cache (id INTEGER PRIMARY KEY, vector JSON)")
        )
        connection.execute(text("INSERT INTO embedding_cache VALUES (1, '[1.0]')"))
    init_db(run_maintenance=False)
    init_db(run_maintenance=False)
    assert "embedding_cache" not in inspect(engine).get_table_names()
