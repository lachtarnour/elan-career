"""Resume dependent Batch waves from a durable journal, one transition at a time.

Network calls never hold a SQLite transaction. A submission intent is committed
before POST; an uncertain POST is recovered by metadata and never sent again.
Importing an output and marking its custom ID consumed is one transaction.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select

from smartapply.concurrency import job_operation
from smartapply.config import get_llm_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.analysis_batches import (
    ACTIVE_ANALYSIS_RUN_STATES,
    AnalysisBatchItem,
    AnalysisBatchRun,
    AnalysisBatchWave,
)
from smartapply.database.repository.llm_cache import purge_expired_cache
from smartapply.llm.batch import BatchRequest, encode_requests, get_batch_transport, split_requests
from smartapply.llm.fit_context import build_fit_context
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.routes import resolve_route
from smartapply.logging_setup import get_logger
from smartapply.pipeline.batch_analysis.locking import batch_operation
from smartapply.pipeline.batch_analysis.results import (
    FrozenConfigurationError,
    accept_cache,
    current_job,
    fail_open_screening,
    import_result,
    item_request,
    new_item,
    phase_route,
)
from smartapply.pipeline.process.scope_screening import scope_context
from smartapply.pipeline.snapshots import check_cancelled

logger = get_logger(__name__)
ACTIVE_RUN_STATES = ACTIVE_ANALYSIS_RUN_STATES
PENDING_ITEM_STATES = ("screening_pending", "extraction_pending", "evaluation_pending")
POLLING_RUN_STATES = ("active", "attention")


def _upload_access_message(error: str, provider="openai") -> str:
    label = "Anthropic" if provider == "anthropic" else "OpenAI"
    if provider == "openai" and "api.files.write" in error:
        return (
            "Accès OpenAI requis : la clé utilisée ne peut pas envoyer les fichiers Batch "
            "(api.files.write). Corrige ses permissions, puis reprends cette analyse."
        )
    return (
        f"L’envoi Batch a été refusé par {label}. Vérifie les permissions "
        "de la clé utilisée, puis reprends cette analyse."
    )


def _is_stored_upload_access_error(run, waves) -> bool:
    """Recognize an earlier denied upload without repeating the API call."""
    message = run.last_error or ""
    return (
        run.state == "active"
        and "api.files.write" in message
        and ("401" in message or "403" in message)
        and any(wave.state == "prepared" and not wave.input_file_id for wave in waves)
        and not any(wave.provider_batch_id for wave in waves)
    )


def _now():
    return datetime.now(timezone.utc)


def _items(session, run_id):
    return list(
        session.scalars(
            select(AnalysisBatchItem)
            .where(AnalysisBatchItem.run_id == run_id)
            .order_by(AnalysisBatchItem.id)
        )
    )


def _waves(session, run_id):
    return list(
        session.scalars(
            select(AnalysisBatchWave)
            .where(AnalysisBatchWave.run_id == run_id)
            .order_by(AnalysisBatchWave.created_at, AnalysisBatchWave.id)
        )
    )


class BatchAnalysisCoordinator:
    def __init__(self, *, transport=None, pipeline=None):
        self.settings = get_llm_settings()
        self.transport = transport
        self._transports = {}
        self.pipeline = pipeline

    def _transport(self, provider):
        if self.transport is not None:
            return self.transport
        if provider not in self._transports:
            self._transports[provider] = get_batch_transport(provider, settings=self.settings)
        return self._transports[provider]

    def start(self, job_ids: list[int] | None = None) -> str:
        if self.pipeline is None:
            raise ValueError("A pipeline is required to prepare an analysis run")
        stop_requested = getattr(self.pipeline, "stop_requested", None)
        with batch_operation():
            check_cancelled(stop_requested)
            screening = resolve_route(self.settings, "job_scope_screening")
            extraction = resolve_route(self.settings, "job_offer_extraction")
            fit = resolve_route(self.settings, "job_fit_assessment")
            if any(
                route.provider not in {"openai", "anthropic"}
                for route in (screening, extraction, fit)
            ):
                raise ValueError("Le traitement Batch nécessite OpenAI ou Anthropic.")
            self.pipeline.prepare_analysis(job_ids=job_ids)
            check_cancelled(stop_requested)
            candidates = self.pipeline.analysis_candidates(job_ids)
            if not candidates:
                raise ValueError("Aucune offre disponible à analyser dans cette sélection.")
            run_id = str(uuid4())
            provider = extraction.provider
            options = self.settings
            with (
                job_operation(
                    [job.id for job in candidates],
                    operation="préparation Batch",
                ),
                write_session_scope() as session,
            ):
                check_cancelled(stop_requested)
                run = AnalysisBatchRun(
                    id=run_id,
                    state="active",
                    model=extraction.model,
                    provider=provider,
                    max_completion_tokens=extraction.max_output_tokens,
                    extraction_effort=extraction.reasoning,
                    profile_data=self.pipeline.profile.model_dump(mode="json"),
                    fit_context=build_fit_context(
                        self.pipeline.profile,
                        fit.model,
                        provider=fit.provider,
                        max_completion_tokens=fit.max_output_tokens,
                        settings=options,
                    ),
                    scope_context=scope_context(
                        provider=screening.provider,
                        model=screening.model,
                        settings=options,
                        max_completion_tokens=screening.max_output_tokens,
                    ),
                    last_error="",
                )
                session.add(run)
                session.flush()
                for job in candidates:
                    session.add(
                        new_item(
                            run_id,
                            job,
                            session=session,
                            model=run.model,
                            provider=provider,
                            effort=run.extraction_effort,
                            screening_context=run.scope_context,
                        )
                    )
            if stop_requested is not None and stop_requested():
                with write_session_scope() as session:
                    session.get(AnalysisBatchRun, run_id).cancel_requested = True
            self._safely_advance(run_id)
            if stop_requested is not None and stop_requested():
                with write_session_scope() as session:
                    run = session.get(AnalysisBatchRun, run_id)
                    needs_cancel = run.state in ACTIVE_RUN_STATES and not run.cancel_requested
                    if needs_cancel:
                        run.cancel_requested = True
                if needs_cancel:
                    self._safely_advance(run_id)
            return run_id

    def sync(self) -> None:
        with batch_operation():
            with session_scope() as session:
                run_ids = list(
                    session.scalars(
                        select(AnalysisBatchRun.id).where(
                            AnalysisBatchRun.state.in_(POLLING_RUN_STATES)
                        )
                    )
                )
            for run_id in run_ids:
                self._safely_advance(run_id)

    def retry(self, run_id: str) -> None:
        with batch_operation():
            with write_session_scope() as session:
                run = session.get(AnalysisBatchRun, run_id)
                if run is None:
                    raise ValueError("Lot introuvable.")
                if run.state == "blocked":
                    recover_only = False
                    run.state = "active"
                    run.last_error = ""
                    run.updated_at = _now()
                elif run.state in POLLING_RUN_STATES:
                    # Includes ambiguous submissions: recover, never reset them.
                    recover_only = True
                else:
                    recover_only = False
                    if run.cancel_requested:
                        raise ValueError(
                            "Un lot annulé ne peut pas être repris. Créez un nouveau lot."
                        )
                    reserved_elsewhere = set(
                        session.scalars(
                            select(AnalysisBatchItem.job_id)
                            .join(AnalysisBatchRun, AnalysisBatchRun.id == AnalysisBatchItem.run_id)
                            .where(
                                AnalysisBatchRun.state.in_(ACTIVE_RUN_STATES),
                                AnalysisBatchRun.id != run_id,
                            )
                        )
                    )
                    retryable = [
                        item
                        for item in _items(session, run_id)
                        if item.state in {"extraction_failed", "evaluation_failed"}
                    ]
                    if not retryable:
                        raise ValueError("Aucune demande échouée à reprendre.")
                    reactivated = 0
                    for item in retryable:
                        if (
                            item.job_id not in reserved_elsewhere
                            and current_job(session, item, run) is not None
                        ):
                            item.state = (
                                "evaluation_pending"
                                if item.offer_data is not None
                                else "extraction_pending"
                            )
                            item.error = ""
                            reactivated += 1
                    if not reactivated:
                        raise ValueError("Aucune offre disponible à reprendre dans ce lot.")
                    run.state = "active"
                    run.completed_at = None
                    run.last_error = ""
                    run.updated_at = _now()
            if recover_only:
                logger.info("Recovering existing batch run %s", run_id)
            self._safely_advance(run_id)

    def cancel(self, run_id: str) -> None:
        with batch_operation():
            with write_session_scope() as session:
                run = session.get(AnalysisBatchRun, run_id)
                if run is None or run.state not in ACTIVE_RUN_STATES:
                    raise ValueError("Ce lot n’est plus en cours.")
                run.cancel_requested = True
                run.updated_at = _now()
            self._safely_advance(run_id)

    def _safely_advance(self, run_id: str) -> None:
        try:
            self._advance(run_id)
        except Exception as exc:
            logger.exception("Batch follow-up failed for %s", run_id)
            with write_session_scope() as session:
                run = session.get(AnalysisBatchRun, run_id)
                run.last_error = (
                    run.last_error + " " + str(exc) if run.state == "attention" else str(exc)
                )
                if isinstance(exc, FrozenConfigurationError):
                    run.state = "blocked"
                run.updated_at = _now()

    def _advance(self, run_id: str) -> None:
        # Three dependent phases plus their cached-only transitions.
        # Provider waiting always returns immediately rather than occupying a worker.
        for _ in range(9):
            with session_scope() as session:
                run = session.get(AnalysisBatchRun, run_id)
                waves = [wave for wave in _waves(session, run_id) if wave.state != "imported"]
                cancelling = run.cancel_requested
                blocked = run.state == "blocked"
                denied_upload = _is_stored_upload_access_error(run, waves)
                error = run.last_error
            if blocked and not cancelling:
                return
            if denied_upload and not cancelling:
                with write_session_scope() as session:
                    run = session.get(AnalysisBatchRun, run_id)
                    run.state = "blocked"
                    run.last_error = _upload_access_message(error, run.provider)
                    run.updated_at = _now()
                return
            if waves:
                waiting = False
                for wave in waves:
                    if wave.state == "prepared":
                        if cancelling:
                            self._abandon_prepared(wave.id)
                            continue
                        if not self._submit(wave.id):
                            with session_scope() as session:
                                if session.get(AnalysisBatchRun, run_id).state == "blocked":
                                    return
                            continue
                    try:
                        refreshed = self._refresh_wave(wave.id, cancelling=cancelling)
                    except Exception as exc:
                        if wave.phase != "screening" or cancelling:
                            raise
                        self._screening_wave_failed(wave.id, exc)
                        refreshed = True
                    if not refreshed:
                        waiting = True
                if waiting:
                    return
                continue
            if cancelling:
                self._finish_cancel(run_id)
                return
            with session_scope() as session:
                items = _items(session, run_id)
                phase = (
                    "screening"
                    if any(i.state == "screening_pending" for i in items)
                    else "extraction"
                    if any(i.state == "extraction_pending" for i in items)
                    else "evaluation"
                    if any(i.state == "evaluation_pending" for i in items)
                    else None
                )
            if phase is None:
                self._finish(run_id)
                return
            self._prepare_waves(run_id, phase)

    def _prepare_waves(self, run_id: str, phase: str) -> None:
        with write_session_scope() as session:
            purge_expired_cache(session, ttl_days=self.settings.llm_cache_ttl_days)
            run = session.get(AnalysisBatchRun, run_id)
            unique: dict[str, tuple[BatchRequest, list[int]]] = {}
            for item in _items(session, run_id):
                if item.state != f"{phase}_pending" or current_job(session, item, run) is None:
                    continue
                try:
                    request = item_request(
                        item,
                        run,
                        phase,
                        run.max_completion_tokens,
                    )
                except FrozenConfigurationError:
                    raise
                except (ValueError, LLMValidationError) as exc:
                    if phase == "screening":
                        fail_open_screening(session, item, run, exc)
                    else:
                        item.state = f"{phase}_failed"
                        item.error = str(exc)
                    continue
                if accept_cache(session, item, run, phase, request):
                    continue
                if request.cache_key in unique:
                    unique[request.cache_key][1].append(item.id)
                else:
                    unique[request.cache_key] = (request, [item.id])
            # All dependent requests are journaled before any provider POST.
            for chunk in split_requests(request for request, _ in unique.values()):
                records = [
                    {
                        "custom_id": request.custom_id,
                        "body": request.body,
                        "cache_key": request.cache_key,
                        "item_ids": unique[request.cache_key][1],
                    }
                    for request in chunk
                ]
                session.add(
                    AnalysisBatchWave(
                        id=str(uuid4()),
                        run_id=run_id,
                        phase=phase,
                        state="prepared",
                        requests=records,
                        provider_data={
                            "total": len(records),
                            "offers": len(
                                {item_id for record in records for item_id in record["item_ids"]}
                            ),
                        },
                    )
                )
            run.updated_at = _now()

    def _prune_prepared(self, wave_id):
        """Revalidate before upload and again immediately before paid submission."""
        with write_session_scope() as session:
            wave = session.get(AnalysisBatchWave, wave_id)
            run = session.get(AnalysisBatchRun, wave.run_id)
            records = []
            for record in wave.requests:
                item_ids = [
                    item_id
                    for item_id in record["item_ids"]
                    if (item := session.get(AnalysisBatchItem, item_id)) is not None
                    and current_job(session, item, run) is not None
                ]
                if item_ids:
                    records.append({**record, "item_ids": item_ids})
            if run.cancel_requested:
                records = []
            changed = records != wave.requests
            wave.requests = records
            wave.provider_data = {
                "total": len(records),
                "offers": len({item_id for record in records for item_id in record["item_ids"]}),
            }
            if changed:
                wave.input_file_id = None
            if not records:
                wave.state = "imported"
            return records, wave.input_file_id, changed

    def _submit(self, wave_id: str) -> bool:
        records, input_file_id, _ = self._prune_prepared(wave_id)
        if not records:
            return False
        requests = [
            BatchRequest(record["custom_id"], record["body"], record["cache_key"])
            for record in records
        ]
        with session_scope() as session:
            wave = session.get(AnalysisBatchWave, wave_id)
            run = session.get(AnalysisBatchRun, wave.run_id)
            provider, _ = phase_route(run, wave.phase)
        transport = self._transport(provider)
        if not input_file_id:
            try:
                input_file_id = transport.upload(encode_requests(requests))
            except Exception as exc:
                if wave.phase == "screening":
                    self._screening_wave_failed(wave_id, exc)
                    return False
                status_code = getattr(exc, "status_code", None) or getattr(
                    exc.__cause__, "status_code", None
                )
                if status_code in {401, 403}:
                    with write_session_scope() as session:
                        wave = session.get(AnalysisBatchWave, wave_id)
                        run = session.get(AnalysisBatchRun, wave.run_id)
                        run.state = "blocked"
                        run.last_error = _upload_access_message(str(exc), provider)
                        run.updated_at = _now()
                    return False
                raise
        records_after_upload, _, changed = self._prune_prepared(wave_id)
        if not records_after_upload:
            return False
        if changed:
            # Rebuild on the next poll; the obsolete upload has no inference cost.
            return False
        with write_session_scope() as session:
            wave = session.get(AnalysisBatchWave, wave_id)
            wave.input_file_id = input_file_id
            wave.state = "submitting"
            metadata = {"elan_run_id": wave.run_id, "elan_wave_id": wave.id}
        try:
            snapshot = (
                transport.create(input_file_id, metadata=metadata, requests=requests)
                if getattr(transport, "requires_journal", False)
                else transport.create(input_file_id, metadata=metadata)
            )
        except Exception as exc:
            if wave.phase == "screening":
                # A failed/ambiguous prefilter is fail-open. Keep the intent in
                # the journal and never resubmit it, even if POST was accepted.
                self._screening_wave_failed(wave_id, exc)
                return False
            status_code = getattr(exc, "status_code", None) or getattr(
                exc.__cause__, "status_code", None
            )
            if status_code in {400, 401, 403, 404, 422, 429}:
                with write_session_scope() as session:
                    wave = session.get(AnalysisBatchWave, wave_id)
                    wave.state = "imported"
                    wave.provider_data = {
                        "offers": wave.provider_data.get("offers", 0),
                        "status": "failed",
                        "total": len(wave.requests),
                        "completed": 0,
                        "failed": len(wave.requests),
                    }
                    wave.last_error = str(exc)
                    for record in wave.requests:
                        for item_id in record["item_ids"]:
                            item = session.get(AnalysisBatchItem, item_id)
                            item.state = f"{wave.phase}_failed"
                            item.error = str(exc)
                return False
            # An exception does not establish whether the provider accepted POST.
            with write_session_scope() as session:
                wave = session.get(AnalysisBatchWave, wave_id)
                run = session.get(AnalysisBatchRun, wave.run_id)
                run.state = "attention"
                run.last_error = (
                    "Envoi à vérifier : Élan recherche le lot chez le fournisseur avant toute reprise. "
                    "Aucun nouvel envoi automatique ne sera effectué."
                )
            raise
        self._save_snapshot(wave_id, snapshot)
        return True

    def _save_snapshot(self, wave_id, snapshot):
        with write_session_scope() as session:
            wave = session.get(AnalysisBatchWave, wave_id)
            wave.provider_batch_id = snapshot.id
            wave.provider_data = {
                **asdict(snapshot),
                "offers": wave.provider_data.get("offers", 0),
            }
            wave.state = snapshot.status
            run = session.get(AnalysisBatchRun, wave.run_id)
            run.state = "active"
            run.last_error = ""
            run.updated_at = _now()

    def _refresh_wave(self, wave_id: str, *, cancelling: bool) -> bool:
        with session_scope() as session:
            wave = session.get(AnalysisBatchWave, wave_id)
            run = session.get(AnalysisBatchRun, wave.run_id)
            provider, _ = phase_route(run, wave.phase)
        transport = self._transport(provider)
        metadata = {"elan_run_id": wave.run_id, "elan_wave_id": wave.id}
        if not wave.provider_batch_id:
            if getattr(transport, "requires_journal", False):
                requests = [
                    BatchRequest(record["custom_id"], record["body"], record["cache_key"])
                    for record in wave.requests
                ]
                matches = transport.recover(
                    metadata, requests=requests, created_after=wave.created_at
                )
            else:
                matches = transport.recover(metadata)
            if len(matches) != 1:
                with write_session_scope() as session:
                    run = session.get(AnalysisBatchRun, wave.run_id)
                    run.state = "attention"
                    run.last_error = (
                        "Envoi à vérifier : aucun lot retrouvé pour l’instant. "
                        "Le suivi réessaiera sans renvoyer les demandes."
                        + (
                            " Chez Anthropic, l’identification attend les résultats du lot."
                            if provider == "anthropic"
                            else ""
                        )
                        if not matches
                        else "Plusieurs lots correspondent à cet envoi. Vérification nécessaire."
                    )
                return False
            snapshot = matches[0]
        else:
            snapshot = transport.retrieve(wave.provider_batch_id)
        self._save_snapshot(wave_id, snapshot)
        if cancelling and not snapshot.terminal and snapshot.status != "cancelling":
            snapshot = transport.cancel(snapshot.id)
            self._save_snapshot(wave_id, snapshot)
        if not snapshot.terminal:
            return False
        results = {}
        for file_id in (snapshot.output_file_id, snapshot.error_file_id):
            if file_id:
                downloaded = (
                    transport.download(file_id, metadata=metadata)
                    if getattr(transport, "requires_journal", False)
                    else transport.download(file_id)
                )
                if results.keys() & downloaded.keys():
                    raise ValueError("Une réponse apparaît dans deux fichiers Batch.")
                results.update(downloaded)
        with write_session_scope() as session:
            wave = session.get(AnalysisBatchWave, wave_id)
            run = session.get(AnalysisBatchRun, wave.run_id)
            expected_ids = {request["custom_id"] for request in wave.requests}
            if results.keys() - expected_ids:
                raise ValueError("Le fichier Batch contient des identifiants étrangers au lot.")
            consumed = set(wave.consumed_ids)
            for request in wave.requests:
                custom_id = request["custom_id"]
                if custom_id in consumed:
                    continue
                result = results.get(custom_id)
                if result is not None:
                    import_result(session, run, wave.phase, request, result)
                else:
                    for item_id in request["item_ids"]:
                        item = session.get(AnalysisBatchItem, item_id)
                        error = f"Batch {snapshot.status} : aucune réponse disponible. " + str(
                            snapshot.errors or ""
                        )
                        if wave.phase == "screening":
                            fail_open_screening(session, item, run, error, key=request["cache_key"])
                        else:
                            item.state = f"{wave.phase}_failed"
                            item.error = error
                consumed.add(custom_id)
            wave.consumed_ids = sorted(consumed)
            wave.state = "imported"
            run.updated_at = _now()
        return True

    def _abandon_prepared(self, wave_id):
        with write_session_scope() as session:
            wave = session.get(AnalysisBatchWave, wave_id)
            wave.state = "imported"

    def _screening_wave_failed(self, wave_id, error):
        with write_session_scope() as session:
            wave = session.get(AnalysisBatchWave, wave_id)
            run = session.get(AnalysisBatchRun, wave.run_id)
            for record in wave.requests:
                for item_id in record["item_ids"]:
                    fail_open_screening(
                        session,
                        session.get(AnalysisBatchItem, item_id),
                        run,
                        error,
                        key=record["cache_key"],
                    )
            wave.state = "imported"
            wave.last_error = str(error)
            wave.consumed_ids = [record["custom_id"] for record in wave.requests]

    def _finish_cancel(self, run_id):
        with write_session_scope() as session:
            run = session.get(AnalysisBatchRun, run_id)
            for item in _items(session, run_id):
                if item.state in PENDING_ITEM_STATES:
                    item.state = "cancelled"
                    item.error = "Traitement annulé."
            run.state = "cancelled"
            run.completed_at = run.updated_at = _now()

    def _finish(self, run_id):
        with write_session_scope() as session:
            run = session.get(AnalysisBatchRun, run_id)
            items = _items(session, run_id)
            success = sum(item.state == "completed" for item in items)
            screened_out = sum(item.state in {"filtered_after_screening"} for item in items)
            finished = success + screened_out
            run.state = (
                "completed" if finished == len(items) else "partial" if finished else "failed"
            )
            run.completed_at = run.updated_at = _now()
            run.last_error = ""
