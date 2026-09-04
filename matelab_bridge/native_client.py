"""HTTP client used by the native console to control its local bridge."""

from __future__ import annotations

import mimetypes
import re
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx

from .config import BridgeConfig

CSRF_RE = re.compile(r'name="bridge-csrf" content="([^"]+)"')


class ConnectorApiError(RuntimeError):
    def __init__(self, problem: dict[str, Any], status_code: int) -> None:
        super().__init__(str(problem.get("message") or f"HTTP {status_code}"))
        self.problem = problem
        self.status_code = status_code


class NativeConnectorClient:
    def __init__(
        self,
        base_url: str,
        config: BridgeConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.config = config
        self.http = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(60, connect=5),
            transport=transport,
        )
        self.csrf = self._load_csrf()

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> NativeConnectorClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _load_csrf(self) -> str:
        response = self.http.get("/ui")
        response.raise_for_status()
        match = CSRF_RE.search(response.text)
        if match is None:
            raise RuntimeError("Connector API did not provide a desktop session token")
        return match.group(1)

    def _headers(self, scope: str | None = None) -> dict[str, str]:
        headers: dict[str, str] = {}
        if scope == "ui":
            headers["X-Bridge-UI-CSRF"] = self.csrf
        elif self.config.auth_mode == "token":
            token = {
                "submit": self.config.submit_token,
                "status": self.config.status_token,
                "maintenance": self.config.maintenance_token,
            }.get(scope or "")
            if token:
                headers["X-Bridge-Token"] = token
        return headers

    @staticmethod
    def _result(response: httpx.Response) -> Any:
        try:
            body = response.json()
        except ValueError as exc:
            raise ConnectorApiError(
                {
                    "code": "invalid_local_response",
                    "message": "本地 Connector 返回了无法识别的数据。",
                    "action": "重启应用后再试。",
                },
                response.status_code,
            ) from exc
        if response.is_error:
            problem = body.get("error", {}) if isinstance(body, dict) else {}
            raise ConnectorApiError(problem, response.status_code)
        return body

    def session(self) -> dict[str, Any]:
        value = self._result(self.http.get("/v1/ui/session", headers=self._headers("ui")))
        return dict(value)

    def login(self, username: str, password: str) -> dict[str, Any]:
        value = self._result(
            self.http.post(
                "/v1/ui/login",
                headers=self._headers("ui"),
                json={"username": username, "password": password},
            )
        )
        return dict(value)

    def logout(self) -> None:
        self._result(self.http.post("/v1/ui/logout", headers=self._headers("ui")))

    def notebooks(self) -> list[dict[str, Any]]:
        value = self._result(self.http.get("/v1/ui/notebooks", headers=self._headers("ui")))
        return [dict(item) for item in value.get("notebooks", [])]

    def set_default_notebook(self, notebook: dict[str, Any]) -> dict[str, Any]:
        value = self._result(
            self.http.put(
                "/v1/ui/default-notebook",
                headers=self._headers("ui"),
                json={
                    "notebook_id": str(notebook["id"]),
                    "notebook_name": str(notebook["name"]),
                },
            )
        )
        return dict(value)

    def summary(self) -> dict[str, Any]:
        value = self._result(self.http.get("/v1/console/summary", headers=self._headers("status")))
        return dict(value)

    def tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        value = self._result(
            self.http.get(
                "/v1/console/tasks",
                params={"limit": limit},
                headers=self._headers("status"),
            )
        )
        return [dict(item) for item in value]

    def retry(self, capture_id: str) -> dict[str, Any]:
        value = self._result(
            self.http.post(
                f"/v1/captures/{capture_id}/retry",
                headers=self._headers("maintenance"),
            )
        )
        return dict(value)

    def submit_note(
        self,
        *,
        notebook: dict[str, Any],
        title: str,
        content: str,
        actor_id: str | None,
        attachments: list[Path],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        data = {
            "notebook_id": str(notebook["id"]),
            "notebook_name": str(notebook["name"]),
            "title": title,
            "content": content,
        }
        if actor_id:
            data["actor_id"] = actor_id
        headers = self._headers("submit")
        headers["Idempotency-Key"] = idempotency_key or f"native-{uuid.uuid4()}"
        with ExitStack() as stack:
            upload_files = [
                (
                    "attachments",
                    (
                        path.name,
                        stack.enter_context(path.open("rb")),
                        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    ),
                )
                for path in attachments
            ]
            value = self._result(
                self.http.post(
                    "/v1/manual-submissions",
                    headers=headers,
                    data=data,
                    files=upload_files,
                )
            )
        return dict(value)
