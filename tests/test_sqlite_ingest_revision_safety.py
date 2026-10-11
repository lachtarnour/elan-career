"""SQLite maintenance permits bounded built-ins and refuses untrusted triggers."""

import sqlite3
from contextlib import closing

import pytest

from smartapply.database import write_session_scope
from smartapply.database.ingest_revision import INGEST_REVISION_KEY, INGEST_REVISION_TRIGGER_SQL
from smartapply.database.models import AppSetting, Job
from smartapply.maintenance._sqlite_safety import SQLiteSafetyError, require_no_table_triggers


def _seed():
    with write_session_scope() as session:
        job = Job(external_id="manual:guard", title="ML Engineer", source_company="Fixture",
                  description="Original source", source="manual")
        session.add(job)
        session.add(AppSetting(key="independent_setting", value="preserve"))
        session.flush()
        return job.id


def _settings(connection):
    return connection.execute("SELECT * FROM app_settings ORDER BY key").fetchall()


@pytest.mark.parametrize("alteration", [
    "increment", "literal_case", "literal_space", "extra_statement", "temp", "settings_trigger",
])
def test_sqlite_guard_refuses_altered_homonyms_temp_and_descendant_triggers(isolated_db, alteration):
    job_id = _seed()
    database = isolated_db / "test.db"
    with closing(sqlite3.connect(database)) as connection:
        original_settings = _settings(connection)
        definition = INGEST_REVISION_TRIGGER_SQL["elan_ingest_jobs_update"]
        if alteration == "temp":
            connection.execute(definition.replace("CREATE TRIGGER ", "CREATE TEMP TRIGGER ", 1))
        elif alteration == "settings_trigger":
            connection.execute(
                "CREATE TRIGGER hidden_side_effect AFTER UPDATE ON app_settings "
                "BEGIN UPDATE jobs SET description = 'damaged'; END"
            )
        else:
            connection.execute("DROP TRIGGER elan_ingest_jobs_update")
            if alteration == "increment":
                definition = definition.replace("INTEGER) + 1", "INTEGER) + 2")
            elif alteration == "literal_case":
                definition = definition.replace("'ingest_job_revision'", "'INGEST_JOB_REVISION'")
            elif alteration == "literal_space":
                definition = definition.replace("'ingest_job_revision'", "'ingest_job_revision '")
            else:
                definition = definition.replace("; END", "; UPDATE jobs SET description='damaged'; END")
            connection.execute(definition)
        with pytest.raises(SQLiteSafetyError, match="refuses triggers"):
            require_no_table_triggers(connection, tables=("jobs", "job_scores"))
        assert _settings(connection) == original_settings
        assert connection.execute("SELECT description FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == (
            "Original source"
        )


@pytest.mark.parametrize("counter_state", ["present", "absent", "no_update_trigger"])
def test_sqlite_guard_verifies_exact_counter_effects_and_preserves_other_settings(
    isolated_db, counter_state,
):
    job_id = _seed()
    with closing(sqlite3.connect(isolated_db / "test.db")) as connection:
        if counter_state == "absent":
            connection.execute("DELETE FROM app_settings WHERE key=?", (INGEST_REVISION_KEY,))
        elif counter_state == "no_update_trigger":
            connection.execute("DROP TRIGGER elan_ingest_jobs_update")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        guard = require_no_table_triggers(connection, tables=("jobs", "job_scores"))
        before = _settings(connection)
        changes_before = connection.total_changes
        connection.execute("UPDATE jobs SET title='Updated ML Engineer' WHERE id=?", (job_id,))
        assert connection.total_changes - changes_before == 1 + guard.additional_writes(1)
        assert guard.additional_writes(1) == int(counter_state == "present")
        guard.verify(connection, job_updates=1)
        after = _settings(connection)
        assert [row for row in before if row[0] != INGEST_REVISION_KEY] == [
            row for row in after if row[0] != INGEST_REVISION_KEY
        ]
        if counter_state == "present":
            initial_value = next(row[1] for row in before if row[0] == INGEST_REVISION_KEY)
            final_value = next(row[1] for row in after if row[0] == INGEST_REVISION_KEY)
            assert int(final_value) == int(initial_value) + 1
        else:
            assert after == before
        connection.rollback()
        assert _settings(connection) == before


@pytest.mark.parametrize("corruption", ["counter", "setting", "new_setting", "delete_counter"])
def test_sqlite_revision_guard_refuses_unexpected_writes_and_rollback_preserves_state(
    isolated_db, corruption,
):
    job_id = _seed()
    with closing(sqlite3.connect(isolated_db / "test.db")) as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = _settings(connection)
        guard = require_no_table_triggers(connection, tables=("jobs", "job_scores"))
        connection.execute("UPDATE jobs SET title='Changed title' WHERE id=?", (job_id,))
        if corruption == "counter":
            connection.execute("UPDATE app_settings SET value=CAST(value AS INTEGER)+1 WHERE key=?",
                               (INGEST_REVISION_KEY,))
        elif corruption == "setting":
            connection.execute("UPDATE app_settings SET value='damaged' WHERE key='independent_setting'")
        elif corruption == "new_setting":
            connection.execute("INSERT INTO app_settings VALUES ('unexpected', 'new', CURRENT_TIMESTAMP)")
        else:
            connection.execute("DELETE FROM app_settings WHERE key=?", (INGEST_REVISION_KEY,))
        with pytest.raises(SQLiteSafetyError, match="revision change|setting changed"):
            guard.verify(connection, job_updates=1)
        connection.rollback()
        assert _settings(connection) == before
        assert connection.execute("SELECT title FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "ML Engineer"


def test_no_job_write_preserves_the_revision_timestamp(isolated_db):
    _seed()
    with closing(sqlite3.connect(isolated_db / "test.db")) as connection:
        guard = require_no_table_triggers(connection, tables=("jobs", "job_scores"))
        connection.execute("UPDATE app_settings SET updated_at='2000-01-01' WHERE key=?",
                           (INGEST_REVISION_KEY,))
        with pytest.raises(SQLiteSafetyError, match="timestamp change"):
            guard.verify(connection, job_updates=0)


@pytest.mark.parametrize("value", ["01", "-1", "invalid"])
def test_noncanonical_revision_values_are_refused(isolated_db, value):
    with closing(sqlite3.connect(isolated_db / "test.db")) as connection:
        connection.execute("UPDATE app_settings SET value=? WHERE key=?", (value, INGEST_REVISION_KEY))
        with pytest.raises(SQLiteSafetyError, match="Invalid ingestion revision"):
            require_no_table_triggers(connection, tables=("jobs", "job_scores"))
