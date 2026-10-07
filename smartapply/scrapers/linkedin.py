"""LinkedIn Jobs scraper via the Apify ``valig/linkedin-jobs-scraper`` actor."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests

from smartapply.config import get_source_settings
from smartapply.logging_setup import get_logger
from smartapply.offers import RawJob, make_external_id
from smartapply.parsing.cleaner import normalize_whitespace, strip_html
from smartapply.scrapers.apify_recovery import (
    ApifyRecoveryJournal,
    ApifyRecoveryJournalError,
    ApifyStartReservedError,
    PendingApifyRun,
    search_pass_fingerprint,
)
from smartapply.scrapers.base import (
    Scraper,
    ScraperConfigError,
    ScraperError,
    ScraperStopError,
)
from smartapply.scrapers.serpapi import normalize_date_posted
from smartapply.utils.contracts import normalize_source_contract_type

logger = get_logger(__name__)


def _should_stop(stop_requested: Callable[[], bool] | None) -> bool:
    return bool(stop_requested and stop_requested())


APIFY_LINKEDIN_JOBS_URL = "https://api.apify.com/v2/actors/valig~linkedin-jobs-scraper/runs"
APIFY_RUN_URL = "https://api.apify.com/v2/actor-runs/{run_id}"
APIFY_DATASET_ITEMS_URL = "https://api.apify.com/v2/datasets/{dataset_id}/items"
_APIFY_ACTIVE_STATUSES = {"READY", "RUNNING", "TIMING-OUT", "ABORTING"}
_APIFY_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
_FATAL_APIFY_STATUS_MESSAGE_MARKERS = (
    "access denied",
    "authentication",
    "billing",
    "credit",
    "insufficient balance",
    "invalid token",
    "not authorized",
    "payment required",
    "permission denied",
    "quota",
    "rate limit",
    "subscription",
    "unauthorized",
)


@dataclass(frozen=True)
class _ApifyFetchResult:
    items: list[dict[str, Any]]
    terminal_error: ScraperError | None = None
    run_id: str | None = None
    dataset_id: str | None = None
    request_payload: dict[str, Any] | None = None
    dataset_ready: bool = False


class _RetryableApifyReadError(Exception):
    """A transient read failure for a run whose identifier is already known."""


class _ApifyStartUncertainError(ScraperStopError):
    """Do not retry this POST; independent searches can still proceed."""


class _ApifyRecoveryRequiredError(ScraperStopError):
    """A known run must be resumed later without creating another Actor run."""

    def __init__(self, message: str, *, run_id: str, dataset_id: str, phase: str):
        super().__init__(message)
        self.run_id = run_id
        self.dataset_id = dataset_id
        self.phase = phase


@dataclass
class _LinkedInSearchSession:
    query: str
    base_payload: dict[str, Any]
    contract_values: list[str]
    experience_passes: list[list[str]]
    remote_values: list[str]
    skip_job_ids: list[str]
    limit: int
    stop_requested: Callable[[], bool] | None
    pass_index: int = 0
    yielded: int = 0
    seen_external_ids: set[str] = field(default_factory=set)
    payloads: list[dict[str, Any]] = field(default_factory=list)
    recovery_runs: list[tuple[int, PendingApifyRun]] = field(default_factory=list)
    recovery_only: bool = False
    recovery_index: int = 0
    done: bool = False


@dataclass(frozen=True)
class _LinkedInSearchPassResult:
    raw_jobs: list[RawJob]
    error: ScraperError | None
    done: bool
    completed_run_ids: tuple[str, ...] = ()


_DATE_POSTED_TO_APIFY = {
    "any": None,
    "today": "r86400",
    # The actor schema only exposes 24h, week and month. Keep the app's
    # "3days" selector valid by widening it to the closest supported
    # LinkedIn filter instead of sending an out-of-schema value.
    "3days": "r604800",
    "week": "r604800",
    "month": "r2592000",
}
_DATE_POSTED_ALLOWED_RAW = {"", "r86400", "r604800", "r2592000"}
_URL_RE = re.compile(r"https?://[^\s\])>]+", flags=re.IGNORECASE)

_CONTRACT_TYPE_ALIASES = {
    "f": "F",
    "fulltime": "F",
    "full time": "F",
    "full-time": "F",
    "temps plein": "F",
    "p": "P",
    "parttime": "P",
    "part time": "P",
    "part-time": "P",
    "temps partiel": "P",
    "c": "C",
    "contract": "C",
    "contractor": "C",
    "t": "T",
    "temporary": "T",
    "temporaire": "T",
    "i": "I",
    "internship": "I",
    "stage": "I",
    "o": "O",
    "other": "O",
    "autre": "O",
}
_EXPERIENCE_LEVEL_ALIASES = {
    "1": "1",
    "internship": "1",
    "stage": "1",
    "2": "2",
    "entry": "2",
    "entry level": "2",
    "entry-level": "2",
    "junior": "2",
    "3": "3",
    "associate": "3",
    "4": "4",
    "mid senior": "4",
    "mid-senior": "4",
    "mid senior level": "4",
    "mid-senior level": "4",
    "senior": "4",
    "5": "5",
    "director": "5",
    "directeur": "5",
    "6": "6",
    "executive": "6",
    "cadre dirigeant": "6",
}
_REMOTE_ALIASES = {
    "1": "1",
    "onsite": "1",
    "on site": "1",
    "on-site": "1",
    "sur site": "1",
    "2": "2",
    "remote": "2",
    "teletravail": "2",
    "télétravail": "2",
    "3": "3",
    "hybrid": "3",
    "hybride": "3",
}


class LinkedInJobsScraper(Scraper):
    """Search LinkedIn jobs through Apify and map results to ``RawJob``."""

    name = "linkedin"

    def __init__(
        self,
        token: str | None = None,
        *,
        url: str = APIFY_LINKEDIN_JOBS_URL,
        http_timeout: int | None = None,
        poll_wait_seconds: int | None = None,
        max_concurrent_runs: int | None = None,
        read_max_attempts: int | None = None,
        read_failure_budget_seconds: int | None = None,
        recovery_path: Path | None = None,
    ):
        settings = get_source_settings()
        self.token = token or settings.apify_token
        self.url = url
        self.http_timeout = (
            http_timeout if http_timeout is not None else settings.linkedin_http_timeout
        )
        self.poll_wait_seconds = (
            poll_wait_seconds
            if poll_wait_seconds is not None
            else settings.linkedin_poll_wait_seconds
        )
        self.max_concurrent_runs = (
            max_concurrent_runs
            if max_concurrent_runs is not None
            else settings.linkedin_max_concurrent_runs
        )
        self.max_concurrent_runs = max(1, min(int(self.max_concurrent_runs), 3))
        self.read_max_attempts = (
            read_max_attempts
            if read_max_attempts is not None
            else settings.linkedin_read_max_attempts
        )
        self.read_failure_budget_seconds = (
            read_failure_budget_seconds
            if read_failure_budget_seconds is not None
            else settings.linkedin_read_failure_budget_seconds
        )
        journal_path = recovery_path or settings.cache_dir / "apify_pending_runs.json"
        self.recovery_journal = ApifyRecoveryJournal(journal_path)
        self._recovery_root = Path(journal_path).parent
        self._fresh_search = False
        self.default_contract_type = _csv_list(settings.linkedin_contract_type)
        self.default_experience_level = _csv_list(settings.linkedin_experience_level)
        self.default_remote = _csv_list(settings.linkedin_remote)
        self.default_date_posted = settings.linkedin_date_posted
        self.default_max_results = settings.linkedin_max_results
        self._thread_state = threading.local()

    @property
    def last_payloads(self) -> list[dict[str, Any]]:
        """Payload trace for the current thread only."""

        return getattr(self._thread_state, "last_payloads", [])

    @last_payloads.setter
    def last_payloads(self, value: list[dict[str, Any]]) -> None:
        self._thread_state.last_payloads = value

    @property
    def last_completed_run_ids(self) -> list[str]:
        """Datasets drained by ``search`` but not acknowledged as persisted."""

        return getattr(self._thread_state, "last_completed_run_ids", [])

    @last_completed_run_ids.setter
    def last_completed_run_ids(self, value: list[str]) -> None:
        self._thread_state.last_completed_run_ids = value

    def is_available(self) -> bool:
        return bool(self.token)

    def begin_fresh_search(self) -> None:
        """Give a user-requested fetch its own durable execution journal.

        The same title searched later can return newly published offers. Old
        reservations and datasets belong to earlier executions, so they must
        neither block this fetch nor replace its live search results.
        """

        self.recovery_journal = ApifyRecoveryJournal(
            self._recovery_root / "apify_searches" / f"{uuid4().hex}.json"
        )
        self._fresh_search = True

    def _collection_error(self, error: ScraperError | None) -> ScraperError | None:
        if self._fresh_search and isinstance(
            error, (_ApifyStartUncertainError, _ApifyRecoveryRequiredError)
        ):
            # Preserve the execution journal without stopping unrelated titles.
            return ScraperError(str(error))
        return error

    def _fetch(
        self,
        payload: dict[str, Any],
        *,
        stop_requested: Callable[[], bool] | None = None,
    ) -> _ApifyFetchResult:
        """Start one Actor run, track it by id, then retrieve its dataset.

        There is deliberately no overall client-side run timeout. Only the
        individual HTTP exchanges have a network timeout. Once a run id exists,
        status and dataset GETs are safe to repeat without creating another paid
        run. The initial POST is never retried because its outcome could be
        uncertain if the connection breaks.
        """

        try:
            pending = self.recovery_journal.claim_start(payload)
        except ApifyStartReservedError as exc:
            raise _ApifyStartUncertainError(
                "LinkedIn : un démarrage Apify identique est déjà en cours ou son "
                "résultat est incertain. Aucun second run n’a été lancé pour cette passe. "
                f"{_redact_secrets(_text(exc))}"
            ) from exc
        except ApifyRecoveryJournalError as exc:
            raise ScraperStopError(
                "LinkedIn : le journal de récupération Apify est inaccessible. "
                "Aucun nouveau run n’a été lancé afin d’éviter un doublon. "
                f"{_redact_secrets(_text(exc))}"
            ) from exc
        if pending is not None:
            return self._resume_known_run(pending, stop_requested=stop_requested)

        try:
            response = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
                params={
                    "restartOnError": "false",
                    # Apify documents this as the billing cap for
                    # pay-per-result Actors. Keep the Actor input limit too:
                    # maxItems does not guarantee the returned item count.
                    "maxItems": payload["limit"],
                },
                timeout=self.http_timeout,
            )
        except requests.Timeout:
            raise _ApifyStartUncertainError(
                "LinkedIn : le démarrage Apify n’a pas répondu. Un run peut avoir été "
                "créé ; son lancement reste enregistré dans le journal. "
                "Aucune relance automatique n’a été effectuée."
            ) from None
        except requests.RequestException:
            raise _ApifyStartUncertainError(
                "LinkedIn : connexion interrompue pendant le démarrage Apify. Un run "
                "peut avoir été créé ; son lancement reste enregistré dans le journal. "
                "Aucune relance automatique n’a été effectuée."
            ) from None
        if _is_apify_configuration_error(response):
            self._release_start_reservation(payload)
            raise ScraperConfigError(_apify_http_error_message(response, token=self.token))
        if response.status_code >= 400:
            if response.status_code == 429:
                self._release_start_reservation(payload)
                raise ScraperStopError(_apify_http_error_message(response, token=self.token))
            raise _ApifyStartUncertainError(_apify_http_error_message(response, token=self.token))
        try:
            data = response.json()
        except ValueError:
            raise _ApifyStartUncertainError(
                "LinkedIn : réponse de démarrage Apify invalide (JSON illisible). "
                "L’état du run est inconnu ; aucune relance automatique n’a été effectuée."
            ) from None
        run = data.get("data") if isinstance(data, dict) else None
        if not isinstance(run, dict):
            raise _ApifyStartUncertainError(
                "LinkedIn : réponse de démarrage Apify invalide (run attendu). "
                "L’état du run est inconnu ; aucune relance automatique n’a été effectuée."
            )
        run_id = _text(run.get("id"))
        dataset_id = _text(run.get("defaultDatasetId"))
        if not run_id or not dataset_id:
            raise _ApifyStartUncertainError(
                "LinkedIn : Apify n’a pas fourni l’identifiant du run ou de son dataset. "
                "Aucune relance automatique n’a été effectuée."
            )

        status = _text(run.get("status")).upper()
        try:
            self.recovery_journal.record_started(
                payload=payload,
                run_id=run_id,
                dataset_id=dataset_id,
                actor_status=status,
            )
        except ApifyRecoveryJournalError as exc:
            raise ScraperStopError(
                "LinkedIn : le run Apify a démarré, mais son identifiant n’a pas pu "
                f"être enregistré (run {run_id}, dataset {dataset_id}). Aucun autre "
                "run ne sera lancé ; conservez ces identifiants pour la récupération."
            ) from exc

        logger.info("LinkedIn Apify run started: run_id=%s", run_id)
        return self._finish_known_run(
            payload=payload,
            run=run,
            run_id=run_id,
            dataset_id=dataset_id,
            stop_requested=stop_requested,
        )

    def _release_start_reservation(self, payload: dict[str, Any]) -> None:
        """Best-effort cleanup after Apify definitively rejected a start."""

        try:
            self.recovery_journal.release_start(payload)
        except ApifyRecoveryJournalError as exc:
            logger.warning(
                "LinkedIn Apify start reservation cleanup failed: query=%r error=%s",
                payload.get("keywords"),
                _redact_secrets(_text(exc)),
            )

    def _resume_known_run(
        self,
        pending: PendingApifyRun,
        *,
        stop_requested: Callable[[], bool] | None,
    ) -> _ApifyFetchResult:
        """Resume an explicit journal entry; never fall back to a paid POST."""

        logger.warning(
            "LinkedIn Apify resuming recorded run without POST: run_id=%s dataset_id=%s",
            pending.run_id,
            pending.dataset_id,
        )
        status = pending.actor_status.upper()
        if status not in _APIFY_ACTIVE_STATUSES | _APIFY_TERMINAL_STATUSES:
            status = "RUNNING"
        run = {
            "id": pending.run_id,
            "defaultDatasetId": pending.dataset_id,
            "status": status,
        }
        # Older v1 records did not store terminal metadata. Refresh a failed
        # terminal run before allowing later cohorts to classify it as recoverable.
        if status != "SUCCEEDED" and status in _APIFY_TERMINAL_STATUSES:
            if pending.terminal_error_fatal is None:
                try:
                    run = self._get_run(pending.run_id, wait_seconds=0)
                except _RetryableApifyReadError as exc:
                    raise ScraperStopError(
                        "LinkedIn : la cause terminale du run Apify "
                        f"{pending.run_id} reste inaccessible. Aucun nouveau run n’a été lancé."
                    ) from exc
            else:
                run["statusMessage"] = pending.terminal_status_message or ""
        return self._finish_known_run(
            payload=pending.request_payload,
            run=run,
            run_id=pending.run_id,
            dataset_id=pending.dataset_id,
            stop_requested=stop_requested,
            dataset_already_ready=pending.state == "dataset_ready",
            terminal_error_fatal=pending.terminal_error_fatal,
        )

    def _finish_known_run(
        self,
        *,
        payload: dict[str, Any],
        run: dict[str, Any],
        run_id: str,
        dataset_id: str,
        stop_requested: Callable[[], bool] | None,
        dataset_already_ready: bool = False,
        terminal_error_fatal: bool | None = None,
    ) -> _ApifyFetchResult:
        """Finish or resume a recorded run without ever submitting another POST."""

        cancelled = False
        try:
            if not dataset_already_ready:
                run, cancelled = self._wait_for_run(
                    run,
                    run_id=run_id,
                    dataset_id=dataset_id,
                    stop_requested=stop_requested,
                )
        except ScraperError as tracking_error:
            self._record_recovery_failure(
                run_id=run_id,
                dataset_id=dataset_id,
                actor_status=_text(run.get("status")).upper(),
                phase=getattr(tracking_error, "phase", "status"),
                error=tracking_error,
            )
            # Preserve already-written items when a one-shot dataset read is
            # still available. The run remains recorded because it may not be
            # terminal yet; a later search resumes it by id instead of POSTing.
            try:
                partial_items = self._fetch_dataset_items(
                    dataset_id=dataset_id,
                    run_id=run_id,
                    stop_requested=lambda: True,
                )
            except ScraperError as dataset_error:
                logger.warning(
                    "LinkedIn Apify partial dataset recovery failed: "
                    "run_id=%s dataset_id=%s error=%s",
                    run_id,
                    dataset_id,
                    _redact_secrets(_text(dataset_error)),
                )
                raise tracking_error from dataset_error
            return _ApifyFetchResult(
                partial_items,
                tracking_error,
                run_id=run_id,
                dataset_id=dataset_id,
                request_payload=payload,
                dataset_ready=False,
            )
        dataset_id = _text(run.get("defaultDatasetId")) or dataset_id
        status = _text(run.get("status")).upper()
        status_message = _redact_secrets(_text(run.get("statusMessage")))
        fatal_terminal_error = (
            terminal_error_fatal
            if terminal_error_fatal is not None
            else _is_fatal_apify_status_message(status_message)
        )
        try:
            items = self._fetch_dataset_items(
                dataset_id=dataset_id,
                run_id=run_id,
                stop_requested=stop_requested,
            )
        except ScraperError as dataset_error:
            self._record_recovery_failure(
                run_id=run_id,
                dataset_id=dataset_id,
                actor_status=status,
                terminal_status_message=status_message,
                terminal_error_fatal=fatal_terminal_error,
                phase="dataset",
                error=dataset_error,
            )
            raise
        try:
            self.recovery_journal.mark_dataset_ready(
                run_id,
                dataset_id=dataset_id,
                actor_status=status,
                terminal_status_message=status_message,
                terminal_error_fatal=fatal_terminal_error,
            )
        except ApifyRecoveryJournalError as exc:
            raise ScraperStopError(
                "LinkedIn : le dataset a été reçu, mais son état de récupération "
                f"n’a pas pu être enregistré pour le run {run_id}. Aucun nouveau run "
                "ne sera lancé."
            ) from exc
        if status != "SUCCEEDED" and not cancelled:
            detail = f" Détail Apify : {status_message}" if status_message else ""
            error_type = ScraperStopError if fatal_terminal_error else ScraperError
            return _ApifyFetchResult(
                items,
                error_type(
                    f"LinkedIn : le run Apify {run_id} s’est terminé avec le statut "
                    f"{status or 'INCONNU'}.{detail}"
                ),
                run_id=run_id,
                dataset_id=dataset_id,
                request_payload=payload,
                dataset_ready=True,
            )
        return _ApifyFetchResult(
            items,
            run_id=run_id,
            dataset_id=dataset_id,
            request_payload=payload,
            dataset_ready=True,
        )

    def _record_recovery_failure(
        self,
        *,
        run_id: str,
        dataset_id: str,
        actor_status: str,
        phase: str,
        error: Exception,
        terminal_status_message: str | None = None,
        terminal_error_fatal: bool | None = None,
    ) -> None:
        try:
            self.recovery_journal.mark_recovery_required(
                run_id,
                dataset_id=dataset_id,
                actor_status=actor_status,
                phase=phase,
                error=_redact_secrets(_text(error)),
                terminal_status_message=terminal_status_message,
                terminal_error_fatal=terminal_error_fatal,
            )
        except ApifyRecoveryJournalError as journal_error:
            raise ScraperStopError(
                "LinkedIn : le run connu ne peut pas être inscrit pour récupération "
                f"(run {run_id}, dataset {dataset_id})."
            ) from journal_error

    def _wait_for_run(
        self,
        initial_run: dict[str, Any],
        *,
        run_id: str,
        dataset_id: str,
        stop_requested: Callable[[], bool] | None,
    ) -> tuple[dict[str, Any], bool]:
        run = initial_run
        cancelled = False
        read_attempts = 0
        first_failure_at: float | None = None
        cancellation_status_reads = 0
        cancellation_started_at: float | None = None
        while True:
            status = _text(run.get("status")).upper()
            if status in _APIFY_TERMINAL_STATUSES:
                return run, cancelled
            if status not in _APIFY_ACTIVE_STATUSES:
                raise ScraperStopError(
                    f"LinkedIn : statut Apify inattendu pour le run {run_id} : "
                    f"{status or 'vide'}. Le run reste enregistré pour récupération."
                )
            if not cancelled and _should_stop(stop_requested):
                cancelled = True
                cancellation_started_at = time.monotonic()
                aborted_run = self._abort_run(run_id)
                if aborted_run is not None:
                    run = aborted_run
                    continue
            attempt_started = time.monotonic()
            try:
                timeout = self._remaining_read_timeout(
                    cancellation_started_at if cancelled else first_failure_at,
                    run_id=run_id,
                    dataset_id=dataset_id,
                    phase="cancellation" if cancelled else "status",
                )
                run = self._get_run(
                    run_id,
                    wait_seconds=1 if cancelled else self.poll_wait_seconds,
                    timeout=timeout,
                )
                if cancelled:
                    cancellation_status_reads += 1
                    refreshed_status = _text(run.get("status")).upper()
                    if (
                        refreshed_status in _APIFY_ACTIVE_STATUSES
                        and cancellation_started_at is not None
                        and self._read_budget_exhausted(
                            cancellation_status_reads,
                            cancellation_started_at,
                        )
                    ):
                        raise _ApifyRecoveryRequiredError(
                            "LinkedIn : l’annulation du run Apify n’a pas pu être "
                            f"confirmée pour le run {run_id}. Le run et le dataset "
                            f"{dataset_id} restent enregistrés pour récupération.",
                            run_id=run_id,
                            dataset_id=dataset_id,
                            phase="cancellation",
                        )
                else:
                    read_attempts = 0
                    first_failure_at = None
            except _RetryableApifyReadError as exc:
                read_attempts += 1
                if first_failure_at is None:
                    first_failure_at = attempt_started
                logger.warning(
                    "LinkedIn Apify status read failed; retrying known run: "
                    "run_id=%s attempt=%d error=%s",
                    run_id,
                    read_attempts,
                    _redact_secrets(_text(exc)),
                )
                if cancelled:
                    raise ScraperError(
                        "LinkedIn : annulation demandée, mais l’état du run Apify "
                        f"{run_id} reste inaccessible. Vérifiez ce run dans Apify."
                    ) from exc
                if self._read_budget_exhausted(read_attempts, first_failure_at):
                    raise _ApifyRecoveryRequiredError(
                        "LinkedIn : les lectures du statut Apify ont échoué de façon "
                        f"répétée pour le run {run_id}. Le run et le dataset "
                        f"{dataset_id} sont enregistrés pour récupération, sans nouveau POST.",
                        run_id=run_id,
                        dataset_id=dataset_id,
                        phase="status",
                    ) from exc
                self._retry_pause(
                    read_attempts,
                    stop_requested=stop_requested,
                    failure_started_at=first_failure_at,
                )

    def _get_run(
        self,
        run_id: str,
        *,
        wait_seconds: int,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        url = APIFY_RUN_URL.format(run_id=run_id)
        timeout = float(self.http_timeout) if timeout is None else timeout
        # Do not deliberately request a server wait longer than the network
        # allowance, especially near the failure-series deadline.
        wait_seconds = min(wait_seconds, max(0, int(timeout) - 1))
        try:
            response = requests.get(
                url,
                headers={"Authorization": f"Bearer {self.token}"},
                params={"waitForFinish": wait_seconds},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise _RetryableApifyReadError(_request_error_summary(exc)) from exc
        if response.status_code == 429:
            raise ScraperStopError(_apify_http_error_message(response, token=self.token))
        if response.status_code == 408 or response.status_code >= 500:
            raise _RetryableApifyReadError(f"HTTP {response.status_code}")
        if _is_apify_configuration_error(response):
            raise ScraperConfigError(_apify_http_error_message(response, token=self.token))
        try:
            data = response.json()
        except ValueError:
            raise _RetryableApifyReadError("JSON illisible") from None
        run = data.get("data") if isinstance(data, dict) else None
        if isinstance(run, dict):
            return run
        raise _RetryableApifyReadError("réponse de statut invalide")

    def _fetch_dataset_items(
        self,
        *,
        dataset_id: str,
        run_id: str,
        stop_requested: Callable[[], bool] | None,
    ) -> list[dict[str, Any]]:
        url = APIFY_DATASET_ITEMS_URL.format(dataset_id=dataset_id)
        attempts = 0
        first_failure_at: float | None = None
        while True:
            timeout = self._remaining_read_timeout(
                first_failure_at,
                run_id=run_id,
                dataset_id=dataset_id,
                phase="dataset",
            )
            attempts += 1
            attempt_started = time.monotonic()
            try:
                response = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {self.token}"},
                    params={"clean": "true"},
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if first_failure_at is None:
                    first_failure_at = attempt_started
                logger.warning(
                    "LinkedIn Apify dataset read failed; retrying completed run: "
                    "run_id=%s dataset_id=%s attempt=%d error=%s",
                    run_id,
                    dataset_id,
                    attempts,
                    _request_error_summary(exc),
                )
                if _should_stop(stop_requested):
                    raise ScraperError(
                        "LinkedIn : lecture du dataset interrompue après annulation pour "
                        f"le run Apify {run_id}. Le dataset {dataset_id} reste consultable "
                        "dans Apify."
                    ) from exc
                if self._read_budget_exhausted(attempts, first_failure_at):
                    raise _ApifyRecoveryRequiredError(
                        "LinkedIn : les lectures du dataset Apify ont échoué de façon "
                        f"répétée pour le run {run_id}. Le dataset {dataset_id} reste "
                        "enregistré pour récupération, sans nouveau POST.",
                        run_id=run_id,
                        dataset_id=dataset_id,
                        phase="dataset",
                    ) from exc
                self._retry_pause(
                    attempts,
                    stop_requested=stop_requested,
                    failure_started_at=first_failure_at,
                )
                continue
            if response.status_code == 429:
                raise ScraperStopError(_apify_http_error_message(response, token=self.token))
            if response.status_code == 408 or response.status_code >= 500:
                if first_failure_at is None:
                    first_failure_at = attempt_started
                logger.warning(
                    "LinkedIn Apify dataset HTTP %s; retrying completed run: "
                    "run_id=%s dataset_id=%s attempt=%d",
                    response.status_code,
                    run_id,
                    dataset_id,
                    attempts,
                )
                if _should_stop(stop_requested):
                    raise ScraperError(
                        "LinkedIn : lecture du dataset interrompue après annulation pour "
                        f"le run Apify {run_id}. Le dataset {dataset_id} reste consultable "
                        "dans Apify."
                    )
                if self._read_budget_exhausted(attempts, first_failure_at):
                    raise _ApifyRecoveryRequiredError(
                        "LinkedIn : les lectures du dataset Apify ont échoué de façon "
                        f"répétée pour le run {run_id}. Le dataset {dataset_id} reste "
                        "enregistré pour récupération, sans nouveau POST.",
                        run_id=run_id,
                        dataset_id=dataset_id,
                        phase="dataset",
                    )
                self._retry_pause(
                    attempts,
                    stop_requested=stop_requested,
                    failure_started_at=first_failure_at,
                )
                continue
            if _is_apify_configuration_error(response):
                raise ScraperConfigError(_apify_http_error_message(response, token=self.token))
            try:
                data = response.json()
            except ValueError:
                read_error = "JSON illisible"
            else:
                read_error = None if isinstance(data, list) else "liste d’offres absente"
            if read_error is not None:
                if first_failure_at is None:
                    first_failure_at = attempt_started
                logger.warning(
                    "LinkedIn Apify dataset response invalid; retrying known dataset: "
                    "run_id=%s dataset_id=%s attempt=%d error=%s",
                    run_id,
                    dataset_id,
                    attempts,
                    read_error,
                )
                if _should_stop(stop_requested):
                    raise ScraperError(
                        "LinkedIn : lecture du dataset interrompue après annulation pour "
                        f"le run Apify {run_id}. Le dataset {dataset_id} reste consultable "
                        "dans Apify."
                    )
                if self._read_budget_exhausted(attempts, first_failure_at):
                    raise _ApifyRecoveryRequiredError(
                        "LinkedIn : les réponses du dataset Apify sont restées "
                        f"invalides pour le run {run_id}. Le dataset {dataset_id} reste "
                        "enregistré pour récupération, sans nouveau POST.",
                        run_id=run_id,
                        dataset_id=dataset_id,
                        phase="dataset",
                    )
                self._retry_pause(
                    attempts,
                    stop_requested=stop_requested,
                    failure_started_at=first_failure_at,
                )
                continue
            return [item for item in data if isinstance(item, dict)]

    def _abort_run(self, run_id: str) -> dict[str, Any] | None:
        try:
            response = requests.post(
                f"{APIFY_RUN_URL.format(run_id=run_id)}/abort",
                headers={"Authorization": f"Bearer {self.token}"},
                params={"gracefully": "false"},
                timeout=self.http_timeout,
            )
        except requests.RequestException as exc:
            logger.warning(
                "LinkedIn Apify abort request failed; run will still be tracked: "
                "run_id=%s error=%s",
                run_id,
                _request_error_summary(exc),
            )
            return None
        if response.status_code >= 400 and response.status_code != 409:
            logger.warning(
                "LinkedIn Apify abort returned HTTP %s; run will still be tracked: run_id=%s",
                response.status_code,
                run_id,
            )
            return None
        try:
            data = response.json()
        except ValueError:
            return None
        run = data.get("data") if isinstance(data, dict) else None
        return run if isinstance(run, dict) else None

    def _retry_pause(
        self,
        attempts: int,
        *,
        stop_requested: Callable[[], bool] | None = None,
        failure_started_at: float | None = None,
    ) -> None:
        delay = min(max(float(self.poll_wait_seconds), 0.1) * (2 ** min(attempts - 1, 3)), 15)
        if failure_started_at is not None:
            remaining_budget = (
                failure_started_at + float(self.read_failure_budget_seconds) - time.monotonic()
            )
            delay = min(delay, max(remaining_budget, 0.0))
        deadline = time.monotonic() + delay
        while not _should_stop(stop_requested):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.1))

    def _read_budget_exhausted(self, attempts: int, first_failure_at: float) -> bool:
        return attempts >= max(1, int(self.read_max_attempts)) or (
            time.monotonic() - first_failure_at >= max(1.0, float(self.read_failure_budget_seconds))
        )

    def _remaining_read_timeout(
        self,
        first_failure_at: float | None,
        *,
        run_id: str,
        dataset_id: str,
        phase: str,
    ) -> float:
        if first_failure_at is None:
            return float(self.http_timeout)
        remaining = (
            first_failure_at + max(1.0, float(self.read_failure_budget_seconds)) - time.monotonic()
        )
        if remaining <= 0:
            raise _ApifyRecoveryRequiredError(
                f"LinkedIn : le budget de lecture Apify est épuisé pour le run {run_id}. "
                f"Le dataset {dataset_id} reste enregistré pour récupération, sans nouveau POST.",
                run_id=run_id,
                dataset_id=dataset_id,
                phase=phase,
            )
        return min(float(self.http_timeout), remaining)

    def search(
        self,
        query: str,
        location: str | None = None,
        *,
        max_results: int | None = None,
        date_posted: str | None = None,
        contract_type: list[str] | tuple[str, ...] | str | None = None,
        experience_level: list[str] | tuple[str, ...] | str | None = None,
        remote: list[str] | tuple[str, ...] | str | None = None,
        url_path: str = "/jobs/search",
        stop_requested: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> Iterator[RawJob]:
        """Run one query serially; the collector uses pass-level cohorts instead.

        Draining the iterator does not prove that a downstream database commit
        succeeded. Completed run ids therefore remain in the recovery journal;
        direct consumers can acknowledge them with :meth:`mark_runs_persisted`.
        """

        session = self.create_search_session(
            query,
            location=location,
            max_results=max_results,
            date_posted=date_posted,
            contract_type=contract_type,
            experience_level=experience_level,
            remote=remote,
            url_path=url_path,
            stop_requested=stop_requested,
            **kwargs,
        )
        self.last_payloads = []
        self.last_completed_run_ids = []
        while not session.done:
            outcome = self.run_search_pass(session)
            self.last_payloads = list(session.payloads)
            if outcome.completed_run_ids:
                self.last_completed_run_ids = list(
                    dict.fromkeys((*self.last_completed_run_ids, *outcome.completed_run_ids))
                )
            yield from outcome.raw_jobs
            if outcome.error is not None:
                raise outcome.error

    def create_search_session(
        self,
        query: str,
        location: str | None = None,
        *,
        max_results: int | None = None,
        date_posted: str | None = None,
        contract_type: list[str] | tuple[str, ...] | str | None = None,
        experience_level: list[str] | tuple[str, ...] | str | None = None,
        remote: list[str] | tuple[str, ...] | str | None = None,
        url_path: str = "/jobs/search",
        stop_requested: Callable[[], bool] | None = None,
        **kwargs: Any,
    ) -> _LinkedInSearchSession:
        """Build mutable state for one query without starting an Actor run."""

        if not self.token:
            raise ScraperConfigError("APIFY_TOKEN is not set")
        limit = self._resolve_max_results(max_results)
        contract_values = _normalise_api_list(
            contract_type,
            default=self.default_contract_type,
            aliases=_CONTRACT_TYPE_ALIASES,
            field_name="contractType",
        )
        experience_values = _normalise_api_list(
            experience_level,
            default=self.default_experience_level,
            aliases=_EXPERIENCE_LEVEL_ALIASES,
            field_name="experienceLevel",
        )
        primary_experience, fallback_experience = _split_experience_levels(experience_values)
        remote_values = _normalise_api_list(
            remote,
            default=self.default_remote,
            aliases=_REMOTE_ALIASES,
            field_name="remote",
        )
        initial_skip_job_ids = [
            str(item).strip()
            for item in (kwargs.get("skip_job_id") or kwargs.get("skipJobId") or [])
            if str(item).strip()
        ]
        skip_job_ids = list(dict.fromkeys(initial_skip_job_ids))
        base_payload: dict[str, Any] = {
            "keywords": query.strip(),
            "location": (location or "").strip() or None,
            "datePosted": _to_apify_date_posted(
                date_posted if date_posted is not None else self.default_date_posted
            ),
            "urlPath": url_path,
        }

        experience_passes = [primary_experience]
        if fallback_experience:
            experience_passes.append(fallback_experience)
        recovery_runs: list[tuple[int, PendingApifyRun]] = []
        if limit > 0 and not _should_stop(stop_requested):
            try:
                pending_runs = self.recovery_journal.list_pending()
            except ApifyRecoveryJournalError as exc:
                raise ScraperStopError(
                    "LinkedIn : le journal de récupération Apify est inaccessible. "
                    "Aucun nouveau run n’a été lancé."
                ) from exc
            for index, experience_pass in enumerate(experience_passes):
                pass_payload = _clean_payload(
                    {
                        **base_payload,
                        "urlParam": _search_url_params(
                            contract_values,
                            experience_pass,
                            remote_values,
                        ),
                    }
                )
                identity = search_pass_fingerprint(pass_payload)
                recovery_runs.extend(
                    (index, pending)
                    for pending in sorted(
                        pending_runs,
                        key=lambda item: item.created_at,
                    )
                    if search_pass_fingerprint(pending.request_payload) == identity
                )
        return _LinkedInSearchSession(
            query=query,
            base_payload=base_payload,
            contract_values=contract_values,
            experience_passes=experience_passes,
            remote_values=remote_values,
            skip_job_ids=skip_job_ids,
            limit=limit,
            stop_requested=stop_requested,
            recovery_runs=recovery_runs,
            recovery_only=bool(recovery_runs),
            done=limit <= 0 or _should_stop(stop_requested),
        )

    def run_search_pass(
        self,
        session: _LinkedInSearchSession,
    ) -> _LinkedInSearchPassResult:
        """Execute at most one Actor run for a query session."""

        if session.done or _should_stop(session.stop_requested):
            session.done = True
            return _LinkedInSearchPassResult([], None, True)
        remaining = session.limit - session.yielded
        pending: PendingApifyRun | None = None
        if session.recovery_only:
            if session.recovery_index >= len(session.recovery_runs):
                session.done = True
                return _LinkedInSearchPassResult([], None, True)
            session.pass_index, pending = session.recovery_runs[session.recovery_index]
        elif remaining <= 0 or session.pass_index >= len(session.experience_passes):
            session.done = True
            return _LinkedInSearchPassResult([], None, True)

        experience_pass = session.experience_passes[session.pass_index]
        display_pass_index = session.pass_index + 1
        payload = (
            dict(pending.request_payload)
            if pending is not None
            else _clean_payload(
                {
                    **session.base_payload,
                    "urlParam": _search_url_params(
                        session.contract_values,
                        experience_pass,
                        session.remote_values,
                    ),
                    "limit": remaining,
                    "skipJobId": list(session.skip_job_ids),
                }
            )
        )
        session.payloads.append(dict(payload))
        request_started = time.monotonic()
        logger.info(
            "LinkedIn Apify request start: q=%r experience=%s limit=%s",
            payload.get("keywords"),
            experience_pass,
            payload.get("limit"),
        )
        try:
            fetched = (
                self._resume_known_run(pending, stop_requested=session.stop_requested)
                if pending is not None
                else self._fetch(payload, stop_requested=session.stop_requested)
            )
        except ScraperError as error:
            error = self._collection_error(error)
            session.done = True
            logger.error(
                "LinkedIn Apify request failed: q=%r experience=%s elapsed=%.1fs error=%s",
                payload.get("keywords"),
                experience_pass,
                time.monotonic() - request_started,
                _redact_secrets(_text(error)),
            )
            return _LinkedInSearchPassResult([], error, True)
        except Exception as error:
            session.done = True
            detail = (
                _request_error_summary(error)
                if isinstance(error, requests.RequestException)
                else _redact_secrets(_text(error))
            )
            logger.error(
                "LinkedIn Apify request failed unexpectedly: "
                "q=%r experience=%s elapsed=%.1fs error=%s",
                payload.get("keywords"),
                experience_pass,
                time.monotonic() - request_started,
                detail,
                exc_info=True,
            )
            raise

        if isinstance(fetched, _ApifyFetchResult):
            items = fetched.items
            terminal_error = self._collection_error(fetched.terminal_error)
            audit_payload = fetched.request_payload or payload
            completed_run_ids = (
                (fetched.run_id,) if fetched.dataset_ready and fetched.run_id else ()
            )
        else:
            # Keep lightweight test/custom scraper overrides compatible.
            items = fetched
            terminal_error = None
            audit_payload = payload
            completed_run_ids = ()

        logger.info(
            "LinkedIn Apify request done: q=%r experience=%s items=%d elapsed=%.1fs",
            audit_payload.get("keywords"),
            experience_pass,
            len(items),
            time.monotonic() - request_started,
        )

        raw_jobs: list[RawJob] = []
        skipped_experience_mismatch = 0
        for raw in items:
            raw_id = _text(raw.get("id"))
            raw_experience_code = _linkedin_experience_code(raw.get("experienceLevel"))
            if (
                experience_pass
                and raw_experience_code
                and raw_experience_code not in experience_pass
            ):
                skipped_experience_mismatch += 1
                continue
            if raw_id and raw_id not in session.skip_job_ids:
                session.skip_job_ids.append(raw_id)
            job = self._to_raw_job(raw)
            if job is None or job.external_id in session.seen_external_ids:
                continue
            session.seen_external_ids.add(job.external_id)
            source_data = dict(job.source_data or {})
            source_data["_smartapply_search"] = {
                "api_payload": audit_payload,
                "experience_pass": display_pass_index,
                "experience_fallback_used": display_pass_index > 1,
                "title": audit_payload.get("keywords"),
                "location": audit_payload.get("location"),
                "datePosted": audit_payload.get("datePosted"),
                "contractType": list(session.contract_values),
                "experienceLevel": list(experience_pass),
                "remote": list(session.remote_values),
                "limit": audit_payload.get("limit"),
            }
            raw_jobs.append(job.model_copy(update={"source_data": source_data}))
            session.yielded += 1

        session.pass_index += 1
        if session.recovery_only:
            session.recovery_index += 1
        if skipped_experience_mismatch:
            logger.info(
                "LinkedIn skipped %d item(s) outside experience pass %s",
                skipped_experience_mismatch,
                experience_pass,
            )
        session.done = bool(
            terminal_error is not None
            or _should_stop(session.stop_requested)
            or (
                session.recovery_index >= len(session.recovery_runs)
                if session.recovery_only
                else session.yielded >= session.limit
                or session.pass_index >= len(session.experience_passes)
            )
        )
        return _LinkedInSearchPassResult(
            raw_jobs,
            terminal_error,
            session.done,
            completed_run_ids,
        )

    def mark_runs_persisted(self, run_ids: tuple[str, ...] | list[str] | set[str]) -> None:
        """Forget datasets only after their mapped offers were handled locally."""

        try:
            self.recovery_journal.remove_many(run_ids)
        except ApifyRecoveryJournalError as exc:
            raise ScraperStopError(
                "LinkedIn : les offres ont été reçues, mais le journal Apify n’a pas "
                "pu être finalisé. Le prochain passage pourra relire le même dataset."
            ) from exc

    def _resolve_max_results(self, max_results: int | None) -> int:
        limit = self.default_max_results if max_results is None else max_results
        if limit > self.default_max_results:
            raise ScraperConfigError(
                f"LinkedIn max_results exceeds LINKEDIN_MAX_RESULTS ({self.default_max_results})."
            )
        return limit

    def _to_raw_job(self, raw: dict[str, Any]) -> RawJob | None:
        title = _text(raw.get("title"))
        company = _text(raw.get("companyName")) or "Entreprise non communiquée"
        if not title:
            return None

        linkedin_url = _url_text(raw.get("url"))
        apply_url = _url_text(raw.get("applyUrl"))
        application_url = apply_url or linkedin_url or None
        external_id = _external_id(self.name, raw, title=title, company=company)

        description = _description_text(raw)
        if not description:
            return None

        contract_type = normalize_source_contract_type(raw.get("contractType"))
        remote_policy = _normalize_remote_policy(raw.get("remote"), raw.get("workType"))
        published = _parse_datetime(raw.get("postedDate"))
        apply_options = _apply_options(raw)
        source_data = dict(raw)
        source_data["_smartapply_normalized"] = {
            "url": linkedin_url,
            "applyUrl": apply_url,
            "companyUrl": _url_text(raw.get("companyUrl")),
            "recruiterUrl": _url_text(raw.get("recruiterUrl")),
            "description_source": _description_source(raw),
        }

        return RawJob(
            external_id=external_id,
            title=title,
            company=company,
            location=_text(raw.get("location")) or None,
            contract_type=contract_type,
            remote_policy=remote_policy,
            description=description,
            application_url=application_url,
            apply_options=apply_options or None,
            published_date=published,
            source=self.name,
            source_data=source_data,
        )


def _to_apify_date_posted(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip().lower()
    if raw in _DATE_POSTED_ALLOWED_RAW:
        return raw
    normalized = normalize_date_posted(value)
    return _DATE_POSTED_TO_APIFY.get(normalized, value)


def _normalise_list(
    value: list[str] | tuple[str, ...] | str | None,
    *,
    default: list[str],
) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, str):
        return _csv_list(value)
    return [str(item).strip() for item in value if str(item).strip()]


def _normalise_api_list(
    value: list[str] | tuple[str, ...] | str | None,
    *,
    default: list[str],
    aliases: dict[str, str],
    field_name: str,
) -> list[str]:
    values = _normalise_list(value, default=default)
    normalized: list[str] = []
    for item in values:
        key = _selector_key(item)
        mapped = aliases.get(key)
        if not mapped:
            allowed = ", ".join(sorted(set(aliases.values())))
            raise ScraperConfigError(
                f"Invalid LinkedIn {field_name} selector {item!r}. Allowed API codes: {allowed}."
            )
        if mapped not in normalized:
            normalized.append(mapped)
    return normalized


def _selector_key(value: Any) -> str:
    text = " ".join(str(value or "").split()).strip().lower()
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def _linkedin_experience_code(value: Any) -> str | None:
    return _EXPERIENCE_LEVEL_ALIASES.get(_selector_key(value))


def _split_experience_levels(values: list[str]) -> tuple[list[str], list[str]]:
    if "4" not in values or len(values) <= 1:
        return values, []
    primary = [value for value in values if value != "4"]
    fallback = ["4"]
    return primary, fallback


def _clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in ("", None, [])}


def _search_url_params(
    contract_type: list[str], experience_level: list[str], remote: list[str]
) -> list[dict[str, str]]:
    """Send legacy LinkedIn selectors through the Actor's supported URL input.

    LinkedIn may ignore URL filters; the experience check on returned facts
    remains necessary and never treats a requested filter as a job fact.
    """
    return [
        {"key": key, "value": ",".join(values)}
        for key, values in (
            ("f_JT", contract_type),
            ("f_E", experience_level),
            ("f_WT", remote),
        )
        if values
    ]


def _is_apify_configuration_error(response: requests.Response) -> bool:
    status_code = response.status_code
    if status_code < 400:
        return False
    return status_code not in {408, 429} and status_code < 500


def _is_fatal_apify_status_message(message: str) -> bool:
    normalized = " ".join((message or "").casefold().split())
    return any(marker in normalized for marker in _FATAL_APIFY_STATUS_MESSAGE_MARKERS)


def _apify_http_error_message(response: requests.Response, *, token: str = "") -> str:
    status_code = response.status_code
    detail = _apify_error_detail(response)
    if token:
        detail = detail.replace(token, "[redacted]")
    if status_code in {401, 403}:
        explanation = "Vérifiez le jeton APIFY_TOKEN et l’accès à l’acteur LinkedIn."
    elif status_code == 402:
        explanation = "Vérifiez les crédits et l’abonnement du compte Apify."
    elif status_code == 429:
        explanation = "Quota ou limite de requêtes Apify atteint."
    elif status_code == 408 or status_code >= 500:
        explanation = (
            "La réponse de la recherche n’a pas pu être récupérée. "
            "Vérifiez l’état de l’exécution sur Apify avant de la relancer."
        )
    else:
        explanation = "Vérifiez les paramètres de recherche et l’accès à l’acteur Apify."
    message = (
        f"LinkedIn : erreur Apify HTTP {status_code}. {explanation} "
        "Aucune relance automatique n’a été effectuée."
    )
    if detail:
        message = f"{message} Détail Apify : {detail}"
    return message


def _apify_error_detail(response: requests.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return _redact_secrets(_text(response.text))[:500]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            detail = _text(error.get("message")) or _text(error.get("type"))
            return _redact_secrets(detail)[:500]
        if error:
            return _redact_secrets(_text(error))[:500]
    return ""


def _request_error_summary(exc: requests.RequestException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, requests.Response):
        return _apify_http_error_message(response)
    return _redact_secrets(_text(exc))


def _redact_secrets(value: str) -> str:
    value = re.sub(r"(token=)[^&\s]+", r"\1[redacted]", value, flags=re.IGNORECASE)
    value = re.sub(r"apify_api_[A-Za-z0-9_-]+", "apify_api_[redacted]", value)
    value = re.sub(
        r"(Authorization:\s*Bearer\s+)[^\s,;]+",
        r"\1[redacted]",
        value,
        flags=re.IGNORECASE,
    )
    return value


def _csv_list(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _url_text(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    match = _URL_RE.search(text)
    return match.group(0) if match else ""


def _description_text(raw: dict[str, Any]) -> str:
    html_text = _html_description_text(raw)
    if html_text:
        return html_text
    return normalize_whitespace(_text(raw.get("description")))


def _html_description_text(raw: dict[str, Any]) -> str:
    html_description = _text(raw.get("descriptionHtml"))
    if html_description:
        return normalize_whitespace(strip_html(html_description))
    return ""


def _description_source(raw: dict[str, Any]) -> str:
    return "descriptionHtml" if _html_description_text(raw) else "description"


def _external_id(source: str, raw: dict[str, Any], *, title: str, company: str) -> str:
    raw_id = _text(raw.get("id"))
    if raw_id:
        return make_external_id(source, raw_id)
    linkedin_url = _url_text(raw.get("url"))
    if linkedin_url:
        return make_external_id(source, linkedin_url)
    return make_external_id(source, title, company, _text(raw.get("location")))


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_remote_policy(*values: Any) -> str | None:
    text = " ".join(_text(value).lower() for value in values)
    if "remote" in text or "télétravail" in text or "teletravail" in text:
        return "remote"
    if "hybrid" in text or "hybride" in text:
        return "hybrid"
    if "on-site" in text or "onsite" in text or "site" in text:
        return "onsite"
    return None


def _apply_options(raw: dict[str, Any]) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    apply_type = _text(raw.get("applyType"))
    apply_url = _url_text(raw.get("applyUrl"))
    if apply_url:
        options.append({"title": "Apply URL", "link": apply_url})
    linkedin_url = _url_text(raw.get("url"))
    if linkedin_url and linkedin_url != apply_url:
        title = "LinkedIn Easy Apply" if apply_type.upper() == "EASY_APPLY" else "LinkedIn"
        options.append({"title": title, "link": linkedin_url})
    return options


__all__ = ["APIFY_LINKEDIN_JOBS_URL", "LinkedInJobsScraper"]
