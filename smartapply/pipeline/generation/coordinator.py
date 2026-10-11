"""Automatic, resumable creation after analysis, using the production Applier."""

from __future__ import annotations

from contextlib import ExitStack, suppress
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import load_only

from smartapply.concurrency import generation_owner, job_operation
from smartapply.config import get_llm_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.generation_runs import (
    RESERVED_GENERATION_STATES,
    TERMINAL_GENERATION_ITEM_STATES,
    GenerationCall,
    GenerationItem,
    GenerationItemContext,
    GenerationRun,
)
from smartapply.database.models import Application, Job, JobStatus
from smartapply.database.repository import (
    application_for_duplicate_group,
    canonical_job,
    pending_duplicate_for_group,
)
from smartapply.jobsearch.offer_state import STALE_RESERVATION_AFTER, is_reusable_reservation
from smartapply.llm import get_llm_provider
from smartapply.llm_policy import resolve_route
from smartapply.logging_setup import get_logger
from smartapply.pipeline.errors import StaleTaskResultError, TaskCancelledError
from smartapply.pipeline.generation.locking import generation_operation
from smartapply.pipeline.generation.offer_resume import (
    can_resume_offer,
    copy_offer_checkpoint,
    has_unresolved_submission,
    latest_offer_items,
)
from smartapply.pipeline.generation.replay import (
    PURPOSES,
    GenerationCallFailed,
    GenerationPending,
    ReplayProvider,
    frozen_settings,
)
from smartapply.pipeline.generation.transport import GenerationBatchTransport
from smartapply.pipeline.process.analysis_admission import reserved_analysis_job_ids
from smartapply.pipeline.snapshots import _digest, job_is_active, job_snapshot_key
from smartapply.profile import Profile, get_profile

logger = get_logger(__name__)


def _now():
    return datetime.now(timezone.utc)


def analysis_key(job):
    return _digest({column.name: getattr(job.analysis, column.name)
                    for column in job.analysis.__table__.columns})


def reserved_generation_job_ids(session):
    return set(session.scalars(select(GenerationItem.job_id).join(GenerationRun).where(
        GenerationRun.state.in_(RESERVED_GENERATION_STATES),
        GenerationItem.state.not_in(TERMINAL_GENERATION_ITEM_STATES),
    )))


