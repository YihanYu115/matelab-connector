from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from matelab_bridge.api import create_app
from matelab_bridge.canonical import canonical_json, sha256_tag
from matelab_bridge.config import BridgeConfig
from matelab_bridge.desktop_settings import DesktopSettings
from matelab_bridge.errors import MatelabAuthRequiredError
from matelab_bridge.models import CaptureEnvelope, SyncState
from matelab_bridge.storage import BridgeStore
from matelab_bridge.sync_engine import SyncEngine
from matelab_bridge.templates import select_template


class GuiMatelab:
    def __init__(self) -> None:
        self.authenticated = False
        self.closed = False

    def credential_status(self) -> dict[str, Any]:
        if not self.authenticated:
            return {"configured": False}
        return {
            "configured": True,
            "access_valid": True,
            "access_expires_at": time.time() + 3600,
            "refresh_valid": True,
            "refresh_expires_at": time.time() + 86400,
        }

    def login(self, username: str, password: str) -> object:
        if username != "user" or password != "pass":
            raise MatelabAuthRequiredError("bad login")
        self.authenticated = True
        return object()

    def clear_tokens(self) -> None:
        self.authenticated = False

    def elns(self) -> dict[str, Any]:
        if not self.authenticated:
            raise MatelabAuthRequiredError("not logged in")
        return {
            "code": 0,
            "my": [{"showtext": "我的记录本", "id": 21, "owner": True, "server": "main"}],
            "share": [
                {
                    "showtext": "协作记录本",
                    "sn": "book-shared",
                    "owner": False,
                    "trans_username": "owner@example.test",
                    "server": "main",
                }
            ],
            "public": [{"showtext": "公开示例", "id": 99, "owner": False, "server": "main"}],
        }

    def close(self) -> None:
        self.closed = True


