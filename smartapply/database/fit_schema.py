"""Add only nullable fit trace columns; never evaluate or repair job history."""

from sqlalchemy import inspect, text

from smartapply.database.analysis_batches import AnalysisBatchRun
from smartapply.database.models import JobAnalysis
from smartapply.database.session import _build_add_column_sql, get_engine


def ensure_fit_schema() -> list[str]:
    engine = get_engine()
    inspector = inspect(engine)
    executed = []
    with engine.begin() as connection:
        for model, field in ((JobAnalysis, "fit_diagnostics"), (AnalysisBatchRun, "fit_context")):
            table = model.__table__
            if not inspector.has_table(table.name):
                raise ValueError("Database is not initialized; use elan init-db for a new database")
            if field in {column["name"] for column in inspector.get_columns(table.name)}:
                continue
            statement = _build_add_column_sql(table.name, table.columns[field], engine.dialect)
            connection.execute(text(statement))
            executed.append(statement)
    return executed
