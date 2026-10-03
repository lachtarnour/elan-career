"""Contradictory operations are reserved by duplicate group, not globally."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from smartapply.concurrency import (
    TaskConflictError,
    busy_job_ids,
    job_operation,
    subscribe_resource_changes,
)
from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Application, Job, JobDuplicateStatus
from smartapply.database.session import subscribe_database_changes


def test_group_reservations_allow_other_offers_and_notes(isolated_db):
    with session_scope() as session:
        first = Job(
            external_id="active",
            title="AI Engineer",
            company="Acme",
            description="Build models",
            source="manual",
        )
        other = Job(
            external_id="other",
            title="ML Engineer",
            company="Elsewhere",
            description="Build models",
            source="manual",
        )
        session.add_all([first, other])
        session.flush()
        alias = Job(
            external_id="alias",
            title=first.title,
            company=first.company,
            description=first.description,
            source="manual",
            canonical_job_id=first.id,
            duplicate_review_status=JobDuplicateStatus.CONFIRMED,
        )
        application = Application(job_id=first.id)
        session.add_all([alias, application])
        session.flush()
        first_id, alias_id, other_id, application_id = (
            first.id,
            alias.id,
            other.id,
            application.id,
        )
    entered, release = Event(), Event()
    observations = []
    unsubscribe = subscribe_resource_changes(lambda: observations.append(busy_job_ids()))

    def process():
        with (
            job_operation([first_id], operation="génération"),
            job_operation([first_id], operation="publication"),
        ):
            entered.set()
            assert release.wait(5)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(process)
            try:
                assert entered.wait(5)
                assert busy_job_ids() == sorted([first_id, alias_id])
                with (
                    pytest.raises(TaskConflictError),
                    job_operation([alias_id], operation="archivage"),
                ):
                    pytest.fail("The active alias must remain reserved")
                with job_operation([other_id], operation="archivage"):
                    pass
                with write_session_scope() as session:
                    session.get(Application, application_id).notes = "Independent note"
            finally:
                release.set()
            future.result()
    finally:
        unsubscribe()
    assert busy_job_ids() == []
    assert observations[0] == sorted([first_id, alias_id])
    assert observations[-1] == []
    with session_scope() as session:
        assert session.get(Application, application_id).notes == "Independent note"


def test_database_notifications_follow_commits_not_reads_notes_or_rollbacks(isolated_db):
    from sqlalchemy import update

    events = []
    topics = []
    unsubscribe = subscribe_database_changes(lambda: events.append("committed"))
    unsubscribe_topics = subscribe_database_changes(topics.append, with_topics=True)
    try:
        with write_session_scope() as session:
            assert session.query(Job).count() == 0
        assert events == []
        with write_session_scope() as session:
            job = Job(
                external_id="observe",
                title="Original",
                company="Acme",
                description="Build models",
                source="manual",
            )
            session.add(job)
            session.flush()
            job_id = job.id
        assert events == ["committed"]
        with write_session_scope(notify=False) as session:
            session.get(Job, job_id).title = "Silent"
        with pytest.raises(RuntimeError), write_session_scope() as session:
            session.get(Job, job_id).title = "Must roll back"
            raise RuntimeError("Failed operation")
        assert events == ["committed"]
        with write_session_scope() as session:
            session.execute(update(Job).where(Job.id == job_id).values(title="Bulk updated"))
        assert events == ["committed", "committed"]
        assert topics == [frozenset({"jobs"}), frozenset({"jobs"})]
    finally:
        unsubscribe()
        unsubscribe_topics()
