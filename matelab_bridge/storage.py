"""SQLite durable outbox, mapping store, and append-only journal."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from .canonical import deterministic_record_uid
from .errors import (
    ArtifactNotFoundError,
    CaptureConflictError,
    CaptureNotFoundError,
    IdempotencyConflictError,
    IncompleteCaptureError,
    InvalidStateError,
    MappingConflictError,
)
from .models import (
    CaptureEnvelope,
    IntegrationEvent,
    MatelabRecordRef,
    RecentCandidate,
    RecordDescriptionReceipt,
    SyncReceipt,
    SyncState,
)
from .security import redact, redact_text

TERMINAL_STATES = {SyncState.COMPLETE, SyncState.ABANDONED}
INFLIGHT_STATES = {
    SyncState.UPLOADING_FILES,
    SyncState.CREATING_RECORD,
    SyncState.POPULATING_RECORD,
    SyncState.VERIFYING_EXPORT,
    SyncState.MATELAB_VERIFIED,
}

ALLOWED_TRANSITIONS: dict[SyncState, set[SyncState]] = {
    SyncState.RECEIVING: {SyncState.VALIDATING},
    SyncState.VALIDATING: {SyncState.ACCEPTED},
    SyncState.ACCEPTED: {SyncState.READY},
    SyncState.READY: {SyncState.UPLOADING_FILES},
    SyncState.UPLOADING_FILES: {SyncState.CREATING_RECORD},
    SyncState.CREATING_RECORD: {SyncState.POPULATING_RECORD},
    SyncState.POPULATING_RECORD: {SyncState.VERIFYING_EXPORT},
    SyncState.VERIFYING_EXPORT: {SyncState.MATELAB_VERIFIED},
    SyncState.MATELAB_VERIFIED: {SyncState.COMPLETE},
    SyncState.RETRY_WAIT: {SyncState.UPLOADING_FILES, SyncState.READY},
    SyncState.AUTH_REQUIRED: {SyncState.READY},
    SyncState.NEEDS_ATTENTION: {SyncState.READY},
    SyncState.COMPLETE: set(),
    SyncState.ABANDONED: set(),
}


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


class BridgeStore:
    """Short-lived SQLite connections keep API and worker access thread-safe."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level="DEFERRED")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS captures (
                    capture_id TEXT PRIMARY KEY,
                    sync_id TEXT NOT NULL UNIQUE,
                    manifest_json TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    capture_kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    record_uid TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    template_version TEXT NOT NULL,
                    template_sha256 TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    committed_at TEXT,
                    producer_kind TEXT NOT NULL,
                    producer_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    run_id TEXT,
                    source_locator TEXT,
                    source_hash TEXT,
                    next_attempt_at TEXT,
                    last_error_json TEXT
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    capture_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    descriptor_json TEXT NOT NULL,
                    expected_size INTEGER NOT NULL,
                    expected_sha256 TEXT NOT NULL,
                    required INTEGER NOT NULL,
                    transfer_policy TEXT NOT NULL,
                    state TEXT NOT NULL,
                    local_path TEXT,
                    received_size INTEGER,
                    received_sha256 TEXT,
                    matelab_receipt_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (capture_id, artifact_id),
                    FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
                );

                CREATE TABLE IF NOT EXISTS journal (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    details_json TEXT NOT NULL,
                    FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
                );

                CREATE TABLE IF NOT EXISTS mappings (
                    capture_id TEXT NOT NULL,
                    record_uid TEXT NOT NULL,
                    record_version INTEGER NOT NULL,
                    record_ref_json TEXT NOT NULL,
                    export_json_path TEXT NOT NULL,
                    export_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (capture_id, record_uid, record_version),
                    FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
                );

                CREATE TABLE IF NOT EXISTS orphan_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id TEXT NOT NULL,
                    object_kind TEXT NOT NULL,
                    remote_identity TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    resolved_at TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
                );

                CREATE TABLE IF NOT EXISTS request_audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    capture_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    run_id TEXT,
                    idempotency_key TEXT NOT NULL,
                    FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
                );

                CREATE TABLE IF NOT EXISTS manual_submission_idempotency (
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    capture_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (operation, idempotency_key),
                    FOREIGN KEY (capture_id) REFERENCES captures(capture_id)
                );

                CREATE TABLE IF NOT EXISTS record_descriptions (
                    description_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    request_sha256 TEXT NOT NULL,
                    record_uid TEXT NOT NULL,
                    notebook_id TEXT NOT NULL,
                    notebook_name TEXT NOT NULL,
                    owner_user TEXT,
                    module_name TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    matelab_response_json TEXT,
                    last_error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS integration_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_captures_state_next
                    ON captures(state, next_attempt_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_captures_recent
                    ON captures(capture_kind, occurred_at DESC);
                CREATE INDEX IF NOT EXISTS idx_captures_source_locator
                    ON captures(source_locator);
                CREATE INDEX IF NOT EXISTS idx_journal_capture
                    ON journal(capture_id, seq);
                CREATE INDEX IF NOT EXISTS idx_request_audit_capture
                    ON request_audit(capture_id, seq);
                CREATE INDEX IF NOT EXISTS idx_manual_submission_capture
                    ON manual_submission_idempotency(capture_id);
                CREATE INDEX IF NOT EXISTS idx_integration_events_type_seq
                    ON integration_events(event_type, seq);

                CREATE TRIGGER IF NOT EXISTS journal_prevent_update
                BEFORE UPDATE ON journal
                BEGIN
                    SELECT RAISE(ABORT, 'journal is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS journal_prevent_delete
                BEFORE DELETE ON journal
                BEGIN
                    SELECT RAISE(ABORT, 'journal is append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS integration_events_prevent_update
                BEFORE UPDATE ON integration_events
                BEGIN
                    SELECT RAISE(ABORT, 'integration events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS integration_events_prevent_delete
                BEFORE DELETE ON integration_events
                BEGIN
                    SELECT RAISE(ABORT, 'integration events are append-only');
                END;
                """
            )

    def create_capture(
        self,
        envelope: CaptureEnvelope,
        manifest_json: bytes,
        manifest_sha256: str,
        *,
        template_id: str,
        template_version: str,
        template_sha256: str,
    ) -> tuple[SyncReceipt, bool]:
        with self.connect() as connection, connection:
            return self._create_capture_in_connection(
                connection,
                envelope,
                manifest_json,
                manifest_sha256,
                template_id=template_id,
                template_version=template_version,
                template_sha256=template_sha256,
            )

    def create_manual_capture(
        self,
        envelope: CaptureEnvelope,
        manifest_json: bytes,
        manifest_sha256: str,
        *,
        template_id: str,
        template_version: str,
        template_sha256: str,
        operation: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> tuple[SyncReceipt, bool]:
        """Atomically bind one manual-submission key to exactly one capture."""
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT request_sha256, capture_id
                FROM manual_submission_idempotency
                WHERE operation=? AND idempotency_key=?
                """,
                (operation, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    raise CaptureConflictError(
                        "Idempotency-Key was already used for a different manual submission"
                    )
                return self._receipt_in_connection(connection, str(existing["capture_id"])), True

            receipt, duplicate = self._create_capture_in_connection(
                connection,
                envelope,
                manifest_json,
                manifest_sha256,
                template_id=template_id,
                template_version=template_version,
                template_sha256=template_sha256,
            )
            connection.execute(
                """
                INSERT INTO manual_submission_idempotency (
                    operation, idempotency_key, request_sha256, capture_id, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (operation, idempotency_key, request_sha256, envelope.capture_id, iso_now()),
            )
            return receipt, duplicate

    def find_manual_capture(
        self, *, operation: str, idempotency_key: str, request_sha256: str
    ) -> SyncReceipt | None:
        """Return a prior matching manual submission, or reject key reuse."""
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT request_sha256, capture_id
                FROM manual_submission_idempotency
                WHERE operation=? AND idempotency_key=?
                """,
                (operation, idempotency_key),
            ).fetchone()
            if existing is None:
                return None
            if existing["request_sha256"] != request_sha256:
                raise CaptureConflictError(
                    "Idempotency-Key was already used for a different manual submission"
                )
            return self._receipt_in_connection(connection, str(existing["capture_id"]))

    def _create_capture_in_connection(
        self,
        connection: sqlite3.Connection,
        envelope: CaptureEnvelope,
        manifest_json: bytes,
        manifest_sha256: str,
        *,
        template_id: str,
        template_version: str,
        template_sha256: str,
    ) -> tuple[SyncReceipt, bool]:
        existing = connection.execute(
            "SELECT manifest_sha256 FROM captures WHERE capture_id=?", (envelope.capture_id,)
        ).fetchone()
        if existing:
            if existing["manifest_sha256"] != manifest_sha256:
                raise CaptureConflictError(
                    "capture_id already exists with a different canonical payload"
                )
            return self._receipt_in_connection(connection, envelope.capture_id), True

        now = iso_now()
        source_locator = envelope.source.locator if envelope.source else None
        source_hash = envelope.source.content_hash if envelope.source else None
        sync_id = f"sync-{uuid.uuid4()}"
        connection.execute(
            """
            INSERT INTO captures (
                capture_id, sync_id, manifest_json, manifest_sha256, capture_kind,
                state, record_uid, template_id, template_version, template_sha256,
                occurred_at, received_at, created_at, updated_at, producer_kind,
                producer_id, actor_id, run_id, source_locator, source_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                envelope.capture_id,
                sync_id,
                manifest_json.decode("utf-8"),
                manifest_sha256,
                envelope.capture_kind.value,
                SyncState.RECEIVING.value,
                deterministic_record_uid(envelope.capture_id),
                template_id,
                template_version,
                template_sha256,
                envelope.occurred_at.isoformat(),
                now,
                now,
                now,
                envelope.producer.kind,
                envelope.producer.producer_id,
                envelope.producer.actor_id,
                envelope.producer.run_id,
                source_locator,
                source_hash,
            ),
        )
        for artifact in envelope.artifacts:
            state = "declared" if artifact.ingress.mode == "stream" else "manifest_only"
            connection.execute(
                """
                INSERT INTO artifacts (
                    capture_id, artifact_id, descriptor_json, expected_size,
                    expected_sha256, required, transfer_policy, state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    envelope.capture_id,
                    artifact.artifact_id,
                    artifact.model_dump_json(),
                    artifact.size,
                    artifact.sha256.lower(),
                    int(artifact.required),
                    artifact.transfer_policy,
                    state,
                    now,
                    now,
                ),
            )
        self._append_journal(
            connection,
            envelope.capture_id,
            "capture_persisted",
            None,
            SyncState.RECEIVING,
            {"manifest_sha256": manifest_sha256, "artifact_count": len(envelope.artifacts)},
        )
        return self._receipt_in_connection(connection, envelope.capture_id), False

    def get_capture_row(self, capture_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if row is None:
                raise CaptureNotFoundError("capture_id was not found")
            return cast(sqlite3.Row, row)

    def get_manifest(self, capture_id: str) -> CaptureEnvelope:
        row = self.get_capture_row(capture_id)
        return CaptureEnvelope.model_validate_json(row["manifest_json"])

    def get_artifact_row(self, capture_id: str, artifact_id: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE capture_id=? AND artifact_id=?",
                (capture_id, artifact_id),
            ).fetchone()
            if row is None:
                if not connection.execute(
                    "SELECT 1 FROM captures WHERE capture_id=?", (capture_id,)
                ).fetchone():
                    raise CaptureNotFoundError("capture_id was not found")
                raise ArtifactNotFoundError("artifact_id was not declared by the capture")
            return cast(sqlite3.Row, row)

    def list_artifacts(self, capture_id: str) -> list[sqlite3.Row]:
        self.get_capture_row(capture_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM artifacts WHERE capture_id=? ORDER BY artifact_id", (capture_id,)
                ).fetchall()
            )

    def mark_artifact_received(
        self,
        capture_id: str,
        artifact_id: str,
        *,
        local_path: Path,
        size: int,
        sha256: str,
    ) -> None:
        now = iso_now()
        with self.connect() as connection, connection:
            row = connection.execute(
                "SELECT state FROM artifacts WHERE capture_id=? AND artifact_id=?",
                (capture_id, artifact_id),
            ).fetchone()
            if row is None:
                raise ArtifactNotFoundError("artifact_id was not declared by the capture")
            connection.execute(
                """
                UPDATE artifacts SET state='received', local_path=?, received_size=?,
                    received_sha256=?, updated_at=?
                WHERE capture_id=? AND artifact_id=?
                """,
                (str(local_path), size, sha256, now, capture_id, artifact_id),
            )
            self._append_journal(
                connection,
                capture_id,
                "artifact_received",
                None,
                None,
                {"artifact_id": artifact_id, "size": size, "sha256_prefix": sha256[:12]},
            )

    def mark_artifact_uploaded(
        self, capture_id: str, artifact_id: str, receipt: dict[str, Any]
    ) -> None:
        now = iso_now()
        safe_receipt = redact(receipt)
        with self.connect() as connection, connection:
            connection.execute(
                """
                UPDATE artifacts SET state='uploaded', matelab_receipt_json=?, updated_at=?
                WHERE capture_id=? AND artifact_id=?
                """,
                (
                    json.dumps(safe_receipt, ensure_ascii=False, separators=(",", ":")),
                    now,
                    capture_id,
                    artifact_id,
                ),
            )
            self._append_journal(
                connection,
                capture_id,
                "artifact_upload_receipt",
                None,
                None,
                {"artifact_id": artifact_id, "remote": safe_receipt},
            )

    def commit_capture(self, capture_id: str) -> SyncReceipt:
        with self.connect() as connection, connection:
            row = connection.execute(
                "SELECT state FROM captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if row is None:
                raise CaptureNotFoundError("capture_id was not found")
            state = SyncState(row["state"])
            if state != SyncState.RECEIVING:
                return self._receipt_in_connection(connection, capture_id)
            missing = connection.execute(
                """
                SELECT artifact_id FROM artifacts
                WHERE capture_id=? AND required=1 AND transfer_policy!='manifest_only'
                    AND state NOT IN ('received', 'uploaded')
                ORDER BY artifact_id
                """,
                (capture_id,),
            ).fetchall()
            if missing:
                ids = ", ".join(item["artifact_id"] for item in missing[:10])
                raise IncompleteCaptureError(f"required artifacts have not been received: {ids}")
            self._transition_in_connection(
                connection, capture_id, SyncState.VALIDATING, "validation_started"
            )
            self._transition_in_connection(
                connection, capture_id, SyncState.ACCEPTED, "capture_accepted"
            )
            self._transition_in_connection(
                connection,
                capture_id,
                SyncState.READY,
                "capture_ready",
                updates={"committed_at": iso_now()},
            )
            return self._receipt_in_connection(connection, capture_id)

    def transition(
        self,
        capture_id: str,
        to_state: SyncState,
        event: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection, connection:
            self._transition_in_connection(connection, capture_id, to_state, event, details=details)

    def _transition_in_connection(
        self,
        connection: sqlite3.Connection,
        capture_id: str,
        to_state: SyncState,
        event: str,
        *,
        details: dict[str, Any] | None = None,
        updates: dict[str, Any] | None = None,
        force: bool = False,
    ) -> None:
        row = connection.execute(
            "SELECT state FROM captures WHERE capture_id=?", (capture_id,)
        ).fetchone()
        if row is None:
            raise CaptureNotFoundError("capture_id was not found")
        from_state = SyncState(row["state"])
        globally_allowed = {
            SyncState.RETRY_WAIT,
            SyncState.AUTH_REQUIRED,
            SyncState.NEEDS_ATTENTION,
            SyncState.ABANDONED,
        }
        if not force and to_state not in ALLOWED_TRANSITIONS[from_state] | globally_allowed:
            raise InvalidStateError(f"illegal transition: {from_state.value} -> {to_state.value}")
        values: dict[str, Any] = {"state": to_state.value, "updated_at": iso_now()}
        if updates:
            values.update(updates)
        assignments = ", ".join(f"{key}=?" for key in values)
        connection.execute(
            f"UPDATE captures SET {assignments} WHERE capture_id=?",
            (*values.values(), capture_id),
        )
        self._append_journal(connection, capture_id, event, from_state, to_state, details or {})
        if to_state == SyncState.COMPLETE:
            self._publish_capture_synced(connection, capture_id)

    def claim_next(self) -> str | None:
        now = iso_now()
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT capture_id, state FROM captures
                WHERE state=? OR (state=? AND (next_attempt_at IS NULL OR next_attempt_at<=?))
                ORDER BY created_at LIMIT 1
                """,
                (SyncState.READY.value, SyncState.RETRY_WAIT.value, now),
            ).fetchone()
            if row is None:
                return None
            capture_id = str(row["capture_id"])
            self._transition_in_connection(
                connection,
                capture_id,
                SyncState.UPLOADING_FILES,
                "sync_attempt_started",
                updates={
                    "attempt": self._attempt(connection, capture_id) + 1,
                    "next_attempt_at": None,
                },
            )
            return capture_id

    def _attempt(self, connection: sqlite3.Connection, capture_id: str) -> int:
        row = connection.execute(
            "SELECT attempt FROM captures WHERE capture_id=?", (capture_id,)
        ).fetchone()
        return int(row["attempt"])

    def schedule_retry(
        self,
        capture_id: str,
        *,
        error_code: str,
        message: str,
        delay_seconds: float,
    ) -> None:
        next_attempt = (utc_now() + timedelta(seconds=delay_seconds)).isoformat()
        safe_error = {"code": error_code, "message": redact_text(message)}
        with self.connect() as connection, connection:
            self._transition_in_connection(
                connection,
                capture_id,
                SyncState.RETRY_WAIT,
                "retry_scheduled",
                details={"error": safe_error, "delay_seconds": delay_seconds},
                updates={
                    "next_attempt_at": next_attempt,
                    "last_error_json": json.dumps(safe_error, ensure_ascii=False),
                },
            )

    def mark_attention(
        self, capture_id: str, state: SyncState, *, error_code: str, message: str
    ) -> None:
        if state not in {SyncState.AUTH_REQUIRED, SyncState.NEEDS_ATTENTION}:
            raise ValueError("attention state must be auth_required or needs_attention")
        safe_error = {"code": error_code, "message": redact_text(message)}
        with self.connect() as connection, connection:
            self._transition_in_connection(
                connection,
                capture_id,
                state,
                "attention_required",
                details={"error": safe_error},
                updates={"last_error_json": json.dumps(safe_error, ensure_ascii=False)},
            )

    def retry_capture(self, capture_id: str) -> SyncReceipt:
        with self.connect() as connection, connection:
            row = connection.execute(
                "SELECT state FROM captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if row is None:
                raise CaptureNotFoundError("capture_id was not found")
            state = SyncState(row["state"])
            if state in TERMINAL_STATES:
                return self._receipt_in_connection(connection, capture_id)
            if state in {SyncState.RETRY_WAIT, SyncState.AUTH_REQUIRED, SyncState.NEEDS_ATTENTION}:
                self._transition_in_connection(
                    connection,
                    capture_id,
                    SyncState.READY,
                    "manual_retry",
                    updates={"next_attempt_at": None, "last_error_json": None},
                )
            return self._receipt_in_connection(connection, capture_id)

    def retry_auth_required(self) -> int:
        """Resume all durable jobs that were paused solely for MatElab login."""
        resumed = 0
        with self.connect() as connection, connection:
            rows = connection.execute(
                "SELECT capture_id FROM captures WHERE state=? ORDER BY created_at",
                (SyncState.AUTH_REQUIRED.value,),
            ).fetchall()
            for row in rows:
                self._transition_in_connection(
                    connection,
                    row["capture_id"],
                    SyncState.READY,
                    "authentication_restored",
                    updates={"next_attempt_at": None, "last_error_json": None},
                )
                resumed += 1
        return resumed

    def recover_inflight(self) -> int:
        recovered = 0
        with self.connect() as connection, connection:
            placeholders = ",".join("?" for _ in INFLIGHT_STATES)
            rows = connection.execute(
                f"SELECT capture_id FROM captures WHERE state IN ({placeholders})",
                tuple(state.value for state in INFLIGHT_STATES),
            ).fetchall()
            for row in rows:
                self._transition_in_connection(
                    connection,
                    row["capture_id"],
                    SyncState.READY,
                    "process_restart_recovery",
                    force=True,
                )
                recovered += 1
        return recovered

    def save_mapping(
        self,
        capture_id: str,
        record_ref: MatelabRecordRef,
        *,
        export_json_path: Path,
    ) -> None:
        with self.connect() as connection, connection:
            existing = connection.execute(
                """
                SELECT export_sha256 FROM mappings
                WHERE capture_id=? AND record_uid=? AND record_version=?
                """,
                (capture_id, record_ref.record_uid, record_ref.record_version),
            ).fetchone()
            if existing:
                if existing["export_sha256"] != record_ref.export_sha256:
                    raise MappingConflictError(
                        "the same MatElab record version has a different export hash"
                    )
                return
            connection.execute(
                """
                INSERT INTO mappings (
                    capture_id, record_uid, record_version, record_ref_json,
                    export_json_path, export_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capture_id,
                    record_ref.record_uid,
                    record_ref.record_version,
                    record_ref.model_dump_json(),
                    str(export_json_path),
                    record_ref.export_sha256,
                    iso_now(),
                ),
            )
            self._append_journal(
                connection,
                capture_id,
                "mapping_saved",
                None,
                None,
                {
                    "record_uid": record_ref.record_uid,
                    "record_version": record_ref.record_version,
                    "export_sha256": record_ref.export_sha256,
                },
            )

    def add_orphan_candidate(
        self, capture_id: str, object_kind: str, remote_identity: str, details: dict[str, Any]
    ) -> None:
        with self.connect() as connection, connection:
            existing = connection.execute(
                """
                SELECT 1 FROM orphan_candidates
                WHERE capture_id=? AND object_kind=? AND remote_identity=? AND resolved_at IS NULL
                """,
                (capture_id, object_kind, remote_identity),
            ).fetchone()
            if existing:
                return
            connection.execute(
                """
                INSERT INTO orphan_candidates (
                    capture_id, object_kind, remote_identity, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    capture_id,
                    object_kind,
                    remote_identity,
                    json.dumps(redact(details), ensure_ascii=False),
                    iso_now(),
                ),
            )

    def get_receipt(self, capture_id: str) -> SyncReceipt:
        with self.connect() as connection:
            return self._receipt_in_connection(connection, capture_id)

    def list_receipts(self, *, limit: int = 100) -> list[SyncReceipt]:
        safe_limit = max(1, min(limit, 500))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT capture_id FROM captures ORDER BY updated_at DESC LIMIT ?",
                (safe_limit,),
            ).fetchall()
            return [self._receipt_in_connection(connection, row["capture_id"]) for row in rows]

    def _receipt_in_connection(
        self, connection: sqlite3.Connection, capture_id: str
    ) -> SyncReceipt:
        row = connection.execute(
            "SELECT * FROM captures WHERE capture_id=?", (capture_id,)
        ).fetchone()
        if row is None:
            raise CaptureNotFoundError("capture_id was not found")
        mapping = connection.execute(
            """
            SELECT record_ref_json FROM mappings WHERE capture_id=?
            ORDER BY record_version DESC, created_at DESC LIMIT 1
            """,
            (capture_id,),
        ).fetchone()
        return SyncReceipt(
            sync_id=row["sync_id"],
            capture_id=row["capture_id"],
            source_locator=row["source_locator"],
            source_hash=row["source_hash"],
            manifest_sha256=row["manifest_sha256"],
            matelab_ref=(
                MatelabRecordRef.model_validate_json(mapping["record_ref_json"])
                if mapping
                else None
            ),
            state=SyncState(row["state"]),
            attempt=row["attempt"],
            last_error=json.loads(row["last_error_json"]) if row["last_error_json"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def history(self, capture_id: str) -> list[dict[str, Any]]:
        self.get_capture_row(capture_id)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT seq, timestamp, event, from_state, to_state, details_json
                FROM journal WHERE capture_id=? ORDER BY seq
                """,
                (capture_id,),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "timestamp": row["timestamp"],
                "event": row["event"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def record_event(
        self, capture_id: str, event: str, details: dict[str, Any] | None = None
    ) -> None:
        with self.connect() as connection, connection:
            if not connection.execute(
                "SELECT 1 FROM captures WHERE capture_id=?", (capture_id,)
            ).fetchone():
                raise CaptureNotFoundError("capture_id was not found")
            self._append_journal(connection, capture_id, event, None, None, details or {})

    def reserve_record_description(
        self,
        *,
        description_id: str,
        idempotency_key: str,
        request_sha256: str,
        record_uid: str,
        notebook_id: str,
        notebook_name: str,
        module_name: str,
        content_sha256: str,
    ) -> tuple[RecordDescriptionReceipt | None, bool]:
        """Reserve a stable MatElab module name before the remote update."""
        with self.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM record_descriptions WHERE idempotency_key=?
                """,
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    raise IdempotencyConflictError(
                        "Idempotency-Key was already used for a different record description"
                    )
                connection.execute(
                    """
                    UPDATE record_descriptions SET attempt=attempt+1, updated_at=?
                    WHERE description_id=?
                    """,
                    (iso_now(), existing["description_id"]),
                )
                if existing["status"] == "completed":
                    return self._description_receipt_in_connection(
                        connection, str(existing["description_id"]), duplicate=True
                    ), True
                return None, True

            now = iso_now()
            connection.execute(
                """
                INSERT INTO record_descriptions (
                    description_id, idempotency_key, request_sha256, record_uid,
                    notebook_id, notebook_name, module_name, content_sha256,
                    status, attempt, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?, ?)
                """,
                (
                    description_id,
                    idempotency_key,
                    request_sha256,
                    record_uid,
                    notebook_id,
                    notebook_name,
                    module_name,
                    content_sha256,
                    now,
                    now,
                ),
            )
            return None, False

    def complete_record_description(
        self,
        description_id: str,
        *,
        owner_user: str | None,
        matelab_response: dict[str, Any],
        duplicate: bool,
    ) -> RecordDescriptionReceipt:
        with self.connect() as connection, connection:
            row = connection.execute(
                "SELECT * FROM record_descriptions WHERE description_id=?",
                (description_id,),
            ).fetchone()
            if row is None:
                raise ValueError("record description reservation was not found")
            safe_response = redact(matelab_response)
            connection.execute(
                """
                UPDATE record_descriptions
                SET owner_user=?, status='completed', matelab_response_json=?,
                    last_error_json=NULL, updated_at=?
                WHERE description_id=?
                """,
                (
                    owner_user,
                    json.dumps(safe_response, ensure_ascii=False, separators=(",", ":")),
                    iso_now(),
                    description_id,
                ),
            )
            event_data = {
                "description_id": description_id,
                "record_uid": str(row["record_uid"]),
                "notebook": {
                    "id": str(row["notebook_id"]),
                    "name": str(row["notebook_name"]),
                    "owner_user": owner_user,
                },
                "module_name": str(row["module_name"]),
                "content_sha256": str(row["content_sha256"]),
                "durability": "matelab_acknowledged",
            }
            self._append_integration_event(
                connection,
                event_type="matelab.record.description_added",
                subject=f"matelab-record:{row['record_uid']}",
                data=event_data,
                dedupe_key=f"record-description:{description_id}",
            )
            return self._description_receipt_in_connection(
                connection, description_id, duplicate=duplicate
            )

    def fail_record_description(
        self, description_id: str, *, error_code: str, message: str
    ) -> None:
        safe_error = {"code": error_code, "message": redact_text(message)}
        with self.connect() as connection, connection:
            connection.execute(
                """
                UPDATE record_descriptions SET status='pending', last_error_json=?, updated_at=?
                WHERE description_id=?
                """,
                (
                    json.dumps(safe_error, ensure_ascii=False, separators=(",", ":")),
                    iso_now(),
                    description_id,
                ),
            )

    def _description_receipt_in_connection(
        self,
        connection: sqlite3.Connection,
        description_id: str,
        *,
        duplicate: bool,
    ) -> RecordDescriptionReceipt:
        row = connection.execute(
            "SELECT * FROM record_descriptions WHERE description_id=?",
            (description_id,),
        ).fetchone()
        if row is None:
            raise ValueError("record description was not found")
        event = connection.execute(
            "SELECT seq FROM integration_events WHERE dedupe_key=?",
            (f"record-description:{description_id}",),
        ).fetchone()
        if event is None:
            raise ValueError("completed record description has no integration event")
        return RecordDescriptionReceipt(
            description_id=str(row["description_id"]),
            record_uid=str(row["record_uid"]),
            notebook_id=str(row["notebook_id"]),
            notebook_name=str(row["notebook_name"]),
            module_name=str(row["module_name"]),
            content_sha256=str(row["content_sha256"]),
            event_cursor=int(event["seq"]),
            duplicate=duplicate,
        )

    def list_integration_events(
        self,
        *,
        after: int = 0,
        limit: int = 100,
        event_types: set[str] | None = None,
    ) -> list[IntegrationEvent]:
        safe_limit = max(1, min(limit, 500))
        parameters: list[Any] = [max(0, after)]
        where = "seq>?"
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            where += f" AND event_type IN ({placeholders})"
            parameters.extend(sorted(event_types))
        parameters.append(safe_limit)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT seq, event_id, event_type, occurred_at, subject, data_json
                FROM integration_events WHERE {where} ORDER BY seq LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [
            IntegrationEvent(
                cursor=int(row["seq"]),
                id=str(row["event_id"]),
                type=str(row["event_type"]),
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                subject=str(row["subject"]),
                data=json.loads(row["data_json"]),
            )
            for row in rows
        ]

    def recent_experiments(
        self,
        *,
        actor_id: str,
        producer_id: str,
        since: datetime,
        limit: int = 10,
    ) -> list[RecentCandidate]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT capture_id, occurred_at, actor_id, producer_id FROM captures
                WHERE capture_kind=? AND actor_id=? AND producer_id=? AND occurred_at>=?
                ORDER BY occurred_at DESC LIMIT ?
                """,
                (
                    "experiment_run",
                    actor_id,
                    producer_id,
                    since.isoformat(),
                    limit,
                ),
            ).fetchall()
        return [
            RecentCandidate(
                capture_id=row["capture_id"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
                actor_id=row["actor_id"],
                producer_id=row["producer_id"],
            )
            for row in rows
        ]

    def metrics(self) -> dict[str, Any]:
        with self.connect() as connection:
            states = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM captures GROUP BY state"
                ).fetchall()
            }
            oldest = connection.execute(
                """
                SELECT MIN(created_at) AS oldest FROM captures
                WHERE state NOT IN (?, ?)
                """,
                (SyncState.COMPLETE.value, SyncState.ABANDONED.value),
            ).fetchone()["oldest"]
            orphans = connection.execute(
                "SELECT COUNT(*) AS count FROM orphan_candidates WHERE resolved_at IS NULL"
            ).fetchone()["count"]
        oldest_age = 0.0
        if oldest:
            oldest_age = max(0.0, (utc_now() - datetime.fromisoformat(oldest)).total_seconds())
        return {"states": states, "oldest_pending_seconds": oldest_age, "orphans": orphans}

    def latest_mappings(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT c.capture_id, c.manifest_json, c.manifest_sha256,
                           m.record_uid, m.record_version, m.record_ref_json,
                           m.export_sha256
                    FROM captures AS c
                    JOIN mappings AS m ON m.capture_id = c.capture_id
                    WHERE m.record_version = (
                        SELECT MAX(m2.record_version) FROM mappings AS m2
                        WHERE m2.capture_id = c.capture_id
                    )
                    ORDER BY c.capture_id
                    """
                ).fetchall()
            )

    def record_request(
        self,
        capture_id: str,
        *,
        operation: str,
        request_id: str,
        idempotency_key: str,
    ) -> None:
        with self.connect() as connection, connection:
            row = connection.execute(
                "SELECT actor_id, run_id FROM captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if row is None:
                raise CaptureNotFoundError("capture_id was not found")
            connection.execute(
                """
                INSERT INTO request_audit (
                    timestamp, request_id, operation, capture_id, actor_id, run_id,
                    idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iso_now(),
                    request_id,
                    operation,
                    capture_id,
                    row["actor_id"],
                    row["run_id"],
                    idempotency_key,
                ),
            )

    def _publish_capture_synced(self, connection: sqlite3.Connection, capture_id: str) -> None:
        row = connection.execute(
            """
            SELECT c.capture_id, c.sync_id, c.capture_kind, c.manifest_sha256,
                   m.record_ref_json
            FROM captures AS c
            JOIN mappings AS m ON m.capture_id=c.capture_id
            WHERE c.capture_id=?
            ORDER BY m.record_version DESC, m.created_at DESC LIMIT 1
            """,
            (capture_id,),
        ).fetchone()
        if row is None:
            raise MappingConflictError("a completed capture has no MatElab mapping")
        record_ref = json.loads(row["record_ref_json"])
        self._append_integration_event(
            connection,
            event_type="matelab.record.synced",
            subject=f"capture:{capture_id}",
            data={
                "capture_id": capture_id,
                "sync_id": str(row["sync_id"]),
                "capture_kind": str(row["capture_kind"]),
                "manifest_sha256": str(row["manifest_sha256"]),
                "matelab_ref": record_ref,
            },
            dedupe_key=f"capture:{capture_id}:synced",
        )

    def _append_integration_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        subject: str,
        data: dict[str, Any],
        dedupe_key: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO integration_events (
                event_id, dedupe_key, event_type, occurred_at, subject, data_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"evt-{uuid.uuid4()}",
                dedupe_key,
                event_type,
                iso_now(),
                subject,
                json.dumps(redact(data), ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def _append_journal(
        self,
        connection: sqlite3.Connection,
        capture_id: str,
        event: str,
        from_state: SyncState | None,
        to_state: SyncState | None,
        details: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO journal (
                capture_id, timestamp, event, from_state, to_state, details_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                capture_id,
                iso_now(),
                event,
                from_state.value if from_state else None,
                to_state.value if to_state else None,
                json.dumps(redact(details), ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def backup_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
