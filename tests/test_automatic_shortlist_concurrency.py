"""Automatic selection cannot invalidate another workflow's offer snapshot."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from smartapply.concurrency import TaskConflictError, job_operation
from smartapply.database import Application, Job, ShortlistOrigin, session_scope
from smartapply.database.fit_ranking import current_policy_fingerprint
from smartapply.database.generation_runs import GenerationItem, GenerationRun
from smartapply.database.models import JobStatus
from smartapply.database.repository import (
    mark_analyzed,
    mark_filtered,
    set_analysis,
    set_score,
    set_shortlisted,
    upsert_job,
)
from smartapply.desktop.services import DesktopService
from smartapply.filtering.hard_reject import HardRejectResult
from smartapply.pipeline.snapshots import PublicationSnapshot, job_snapshot_key


def _offer(external_id, score, *, shortlisted=False, reservation=False):
    with session_scope() as session:
        job = upsert_job(
            session, external_id=external_id, title="AI Engineer", company=external_id,
            description="Build Python models", source="manual",
        )
        session.flush()
        mark_filtered(session, job.id)
        set_score(session, job.id, components={
            "filter_disposition": "pending_relevance",
            "hard_reject": HardRejectResult(decisions=()).audit_payload(),
        })
        set_analysis(session, job.id, fit_score=score, fit_diagnostics={
            "policy_fingerprint": current_policy_fingerprint(),
        })
        mark_analyzed(session, job.id)
        session.flush()
        session.expire(job, ["analysis", "score"])
        if shortlisted:
            assert set_shortlisted(
                session, job.id, selected=True, origin=ShortlistOrigin.AUTOMATIC,
            ) is not None
        if reservation:
            session.add(Application(job_id=job.id, status=JobStatus.ANALYZED))
        return job.id


@contextmanager
def _busy(job_id):
    entered, release = Event(), Event()

    def run():
        with job_operation([job_id], operation="création de candidature"):
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(run)
        try:
            assert entered.wait(5)
            yield
        finally:
            release.set()
            worker.result(timeout=5)


@pytest.mark.parametrize("reserve_alias", [False, True])
def test_automatic_top_k_refills_after_busy_candidate_without_changing_its_snapshot(
    isolated_db, reserve_alias,
):
    busy_id = _offer("busy", 0.99)
    available_id = _offer("available", 0.80)
    with session_scope() as session:
        job = session.get(Job, busy_id)
        snapshot = job_snapshot_key(job)
        alias = Job(
            external_id="busy-alias", title=job.title, source_company=job.source_company,
            description=job.description, source="manual", canonical_job_id=job.id,
            status=JobStatus.ARCHIVED,
        )
        session.add(alias)
        session.flush()
        alias_id = alias.id

    with _busy(alias_id if reserve_alias else busy_id):
        report = DesktopService().update_shortlist(top_k=1)
        with session_scope() as session:
            assert job_snapshot_key(session.get(Job, busy_id)) == snapshot

    assert report["ranked_ids"] == [busy_id, available_id]
    assert report["shortlisted_ids"] == [available_id]
    assert report["skipped_busy"] == 1 and report["skipped_busy_ids"] == [busy_id]
    with session_scope() as session:
        assert session.get(Job, available_id).shortlist_origin == ShortlistOrigin.AUTOMATIC


def test_generating_previous_automatic_selection_is_preserved_with_publication_snapshot(isolated_db):
    busy_id = _offer("generating", 0.20, shortlisted=True, reservation=True)
    available_id = _offer("available", 0.90)
    with session_scope() as session:
        job = session.get(Job, busy_id)
        snapshot = PublicationSnapshot.capture(job, job.application)

    with _busy(busy_id):
        report = DesktopService().update_shortlist(top_k=1)
        with session_scope() as session:
            job = session.get(Job, busy_id)
            snapshot.validate(job, job.application)

    assert report["shortlisted_ids"] == [available_id, busy_id]
    assert report["shortlisted"] == 2
    assert report["skipped_busy"] == 1 and report["skipped_busy_ids"] == [busy_id]
    with session_scope() as session:
        assert session.get(Job, busy_id).shortlist_origin == ShortlistOrigin.AUTOMATIC


def test_automatic_shortlist_keeps_changed_offers_reserved_until_commit(isolated_db):
    old_id = _offer("previous", 0.10, shortlisted=True)
    selected_id = _offer("new", 0.90)
    observed = []

    def try_claim(job_id):
        try:
            with job_operation([job_id], operation="concurrent pipeline"):
                return False
        except TaskConflictError:
            return True

    def before_commit(session):
        if not any(isinstance(item, Job) and item.id == selected_id for item in session.dirty):
            return
        with ThreadPoolExecutor(max_workers=1) as pool:
            observed.append([
                pool.submit(try_claim, job_id).result(timeout=5)
                for job_id in (old_id, selected_id)
            ])

    event.listen(Session, "before_commit", before_commit)
    try:
        report = DesktopService().update_shortlist(top_k=1)
    finally:
        event.remove(Session, "before_commit", before_commit)

    assert report["shortlisted_ids"] == [selected_id]
    assert observed == [[True, True]]
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert not pool.submit(try_claim, selected_id).result(timeout=5)
    with session_scope() as session:
        assert session.get(Job, old_id).shortlisted_at is None


def test_paused_generation_checkpoint_remains_valid_during_automatic_shortlist(isolated_db):
    busy_id = _offer("paused-generation", 0.20, shortlisted=True, reservation=True)
    available_id = _offer("available", 0.90)
    with session_scope() as session:
        job = session.get(Job, busy_id)
        snapshot = job_snapshot_key(job)
        session.add(GenerationRun(
            id="paused-run", mode="immediate", state="paused", profile_data={},
            routes={}, reference_date="2026-10-11",
        ))
        session.flush()
        session.add(GenerationItem(
            run_id="paused-run", job_id=busy_id, state="waiting", job_snapshot=snapshot,
            analysis_snapshot="unused-in-this-test", title=job.title, company=job.source_company,
            application_id=job.application.id,
        ))

    report = DesktopService().update_shortlist(top_k=1)

    assert report["shortlisted_ids"] == [available_id, busy_id]
    assert report["skipped_busy"] == 1 and report["skipped_busy_ids"] == [busy_id]
    with session_scope() as session:
        assert job_snapshot_key(session.get(Job, busy_id)) == snapshot
