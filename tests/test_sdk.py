from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from matelab_bridge.sdk import BridgeClient


def receipt(capture_id: str, state: str = "ready") -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "sync_id": "sync-1",
        "capture_id": capture_id,
        "source_locator": None,
        "source_hash": None,
        "manifest_sha256": f"sha256:{'a' * 64}",
        "matelab_ref": None,
        "state": state,
        "attempt": 0,
        "last_error": None,
        "created_at": now,
        "updated_at": now,
    }


def field_note_manifest(content: bytes) -> dict:
    return {
        "schema": "capture-envelope/v1",
        "capture_id": "note-sdk-1",
        "capture_kind": "field_note",
        "occurred_at": "2026-09-04T12:00:00+08:00",
        "producer": {"kind": "desktop", "producer_id": "pc", "actor_id": "a"},
        "artifacts": [
            {
                "artifact_id": "file",
                "role": "note_attachment",
                "filename": "note.txt",
                "mime_type": "text/plain",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "ingress": {"mode": "stream", "upload_id": "upload-file"},
                "transfer_policy": "copy",
            }
        ],
        "extensions": {"quick_note": {"original_text": "note"}},
    }


def test_sdk_submit_status_retry_recent_and_wait(tmp_path: Path) -> None:
    content = b"note"
    artifact = tmp_path / "note.txt"
    artifact.write_bytes(content)
    calls: list[tuple[str, str]] = []
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        calls.append((request.method, request.url.path))
        expected_token = "submit"
        if request.url.path == "/v1/captures/recent" or (
            request.method == "GET" and request.url.path.endswith("note-sdk-1")
        ):
            expected_token = "status"
        elif request.url.path.endswith("/retry"):
            expected_token = "maintenance"
        assert request.headers["X-Bridge-Token"] == expected_token
        if request.url.path == "/v1/captures" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "capture_id": "note-sdk-1",
                    "sync_id": "sync-1",
                    "manifest_sha256": f"sha256:{'a' * 64}",
                    "state": "receiving",
                    "duplicate": False,
                },
            )
        if request.url.path.endswith("/artifacts/file"):
            return httpx.Response(
                200,
                json={
                    "artifact_id": "file",
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "state": "received",
                },
            )
        if request.url.path.endswith("/commit"):
            return httpx.Response(202, json=receipt("note-sdk-1"))
        if request.url.path.endswith("/retry"):
            return httpx.Response(200, json=receipt("note-sdk-1"))
        if request.url.path == "/v1/captures/recent":
            return httpx.Response(
                200,
                json=[
                    {
                        "capture_id": "experiment-1",
                        "occurred_at": "2026-09-04T11:00:00+08:00",
                        "actor_id": "a",
                        "producer_id": "pc",
                    }
                ],
            )
        if request.url.path == "/v1/captures/note-sdk-1":
            status_calls += 1
            state = "ready" if status_calls == 1 else "complete"
            return httpx.Response(200, json=receipt("note-sdk-1", state))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    http = httpx.Client(base_url="http://bridge", transport=httpx.MockTransport(handler))
    with BridgeClient(
        http_client=http,
        submit_token="submit",
        status_token="status",
        maintenance_token="maintenance",
    ) as client:
        committed = client.submit_bundle(field_note_manifest(content), {"file": artifact})
        assert committed.state.value == "ready"
        assert client.retry("note-sdk-1").capture_id == "note-sdk-1"
        assert client.recent("a", "pc")[0]["capture_id"] == "experiment-1"
        assert client.wait("note-sdk-1", interval=0).state.value == "complete"
    assert ("PUT", "/v1/captures/note-sdk-1/artifacts/file") in calls


def test_sdk_rejects_artifact_mapping_and_changed_file(tmp_path: Path) -> None:
    content = b"expected"
    manifest = field_note_manifest(content)
    path = tmp_path / "note.txt"
    path.write_bytes(b"different")
    http = httpx.Client(
        base_url="http://bridge",
        transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
    )
    client = BridgeClient(http_client=http)
    with pytest.raises(ValueError, match="artifact path mismatch"):
        client.submit_bundle(manifest, {})
    with pytest.raises(ValueError, match="does not match"):
        client.submit_bundle(manifest, {"file": path})
    client.close()


def test_sdk_wait_timeout() -> None:
    http = httpx.Client(
        base_url="http://bridge",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=receipt("capture", "ready"))
        ),
    )
    client = BridgeClient(http_client=http)
    with pytest.raises(TimeoutError):
        client.wait("capture", timeout=0, interval=0)
    client.close()
