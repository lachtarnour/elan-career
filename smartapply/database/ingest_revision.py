"""Detect changed offer snapshots without relying on monotonically growing IDs."""

from sqlalchemy import select

from smartapply.database.models import AppSetting

INGEST_REVISION_KEY = "ingest_job_revision"
INGEST_REVISION_TRIGGER_SQL = {
    f"elan_ingest_jobs_{action.lower()}": (
        f"CREATE TRIGGER elan_ingest_jobs_{action.lower()} AFTER {action} ON jobs BEGIN "
        "UPDATE app_settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT), "
        f"updated_at = CURRENT_TIMESTAMP WHERE key = '{INGEST_REVISION_KEY}'; END"
    )
    for action in ("INSERT", "UPDATE", "DELETE")
}


def ensure_ingest_revision(engine) -> None:
    """Advance one durable counter in the same SQLite transaction as each job write."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO app_settings (key, value, updated_at) "
            f"VALUES ('{INGEST_REVISION_KEY}', '0', CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO NOTHING"
        )
        for definition in INGEST_REVISION_TRIGGER_SQL.values():
            connection.exec_driver_sql(definition.replace(
                "CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1,
            ))


def ingest_revision(session) -> int | None:
    """Read the counter, or require conservative snapshot refresh on other backends."""
    value = session.scalar(select(AppSetting.value).where(AppSetting.key == INGEST_REVISION_KEY))
    return int(value) if value is not None else None
