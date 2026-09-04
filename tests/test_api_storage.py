from __future__ import annotations

import copy
import hashlib

from fastapi.testclient import TestClient

from matelab_bridge.api import create_app
from matelab_bridge.config import BridgeConfig
from matelab_bridge.models import SyncState
from matelab_bridge.storage import BridgeStore

from .conftest import artifact_descriptor


def test_capture_idempotency_and_conflict(config: BridgeConfig, manifest: dict) -> None:
    with TestClient(create_app(config)) as client:
        first = client.post("/v1/captures", json=manifest)
        duplicates = [client.post("/v1/captures", json=manifest) for _ in range(9)]
        duplicate = duplicates[-1]
        assert first.status_code == 201
        assert all(response.status_code == 200 for response in duplicates)
        assert duplicate.json()["duplicate"] is True
        assert first.json()["sync_id"] == duplicate.json()["sync_id"]

        changed = copy.deepcopy(manifest)
        changed["summary"]["observations"] = ["different"]
        conflict = client.post("/v1/captures", json=changed)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "capture_identity_conflict"


def test_artifact_ingress_commit_and_history(config: BridgeConfig, manifest: dict) -> None:
    content = b"print('traceable')\n"
    manifest["artifacts"] = [artifact_descriptor(content)]
    with TestClient(create_app(config)) as client:
        created = client.post(
            "/v1/captures",
            json=manifest,
            headers={"X-Request-ID": "request-create-1", "Idempotency-Key": "event-1"},
        )
        assert created.status_code == 201
        assert created.headers["X-Request-ID"] == "request-create-1"
        incomplete = client.post(f"/v1/captures/{manifest['capture_id']}/commit")
        assert incomplete.status_code == 422
        received = client.put(
            f"/v1/captures/{manifest['capture_id']}/artifacts/script", content=content
        )
        assert received.status_code == 200
        assert received.json()["sha256"] == hashlib.sha256(content).hexdigest()
        duplicate = client.put(
            f"/v1/captures/{manifest['capture_id']}/artifacts/script", content=content
        )
        assert duplicate.status_code == 200
        committed = client.post(f"/v1/captures/{manifest['capture_id']}/commit")
        assert committed.status_code == 202
        assert committed.json()["state"] == SyncState.READY.value
        history = client.get(f"/v1/captures/{manifest['capture_id']}/history").json()
        assert [event["event"] for event in history["events"]][-3:] == [
            "validation_started",
            "capture_accepted",
            "capture_ready",
        ]
        store = BridgeStore(config.database_path)
        with store.connect() as connection:
            audit = connection.execute(
                "SELECT request_id, actor_id, run_id, idempotency_key FROM request_audit "
                "WHERE operation='capture_create'"
            ).fetchone()
        assert tuple(audit) == ("request-create-1", "operator-01", "run-001", "event-1")


def test_artifact_hash_mismatch_does_not_persist(config: BridgeConfig, manifest: dict) -> None:
    manifest["artifacts"] = [artifact_descriptor(b"expected")]
    with TestClient(create_app(config)) as client:
        client.post("/v1/captures", json=manifest)
        response = client.put(
            f"/v1/captures/{manifest['capture_id']}/artifacts/script", content=b"different"
        )
        assert response.status_code == 422
        store = BridgeStore(config.database_path)
        assert store.get_artifact_row(manifest["capture_id"], "script")["state"] == "declared"


def test_strict_json_and_local_auth(config: BridgeConfig, manifest: dict) -> None:
    secured = BridgeConfig(
        data_dir=config.data_dir,
        auth_mode="token",
        submit_token="submit-secret",
        status_token="status-secret",
        maintenance_token="maintenance-secret",
        worker_enabled=False,
    )
    with TestClient(create_app(secured)) as client:
        assert client.post("/v1/captures", json=manifest).status_code == 401
        invalid = client.post(
            "/v1/captures",
            content=b'{"value":NaN}',
            headers={"X-Bridge-Token": "submit-secret"},
        )
        assert invalid.status_code == 400
        accepted = client.post(
            "/v1/captures", json=manifest, headers={"X-Bridge-Token": "submit-secret"}
        )
        assert accepted.status_code == 201
        status = client.get(
            f"/v1/captures/{manifest['capture_id']}",
            headers={"X-Bridge-Token": "status-secret"},
        )
        assert status.status_code == 200


def test_config_rejects_unprotected_network_listener(tmp_path) -> None:
    config = BridgeConfig(data_dir=tmp_path, host="0.0.0.0")
    try:
        config.validate()
    except ValueError as exc:
        assert "non-loopback" in str(exc)
    else:
        raise AssertionError("unprotected non-loopback listener was accepted")


def test_config_requires_distinct_scoped_tokens(tmp_path) -> None:
    config = BridgeConfig(
        data_dir=tmp_path,
        auth_mode="token",
        submit_token="same",
        status_token="same",
        maintenance_token="same",
    )
    try:
        config.validate()
    except ValueError as exc:
        assert "distinct" in str(exc)
    else:
        raise AssertionError("identical scoped tokens were accepted")
