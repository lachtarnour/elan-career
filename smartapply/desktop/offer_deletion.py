"""One deletion boundary for ready offers, database rows and dossier files."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import joinedload

from smartapply.concurrency import application_publication, job_operation
from smartapply.config import get_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.models import Application, Job, JobStatus
from smartapply.database.repository.offer_deletion import (
    assert_no_active_batch,
    confirmed_offer_ids,
    erase_offer_rows,
)
from smartapply.database.session import ensure_startup_maintenance_complete
from smartapply.pipeline.output_deletion import (
    ApplicationOutputDeletion,
    owned_application_directory,
)
from smartapply.pipeline.output_publication import recover_application_publication


@dataclass(frozen=True)
class OfferDeletionResult:
    job_ids: tuple[int, ...]
    application_ids: tuple[int, ...]
    files_removed: bool


def _load_group(session, job_id: int) -> list[Job]:
    if session.get(Job, job_id) is None:
        raise ValueError("Cette offre a déjà été supprimée.")
    ids = confirmed_offer_ids(session, job_id)
    assert_no_active_batch(session, ids)
    jobs = list(
        session.scalars(
            select(Job)
            .options(
                joinedload(Job.application).joinedload(Application.documents),
                joinedload(Job.analysis),
                joinedload(Job.score),
            )
            .where(Job.id.in_(ids))
        ).unique()
    )
    for job in jobs:
        app = job.application
        if app and (app.status != JobStatus.READY_FOR_FORM_SUBMISSION or app.form_submitted_at):
            raise ValueError("Seuls les dossiers prêts et non envoyés peuvent être supprimés ici.")
    selected = next(job for job in jobs if job.id == job_id)
    if (
        selected.application is None
        or selected.status != JobStatus.READY_FOR_FORM_SUBMISSION
        or selected.archived_at
    ):
        raise ValueError("Cette offre n’est plus prête. Actualisez la liste avant de continuer.")
    return jobs


def delete_ready_offer(job_id: int) -> OfferDeletionResult:
    ensure_startup_maintenance_complete()
    with job_operation([job_id], operation="suppression définitive"), ExitStack() as locks:
        with session_scope() as session:
            jobs = _load_group(session, job_id)
            ids = {job.id for job in jobs}
            for app in sorted(
                (job.application for job in jobs if job.application), key=lambda app: app.id
            ):
                locks.enter_context(application_publication(job_id, app.id))
            application_ids = sorted(job.application.id for job in jobs if job.application)
        # A committed publication can still retain its backup after a cleanup
        # error. Resolve its journal before staging the dossier for deletion.
        for application_id in application_ids:
            recover_application_publication(application_id)
        with session_scope() as session:
            jobs = _load_group(session, job_id)
            if {job.id for job in jobs} != ids:
                raise ValueError("Les copies de cette offre ont changé. Actualisez la liste.")
            directories = {
                job.application.id: owned_application_directory(
                    session, job.application, get_settings().output_dir
                )
                for job in jobs
                if job.application
            }
        output = ApplicationOutputDeletion(directories)
        output.prepare()
        try:
            with write_session_scope() as session:
                if session.bind.dialect.name == "sqlite":
                    session.execute(text("BEGIN IMMEDIATE"))
                jobs = _load_group(session, job_id)
                if {job.id for job in jobs} != ids:
                    raise ValueError("Les copies de cette offre ont changé. Actualisez la liste.")
                current = {
                    job.application.id: owned_application_directory(
                        session, job.application, get_settings().output_dir
                    )
                    for job in jobs
                    if job.application
                }
                if current != directories:
                    raise ValueError("Le dossier a changé. Actualisez la liste.")
                output.stage()
                erase_offer_rows(session, jobs)
                output.mark_committed(session)
        except Exception:
            output.rollback()
            raise
        return OfferDeletionResult(tuple(sorted(ids)), tuple(sorted(directories)), output.finish())
