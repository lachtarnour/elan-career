"""Checkpoint each structured call while replaying the unchanged Python pipeline.

Replaying saved responses reexecutes assembly, normalization and validators. A
Batch run suspends at the first missing response; no Python step is bypassed.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date

from pydantic import ValidationError
from sqlalchemy import select

from smartapply.config import LLMSettingsSnapshot, get_llm_settings
from smartapply.database import session_scope, write_session_scope
from smartapply.database.generation_runs import GenerationCall
from smartapply.llm.batch import build_batch_request
from smartapply.llm.provider import LLMValidationError
from smartapply.llm.routes import frozen_provider
from smartapply.llm_policy import configuration_purpose
from smartapply.pipeline.errors import StaleTaskResultError
from smartapply.pipeline.snapshots import check_cancelled

PURPOSES = (
    "cv_adaptation", "cv_selection_correction", "application_texts",
    "document_review", "document_correction", "motivation_letter_repair",
)
LABELS = {
    "cv_adaptation": "Sélection du CV",
    "cv_selection_correction": "Correction de la sélection",
    "application_texts": "Résumé et lettre",
    "document_review": "Relecture des documents",
    "document_correction": "Correction des documents",
    "motivation_letter_repair": "Correction de la lettre",
    "rendering": "Validation et rendu Python",
}


class GenerationPending(RuntimeError):
    """A validated pipeline needs a result that has not arrived yet."""


class GenerationCallFailed(RuntimeError):
    """Stop before publication so a failed document call can be resumed."""


def request_digest(provider, body):
    return hashlib.sha256(json.dumps(
        {"provider": provider, "body": body}, sort_keys=True,
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def frozen_settings(routes):
    calls = {
        configuration_purpose(purpose): {
            **route, "reasoning": route["reasoning"] or "auto",
        }
        for purpose, route in routes.items()
    }
    return LLMSettingsSnapshot(get_llm_settings().runtime, calls)


class ReplayProvider:
    """One cursor per offer, shared across routed providers and review branches."""

    def __init__(self, *, item_id, mode, routes, reference_date, provider,
                 validate, stop_requested=None, progress=None):
        self.item_id = item_id
        self.mode = mode
        self.routes = routes
        self.reference_date = date.fromisoformat(reference_date)
        self._provider = provider
        self._settings = frozen_settings(routes)
        self._validate = validate
        self._stop_requested = stop_requested
        self._progress = progress
        self.position = 0

    def for_purpose(self, _purpose):
        return self

    def complete_json(self, **kwargs):
        check_cancelled(self._stop_requested)
        self._validate()
        purpose = kwargs["purpose"]
        route = self.routes[purpose]
        if self._progress:
            self._progress(LABELS[purpose] + "…")
        request = build_batch_request(
            custom_id="checkpoint", system=kwargs["system"], user=kwargs["user"],
            schema=kwargs["schema"], model=route["model"],
            max_completion_tokens=route["max_output_tokens"], purpose=purpose,
            temperature=kwargs.get("temperature", 0.2), provider=route["provider"],
            effort=route["reasoning"],
        )
        key = request_digest(route["provider"], request.body)
        position = self.position
        self.position += 1
        with write_session_scope() as session:
            call = session.scalar(select(GenerationCall).where(
                GenerationCall.item_id == self.item_id, GenerationCall.position == position,
            ))
            if call is None:
                call = GenerationCall(
                    item_id=self.item_id, position=position, purpose=purpose,
                    schema_name=kwargs["schema"].__name__, provider=route["provider"],
                    request_body=request.body, request_key=key, state="prepared",
                )
                session.add(call)
                session.flush()
            elif (call.request_key != key or call.schema_name != kwargs["schema"].__name__
                  or call.provider != route["provider"] or call.purpose != purpose
                  or request_digest(call.provider, call.request_body) != call.request_key):
                raise StaleTaskResultError()
            call_id, state, response = call.id, call.state, call.response_data
        if state == "completed":
            return kwargs["schema"].model_validate(response)
        if self.mode == "batch" and state in {"uncertain", "submitting", "submitted"}:
            raise GenerationPending()
        if state in {"failed", "uncertain", "submitting"}:
            raise GenerationCallFailed(
                "Envoi à vérifier ; aucun renvoi automatique."
                if state in {"uncertain", "submitting"}
                else "Appel en échec ; reprenez le parcours pour cette étape."
            )
        if self.mode == "batch":
            raise GenerationPending()
        # Commit intent BEFORE the synchronous call. A process lost here cannot
        # know whether the request was paid, so it must never resubmit itself.
        self._validate()
        check_cancelled(self._stop_requested)
        try:
            provider = frozen_provider(
                self._provider, route["provider"], route["model"], purpose=purpose,
                reasoning=route["reasoning"] or "auto",
                max_output_tokens=route["max_output_tokens"],
            )
            provider._disable_ambiguous_retries = True
            prepare_client = getattr(provider, "_client_lazy", None)
            if prepare_client is not None:
                prepare_client()
        except Exception as exc:
            # Credentials/configuration failed locally, before an inference POST.
            with write_session_scope() as session:
                call = session.get(GenerationCall, call_id)
                call.state = "failed"
                call.error = "Configuration du fournisseur à vérifier. Aucun appel envoyé."
            raise GenerationCallFailed("Configuration du fournisseur à vérifier ; reprise possible.") from exc
        self._validate()
        check_cancelled(self._stop_requested)
        with write_session_scope() as session:
            call = session.get(GenerationCall, call_id)
            call.state = "submitting"
            call.attempt += 1
        try:
            # The journal owns exact reuse, including the output budget. Avoid
            # depending on the separate response cache or its expiration.
            result = provider.complete_json(**{
                **kwargs, "model": route["model"], "effort": route["reasoning"],
                "max_completion_tokens": route["max_output_tokens"],
                "use_cache": False, "refresh_cache": False,
            })
            validated = kwargs["schema"].model_validate(result.model_dump(mode="json"))
        except Exception as exc:
            cause = exc
            known = isinstance(exc, (LLMValidationError, ValidationError))
            while cause is not None:
                known = known or getattr(cause, "status_code", None) is not None
                cause = cause.__cause__
            with write_session_scope() as session:
                call = session.get(GenerationCall, call_id)
                call.state = "failed" if known else "uncertain"
                call.error = "Réponse invalide ou refusée." if known else "Envoi à vérifier."
            raise GenerationCallFailed(
                "Appel en échec ; reprise possible." if known
                else "Envoi à vérifier ; aucun renvoi automatique."
            ) from exc
        # Save before cancellation, so Stop never discards a paid response.
        with write_session_scope() as session:
            call = session.get(GenerationCall, call_id)
            call.response_data = validated.model_dump(mode="json")
            call.state = "completed"
            call.error = ""
        check_cancelled(self._stop_requested)
        return validated


def call_states(item_id):
    with session_scope() as session:
        return list(session.scalars(select(GenerationCall.state).where(
            GenerationCall.item_id == item_id,
        )))
