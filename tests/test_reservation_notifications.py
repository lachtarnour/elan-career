"""Reservation refreshes avoid redundant work without weakening ownership."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import smartapply.concurrency as reservations
from smartapply.database import session_scope
from smartapply.database.generation_runs import (
    RESERVED_GENERATION_STATES,
    GenerationItem,
    GenerationRun,
)
from smartapply.database.models import Job, JobDuplicateStatus


def _offers():
    with session_scope() as session:
        root = Job(
            external_id="notify-root",
            title="AI Engineer",
            source_company="Acme",
            source="manual",
            description="Build models",
        )
        other = Job(
            external_id="notify-other",
            title="ML Engineer",
            source_company="Elsewhere",
            source="manual",
            description="Deploy models",
        )
        session.add_all([root, other])
        session.flush()
        alias = Job(
            external_id="notify-alias",
            title=root.title,
            source_company=root.source_company,
            source="manual",
            description=root.description,
            canonical_job_id=root.id,
            duplicate_review_status=JobDuplicateStatus.CONFIRMED,
        )
        pending = Job(
            external_id="notify-pending",
            title=root.title,
            source_company=root.source_company,
            source="manual",
            description=root.description,
            possible_duplicate_of_id=root.id,
            duplicate_review_status=JobDuplicateStatus.PENDING,
        )
        session.add_all([alias, pending])
        session.flush()
        return root.id, alias.id, pending.id, other.id


def _reserve_generation(job_id, state="active"):
    with session_scope() as session:
        session.add(
            GenerationRun(
                id="durable-owner",
                mode="batch",
                state=state,
                profile_data={},
                routes={},
                reference_date="2026-10-11",
            )
        )
        session.flush()
        session.add(
            GenerationItem(
                run_id="durable-owner",
                job_id=job_id,
                job_snapshot="frozen",
                analysis_snapshot="frozen",
                title="AI Engineer",
                company="Acme",
                state="waiting",
            )
        )


def test_nested_same_group_notifies_only_on_first_claim_and_final_release(isolated_db):
    root, alias, pending, _ = _offers()
    group = sorted([root, alias, pending])
    observations = []
    unsubscribe = reservations.subscribe_resource_changes(
        lambda: observations.append(reservations.busy_job_ids()),
    )
    try:
        with reservations.job_operation([root], operation="generation"):
            assert observations == [group]
            with reservations.job_operation([alias], operation="publication"):
                with reservations.job_operation([pending], operation="validation"):
                    assert reservations.running_job_ids() == group
                    assert observations == [group]
                assert observations == [group]
            assert observations == [group]
        assert observations == [group, []]
    finally:
        unsubscribe()


def test_nested_additional_offer_notifies_and_failure_releases_only_its_members(isolated_db):
    root, alias, pending, other = _offers()
    group = sorted([root, alias, pending])
    observations = []
    unsubscribe = reservations.subscribe_resource_changes(
        lambda: observations.append(reservations.busy_job_ids()),
    )
    try:
        with reservations.job_operation([root], operation="generation"):
            with (
                pytest.raises(ValueError, match="cancelled"),
                reservations.job_operation([root, other], operation="nested validation"),
            ):
                assert reservations.busy_job_ids() == sorted([*group, other])
                raise ValueError("cancelled")
            assert reservations.busy_job_ids() == group
        assert observations == [group, sorted([*group, other]), group, []]
    finally:
        unsubscribe()


def test_competing_alias_claim_is_rejected_without_notification_or_partial_reservation(isolated_db):
    root, alias, pending, other = _offers()
    group = sorted([root, alias, pending])
    entered, release = Event(), Event()
    observations = []
    unsubscribe = reservations.subscribe_resource_changes(
        lambda: observations.append(reservations.busy_job_ids()),
    )

    def hold():
        with reservations.job_operation([root], operation="generation"):
            entered.set()
            assert release.wait(5)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(hold)
            try:
                assert entered.wait(5)
                with (
                    pytest.raises(reservations.TaskConflictError),
                    reservations.job_operation([other, alias], operation="archive"),
                ):
                    pytest.fail("A competing alias must remain protected")
                assert reservations.busy_job_ids() == group
                assert observations == [group]
            finally:
                release.set()
            future.result(timeout=5)
        assert observations == [group, []]
        with reservations.job_operation([alias, other], operation="after release"):
            assert reservations.busy_job_ids() == sorted([*group, other])
    finally:
        unsubscribe()


@pytest.mark.parametrize("state", RESERVED_GENERATION_STATES)
def test_durable_owner_and_alias_protection_survive_notification_optimization(isolated_db, state):
    root, alias, pending, _ = _offers()
    group = sorted([root, alias, pending])
    _reserve_generation(root, state)
    observations = []
    unsubscribe = reservations.subscribe_resource_changes(lambda: observations.append("changed"))
    try:
        assert reservations.busy_job_ids() == group
        assert reservations.running_job_ids() == []
        with (
            pytest.raises(reservations.TaskConflictError),
            reservations.job_operation([alias], operation="archive"),
        ):
            pytest.fail("The durable generation owns every alias")
        with (
            reservations.generation_owner("unrelated-owner"),
            pytest.raises(reservations.TaskConflictError),
            reservations.job_operation([pending], operation="wrong generation"),
        ):
            pytest.fail("The wrong generation cannot borrow an alias")
        assert observations == []
        with (
            reservations.generation_owner("durable-owner"),
            reservations.job_operation([alias], operation="own generation"),
        ):
            assert reservations.running_job_ids() == group
            with reservations.job_operation([root], operation="nested publication"):
                assert observations == ["changed"]
        assert observations == ["changed", "changed"]
        assert reservations.busy_job_ids() == group
        assert reservations.running_job_ids() == []
    finally:
        unsubscribe()


def test_busy_snapshot_expands_local_and_durable_aliases_once(isolated_db, monkeypatch):
    root, alias, pending, other = _offers()
    _reserve_generation(root)
    expand = reservations._group_ids
    expanded_inputs = []

    def observe(ids):
        expanded_inputs.append(set(ids))
        return expand(ids)

    with reservations.job_operation([other], operation="independent operation"):
        monkeypatch.setattr(reservations, "_group_ids", observe)
        assert reservations.busy_job_ids() == sorted([root, alias, pending, other])
        assert expanded_inputs == [{root, other}]


def test_callback_failure_cannot_leak_nested_reservations(isolated_db):
    root, alias, pending, _ = _offers()
    attempts = []

    def broken_observer():
        attempts.append("notification")
        raise RuntimeError("Observer failed")

    unsubscribe = reservations.subscribe_resource_changes(broken_observer)
    try:
        with (
            pytest.raises(ValueError, match="operation failed"),
            reservations.job_operation([root], operation="generation"),
            reservations.job_operation([alias], operation="publication"),
        ):
            raise ValueError("operation failed")
        assert attempts == ["notification", "notification"]
        assert reservations.running_job_ids() == []
        assert reservations.busy_job_ids() == []
        with reservations.job_operation([pending], operation="recovery"):
            assert reservations.running_job_ids() == sorted([root, alias, pending])
    finally:
        unsubscribe()
