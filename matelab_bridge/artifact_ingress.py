"""Bounded, hash-verified Artifact ingestion."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

from .errors import (
    ArtifactConflictError,
    ArtifactHashMismatchError,
    ArtifactSizeMismatchError,
    InvalidStateError,
)
from .models import ArtifactReceipt, SyncState
from .security import contains_secret_material, safe_child
from .storage import BridgeStore


class ArtifactIngressService:
    def __init__(self, store: BridgeStore, artifact_root: Path, *, max_bytes: int) -> None:
        self.store = store
        self.artifact_root = artifact_root
        self.max_bytes = max_bytes
        artifact_root.mkdir(parents=True, exist_ok=True)

    async def receive(
        self,
        capture_id: str,
        artifact_id: str,
        stream: AsyncIterator[bytes],
    ) -> ArtifactReceipt:
        capture = self.store.get_capture_row(capture_id)
        if SyncState(capture["state"]) != SyncState.RECEIVING:
            raise InvalidStateError("artifacts can only be uploaded before commit")
        descriptor = self.store.get_artifact_row(capture_id, artifact_id)
        if descriptor["transfer_policy"] == "manifest_only":
            raise ArtifactConflictError("manifest_only artifacts do not accept content")
        if descriptor["state"] in {"received", "uploaded"}:
            return ArtifactReceipt(
                artifact_id=artifact_id,
                size=int(descriptor["received_size"]),
                sha256=str(descriptor["received_sha256"]),
            )

        expected_size = int(descriptor["expected_size"])
        descriptor_value = json.loads(descriptor["descriptor_json"])
        if descriptor_value["role"] in {"log", "stdout", "stderr"}:
            if expected_size > 10 * 1024**2:
                raise ArtifactSizeMismatchError("log artifacts are limited to 10 MB")
            if not descriptor_value["mime_type"].startswith(("text/", "application/json")):
                raise ArtifactConflictError("log artifacts must use a text or JSON MIME type")
        effective_limit = min(self.max_bytes, expected_size)
        capture_dir = safe_child(self.artifact_root, capture_id)
        capture_dir.mkdir(parents=True, exist_ok=True)
        destination = safe_child(capture_dir, f"{artifact_id}.blob")
        partial = safe_child(capture_dir, f".{artifact_id}.part")
        digest = hashlib.sha256()
        size = 0
        try:
            with partial.open("wb") as handle:
                async for chunk in stream:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > effective_limit:
                        raise ArtifactSizeMismatchError(
                            f"artifact exceeds its declared size of {expected_size} bytes"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size != expected_size:
                raise ArtifactSizeMismatchError(
                    f"artifact size is {size}, expected {expected_size} bytes"
                )
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != descriptor["expected_sha256"]:
                raise ArtifactHashMismatchError("artifact SHA-256 does not match the manifest")
            if descriptor_value["role"] in {"log", "stdout", "stderr"}:
                log_text = partial.read_text(encoding="utf-8", errors="replace")
                if contains_secret_material(log_text):
                    raise ArtifactConflictError(
                        "log artifact contains credential-shaped material and was rejected"
                    )
            os.replace(partial, destination)
            self.store.mark_artifact_received(
                capture_id,
                artifact_id,
                local_path=destination,
                size=size,
                sha256=actual_sha256,
            )
            return ArtifactReceipt(artifact_id=artifact_id, size=size, sha256=actual_sha256)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