class GenerationCoordinator:
    def __init__(self, *, transport=None, provider=None, profile=None, pipeline_factory=None):
        self.transport = GenerationBatchTransport(transport=transport)
        self.provider = provider
        self.profile = profile
        self.pipeline_factory = pipeline_factory

    def start(self, job_ids, mode="batch", *, stop_requested=None, progress=None):
        if mode not in {"batch", "immediate"}:
            raise ValueError("Mode de création inconnu.")
        ids = list(dict.fromkeys(int(value) for value in job_ids if int(value) > 0))
        if not ids:
            raise ValueError("Sélectionnez au moins une offre analysée.")
        if stop_requested and stop_requested():
            raise TaskCancelledError()
        settings = get_llm_settings()
        routes = {purpose: asdict(resolve_route(settings, purpose)) for purpose in PURPOSES}
        profile = self.profile or get_profile()
        run_id = str(uuid4())
        # Source run locks prevent a simultaneous retry/sync while transferring
        # selected offers. The global lock serializes transfer and new starts.
        with generation_operation(), ExitStack() as locks:
            with session_scope() as session:
                sources = latest_offer_items(session, ids)
                source_runs = {item.run_id for item in sources.values()}
                for item in sources.values():
                    if has_unresolved_submission(session, item):
                        raise ValueError("Un envoi est en cours ou reste à vérifier. Aucun nouvel appel envoyé.")
                    if not can_resume_offer(session, item):
                        raise ValueError("Cette offre possède un parcours en cours ou modifié. Arrêtez-le avant de reprendre.")
            for source_run in sorted(source_runs):
                locks.enter_context(generation_operation(source_run))
            locks.enter_context(generation_owner(source_runs))
            locks.enter_context(job_operation(ids, operation="préparation de candidatures"))
            with write_session_scope() as session:
                self._register_run(session, ids, run_id, mode, routes, profile)
        self._advance(run_id, stop_requested=stop_requested, progress=progress)
        return run_id

    def _register_run(self, session, ids, run_id, mode, routes, profile):
        sources = latest_offer_items(session, ids)
        for item in sources.values():
            if not can_resume_offer(session, item):
                raise ValueError("La reprise de cette offre n’est plus disponible. Actualisez le suivi.")
        source_runs = {item.run_id for item in sources.values()}
        resumable = set(sources)
        # The write transaction serializes duplicate starts in separate processes.
        analysis_reserved = reserved_analysis_job_ids(session)
        reserved = reserved_generation_job_ids(session) | analysis_reserved
        jobs = []
        for job_id in ids:
            job = canonical_job(session, job_id)
            if (job is None or int(job.id) != job_id or not job_is_active(job)
                    or job.analysis is None or pending_duplicate_for_group(session, job_id)
                    or (job_id in reserved and job_id not in resumable)
                    or job_id in analysis_reserved
                    or (job.application is not None and job_id not in resumable
                        and not is_reusable_reservation(job.application))):
                raise ValueError(
                    f"Offre {job_id} indisponible. Terminez son analyse ou reprenez son parcours existant."
                )
            context = (
                session.get(GenerationItemContext, sources[job_id].id)
                or session.get(GenerationRun, sources[job_id].run_id)
                if job_id in sources else None
            )
            effective_routes = context.routes if context else routes
            if mode == "batch" and any(route["provider"] not in {"openai", "anthropic"}
                                       for route in effective_routes.values()):
                raise ValueError("Le traitement Batch nécessite OpenAI ou Anthropic pour chaque offre.")
            jobs.append(job)
        session.add(GenerationRun(
            id=run_id, mode=mode, state="active", routes=routes,
            profile_data=profile.model_dump(mode="json"),
            reference_date=_now().astimezone(ZoneInfo("Europe/Paris")).date().isoformat(),
        ))
        session.flush()
        for job in jobs:
            item = GenerationItem(
                run_id=run_id, job_id=job.id, job_snapshot=job_snapshot_key(job),
                analysis_snapshot=analysis_key(job), title=job.title, company=job.source_company,
                state="pending", application_id=job.application.id if job.application else None,
            )
            session.add(item)
            session.flush()
            if job.id in sources:
                copy_offer_checkpoint(session, sources[job.id], item)
        session.flush()
        # A selected subset leaves other offers paused in their source run.
        for source_run in source_runs:
            old = session.get(GenerationRun, source_run)
            states = list(session.scalars(select(GenerationItem.state).where(
                GenerationItem.run_id == source_run,
                GenerationItem.state != "transferred",
            )))
            if not states:
                old.state = "cancelled"
            elif all(state in ("completed", "stale") for state in states):
                old.state = "completed" if "completed" in states else "failed"
            old.updated_at = _now()

    def sync(self):
        with session_scope() as session:
            runs = list(session.execute(select(GenerationRun.id, GenerationRun.mode).where(
                GenerationRun.state.in_(("active", "attention", "blocked")),
            )))
        for run_id, mode in runs:
            # An immediate run interrupted by app closure pauses for explicit
            # resume; completed calls remain available. Batch continues itself.
            try:
                with generation_operation(run_id):
                    if mode == "immediate":
                        self._pause_interrupted(run_id)
                    else:
                        self._advance_locked(run_id)
            except RuntimeError:
                # Another worker/process owns this run; a read is still safe.
                continue

    def retry(self, run_id, *, stop_requested=None, progress=None):
        with generation_operation(run_id):
            with generation_operation(), write_session_scope() as session:
                run = session.get(GenerationRun, run_id)
                if run is None:
                    raise ValueError("Parcours introuvable.")
                remaining = list(session.scalars(select(GenerationItem).where(
                    GenerationItem.run_id == run_id,
                    GenerationItem.state.not_in(TERMINAL_GENERATION_ITEM_STATES),
                )))
                if not remaining:
                    raise ValueError("Aucune étape disponible à reprendre.")
                reserved_elsewhere = set(session.scalars(select(GenerationItem.job_id).join(GenerationRun).where(
                    GenerationRun.id != run_id, GenerationRun.state.in_(RESERVED_GENERATION_STATES),
                    GenerationItem.state.not_in(TERMINAL_GENERATION_ITEM_STATES),
                ))) | reserved_analysis_job_ids(session)
                for item in remaining:
                    job = session.get(Job, item.job_id)
                    app = job.application if job else None
                    if item.job_id in reserved_elsewhere or (app and (
                        app.documents or app.cv_json or app.cv_docx_path or app.cv_pdf_path
                        or app.form_submitted_at is not None
                    )):
                        raise ValueError("Une offre est utilisée par un autre parcours ou possède déjà un dossier.")
                uncertain = session.scalar(select(GenerationCall.id).join(GenerationItem).where(
                    GenerationItem.run_id == run_id,
                    GenerationCall.state.in_(("uncertain", "submitting")),
                ))
                if run.mode == "immediate" and uncertain is not None:
                    raise ValueError("Un envoi immédiat reste à vérifier. Aucun renvoi automatique.")
                for call in session.scalars(select(GenerationCall).join(GenerationItem).where(
                    GenerationItem.run_id == run_id,
                    GenerationItem.state.not_in(TERMINAL_GENERATION_ITEM_STATES),
                    GenerationCall.state == "failed",
                )):
                    call.state, call.wave_id, call.error = "prepared", None, ""
                for item in session.scalars(select(GenerationItem).where(
                    GenerationItem.run_id == run_id, GenerationItem.state.in_(("failed", "waiting")),
                )):
                    item.state, item.error = "pending", ""
                run.state, run.cancel_requested, run.last_error = "active", False, ""
                run.updated_at = _now()
            self._advance_locked(run_id, stop_requested=stop_requested, progress=progress)

    def cancel(self, run_id):
        # Persist Stop even while a synchronous worker owns the run lock.
        with write_session_scope() as session:
            run = session.get(GenerationRun, run_id)
            if run is None or run.state not in RESERVED_GENERATION_STATES:
                raise ValueError("Ce parcours n’est plus en cours.")
            run.cancel_requested = True
            run.updated_at = _now()
        with suppress(RuntimeError):
            self._advance(run_id)

    def abandon(self, run_id):
        """Release a paused run explicitly, retaining its acquired responses."""
        with generation_operation(run_id):
            if self.transport.outstanding(run_id):
                raise ValueError("Attendez la confirmation de l’arrêt des lots avant de libérer les offres.")
            with write_session_scope() as session:
                run = session.get(GenerationRun, run_id)
                if run is None or not (run.state in {"paused", "partial", "failed"}
                                       or (run.mode == "immediate" and run.state == "attention")):
                    raise ValueError("Arrêtez d’abord le parcours avant de libérer ses offres.")
                for item in session.scalars(select(GenerationItem).where(
                    GenerationItem.run_id == run_id,
                    GenerationItem.state.not_in(TERMINAL_GENERATION_ITEM_STATES),
                )):
                    self._release_empty_reservation(session, item)
                run.state = "cancelled"
                run.updated_at = _now()

    @staticmethod
    def _release_empty_reservation(session, item):
        app = session.get(Application, item.application_id) if item.application_id else None
        if (app is None or app.job_id != item.job_id or app.status != JobStatus.ANALYZED
                or app.cv_json or app.documents or app.cv_docx_path or app.cv_pdf_path
                or app.form_submitted_at is not None):
            return
        if app.notes:
            if not is_reusable_reservation(app):
                app.updated_at = _now() - STALE_RESERVATION_AFTER
            return
        # Historical stale/abandoned runs can refer to this same empty app.
        for saved in session.scalars(select(GenerationItem).where(
            GenerationItem.application_id == app.id,
        )):
            saved.application_id = None
        session.flush()
        session.delete(app)

    def _advance(self, run_id, **kwargs):
        with generation_operation(run_id):
            self._advance_locked(run_id, **kwargs)

    def _stop(self, run_id, stop_requested=None):
        if stop_requested and stop_requested():
            with write_session_scope() as session:
                run = session.get(GenerationRun, run_id, options=[load_only(
                    GenerationRun.cancel_requested, GenerationRun.updated_at,
                )])
                if not run.cancel_requested:
                    run.cancel_requested = True
                    run.updated_at = _now()
            return True
        with session_scope() as session:
            return bool(session.scalar(select(GenerationRun.cancel_requested).where(
                GenerationRun.id == run_id,
            )))

    def _validate(self, item_id):
        with session_scope() as session:
            item = session.get(GenerationItem, item_id)
            job = session.get(Job, item.job_id)
            if (job is None or not job_is_active(job) or job.analysis is None
                    or job_snapshot_key(job) != item.job_snapshot
                    or analysis_key(job) != item.analysis_snapshot
                    or pending_duplicate_for_group(session, item.job_id) is not None):
                raise StaleTaskResultError()

    def _advance_locked(self, run_id, *, stop_requested=None, progress=None):
        try:
            with session_scope() as session:
                run = session.execute(select(
                    GenerationRun.mode, GenerationRun.cancel_requested,
                ).where(GenerationRun.id == run_id)).one_or_none()
                if run is None:
                    return
                mode, cancelling = run.mode, run.cancel_requested
            if mode == "batch":
                self.transport.sync(
                    run_id, cancelling=cancelling, stop_requested=stop_requested,
                )
            # The journal survives coordinator/process restarts. Only a newly
            # available response or failure needs the Python replay; polling an
            # unchanged submitted call must not reconstruct the frozen profile,
            # prompts and every response preceding it. Read AFTER importing
            # provider results so dependent stages advance in the same follow-up.
            last_call_state = select(GenerationCall.state).where(
                GenerationCall.item_id == GenerationItem.id,
            ).order_by(GenerationCall.position.desc()).limit(1).correlate(
                GenerationItem,
            ).scalar_subquery()
            with session_scope() as session:
                items = list(session.execute(select(
                    GenerationItem.id, GenerationItem.state, last_call_state,
                ).where(
                    GenerationItem.run_id == run_id,
                    GenerationItem.state.in_(("pending", "waiting")),
                ).order_by(GenerationItem.id)))
            item_ids = [item_id for item_id, _, _ in items]
            for item_id, item_state, call_state in items:
                if self._stop(run_id, stop_requested):
                    break
                self._execute_item(
                    item_id, stop_requested=stop_requested, progress=progress,
                    waiting_for_response=(
                        mode == "batch" and item_state == "waiting"
                        and call_state in {"submitted", "submitting", "uncertain"}
                    ),
                )
            stopped = self._stop(run_id, stop_requested)
            if mode == "batch":
                if stopped:
                    self.transport.sync(run_id, cancelling=True)
                else:
                    # Recheck freshness before paying for any newly prepared call.
                    for item_id in item_ids:
                        with session_scope() as session:
                            item = session.get(GenerationItem, item_id)
                            eligible = item.state in {"pending", "waiting"}
                        if eligible:
                            self._validate(item_id)
                    self.transport.submit_pending(run_id, stop_requested=stop_requested)
                    stopped = self._stop(run_id, stop_requested)
                    if stopped:
                        self.transport.sync(run_id, cancelling=True)
            self._finish(run_id, stopped=stopped, previous_item_states={item_id: state for item_id, state, _ in items})
        except Exception:
            logger.exception("Creation follow-up failed for %s", run_id)
            with write_session_scope() as session:
                run = session.get(GenerationRun, run_id)
                if run is not None:
                    run.state = "blocked"
                    run.last_error = "Le suivi est interrompu. Les résultats acquis sont conservés."
                    run.updated_at = _now()

    def _validate_waiting_reservation(self, item_id):
        """Keep the Applier's empty-resume guards without constructing it.

        A missing acknowledgement requires the usual reservation recovery path.
        Notes and updated_at are independent of document generation.
        """
        with session_scope() as session:
            item = session.get(GenerationItem, item_id)
            if item.application_id is None:
                return False
            existing = session.scalar(select(Application).where(Application.job_id == item.job_id))
            group_application = application_for_duplicate_group(session, item.job_id)
            if (existing is None or existing.id != item.application_id
                    or (group_application is not None and group_application.job_id != item.job_id)
                    or existing.status != JobStatus.ANALYZED
                    or existing.form_submitted_at is not None
                    or existing.documents or existing.cv_json
                    or existing.cv_docx_path or existing.cv_pdf_path):
                raise StaleTaskResultError()
        return True

    def _execute_item(self, item_id, *, stop_requested=None, progress=None,
                      waiting_for_response=False):
        from smartapply.pipeline import Pipeline

        with session_scope() as session:
            item = session.execute(select(
                GenerationItem.run_id, GenerationItem.job_id, GenerationItem.application_id,
                GenerationRun.mode,
            ).join(GenerationRun).where(GenerationItem.id == item_id)).one_or_none()
            if item is None:
                return
            run_id, job_id, app_id, mode = item
        def stop():
            return self._stop(run_id, stop_requested)
        try:
            self._validate(item_id)
            with generation_owner(run_id), job_operation([job_id], operation="création de candidature"):
                self._validate(item_id)
                if waiting_for_response and self._validate_waiting_reservation(item_id):
                    return
                with session_scope() as session:
                    run = session.get(GenerationRun, run_id)
                    routes, profile_data, reference_date = (
                        run.routes, run.profile_data, run.reference_date,
                    )
                    context = session.get(GenerationItemContext, item_id)
                    if context is not None:
                        routes, profile_data, reference_date = (
                            context.routes, context.profile_data, context.reference_date,
                        )
                # Recover the tiny reservation/acknowledgement gap without a
                # second allocation. No provider call can precede this callback.
                if app_id is None:
                    with write_session_scope() as session:
                        item = session.get(GenerationItem, item_id)
                        app = session.scalar(select(Application).where(Application.job_id == job_id))
                        if app is not None:
                            if app.cv_json or app.documents or app.form_submitted_at:
                                raise StaleTaskResultError()
                            item.application_id = app_id = app.id
                replay = ReplayProvider(
                    item_id=item_id, mode=mode, routes=routes, reference_date=reference_date,
                    provider=self.provider or get_llm_provider(settings=frozen_settings(routes)),
                    validate=lambda: self._validate(item_id), stop_requested=stop, progress=progress,
                )
                factory = self.pipeline_factory or Pipeline
                pipeline = factory(profile=Profile.model_validate(profile_data), llm=replay, stop_requested=stop)

                def reserved(report):
                    with write_session_scope() as session:
                        item = session.get(GenerationItem, item_id)
                        item.application_id = report.application_id

                def published(report, session):
                    item = session.get(GenerationItem, item_id)
                    item.state = "completed"
                    item.result = asdict(report)
                    item.error = ""

                pipeline._applier.apply(
                    job_id, resume_application_id=app_id,
                    reservation_callback=reserved, publication_callback=published,
                )
        except GenerationPending:
            with write_session_scope() as session:
                session.get(GenerationItem, item_id).state = "waiting"
        except TaskCancelledError:
            pass
        except StaleTaskResultError:
            with write_session_scope() as session:
                item = session.get(GenerationItem, item_id)
                item.state, item.error = "stale", str(StaleTaskResultError())
                self._release_empty_reservation(session, item)
                for call in session.scalars(select(GenerationCall).where(
                    GenerationCall.item_id == item_id, GenerationCall.state == "prepared",
                )):
                    call.state, call.error = "failed", "Offre ou analyse modifiée."
        except Exception as exc:
            with write_session_scope() as session:
                item = session.get(GenerationItem, item_id)
                if item.state != "completed":
                    item.state = "failed"
                    item.error = (str(exc) if isinstance(exc, GenerationCallFailed)
                                  else "Création interrompue. Les réponses acquises sont conservées.")
            logger.exception("Creation interrupted for offer %s", job_id)

    def _pause_interrupted(self, run_id):
        progressed = False
        with write_session_scope() as session:
            for call in session.scalars(select(GenerationCall).join(GenerationItem).where(
                GenerationItem.run_id == run_id, GenerationCall.state == "submitting",
            )):
                call.state, call.error = "uncertain", "Envoi immédiat à vérifier."
                progressed = True
        self._finish(run_id, stopped=True, progressed=progressed)

    def _finish(self, run_id, *, stopped=False, previous_item_states=None, progressed=False):
        outstanding = self.transport.outstanding(run_id)
        with write_session_scope() as session:
            run = session.get(GenerationRun, run_id, options=[load_only(
                GenerationRun.state, GenerationRun.cancel_requested,
                GenerationRun.last_error, GenerationRun.updated_at,
            )])
            items = list(session.scalars(select(GenerationItem).options(load_only(
                GenerationItem.job_id, GenerationItem.state, GenerationItem.application_id,
            )).where(GenerationItem.run_id == run_id)))
            states = [item.state for item in items if item.state != "transferred"]
            for item in items:
                if item.state == "stale":
                    self._release_empty_reservation(session, item)
            uncertain = session.scalar(select(GenerationCall.id).join(GenerationItem).where(
                GenerationItem.run_id == run_id, GenerationCall.state.in_(("uncertain", "submitting")),
            ))
            state, last_error, cancelling = run.state, run.last_error, run.cancel_requested
            if uncertain is not None:
                # Keep the transport's recovery diagnostic. Replacing it with
                # a generic message would make every unchanged recovery poll
                # toggle last_error and emit two artificial database changes.
                if state != "attention" or not last_error:
                    last_error = "Envoi à vérifier ; aucun renvoi automatique."
                state = "attention"
            elif stopped:
                state = ("completed" if all(item_state == "completed" for item_state in states)
                         else "active" if outstanding else "paused")
                cancelling = True
            elif run.state in {"attention", "blocked"} and run.last_error:
                pass
            elif "pending" in states or "waiting" in states or outstanding:
                state = "active"
            elif all(state == "completed" for state in states):
                state, last_error = "completed", ""
            else:
                state = "partial" if "completed" in states else "failed"
            changed = False
            for field, value in (("state", state), ("last_error", last_error), ("cancel_requested", cancelling)):
                if getattr(run, field) != value:
                    setattr(run, field, value)
                    changed = True
            if previous_item_states is not None:
                progressed = progressed or any(
                    item.id in previous_item_states and item.state != previous_item_states[item.id]
                    for item in items
                )
            if changed or progressed:
                run.updated_at = _now()
