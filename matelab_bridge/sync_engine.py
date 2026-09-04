"""Restart-safe Saga that synchronizes durable captures to MatElab."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from .canonical import canonical_json, deterministic_upload_name, sha256_tag
from .config import BridgeConfig
from .errors import (
    MappingConflictError,
    MatelabAuthRequiredError,
    MatelabConflictError,
    MatelabContractError,
    MatelabRetryableError,
)
from .matelab_client import MatelabClient
from .models import MatelabRecordRef, SyncState
from .security import redact_text, safe_child
from .storage import BridgeStore
from .templates import build_import_payload, select_template

LOGGER = logging.getLogger("matelab_bridge.sync")


class SyncEngine:
    def __init__(self, config: BridgeConfig, store: BridgeStore, matelab: MatelabClient) -> None:
        self.config = config
        self.store = store
        self.matelab = matelab
        self.export_root = config.data_dir / "exports"
        self.export_root.mkdir(parents=True, exist_ok=True)

    def process_once(self) -> str | None:
        capture_id = self.store.claim_next()
        if capture_id is None:
            return None
        try:
            self._synchronize(capture_id)
        except MatelabAuthRequiredError as exc:
            self.store.mark_attention(
                capture_id,
                SyncState.AUTH_REQUIRED,
                error_code=exc.code,
                message=str(exc),
            )
        except MatelabRetryableError as exc:
            row = self.store.get_capture_row(capture_id)
            attempt = int(row["attempt"])
            if attempt >= self.config.max_attempts:
                self.store.mark_attention(
                    capture_id,
                    SyncState.NEEDS_ATTENTION,
                    error_code="retry_exhausted",
                    message=str(exc),
                )
            else:
                delay = min(3600.0, float(2 ** min(attempt, 10)))
                self.store.schedule_retry(
                    capture_id,
                    error_code=exc.code,
                    message=str(exc),
                    delay_seconds=delay,
                )
        except (MatelabConflictError, MatelabContractError, MappingConflictError) as exc:
            self._record_orphans(capture_id, str(exc))
            self.store.mark_attention(
                capture_id,
                SyncState.NEEDS_ATTENTION,
                error_code=exc.code,
                message=str(exc),
            )
        except Exception as exc:  # fail closed; worker must survive unexpected defects
            LOGGER.exception("unexpected sync failure for capture_id=%s", capture_id)
            self._record_orphans(capture_id, "unexpected local synchronization failure")
            self.store.mark_attention(
                capture_id,
                SyncState.NEEDS_ATTENTION,
                error_code="unexpected_sync_error",
                message=redact_text(str(exc)),
            )
        return capture_id

    def _synchronize(self, capture_id: str) -> None:
        capture_row = self.store.get_capture_row(capture_id)
        envelope = self.store.get_manifest(capture_id)
        selection = select_template(self.config, envelope.capture_kind)
        artifact_rows = self.store.list_artifacts(capture_id)

        for row in artifact_rows:
            if row["transfer_policy"] == "manifest_only" or row["state"] == "uploaded":
                continue
            if row["state"] != "received" or not row["local_path"]:
                raise MatelabContractError("committed capture has an unavailable artifact")
            descriptor = json.loads(row["descriptor_json"])
            upload_name = deterministic_upload_name(capture_id, row["artifact_id"])
            self.store.record_event(
                capture_id,
                "artifact_upload_intent",
                {
                    "artifact_id": row["artifact_id"],
                    "size": row["expected_size"],
                    "sha256_prefix": row["expected_sha256"][:12],
                },
            )
            receipt = self.matelab.upload_file(
                Path(row["local_path"]),
                uid=upload_name,
                name=upload_name,
                notebook=selection.notebook,
                mime_type=descriptor["mime_type"],
                expected_sha256=row["expected_sha256"],
            )
            receipt.setdefault("name", upload_name)
            receipt.setdefault("filename", descriptor["filename"])
            receipt.setdefault("size", descriptor["size"])
            receipt.setdefault("hash", descriptor["sha256"])
            self.store.mark_artifact_uploaded(capture_id, row["artifact_id"], receipt)

        self.store.transition(
            capture_id,
            SyncState.CREATING_RECORD,
            "record_create_intent",
            details={"record_uid": capture_row["record_uid"]},
        )
        artifact_rows = self.store.list_artifacts(capture_id)
        existing = self._find_record(selection.notebook, capture_row["record_uid"])
        import_receipt: dict[str, Any] = {}
        if existing is None:
            payload = build_import_payload(self.config, envelope, capture_row, artifact_rows)
            import_receipt = self.matelab.import_record(payload)
            existing = self._find_record(selection.notebook, capture_row["record_uid"])
            if existing is None:
                ids = import_receipt.get("id")
                record_id = ids[0] if isinstance(ids, list) and ids else "unknown"
                existing = {
                    "id": record_id,
                    "sn": capture_row["record_uid"],
                    "version": 1,
                    "locked": False,
                    "signed": False,
                }
        self.store.transition(
            capture_id,
            SyncState.POPULATING_RECORD,
            "record_create_receipt",
            details={"record_uid": capture_row["record_uid"], "record_id": existing.get("id")},
        )
        self.store.transition(capture_id, SyncState.VERIFYING_EXPORT, "record_export_intent")
        exported = self.matelab.export_record(
            selection.notebook,
            capture_row["record_uid"],
            user=self.config.owner_id,
        )
        export_bytes = canonical_json(exported)
        if not self._contains_value(exported, capture_id) or not self._contains_value(
            exported, capture_row["manifest_sha256"]
        ):
            raise MatelabConflictError(
                "exported record does not contain the expected Capture ID and Capture Hash"
            )
        export_sha256 = sha256_tag(export_bytes)
        version = int(existing.get("version") or 1)
        export_path = self._write_export(capture_id, version, export_bytes)
        notebook_id_value = import_receipt.get("eln_id") or self._configured_notebook_id(
            envelope.capture_kind.value
        )
        if notebook_id_value is None:
            raise MatelabContractError(
                "record was reconciled by UID but no stable notebook ID is configured"
            )
        notebook_id = str(notebook_id_value)
        attachment_hashes = [f"sha256:{row['expected_sha256']}" for row in artifact_rows]
        record_ref = MatelabRecordRef(
            server_id=self.config.server_id,
            owner_id=self.config.owner_id,
            notebook_id=notebook_id,
            notebook_name_snapshot=selection.notebook,
            record_id=str(existing.get("id", "unknown")),
            record_uid=str(existing.get("sn") or capture_row["record_uid"]),
            record_version=version,
            locked=bool(existing.get("locked", False)),
            signed=bool(existing.get("signed", False)),
            created_at=existing.get("datetime_create"),
            modified_at=existing.get("datetime_modify"),
            export_sha256=export_sha256,
            attachment_hashes=attachment_hashes,
            mutable_source=True,
        )
        self.store.save_mapping(capture_id, record_ref, export_json_path=export_path)
        self.store.transition(
            capture_id,
            SyncState.MATELAB_VERIFIED,
            "record_export_verified",
            details={"export_sha256": export_sha256, "record_version": version},
        )
        self.store.transition(capture_id, SyncState.COMPLETE, "sync_complete")

    def _find_record(self, notebook: str, record_uid: str) -> dict[str, Any] | None:
        matches = [item for item in self.matelab.items(notebook) if item.get("sn") == record_uid]
        if len(matches) > 1:
            raise MatelabConflictError("MatElab returned duplicate records for deterministic UID")
        return matches[0] if matches else None

    def _write_export(self, capture_id: str, version: int, export_bytes: bytes) -> Path:
        directory = safe_child(self.export_root, capture_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = safe_child(directory, f"v{version}.json")
        temporary = safe_child(directory, f".v{version}.tmp")
        with temporary.open("wb") as handle:
            handle.write(export_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return destination

    def _configured_notebook_id(self, capture_kind: str) -> str | None:
        if capture_kind == "experiment_run":
            return self.config.experiment_notebook_id
        if capture_kind in {"analysis_run", "derivation", "correction"}:
            return self.config.analysis_notebook_id
        return self.config.note_notebook_id

    @classmethod
    def _contains_value(cls, value: Any, expected: str) -> bool:
        if value == expected:
            return True
        if isinstance(value, str):
            return expected in value
        if isinstance(value, dict):
            return any(cls._contains_value(item, expected) for item in value.values())
        if isinstance(value, list):
            return any(cls._contains_value(item, expected) for item in value)
        return False

    def _record_orphans(self, capture_id: str, message: str) -> None:
        for row in self.store.list_artifacts(capture_id):
            if row["state"] == "uploaded" and row["matelab_receipt_json"]:
                receipt = json.loads(row["matelab_receipt_json"])
                self.store.add_orphan_candidate(
                    capture_id,
                    "uploaded_file",
                    str(receipt.get("name") or row["artifact_id"]),
                    {"reason": redact_text(message)},
                )


async def run_worker(
    engine: SyncEngine,
    stop_event: asyncio.Event,
    wake_event: asyncio.Event,
    *,
    poll_seconds: float,
) -> None:
    while not stop_event.is_set():
        processed = await asyncio.to_thread(engine.process_once)
        if processed is not None:
            continue
        with suppress(TimeoutError):
            await asyncio.wait_for(wake_event.wait(), timeout=poll_seconds)
        wake_event.clear()
