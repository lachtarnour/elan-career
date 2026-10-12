"""The ingest counter follows committed job mutations, including reused IDs."""

import pytest

from smartapply.database import get_engine, session_scope, write_session_scope
from smartapply.database.ingest_revision import ensure_ingest_revision, ingest_revision
from smartapply.database.models import Job

pytestmark = pytest.mark.usefixtures("isolated_db")


def _job(external_id):
    return Job(external_id=external_id, title="AI Engineer", source_company="Example",
               description="Build models.", source="manual")


def test_revision_setup_is_idempotent_and_rolled_back_changes_are_invisible():
    engine = get_engine()
    with session_scope() as session:
        original = ingest_revision(session)
    ensure_ingest_revision(engine)
    ensure_ingest_revision(engine)
    with session_scope() as session:
        assert ingest_revision(session) == original
    with pytest.raises(RuntimeError), write_session_scope() as session:
        session.add(_job("manual:rollback"))
        session.flush()
        assert ingest_revision(session) == original + 1
        raise RuntimeError("abort write")
    with session_scope() as session:
        assert ingest_revision(session) == original
        assert session.query(Job).count() == 0


def test_revision_detects_insert_update_and_delete_with_reused_id():
    with write_session_scope() as session:
        revision = ingest_revision(session)
        first = _job("manual:first")
        session.add(first)
        session.flush()
        reused_id = first.id
        assert ingest_revision(session) == revision + 1
        first.title = "Updated source identity"
        session.flush()
        assert ingest_revision(session) == revision + 2
        session.delete(first)
        session.flush()
        assert ingest_revision(session) == revision + 3
        replacement = _job("manual:replacement")
        session.add(replacement)
        session.flush()
        assert replacement.id == reused_id
        assert ingest_revision(session) == revision + 4
    with session_scope() as session:
        assert ingest_revision(session) == revision + 4
