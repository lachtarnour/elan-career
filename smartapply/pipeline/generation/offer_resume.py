"""Offer-level checkpoint selection, independent of the requested execution mode."""

from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.orm import joinedload, load_only

from smartapply.database.generation_runs import (
    TERMINAL_GENERATION_ITEM_STATES,
    GenerationCall,
    GenerationItem,
    GenerationItemContext,
    GenerationRun,
    GenerationWave,
)
from smartapply.database.models import Application, GeneratedDocument, Job, JobStatus
from smartapply.database.repository import canonical_job, pending_duplicate_for_group
from smartapply.pipeline.snapshots import _digest, job_is_active, job_snapshot_key

RESUMABLE_RUN_STATES = {"paused", "partial", "failed", "blocked", "cancelled"}
UNRESOLVED_CALL_STATES = {"submitting", "submitted", "uncertain"}
UNRESOLVED_WAVE_STATES = {"prepared", "submitting", "submission_unknown", "submitted"}


def latest_offer_items(session, job_ids=None, *, readiness_only=False):
    query = select(GenerationItem).join(GenerationRun).where(
        GenerationItem.state.not_in(TERMINAL_GENERATION_ITEM_STATES),
    ).order_by(GenerationRun.created_at.desc(), GenerationItem.id.desc())
    if job_ids is not None:
        query = query.where(GenerationItem.job_id.in_(job_ids))
    if readiness_only:
        query = query.options(load_only(
            GenerationItem.id, GenerationItem.run_id, GenerationItem.job_id,
            GenerationItem.job_snapshot, GenerationItem.analysis_snapshot,
            GenerationItem.application_id, raiseload=True,
        ))
    latest = {}
    for item in session.scalars(query):
        latest.setdefault(item.job_id, item)
    return latest


def has_unresolved_submission(session, item):
    if session.scalar(select(GenerationCall.id).where(
        GenerationCall.item_id == item.id,
        GenerationCall.state.in_(UNRESOLVED_CALL_STATES),
    ).limit(1)) is not None:
        return True
    return session.scalar(select(GenerationWave.id).join(
        GenerationCall, GenerationCall.wave_id == GenerationWave.id,
    ).where(
        GenerationCall.item_id == item.id,
        GenerationWave.state.in_(UNRESOLVED_WAVE_STATES),
    ).limit(1)) is not None


def can_resume_offer(session, item):
    # UI readiness must not materialize the frozen profile or prompts.
    run_state = session.scalar(select(GenerationRun.state).where(GenerationRun.id == item.run_id))
    if run_state not in RESUMABLE_RUN_STATES or has_unresolved_submission(session, item):
        return False
    return _unchanged_offer(session, item)


def _unchanged_offer(session, item):
    job = canonical_job(session, item.job_id)
    if (job is None or job.id != item.job_id or not job_is_active(job) or job.analysis is None
            or pending_duplicate_for_group(session, item.job_id)
            or job_snapshot_key(job) != item.job_snapshot
            or _digest({column.name: getattr(job.analysis, column.name)
                        for column in job.analysis.__table__.columns}) != item.analysis_snapshot):
        return False
    app = job.application
    return app is None or (
        app.id == item.application_id and app.status == JobStatus.ANALYZED
        and not (app.cv_json or app.documents or app.cv_docx_path or app.cv_pdf_path
                 or app.form_submitted_at is not None)
    )


def resumable_generation_job_ids(session, job_ids=None):
    from smartapply.concurrency import running_job_ids
    from smartapply.pipeline.process.analysis_admission import reserved_analysis_job_ids

    if job_ids is not None:
        job_ids = set(job_ids)
        if not job_ids:
            return set()
    latest = latest_offer_items(session, job_ids, readiness_only=True)
    if not latest:
        return set()
    busy = set(running_job_ids()) | reserved_analysis_job_ids(session)
    states = dict(session.execute(select(GenerationRun.id, GenerationRun.state).where(
        GenerationRun.id.in_({item.run_id for item in latest.values()}),
    )).all())
    candidates = {job_id: item for job_id, item in latest.items()
                  if job_id not in busy and states.get(item.run_id) in RESUMABLE_RUN_STATES}
    if not candidates:
        return set()
    item_ids = {item.id for item in candidates.values()}
    unresolved = set(session.scalars(select(GenerationCall.item_id).where(
        GenerationCall.item_id.in_(item_ids),
        GenerationCall.state.in_(UNRESOLVED_CALL_STATES),
    )))
    unresolved.update(session.scalars(select(GenerationCall.item_id).join(
        GenerationWave, GenerationCall.wave_id == GenerationWave.id,
    ).where(
        GenerationCall.item_id.in_(item_ids),
        GenerationWave.state.in_(UNRESOLVED_WAVE_STATES),
    )))
    # Keep strong references for the ORM identity map during validation. A
    # large paused batch should not issue separate reads for every analysis
    # and application. No saved profile/request/response body is loaded here.
    jobs = list(session.scalars(select(Job).where(Job.id.in_(candidates)).options(
        joinedload(Job.analysis),
        joinedload(Job.application).load_only(
            Application.id, Application.status, Application.form_submitted_at,
            Application.cv_json, Application.cv_docx_path, Application.cv_pdf_path,
            raiseload=True,
        ).joinedload(Application.documents).load_only(GeneratedDocument.id, raiseload=True),
    )).unique())
    available = {job.id for job in jobs}
    return {job_id for job_id, item in candidates.items()
            if job_id in available and item.id not in unresolved and _unchanged_offer(session, item)}


def copy_offer_checkpoint(session, source, target):
    """Retain history and atomically transfer exclusive ownership of one offer."""
    run = session.get(GenerationRun, source.run_id)
    context = session.get(GenerationItemContext, source.id) or run
    session.add(GenerationItemContext(
        item_id=target.id, profile_data=deepcopy(context.profile_data),
        routes=deepcopy(context.routes), reference_date=context.reference_date,
    ))
    for call in session.scalars(select(GenerationCall).where(
        GenerationCall.item_id == source.id,
    ).order_by(GenerationCall.position)):
        # Successful native calls have already recorded their usage in the
        # source wave. Copies are replay-only and never reimport that usage.
        session.add(GenerationCall(
            item_id=target.id, position=call.position, purpose=call.purpose,
            schema_name=call.schema_name, provider=call.provider,
            request_body=deepcopy(call.request_body), request_key=call.request_key,
            state="completed" if call.state == "completed" else "prepared",
            response_data=deepcopy(call.response_data) if call.state == "completed" else None,
            attempt=call.attempt, error="", wave_id=None,
        ))
    target.application_id = source.application_id
    source.state, source.error = "transferred", ""
