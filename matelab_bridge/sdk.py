"""Thin Python SDK; it never discovers or reads Data Vault internals."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

import httpx

from .models import (
    CaptureEnvelope,
    IntegrationEvent,
    IntegrationEventBatch,
    RecordDescriptionReceipt,
    SyncReceipt,
    SyncState,
)


class BridgeClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        *,
        token: str | None = None,
        submit_token: str | None = None,
        status_token: str | None = None,
        maintenance_token: str | None = None,
        timeout: float = 60,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.submit_token = submit_token or token
        self.status_token = status_token or token
        self.maintenance_token = maintenance_token or token
        self.http = http_client or httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    def _headers(self, token: str | None) -> dict[str, str]:
        return {"X-Bridge-Token": token} if token else {}

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> BridgeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def create_capture(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        envelope = CaptureEnvelope.model_validate(dict(manifest))
        response = self.http.post(
            "/v1/captures",
            json=envelope.model_dump(mode="json", by_alias=True),
            headers=self._headers(self.submit_token),
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def upload_artifact(self, capture_id: str, artifact_id: str, path: Path) -> dict[str, Any]:
        with path.open("rb") as handle:
            response = self.http.put(
                f"/v1/captures/{capture_id}/artifacts/{artifact_id}",
                content=handle,
                headers=self._headers(self.submit_token),
            )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())

    def commit(self, capture_id: str) -> SyncReceipt:
        response = self.http.post(
            f"/v1/captures/{capture_id}/commit", headers=self._headers(self.submit_token)
        )
        response.raise_for_status()
        return SyncReceipt.model_validate(response.json())

    def status(self, capture_id: str) -> SyncReceipt:
        response = self.http.get(
            f"/v1/captures/{capture_id}", headers=self._headers(self.status_token)
        )
        response.raise_for_status()
        return SyncReceipt.model_validate(response.json())

    def retry(self, capture_id: str) -> SyncReceipt:
        response = self.http.post(
            f"/v1/captures/{capture_id}/retry",
            headers=self._headers(self.maintenance_token),
        )
        response.raise_for_status()
        return SyncReceipt.model_validate(response.json())

    def recent(
        self, actor_id: str, producer_id: str, *, within_minutes: int = 120
    ) -> list[dict[str, Any]]:
        response = self.http.get(
            "/v1/captures/recent",
            params={
                "actor_id": actor_id,
                "producer_id": producer_id,
                "within_minutes": within_minutes,
            },
            headers=self._headers(self.status_token),
        )
        response.raise_for_status()
        return cast(list[dict[str, Any]], response.json())

    def add_record_description(
        self,
        record_uid: str,
        content: str,
        *,
        title: str | None = None,
        notebook_id: str | None = None,
        notebook_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> RecordDescriptionReceipt:
        if bool(notebook_id) != bool(notebook_name):
            raise ValueError("notebook_id and notebook_name must be provided together")
        payload: dict[str, Any] = {"content": content}
        if title is not None:
            payload["title"] = title
        if notebook_id is not None and notebook_name is not None:
            payload["notebook_id"] = notebook_id
            payload["notebook_name"] = notebook_name
        headers = self._headers(self.submit_token)
        headers["Idempotency-Key"] = idempotency_key or f"sdk-{uuid.uuid4()}"
        response = self.http.post(
            f"/v1/records/{record_uid}/descriptions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        return RecordDescriptionReceipt.model_validate(response.json())

    def events(
        self,
        *,
        after: int = 0,
        limit: int = 100,
        event_types: list[str] | None = None,
    ) -> IntegrationEventBatch:
        params: list[tuple[str, str | int | float | bool | None]] = [
            ("after", after),
            ("limit", limit),
        ]
        params.extend(("type", event_type) for event_type in event_types or [])
        response = self.http.get(
            "/v1/events",
            params=params,
            headers=self._headers(self.status_token),
        )
        response.raise_for_status()
        return IntegrationEventBatch.model_validate(response.json())

    def stream_events(
        self,
        *,
        after: int = 0,
        event_types: list[str] | None = None,
    ) -> Iterator[IntegrationEvent]:
        """Yield replayed and live events from one SSE connection."""
        params: list[tuple[str, str | int | float | bool | None]] = [("after", after)]
        params.extend(("type", event_type) for event_type in event_types or [])
        with self.http.stream(
            "GET",
            "/v1/events/stream",
            params=params,
            headers=self._headers(self.status_token),
        ) as response:
            response.raise_for_status()
            data_lines: list[str] = []
            for line in response.iter_lines():
                if not line:
                    if data_lines:
                        yield IntegrationEvent.model_validate_json("\n".join(data_lines))
                        data_lines.clear()
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())

    def submit_bundle(
        self,
        manifest: Mapping[str, Any],
        artifact_paths: Mapping[str, Path],
    ) -> SyncReceipt:
        envelope = CaptureEnvelope.model_validate(dict(manifest))
        expected_ids = {
            item.artifact_id
            for item in envelope.artifacts
            if item.transfer_policy != "manifest_only"
        }
        if set(artifact_paths) != expected_ids:
            missing = sorted(expected_ids - set(artifact_paths))
            extra = sorted(set(artifact_paths) - expected_ids)
            raise ValueError(f"artifact path mismatch; missing={missing}, extra={extra}")
        descriptors = {item.artifact_id: item for item in envelope.artifacts}
        for artifact_id, path in artifact_paths.items():
            descriptor = descriptors[artifact_id]
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            if size != descriptor.size or digest.hexdigest() != descriptor.sha256.lower():
                raise ValueError(f"local artifact does not match descriptor: {artifact_id}")
        self.create_capture(envelope.model_dump(mode="json", by_alias=True))
        for artifact_id, path in artifact_paths.items():
            self.upload_artifact(envelope.capture_id, artifact_id, path)
        return self.commit(envelope.capture_id)

    def wait(
        self,
        capture_id: str,
        *,
        timeout: float = 120,
        interval: float = 1,
    ) -> SyncReceipt:
        deadline = time.monotonic() + timeout
        while True:
            receipt = self.status(capture_id)
            if receipt.state in {
                SyncState.COMPLETE,
                SyncState.AUTH_REQUIRED,
                SyncState.NEEDS_ATTENTION,
                SyncState.ABANDONED,
            }:
                return receipt
            if time.monotonic() >= deadline:
                raise TimeoutError(f"capture did not finish within {timeout} seconds")
            time.sleep(interval)
