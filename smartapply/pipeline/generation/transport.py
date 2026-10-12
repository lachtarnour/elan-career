"""Native Batch submissions for durable, replayable document generation calls.

Submission intent precedes every inference POST. Unknown outcomes are recovered
through the provider's existing journal protocol and never automatically resent.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import load_only

from smartapply.config import get_llm_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.generation_runs import (
    GenerationCall,
    GenerationItem,
    GenerationRun,
    GenerationWave,
)
from smartapply.database.models import Application, Job, JobStatus
from smartapply.database.repository import pending_duplicate_for_group
from smartapply.database.repository.llm_cache import record_usage
from smartapply.llm.batch import BatchRequest, encode_requests, get_batch_transport, split_requests
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.schemas import (
    ApplicationTexts,
    CVSelection,
    DocumentTextReview,
    MotivationLetterRepair,
)
from smartapply.llm.usage import estimate_cost_usd
from smartapply.pipeline.generation.locking import generation_operation
from smartapply.pipeline.snapshots import _digest, job_is_active, job_snapshot_key

OUTSTANDING_WAVE_STATES = ("prepared", "submitting", "submission_unknown", "submitted")
SCHEMAS = {
    model.__name__: model
    for model in (CVSelection, ApplicationTexts, DocumentTextReview, MotivationLetterRepair)
}
PURPOSE_SCHEMAS = {
    "cv_adaptation": CVSelection,
    "cv_selection_correction": CVSelection,
    "application_texts": ApplicationTexts,
    "document_review": DocumentTextReview,
    "document_correction": DocumentTextReview,
    "motivation_letter_repair": MotivationLetterRepair,
}


def _now():
    return datetime.now(timezone.utc)


def _status_code(exc):
    return getattr(exc, "status_code", None) or getattr(exc.__cause__, "status_code", None)


def _validate(call, body, result):
    schema = SCHEMAS.get(call.schema_name)
    stored = body.get("response_format", {}).get("json_schema", {})
    if (
        schema is None
        or PURPOSE_SCHEMAS.get(call.purpose) is not schema
        or stored.get("name") != call.schema_name
        or not stored.get("schema")
    ):
        raise LLMValidationError("Schéma de réponse absent ou incompatible dans la création.")
    validated = result.validate(schema)
    try:
        Draft202012Validator(stored["schema"]).validate(json.loads(result.content))
    except Exception as exc:
        raise LLMValidationError(f"Réponse incompatible avec le schéma enregistré : {exc}") from exc
    return validated.model_dump(mode="json")


class GenerationBatchTransport:
    def __init__(self, *, transport=None):
        self.settings = get_llm_settings()
        self.transport = transport
        self._transports = {}

    def _transport(self, provider):
        if provider not in {"openai", "anthropic"}:
            raise ValueError("Le traitement Batch nécessite OpenAI ou Anthropic.")
        if self.transport is not None:
            return self.transport
        if provider not in self._transports:
            self._transports[provider] = get_batch_transport(provider, settings=self.settings)
        return self._transports[provider]

    def outstanding(self, run_id: str) -> bool:
        with session_scope() as session:
            return session.scalar(
                select(GenerationWave.id).where(
                    GenerationWave.run_id == run_id,
                    GenerationWave.state.in_(OUTSTANDING_WAVE_STATES),
                ).limit(1)
            ) is not None

    @staticmethod
    def _record_stop(run, stop_requested) -> bool:
        if stop_requested is not None and stop_requested() and not run.cancel_requested:
            run.cancel_requested = True
            run.updated_at = _now()
        return bool(run.cancel_requested)

    def submit_pending(self, run_id: str, *, stop_requested=None) -> None:
        with generation_operation():
            with write_session_scope() as session:
                run = session.get(GenerationRun, run_id, options=[load_only(
                    GenerationRun.state, GenerationRun.cancel_requested, GenerationRun.updated_at,
                )])
                if run is None or self._record_stop(run, stop_requested) or run.state != "active":
                    return
                calls = list(session.scalars(
                    select(GenerationCall)
                    .join(GenerationItem, GenerationItem.id == GenerationCall.item_id)
                    .where(
                        GenerationItem.run_id == run_id,
                        GenerationItem.state.in_(("pending", "waiting")),
                        GenerationCall.state == "prepared",
                        GenerationCall.wave_id.is_(None),
                    )
                    .order_by(GenerationCall.id)
                ))
                groups = defaultdict(list)
                for call in calls:
                    if call.provider not in {"openai", "anthropic"}:
                        raise ValueError("Fournisseur Batch invalide dans la création.")
                    call.attempt += 1
                    groups[(call.provider, call.request_body.get("model"))].append(call)
                for (provider, _), group in groups.items():
                    by_id = {
                        f"c{call.id}a{call.attempt}": call for call in group
                    }
                    requests = [
                        BatchRequest(custom_id, call.request_body, call.request_key)
                        for custom_id, call in by_id.items()
                    ]
                    for chunk in split_requests(requests):
                        wave_id = str(uuid4())
                        records = []
                        for request in chunk:
                            call = by_id[request.custom_id]
                            records.append({
                                "custom_id": request.custom_id,
                                "body": request.body,
                                "cache_key": request.cache_key,
                                "call_id": call.id,
                            })
                        session.add(GenerationWave(
                            id=wave_id, run_id=run_id, provider=provider,
                            state="prepared", requests=records,
                            provider_data={"total": len(records)},
                        ))
                        # Existing call rows are updated before unrelated ORM
                        # inserts unless the FK target has explicitly flushed.
                        session.flush()
                        for request in chunk:
                            by_id[request.custom_id].wave_id = wave_id
                if calls:
                    run.updated_at = _now()
            for wave_id in self._wave_ids(run_id, states=("prepared",)):
                if not self._submit(wave_id, stop_requested=stop_requested):
                    break

    def _wave_ids(self, run_id, *, states=OUTSTANDING_WAVE_STATES):
        with session_scope() as session:
            return list(session.scalars(
                select(GenerationWave.id).where(
                    GenerationWave.run_id == run_id, GenerationWave.state.in_(states)
                ).order_by(GenerationWave.created_at, GenerationWave.id)
            ))

    def sync(self, run_id: str, cancelling: bool = False, *, stop_requested=None) -> None:
        with generation_operation():
            for wave_id in self._wave_ids(run_id):
                with write_session_scope() as session:
                    state = session.scalar(select(GenerationWave.state).where(GenerationWave.id == wave_id))
                    run = session.get(GenerationRun, run_id, options=[load_only(
                        GenerationRun.state, GenerationRun.cancel_requested, GenerationRun.updated_at,
                    )])
                    stopping = self._record_stop(run, stop_requested) or cancelling
                    blocked = run.state == "blocked"
                if state == "prepared":
                    if stopping:
                        self._cancel_prepared(wave_id)
                        continue
                    if blocked:
                        continue
                    if not self._submit(wave_id, stop_requested=stop_requested):
                        continue
                with write_session_scope() as session:
                    stopping = self._record_stop(
                        session.get(GenerationRun, run_id, options=[load_only(
                            GenerationRun.cancel_requested, GenerationRun.updated_at,
                        )]), stop_requested,
                    ) or cancelling
                self._refresh(wave_id, cancelling=stopping)

    def _requests(self, records):
        return [
            BatchRequest(record["custom_id"], record["body"], record["cache_key"])
            for record in records
        ]

    def _prune_prepared(self, wave_id):
        """Recheck local input and reservations on both sides of the upload."""
        with write_session_scope() as session:
            wave = session.get(GenerationWave, wave_id)
            records = []
            for record in wave.requests:
                call = session.get(GenerationCall, record["call_id"])
                item = session.get(GenerationItem, call.item_id)
                job = session.get(Job, item.job_id)
                application = (
                    session.get(Application, item.application_id)
                    if item.application_id is not None else job.application if job else None
                )
                current = (
                    item.state in {"pending", "waiting"}
                    and job is not None and job_is_active(job) and job.analysis is not None
                    and job_snapshot_key(job) == item.job_snapshot
                    and _digest({column.name: getattr(job.analysis, column.name)
                                 for column in job.analysis.__table__.columns}) == item.analysis_snapshot
                    and pending_duplicate_for_group(session, item.job_id) is None
                    and (
                        application is None
                        or (application.id == item.application_id
                            and application.job_id == item.job_id
                            and application.status == JobStatus.ANALYZED
                            and application.form_submitted_at is None
                            and not application.cv_json and not application.documents
                            and not application.cv_docx_path and not application.cv_pdf_path)
                    )
                )
                if current:
                    records.append(record)
                else:
                    if item.state in {"pending", "waiting"}:
                        item.state = "stale"
                        item.error = "Offre, analyse ou candidature modifiée."
                    call.state = "failed"
                    call.error = "Offre, analyse ou candidature modifiée."
            changed = wave.requests != records
            if changed:
                wave.requests = records
                wave.input_file_id = None
                wave.provider_data = {"total": len(records)}
            if not records:
                wave.state = "completed"
                wave.provider_data = {"total": 0, "status": "skipped"}
            return records, wave.input_file_id, changed

    def _submit(self, wave_id, *, stop_requested=None):
        with write_session_scope() as session:
            wave = session.get(GenerationWave, wave_id)
            run = session.get(GenerationRun, wave.run_id)
            if wave.state != "prepared":
                return False
            stopping = self._record_stop(run, stop_requested)
        if stopping:
            self._cancel_prepared(wave_id)
            return False
        records, saved_input_file_id, _ = self._prune_prepared(wave_id)
        if not records:
            return False
        transport = self._transport(wave.provider)
        requests = self._requests(records)
        try:
            input_file_id = saved_input_file_id or transport.upload(encode_requests(requests))
        except Exception as exc:
            with write_session_scope() as session:
                run = session.get(GenerationRun, wave.run_id)
                run.last_error = "L’envoi Batch est interrompu. Les demandes sont conservées."
                if _status_code(exc) in {401, 403}:
                    run.state = "blocked"
                    run.last_error = "Accès au fournisseur requis. Vérifiez la clé, puis reprenez."
                run.updated_at = _now()
            raise
        records_after_upload, _, changed = self._prune_prepared(wave_id)
        if not records_after_upload or changed:
            # An obsolete upload has no inference cost. Rebuild it on resume.
            return False
        with write_session_scope() as session:
            current = session.get(GenerationWave, wave_id)
            run = session.get(GenerationRun, wave.run_id)
            if self._record_stop(run, stop_requested):
                cancel_after_upload = True
            else:
                cancel_after_upload = False
                current.input_file_id = input_file_id
                current.state = "submitting"
                for record in current.requests:
                    session.get(GenerationCall, record["call_id"]).state = "submitting"
                run.updated_at = _now()
        if cancel_after_upload:
            self._cancel_prepared(wave_id)
            return False
        metadata = {"elan_run_id": wave.run_id, "elan_wave_id": wave.id}
        try:
            snapshot = (
                transport.create(input_file_id, metadata=metadata, requests=requests)
                if getattr(transport, "requires_journal", False)
                else transport.create(input_file_id, metadata=metadata)
            )
        except Exception as exc:
            if _status_code(exc) in {400, 401, 403, 404, 422, 429}:
                self._fail_wave(wave_id, f"Envoi refusé par le fournisseur ({_status_code(exc)}).")
                return False
            with write_session_scope() as session:
                current = session.get(GenerationWave, wave_id)
                current.state = "submission_unknown"
                current.last_error = "La réponse à l’envoi du lot n’a pas été reçue."
                for record in current.requests:
                    session.get(GenerationCall, record["call_id"]).state = "uncertain"
                run = session.get(GenerationRun, current.run_id)
                run.state = "attention"
                run.last_error = (
                    "Envoi à vérifier : Élan recherche le lot avant toute reprise, "
                    "sans renvoyer les demandes."
                )
                run.updated_at = _now()
            return False
        self._save_snapshot(wave_id, snapshot)
        return True

    def _save_snapshot(self, wave_id, snapshot):
        with write_session_scope() as session:
            wave = session.get(GenerationWave, wave_id)
            changed = False
            if wave.provider_batch_id != snapshot.id:
                wave.provider_batch_id = snapshot.id
                changed = True
            data = asdict(snapshot)
            if wave.provider_data != data:
                wave.provider_data = data
                changed = True
            # Terminal provider outputs still need an atomic local import.
            if wave.state != "submitted":
                wave.state = "submitted"
                changed = True
            for record in wave.requests:
                call = session.get(GenerationCall, record["call_id"], options=[load_only(GenerationCall.state)])
                if record["custom_id"] not in wave.consumed_ids and call.state != "submitted":
                    call.state = "submitted"
                    changed = True
            run = session.get(GenerationRun, wave.run_id, options=[load_only(
                GenerationRun.state, GenerationRun.last_error, GenerationRun.updated_at,
            )])
            if run.state == "attention":
                another_unknown = session.scalar(select(GenerationWave.id).where(
                    GenerationWave.run_id == wave.run_id,
                    GenerationWave.id != wave.id,
                    GenerationWave.state.in_(("submitting", "submission_unknown")),
                ).limit(1))
                if not another_unknown:
                    run.state = "active"
                    if run.last_error:
                        run.last_error = ""
                    changed = True
            if changed:
                run.updated_at = _now()

    def _refresh(self, wave_id, *, cancelling):
        with session_scope() as session:
            wave = session.get(GenerationWave, wave_id, options=[load_only(
                GenerationWave.run_id, GenerationWave.provider, GenerationWave.provider_batch_id,
                GenerationWave.created_at,
            )])
            # Recovery must compare the exact durable requests. A simple status
            # GET needs no multi-megabyte prompt payload; terminal import below
            # independently reloads and validates the full journal.
            requests = self._requests(wave.requests) if not wave.provider_batch_id else None
        transport = self._transport(wave.provider)
        metadata = {"elan_run_id": wave.run_id, "elan_wave_id": wave.id}
        if not wave.provider_batch_id:
            matches = (
                transport.recover(
                    metadata, requests=requests, created_after=wave.created_at
                )
                if getattr(transport, "requires_journal", False)
                else transport.recover(metadata)
            )
            if len(matches) != 1:
                with write_session_scope() as session:
                    run = session.get(GenerationRun, wave.run_id, options=[load_only(
                        GenerationRun.state, GenerationRun.last_error, GenerationRun.updated_at,
                    )])
                    error = (
                        "Envoi à vérifier : aucun lot retrouvé pour l’instant. "
                        "Le suivi réessaiera sans renvoyer les demandes."
                        if not matches else "Plusieurs lots correspondent à cet envoi."
                    )
                    changed = False
                    if run.state != "attention":
                        run.state = "attention"
                        changed = True
                    if run.last_error != error:
                        run.last_error = error
                        changed = True
                    if changed:
                        run.updated_at = _now()
                return
            snapshot = matches[0]
        else:
            snapshot = transport.retrieve(wave.provider_batch_id)
        self._save_snapshot(wave_id, snapshot)
        if cancelling and not snapshot.terminal and snapshot.status != "cancelling":
            snapshot = transport.cancel(snapshot.id)
            self._save_snapshot(wave_id, snapshot)
        if not snapshot.terminal:
            return
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
            wave = session.get(GenerationWave, wave_id)
            expected = {record["custom_id"] for record in wave.requests}
            if results.keys() - expected:
                raise ValueError("Le fichier Batch contient des identifiants étrangers au lot.")
            consumed = set(wave.consumed_ids)
            for record in wave.requests:
                custom_id = record["custom_id"]
                if custom_id in consumed:
                    continue
                call = session.get(GenerationCall, record["call_id"])
                if (
                    call is None or call.wave_id != wave.id
                    or call.request_body != record["body"]
                    or call.request_key != record["cache_key"]
                ):
                    raise ValueError("La demande enregistrée a changé pendant le Batch.")
                result = results.get(custom_id)
                if result is not None and result.custom_id != custom_id:
                    raise ValueError("L’identifiant de la réponse Batch ne correspond pas à la demande.")
                if result is None:
                    call.state = "failed"
                    call.error = f"Batch {snapshot.status} : aucune réponse disponible."
                else:
                    self._import_result(session, wave, call, record, result)
                consumed.add(custom_id)
            wave.consumed_ids = sorted(consumed)
            wave.state = (
                "cancelled" if snapshot.status == "cancelled"
                else "failed" if snapshot.status in {"failed", "expired"}
                else "completed"
            )
            session.get(GenerationRun, wave.run_id).updated_at = _now()

    def _import_result(self, session, wave, call, record, result):
        item = session.get(GenerationItem, call.item_id)
        requested = record["body"]["model"]
        served = result.model or requested
        record_usage(
            session, purpose=call.purpose, model=served, requested_model=requested,
            reasoning_effort=record["body"].get(
                "reasoning_effort", record["body"].get("output_config", {}).get("effort")
            ),
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
            cached_prompt_tokens=result.cached_prompt_tokens,
            cache_write_prompt_tokens=result.cache_write_prompt_tokens,
            cost_usd=estimate_cost_usd(
                served, result.prompt_tokens, result.completion_tokens,
                cached_prompt_tokens=result.cached_prompt_tokens,
                cache_write_prompt_tokens=result.cache_write_prompt_tokens,
            ) * 0.5,
            job_id=item.job_id, cache_key=call.request_key, provider=wave.provider,
        )
        try:
            call.response_data = _validate(call, record["body"], result)
        except (ValueError, LLMValidationError):
            call.state = "failed"
            call.error = "Réponse invalide ou refusée. Reprenez cette étape."
        else:
            call.state = "completed"
            call.error = ""

    def _fail_wave(self, wave_id, error):
        with write_session_scope() as session:
            wave = session.get(GenerationWave, wave_id)
            wave.state = "failed"
            wave.last_error = error
            for record in wave.requests:
                call = session.get(GenerationCall, record["call_id"])
                call.state = "failed"
                call.error = error
            session.get(GenerationRun, wave.run_id).updated_at = _now()

    def _cancel_prepared(self, wave_id):
        with write_session_scope() as session:
            wave = session.get(GenerationWave, wave_id)
            wave.state = "cancelled"
            for record in wave.requests:
                call = session.get(GenerationCall, record["call_id"])
                call.state = "prepared"
                call.wave_id = None
            session.get(GenerationRun, wave.run_id).updated_at = _now()
