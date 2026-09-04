from __future__ import annotations

from pathlib import Path

import httpx

from matelab_bridge.config import BridgeConfig
from matelab_bridge.desktop_settings import DesktopSettings
from matelab_bridge.native_client import ConnectorApiError, NativeConnectorClient


def test_desktop_settings_round_trip_and_invalid_fallback(tmp_path: Path) -> None:
    settings = DesktopSettings(api_port=9988)
    settings.save(tmp_path)
    assert DesktopSettings.load(tmp_path, default_port=8765) == settings
    assert (tmp_path / "desktop-settings.json").read_bytes().endswith(b"\n")

    (tmp_path / "desktop-settings.json").write_text("not-json", encoding="utf-8")
    assert DesktopSettings.load(tmp_path, default_port=8765).api_port == 8765


def test_native_client_session_notebooks_tasks_and_problem(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ui":
            return httpx.Response(
                200,
                text='<meta name="bridge-csrf" content="native-test-token">',
            )
        if request.url.path == "/v1/ui/session":
            assert request.headers.get("X-Bridge-UI-CSRF") == "native-test-token"
            return httpx.Response(200, json={"authenticated": True})
        if request.url.path == "/v1/ui/notebooks":
            assert request.headers.get("X-Bridge-UI-CSRF") == "native-test-token"
            return httpx.Response(
                200,
                json={"notebooks": [{"id": "7", "name": "测试本", "editable": True}]},
            )
        if request.url.path == "/v1/console/tasks":
            return httpx.Response(200, json=[{"capture_id": "manual-1", "state": "complete"}])
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "matelab_auth_required",
                    "message": "需要登录",
                    "action": "重新登录",
                }
            },
        )

    config = BridgeConfig(data_dir=tmp_path)
    with NativeConnectorClient(
        "http://connector.test",
        config,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.session()["authenticated"] is True
        assert client.notebooks()[0]["name"] == "测试本"
        assert client.tasks()[0]["capture_id"] == "manual-1"
        try:
            client.logout()
        except ConnectorApiError as exc:
            assert exc.status_code == 401
            assert exc.problem["action"] == "重新登录"
        else:
            raise AssertionError("logout should surface the structured API problem")


def test_native_client_manual_submission_sends_reusable_idempotency_key(
    tmp_path: Path,
) -> None:
    seen_keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ui":
            return httpx.Response(
                200,
                text='<meta name="bridge-csrf" content="native-test-token">',
            )
        if request.url.path == "/v1/manual-submissions":
            seen_keys.append(request.headers.get("Idempotency-Key"))
            return httpx.Response(
                202,
                json={"capture_id": "manual-1", "sync_id": "sync-1", "state": "ready"},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    attachment = tmp_path / "trace.txt"
    attachment.write_text("trace", encoding="utf-8")
    config = BridgeConfig(data_dir=tmp_path)
    with NativeConnectorClient(
        "http://connector.test",
        config,
        transport=httpx.MockTransport(handler),
    ) as client:
        for _ in range(2):
            client.submit_note(
                notebook={"id": "21", "name": "测试本"},
                title="测试",
                content="正文",
                actor_id=None,
                attachments=[attachment],
                idempotency_key="desktop-submit-001",
            )

    assert seen_keys == ["desktop-submit-001", "desktop-submit-001"]
