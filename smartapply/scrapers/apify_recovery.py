"""Durable journal for Apify runs that must be resumed without a new POST."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

_SCHEMA_VERSION = 1
_JOURNAL_LOCK = RLock()


class ApifyRecoveryJournalError(RuntimeError):
    """The recovery journal could not be read or updated safely."""


class ApifyStartReservedError(ApifyRecoveryJournalError):
    """Another process already reserved this Actor request before its POST."""

    def __init__(self, reservation: ApifyStartReservation):
        super().__init__(
            "Un démarrage Apify identique est déjà réservé depuis "
            f"{reservation.created_at} (processus {reservation.process_id})."
        )
        self.reservation = reservation


@dataclass(frozen=True)
class ApifyStartReservation:
    """Durable pre-POST guard against concurrent duplicate Actor starts."""

    query: str
    request_payload: dict[str, Any]
    request_fingerprint: str
    process_id: int
    created_at: str

    @classmethod
    def from_dict(cls, value: object) -> ApifyStartReservation:
        if not isinstance(value, dict):
            raise ApifyRecoveryJournalError("Réservation de démarrage Apify invalide.")
        payload = value.get("request_payload")
        if not isinstance(payload, dict):
            raise ApifyRecoveryJournalError("Payload de réservation Apify invalide.")
        if any(
            not isinstance(value.get(field), str)
            for field in ("query", "request_fingerprint", "created_at")
        ) or not isinstance(value.get("process_id"), int):
            raise ApifyRecoveryJournalError("Champs de réservation Apify invalides.")
        return cls(
            query=value["query"],
            request_payload=dict(payload),
            request_fingerprint=value["request_fingerprint"],
            process_id=value["process_id"],
            created_at=value["created_at"],
        )


@dataclass(frozen=True)
class PendingApifyRun:
    """One known Actor run whose dataset has not yet been persisted locally."""

    run_id: str
    dataset_id: str
    query: str
    request_payload: dict[str, Any]
    request_fingerprint: str
    state: str
    actor_status: str
    failure_phase: str | None
    last_error: str | None
    created_at: str
    updated_at: str
    # Optional fields preserve compatibility with existing schema-version-1 journals.
    terminal_status_message: str | None = None
    terminal_error_fatal: bool | None = None

    @classmethod
    def from_dict(cls, value: object) -> PendingApifyRun:
        if not isinstance(value, dict):
            raise ApifyRecoveryJournalError("Entrée de récupération Apify invalide.")
        payload = value.get("request_payload")
        if not isinstance(payload, dict):
            raise ApifyRecoveryJournalError("Payload de récupération Apify invalide.")
        required = {
            "run_id",
            "dataset_id",
            "query",
            "request_fingerprint",
            "state",
            "actor_status",
            "created_at",
            "updated_at",
        }
        if any(not isinstance(value.get(field), str) for field in required):
            raise ApifyRecoveryJournalError("Identifiants de récupération Apify invalides.")
        failure_phase = value.get("failure_phase")
        last_error = value.get("last_error")
        terminal_message = value.get("terminal_status_message")
        terminal_fatal = value.get("terminal_error_fatal")
        if failure_phase is not None and not isinstance(failure_phase, str):
            raise ApifyRecoveryJournalError("Phase de récupération Apify invalide.")
        if last_error is not None and not isinstance(last_error, str):
            raise ApifyRecoveryJournalError("Erreur de récupération Apify invalide.")
        if terminal_message is not None and not isinstance(terminal_message, str):
            raise ApifyRecoveryJournalError("Message terminal Apify invalide.")
        if terminal_fatal is not None and not isinstance(terminal_fatal, bool):
            raise ApifyRecoveryJournalError("Classe d’erreur terminale Apify invalide.")
        return cls(
            run_id=value["run_id"],
            dataset_id=value["dataset_id"],
            query=value["query"],
            request_payload=dict(payload),
            request_fingerprint=value["request_fingerprint"],
            state=value["state"],
            actor_status=value["actor_status"],
            failure_phase=failure_phase,
            last_error=last_error,
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            terminal_status_message=terminal_message,
            terminal_error_fatal=terminal_fatal,
        )


def request_fingerprint(payload: dict[str, Any]) -> str:
    """Return the stable identity of one Actor request.

    ``skipJobId`` is deliberately excluded. It is an execution detail derived
    from an earlier experience pass, not part of the user's search intent.
    """

    semantic_payload = {
        key: value for key, value in payload.items() if key not in {"skipJobId", "skip_job_id"}
    }
    serialized = json.dumps(
        semantic_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def search_pass_fingerprint(payload: dict[str, Any]) -> str:
    """Identify a search pass independently of its remaining result allowance.

    Keep the executed payload and its original fingerprint unchanged for audit
    and version-1 journals. Changing a limit must not bypass a pending run or an
    uncertain pre-POST reservation for the same pass.
    """

    return request_fingerprint({key: value for key, value in payload.items() if key != "limit"})


class ApifyRecoveryJournal:
    """Atomically persist run identifiers without storing the Apify token."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def find(self, payload: dict[str, Any]) -> PendingApifyRun | None:
        fingerprint = request_fingerprint(payload)
        with self._exclusive_lock():
            records, _reservations = self._load_document_unlocked()
            matches = [item for item in records if item.request_fingerprint == fingerprint]
            return min(matches, key=lambda item: item.created_at) if matches else None

    def list_pending(self) -> list[PendingApifyRun]:
        with self._exclusive_lock():
            records, _reservations = self._load_document_unlocked()
            return list(records)

    def list_start_reservations(self) -> list[ApifyStartReservation]:
        with self._exclusive_lock():
            _records, reservations = self._load_document_unlocked()
            return list(reservations)

    def claim_start(self, payload: dict[str, Any]) -> PendingApifyRun | None:
        """Atomically find a known run or reserve the initial POST.

        The reservation intentionally has no automatic expiry: after a hard
        crash around the POST, only Apify can establish whether a run exists.
        Starting another paid run automatically would be unsafe.
        """

        fingerprint = request_fingerprint(payload)
        pass_fingerprint = search_pass_fingerprint(payload)
        with self._exclusive_lock():
            records, reservations = self._load_document_unlocked()
            matches = [
                item
                for item in records
                if search_pass_fingerprint(item.request_payload) == pass_fingerprint
            ]
            if matches:
                # A completed record_started write supersedes any stale
                # reservation left by an interrupted cleanup.
                recorded_fingerprints = {item.request_fingerprint for item in matches}
                kept = [
                    item
                    for item in reservations
                    if item.request_fingerprint not in recorded_fingerprints
                ]
                if len(kept) != len(reservations):
                    self._write_unlocked(records, kept)
                return min(matches, key=lambda item: item.created_at)
            for reservation in reservations:
                if search_pass_fingerprint(reservation.request_payload) == pass_fingerprint:
                    raise ApifyStartReservedError(reservation)
            reservation = ApifyStartReservation(
                query=str(payload.get("keywords") or ""),
                request_payload=dict(payload),
                request_fingerprint=fingerprint,
                process_id=os.getpid(),
                created_at=_utcnow_text(),
            )
            reservations.append(reservation)
            self._write_unlocked(records, reservations)
            return None

    def release_start(self, payload: dict[str, Any]) -> None:
        """Release a reservation after a definitive no-run response."""

        fingerprint = request_fingerprint(payload)
        with self._exclusive_lock():
            records, reservations = self._load_document_unlocked()
            kept = [item for item in reservations if item.request_fingerprint != fingerprint]
            if len(kept) != len(reservations):
                self._write_unlocked(records, kept)

    def record_started(
        self,
        *,
        payload: dict[str, Any],
        run_id: str,
        dataset_id: str,
        actor_status: str,
    ) -> PendingApifyRun:
        now = _utcnow_text()
        fingerprint = request_fingerprint(payload)
        with self._exclusive_lock():
            records, reservations = self._load_document_unlocked()
            reservations = [
                item for item in reservations if item.request_fingerprint != fingerprint
            ]
            for index, current in enumerate(records):
                if current.run_id != run_id:
                    continue
                updated = PendingApifyRun(
                    run_id=run_id,
                    dataset_id=dataset_id or current.dataset_id,
                    query=str(payload.get("keywords") or current.query),
                    request_payload=dict(payload),
                    request_fingerprint=fingerprint,
                    state="tracking",
                    actor_status=actor_status,
                    failure_phase=None,
                    last_error=None,
                    created_at=current.created_at,
                    updated_at=now,
                )
                records[index] = updated
                self._write_unlocked(records, reservations)
                return updated
            record = PendingApifyRun(
                run_id=run_id,
                dataset_id=dataset_id,
                query=str(payload.get("keywords") or ""),
                request_payload=dict(payload),
                request_fingerprint=fingerprint,
                state="tracking",
                actor_status=actor_status,
                failure_phase=None,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
            records.append(record)
            self._write_unlocked(records, reservations)
            return record

    def mark_recovery_required(
        self,
        run_id: str,
        *,
        dataset_id: str,
        actor_status: str,
        phase: str,
        error: str,
        terminal_status_message: str | None = None,
        terminal_error_fatal: bool | None = None,
    ) -> None:
        terminal_fields = {}
        if terminal_error_fatal is not None:
            terminal_fields = {
                "terminal_status_message": terminal_status_message,
                "terminal_error_fatal": terminal_error_fatal,
            }
        self._update(
            run_id,
            dataset_id=dataset_id,
            state="recovery_required",
            actor_status=actor_status,
            failure_phase=phase,
            last_error=error[:1000],
            **terminal_fields,
        )

    def mark_dataset_ready(
        self,
        run_id: str,
        *,
        dataset_id: str,
        actor_status: str,
        terminal_status_message: str | None = None,
        terminal_error_fatal: bool | None = None,
    ) -> None:
        self._update(
            run_id,
            dataset_id=dataset_id,
            state="dataset_ready",
            actor_status=actor_status,
            failure_phase=None,
            last_error=None,
            terminal_status_message=terminal_status_message,
            terminal_error_fatal=terminal_error_fatal,
        )

    def remove_many(self, run_ids: list[str] | tuple[str, ...] | set[str]) -> None:
        targets = {str(run_id).strip() for run_id in run_ids if str(run_id).strip()}
        if not targets:
            return
        with self._exclusive_lock():
            records, reservations = self._load_document_unlocked()
            kept = [record for record in records if record.run_id not in targets]
            if len(kept) != len(records):
                self._write_unlocked(kept, reservations)

    def _update(self, run_id: str, **changes: Any) -> None:
        with self._exclusive_lock():
            records, reservations = self._load_document_unlocked()
            for index, current in enumerate(records):
                if current.run_id != run_id:
                    continue
                values = asdict(current)
                values.update(changes)
                values["updated_at"] = _utcnow_text()
                records[index] = PendingApifyRun.from_dict(values)
                self._write_unlocked(records, reservations)
                return
        raise ApifyRecoveryJournalError(
            f"Le run Apify {run_id} est absent du journal de récupération."
        )

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """Serialize journal transactions across threads and local processes."""

        with _JOURNAL_LOCK:
            lock_path = self.path.with_name(f"{self.path.name}.lock")
            lock_handle = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                lock_handle = lock_path.open("a+", encoding="utf-8")
                lock_path.chmod(0o600)
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                if lock_handle is not None:
                    lock_handle.close()
                raise ApifyRecoveryJournalError(
                    f"Verrou du journal de récupération Apify inaccessible : {lock_path}."
                ) from exc
            try:
                yield
            finally:
                with suppress(OSError):
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()

    def _load_document_unlocked(
        self,
    ) -> tuple[list[PendingApifyRun], list[ApifyStartReservation]]:
        if not self.path.exists():
            return [], []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ApifyRecoveryJournalError(
                f"Journal de récupération Apify illisible : {self.path}."
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise ApifyRecoveryJournalError(
                f"Version du journal de récupération Apify invalide : {self.path}."
            )
        runs = payload.get("runs")
        if not isinstance(runs, list):
            raise ApifyRecoveryJournalError(
                f"Contenu du journal de récupération Apify invalide : {self.path}."
            )
        reservations = payload.get("start_reservations", [])
        if not isinstance(reservations, list):
            raise ApifyRecoveryJournalError(
                f"Réservations du journal de récupération Apify invalides : {self.path}."
            )
        return (
            [PendingApifyRun.from_dict(item) for item in runs],
            [ApifyStartReservation.from_dict(item) for item in reservations],
        )

    def _write_unlocked(
        self,
        records: list[PendingApifyRun],
        reservations: list[ApifyStartReservation],
    ) -> None:
        temporary_path: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.path.parent),
                prefix=f"{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "runs": [asdict(record) for record in records],
                        "start_reservations": [asdict(reservation) for reservation in reservations],
                    },
                    temporary,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.chmod(0o600)
            os.replace(temporary_path, self.path)
            self.path.chmod(0o600)
            with suppress(OSError):
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except (OSError, TypeError, ValueError) as exc:
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)
            raise ApifyRecoveryJournalError(
                f"Impossible d’enregistrer le journal de récupération Apify : {self.path}."
            ) from exc


def _utcnow_text() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ApifyRecoveryJournal",
    "ApifyRecoveryJournalError",
    "ApifyStartReservation",
    "ApifyStartReservedError",
    "PendingApifyRun",
    "request_fingerprint",
    "search_pass_fingerprint",
]