def _csrf(client: TestClient) -> str:
    response = client.get("/ui")
    assert response.status_code == 200
    assert "新建手动记录" in response.text
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    match = re.search(r'name="bridge-csrf" content="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def test_gui_login_notebook_selection_and_manual_ingress(config: BridgeConfig) -> None:
    store = BridgeStore(config.database_path)
    remote = GuiMatelab()
    engine = SyncEngine(config, store, remote)  # type: ignore[arg-type]
    with TestClient(create_app(config, store=store, engine=engine)) as client:
        token = _csrf(client)
        headers = {"X-Bridge-UI-CSRF": token}
        denied = client.get("/v1/ui/session")
        assert denied.status_code == 403
        assert denied.json()["error"]["action"]
        assert denied.json()["error"]["request_id"]

        assert client.get("/v1/ui/session", headers=headers).json()["authenticated"] is False
        bad = client.post(
            "/v1/ui/login",
            headers=headers,
            json={"username": "user", "password": "wrong"},
        )
        assert bad.status_code == 401
        assert bad.json()["error"]["code"] == "matelab_auth_required"
        logged_in = client.post(
            "/v1/ui/login",
            headers=headers,
            json={"username": "user", "password": "pass"},
        )
        assert logged_in.status_code == 200
        assert logged_in.json()["session"]["authenticated"] is True
        assert [item["name"] for item in logged_in.json()["notebooks"]] == [
            "我的记录本",
            "协作记录本",
            "公开示例",
        ]
        assert logged_in.json()["notebooks"][0]["id"] == "21"
        assert logged_in.json()["notebooks"][2]["editable"] is False
        public_denied = client.post(
            "/v1/manual-submissions",
            data={
                "title": "public note",
                "content": "must not be written",
                "notebook_id": "99",
                "notebook_name": "公开示例",
            },
        )
        assert public_denied.status_code == 403
        assert public_denied.json()["error"]["code"] == "notebook_not_writable"
        api_submission = client.post(
            "/v1/manual-submissions",
            data={
                "title": "API note",
                "content": "submitted by another local program",
                "notebook_id": "21",
                "notebook_name": "我的记录本",
            },
        )
        assert api_submission.status_code == 202
        assert api_submission.json()["status_url"].startswith("/v1/captures/")
        api_manifest = store.get_manifest(api_submission.json()["capture_id"])
        assert api_manifest.producer.kind == "local_api"
        summary = client.get("/v1/console/summary")
        assert summary.status_code == 200
        assert summary.json()["service"] == "running"
        tasks = client.get("/v1/console/tasks")
        assert tasks.status_code == 200
        assert tasks.json()[0]["capture_id"] == api_submission.json()["capture_id"]

        idempotent_data = {
            "title": "retry-safe API note",
            "content": "the same HTTP operation may be retried",
            "notebook_id": "21",
            "notebook_name": "我的记录本",
        }
        idempotent_headers = {"Idempotency-Key": "manual-operation-001"}
        first_attempt = client.post(
            "/v1/manual-submissions",
            headers=idempotent_headers,
            data=idempotent_data,
            files={"attachments": ("trace.txt", b"retry-safe", "text/plain")},
        )
        repeated_attempt = client.post(
            "/v1/manual-submissions",
            headers=idempotent_headers,
            data=idempotent_data,
            files={"attachments": ("trace.txt", b"retry-safe", "text/plain")},
        )
        assert first_attempt.status_code == 202
        assert first_attempt.json()["duplicate"] is False
        assert repeated_attempt.status_code == 202
        assert repeated_attempt.json()["duplicate"] is True
        assert repeated_attempt.json()["capture_id"] == first_attempt.json()["capture_id"]
        assert repeated_attempt.json()["sync_id"] == first_attempt.json()["sync_id"]
        changed_attempt = client.post(
            "/v1/manual-submissions",
            headers=idempotent_headers,
            data={**idempotent_data, "content": "different content"},
            files={"attachments": ("trace.txt", b"retry-safe", "text/plain")},
        )
        assert changed_attempt.status_code == 409
        assert changed_attempt.json()["error"]["code"] == "capture_identity_conflict"

        mismatch = client.post(
            "/v1/ui/manual-submissions",
            headers=headers,
            data={
                "title": "title",
                "content": "body",
                "notebook_id": "21",
                "notebook_name": "renamed",
            },
        )
        assert mismatch.status_code == 404
        assert mismatch.json()["error"]["code"] == "notebook_not_found"

        submitted = client.post(
            "/v1/ui/manual-submissions",
            headers={**headers, "X-Request-ID": "gui-request-1"},
            data={
                "title": "低温系统检查",
                "content": "真空正常, 开始降温。",
                "notebook_id": "21",
                "notebook_name": "我的记录本",
                "actor_id": "operator",
            },
            files={"attachments": ("trace.txt", b"trace-data", "text/plain")},
        )
        assert submitted.status_code == 202
        capture_id = submitted.json()["capture_id"]
        assert submitted.json()["state"] == "ready"
        assert submitted.headers["X-Request-ID"] == "gui-request-1"
        manifest = store.get_manifest(capture_id)
        assert manifest.routing_hints["target_notebook_id"] == "21"
        assert manifest.extensions["quick_note"]["title"] == "低温系统检查"
        artifact = store.get_artifact_row(capture_id, "attachment-001")
        assert artifact["received_sha256"] == hashlib.sha256(b"trace-data").hexdigest()
        status = client.get(f"/v1/ui/captures/{capture_id}", headers=headers)
        assert status.json()["state"] == "ready"

        logged_out = client.post("/v1/ui/logout", headers=headers)
        assert logged_out.json()["session"]["authenticated"] is False
        replay_while_logged_out = client.post(
            "/v1/manual-submissions",
            headers=idempotent_headers,
            data=idempotent_data,
            files={"attachments": ("trace.txt", b"retry-safe", "text/plain")},
        )
        assert replay_while_logged_out.status_code == 202
        assert replay_while_logged_out.json()["capture_id"] == first_attempt.json()["capture_id"]
        assert replay_while_logged_out.json()["duplicate"] is True
        store.mark_attention(
            capture_id,
            SyncState.AUTH_REQUIRED,
            error_code="matelab_auth_required",
            message="expired",
        )
        resumed = client.post(
            "/v1/ui/login",
            headers=headers,
            json={"username": "user", "password": "pass"},
        )
        assert resumed.json()["resumed_tasks"] == 1
        assert store.get_receipt(capture_id).state == SyncState.READY
    assert remote.closed is True


def test_api_uses_persisted_default_notebook_and_preserves_idempotency(
    config: BridgeConfig,
) -> None:
    store = BridgeStore(config.database_path)
    remote = GuiMatelab()
    engine = SyncEngine(config, store, remote)  # type: ignore[arg-type]
    with TestClient(create_app(config, store=store, engine=engine)) as client:
        token = _csrf(client)
        ui_headers = {"X-Bridge-UI-CSRF": token}
        client.post(
            "/v1/ui/login",
            headers=ui_headers,
            json={"username": "user", "password": "pass"},
        ).raise_for_status()

        current = client.get("/v1/default-notebook")
        assert current.status_code == 200
        assert current.json() == {"configured": False, "notebook": None}

        missing = client.post(
            "/v1/manual-submissions",
            data={"title": "没有默认本", "content": "应返回明确错误"},
        )
        assert missing.status_code == 409
        assert missing.json()["error"]["code"] == "default_notebook_not_configured"
        assert "设为 API 默认记录本" in missing.json()["error"]["action"]

        incomplete = client.post(
            "/v1/manual-submissions",
            data={"title": "字段不完整", "content": "只给 ID", "notebook_id": "21"},
        )
        assert incomplete.status_code == 422
        assert incomplete.json()["error"]["code"] == "notebook_selection_incomplete"

        denied = client.put(
            "/v1/ui/default-notebook",
            json={"notebook_id": "21", "notebook_name": "我的记录本"},
        )
        assert denied.status_code == 403
        saved = client.put(
            "/v1/ui/default-notebook",
            headers=ui_headers,
            json={"notebook_id": "21", "notebook_name": "我的记录本"},
        )
        assert saved.status_code == 200
        assert saved.json()["notebook"] == {"id": "21", "name": "我的记录本"}
        settings = DesktopSettings.load(config.data_dir, default_port=config.port)
        assert settings.default_notebook_id == "21"
        assert settings.default_notebook_name == "我的记录本"
        assert client.get("/v1/default-notebook").json() == {
            "configured": True,
            "notebook": {"id": "21", "name": "我的记录本"},
        }

        request_data = {"title": "默认路由记录", "content": "调用方没有传记录本字段"}
        request_headers = {"Idempotency-Key": "default-notebook-operation-001"}
        first = client.post(
            "/v1/manual-submissions",
            headers=request_headers,
            data=request_data,
        )
        assert first.status_code == 202
        first_manifest = store.get_manifest(first.json()["capture_id"])
        assert first_manifest.routing_hints["target_notebook_id"] == "21"
        assert first_manifest.routing_hints["target_notebook"] == "我的记录本"

        changed = client.put(
            "/v1/ui/default-notebook",
            headers=ui_headers,
            json={"notebook_id": "book-shared", "notebook_name": "协作记录本"},
        )
        assert changed.status_code == 200
        replay = client.post(
            "/v1/manual-submissions",
            headers=request_headers,
            data=request_data,
        )
        assert replay.status_code == 202
        assert replay.json()["duplicate"] is True
        assert replay.json()["capture_id"] == first.json()["capture_id"]

        next_submission = client.post(
            "/v1/manual-submissions",
            data={"title": "新的默认路由", "content": "应该进入协作记录本"},
        )
        assert next_submission.status_code == 202
        next_manifest = store.get_manifest(next_submission.json()["capture_id"])
        assert next_manifest.routing_hints["target_notebook_id"] == "book-shared"
        assert next_manifest.routing_hints["target_notebook"] == "协作记录本"

        summary = client.get("/v1/console/summary").json()
        assert summary["default_notebook"] == {
            "id": "book-shared",
            "name": "协作记录本",
        }


class ManualSyncMatelab:
    def __init__(self) -> None:
        self.record: dict[str, Any] | None = None
        self.data: dict[str, Any] = {}
        self.create_payload: dict[str, Any] | None = None
        self.update_payload: dict[str, Any] | None = None
        self.upload_notebook: str | None = None

    def upload_file(self, path: Path, **kwargs: Any) -> dict[str, Any]:
        self.upload_notebook = kwargs["notebook"]
        return {
            "name": kwargs["name"],
            "filename": "trace.txt",
            "size": path.stat().st_size,
            "hash": kwargs["expected_sha256"],
        }

    def items(self, _notebook: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return [self.record] if self.record else []

    def create_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.create_payload = payload
        self.record = {
            "id": 77,
            "sn": payload["uid"],
            "version": 1,
            "locked": False,
            "signed": False,
        }
        return {"code": 0}

    def update_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.update_payload = payload
        self.data = {module["name"]: module["data"] for module in payload["addModule"]}
        return {"code": 0}

    def export_record(self, notebook: str, uid: str, **_kwargs: Any) -> dict[str, Any]:
        return {"code": 0, "dataset": [{"eln": notebook, "uid": uid, "data": self.data}]}


def test_manual_sync_uses_selected_notebook_and_create_update(config: BridgeConfig) -> None:
    content = b"trace-data"
    artifact_path = config.data_dir / "trace.txt"
    artifact_path.write_bytes(content)
    manifest = {
        "schema": "capture-envelope/v1",
        "capture_id": "manual-test-1",
        "capture_kind": "field_note",
        "occurred_at": "2026-09-04T08:21:13+08:00",
        "producer": {"kind": "desktop_gui", "producer_id": "pc", "actor_id": "operator"},
        "scope": {},
        "summary": {"observations": ["body"]},
        "artifacts": [
            {
                "artifact_id": "attachment-001",
                "role": "note_attachment",
                "filename": "trace.txt",
                "mime_type": "text/plain",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "ingress": {"mode": "stream", "upload_id": "upload-1"},
                "transfer_policy": "copy",
            }
        ],
        "routing_hints": {
            "creation_mode": "create_update",
            "target_notebook": "目标记录本",
            "target_notebook_id": "book-42",
            "target_user": None,
        },
        "extensions": {
            "quick_note": {
                "title": "检查记录",
                "original_text": "body",
                "human_confirmed": True,
            }
        },
    }
    envelope = CaptureEnvelope.model_validate(manifest)
    canonical = canonical_json(envelope.model_dump(mode="json", by_alias=True))
    template = select_template(config, envelope.capture_kind)
    store = BridgeStore(config.database_path)
    store.create_capture(
        envelope,
        canonical,
        sha256_tag(canonical),
        template_id="matelab-manual-create/v1",
        template_version=template.version,
        template_sha256=template.sha256,
    )
    store.mark_artifact_received(
        envelope.capture_id,
        "attachment-001",
        local_path=artifact_path,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    store.commit_capture(envelope.capture_id)
    remote = ManualSyncMatelab()
    SyncEngine(config, store, remote).process_once()  # type: ignore[arg-type]

    receipt = store.get_receipt(envelope.capture_id)
    assert receipt.state == SyncState.COMPLETE
    assert receipt.matelab_ref is not None
    assert receipt.matelab_ref.notebook_id == "book-42"
    assert receipt.matelab_ref.notebook_name_snapshot == "目标记录本"
    assert remote.upload_notebook == "目标记录本"
    assert remote.create_payload is not None and remote.create_payload["eln"] == "目标记录本"
    assert remote.update_payload is not None
    assert {item["name"] for item in remote.update_payload["addModule"]} == {
        "记录内容",
        "附件",
        "Connector 元数据",
    }
    assert "elnurl://" in remote.data["附件"]
    assert envelope.capture_id in remote.data["Connector 元数据"]
