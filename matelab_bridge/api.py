"""Local Submission Gateway HTTP API."""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import json
import logging
import mimetypes
import re
import secrets
import socket
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html import escape
from importlib.resources import files as resource_files
from typing import Annotated, Any, Literal

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import Field, SecretStr, ValidationError

from . import __version__
from .artifact_ingress import ArtifactIngressService
from .canonical import canonical_json, sha256_tag, strict_json_loads
from .config import BridgeConfig
from .desktop_settings import DesktopSettings
from .errors import (
    BridgeError,
    DefaultNotebookNotConfiguredError,
    MatelabContractError,
    NotebookNotFoundError,
    NotebookNotWritableError,
    NotebookSelectionIncompleteError,
    RecordNotFoundError,
    RecordNotWritableError,
)
from .matelab_client import MatelabClient
from .models import (
    ArtifactReceipt,
    CaptureAccepted,
    CaptureEnvelope,
    IntegrationEventBatch,
    RecentCandidate,
    RecordDescriptionReceipt,
    RecordDescriptionRequest,
    StrictModel,
    SyncReceipt,
    SyncState,
)
from .port_manager import NATIVE_CONSOLE_API_VERSION
from .security import redact_text
from .storage import BridgeStore
from .sync_engine import SyncEngine, run_worker
from .templates import build_description_update_payload, select_template

ScopeName = Literal["submit", "status", "maintenance"]
AUDIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z0-9_]+)+$")
LOGGER = logging.getLogger("matelab_bridge.api")


class UiLoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=255)
    password: SecretStr = Field(min_length=1, max_length=1024)


class UiDefaultNotebookRequest(StrictModel):
    notebook_id: str = Field(min_length=1, max_length=255)
    notebook_name: str = Field(min_length=1, max_length=255)


class DefaultNotebookReference(StrictModel):
    id: str
    name: str


class DefaultNotebookStatus(StrictModel):
    configured: bool
    notebook: DefaultNotebookReference | None


class ManualSubmissionAccepted(StrictModel):
    capture_id: str
    sync_id: str
    state: SyncState
    status_url: str
    duplicate: bool


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return str(value or f"req-{uuid.uuid4()}")


def _problem(
    request: Request,
    *,
    code: str,
    message: str,
    retryable: bool,
    action: str,
    details: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "action": action,
        "request_id": _request_id(request),
    }
    if details is not None:
        error["details"] = details
    return {"error": error}


def _normalize_notebooks(result: dict[str, Any]) -> list[dict[str, Any]]:
    grouped = any(group in result for group in ("my", "share", "public"))
    candidates: list[tuple[dict[str, Any], str]] = []
    if grouped:
        for group in ("my", "share", "public"):
            raw_group = result.get(group, [])
            if isinstance(raw_group, dict):
                raw_group = [raw_group]
            if not isinstance(raw_group, list):
                raise ValueError(f"MatElab {group} 记录本列表不是数组")
            candidates.extend((item, group) for item in raw_group if isinstance(item, dict))
    else:
        raw: Any = result.get("items") or result.get("elns") or result.get("data") or []
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raise ValueError("MatElab 记录本列表不是数组")
        candidates.extend((item, "accessible") for item in raw if isinstance(item, dict))
    normalized: list[dict[str, Any]] = []
    for item, access in candidates:
        name = str(item.get("showtext") or item.get("name") or item.get("title") or "").strip()
        notebook_id = str(item.get("sn") or item.get("id") or "").strip()
        if not name or not notebook_id:
            continue
        owner_value = item.get("owner", access == "my")
        owner = (
            access == "my"
            or owner_value is True
            or str(owner_value).lower()
            in {
                "1",
                "true",
                "yes",
            }
        )
        user_value = item.get("user") or item.get("username") or item.get("trans_username")
        target_user = None if owner or not user_value else str(user_value)
        editable = access != "public" and (owner or access == "accessible" or bool(target_user))
        normalized.append(
            {
                "id": notebook_id,
                "name": name,
                "server": str(item.get("server") or ""),
                "owner": owner,
                "user": target_user,
                "access": access,
                "editable": editable,
            }
        )
    access_order = {"my": 0, "accessible": 1, "share": 2, "public": 3}
    normalized.sort(key=lambda item: (access_order.get(item["access"], 9), item["name"].casefold()))
    return normalized


def _audit_ids(
    capture_id: str, request_id: str | None, idempotency_key: str | None
) -> tuple[str, str]:
    request_id = request_id or f"req-{uuid.uuid4()}"
    idempotency_key = idempotency_key or capture_id
    if not AUDIT_ID_RE.fullmatch(request_id) or not AUDIT_ID_RE.fullmatch(idempotency_key):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail="request and idempotency identifiers must be opaque ASCII identifiers",
        )
    return request_id, idempotency_key


def _validation_details(exc: ValidationError | RequestValidationError) -> list[dict[str, Any]]:
    return [
        {
            "loc": [str(part) for part in error["loc"]],
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]


class LocalAuthorizer:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config

    def dependency(self, scope: ScopeName) -> Callable[..., None]:
        def authorize(x_bridge_token: str | None = Header(default=None)) -> None:
            if self.config.auth_mode == "none":
                return
            expected = {
                "submit": self.config.submit_token,
                "status": self.config.status_token,
                "maintenance": self.config.maintenance_token,
            }[scope]
            if (
                not expected
                or not x_bridge_token
                or not secrets.compare_digest(expected, x_bridge_token)
            ):
                from fastapi import HTTPException

                raise HTTPException(status_code=401, detail="invalid local bridge credential")

        return authorize


def create_app(
    config: BridgeConfig | None = None,
    *,
    store: BridgeStore | None = None,
    engine: SyncEngine | None = None,
) -> FastAPI:
    config = config or BridgeConfig.from_env()
    config.validate()
    store = store or BridgeStore(config.database_path)
    matelab = engine.matelab if engine else MatelabClient(config)
    engine = engine or SyncEngine(config, store, matelab)
    ingress = ArtifactIngressService(
        store, config.data_dir / "artifacts", max_bytes=config.max_artifact_bytes
    )
    authorizer = LocalAuthorizer(config)
    stop_event = asyncio.Event()
    wake_event = asyncio.Event()
    manual_submission_lock = asyncio.Lock()
    worker_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        nonlocal worker_task
        store.recover_inflight()
        if config.worker_enabled:
            worker_task = asyncio.create_task(
                run_worker(
                    engine,
                    stop_event,
                    wake_event,
                    poll_seconds=config.worker_poll_seconds,
                )
            )
        yield
        stop_event.set()
        wake_event.set()
        if worker_task:
            await worker_task
        matelab.close()

    app = FastAPI(
        title="MatElab Desktop Bridge",
        version=__version__,
        description="Durable local Capture Envelope submission gateway",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.store = store
    app.state.engine = engine
    app.state.wake_event = wake_event
    ui_csrf = secrets.token_urlsafe(32)
    ui_root = resource_files("matelab_bridge.ui")
    ui_index = ui_root.joinpath("index.html").read_text(encoding="utf-8")
    ui_styles = ui_root.joinpath("styles.css").read_text(encoding="utf-8")
    ui_script = ui_root.joinpath("app.js").read_text(encoding="utf-8")

    @app.middleware("http")
    async def request_identity(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.get("X-Request-ID")
        request.state.request_id = (
            supplied if supplied and AUDIT_ID_RE.fullmatch(supplied) else f"req-{uuid.uuid4()}"
        )
        response = await call_next(request)
        response.headers.setdefault("X-Request-ID", request.state.request_id)
        return response

    @app.exception_handler(BridgeError)
    async def bridge_error_handler(request: Request, exc: BridgeError) -> JSONResponse:
        status = {
            "capture_not_found": 404,
            "artifact_not_found": 404,
            "notebook_not_found": 404,
            "notebook_not_writable": 403,
            "default_notebook_not_configured": 409,
            "notebook_selection_incomplete": 422,
            "record_not_found": 404,
            "record_not_writable": 409,
            "idempotency_conflict": 409,
            "capture_identity_conflict": 409,
            "artifact_conflict": 409,
            "mapping_conflict": 409,
            "matelab_conflict": 409,
            "invalid_state": 409,
            "capture_incomplete": 422,
            "artifact_hash_mismatch": 422,
            "artifact_size_mismatch": 422,
            "invalid_json": 400,
            "matelab_auth_required": 401,
            "matelab_contract_error": 502,
            "matelab_retryable": 503,
        }.get(exc.code, 500)
        messages = {
            "capture_not_found": "找不到这条本地提交记录。",
            "artifact_not_found": "找不到声明的附件。",
            "notebook_not_found": "所选记录本已不存在或当前账号无权访问。",
            "notebook_not_writable": "所选记录本是只读记录本, 不能上传记录。",
            "default_notebook_not_configured": "尚未设置供本地 API 使用的默认记录本。",
            "notebook_selection_incomplete": (
                "notebook_id 和 notebook_name 必须同时提供, 或者同时省略以使用默认记录本。"
            ),
            "record_not_found": "目标实验记录不存在于所选记录本中。",
            "record_not_writable": "目标实验记录已定稿、签名或不可修改。",
            "idempotency_conflict": "同一幂等键对应了不同的补充描述请求。",
            "capture_identity_conflict": "同一提交编号对应了不同内容。",
            "artifact_conflict": "附件状态或内容发生冲突。",
            "mapping_conflict": "本地记录与 MatElab 记录的映射发生冲突。",
            "matelab_conflict": "MatElab 中的记录或附件与本地内容不一致。",
            "invalid_state": "当前提交状态不允许执行此操作。",
            "capture_incomplete": "仍有必需附件尚未接收。",
            "artifact_hash_mismatch": "附件校验失败, 文件内容与声明不一致。",
            "artifact_size_mismatch": "附件大小与声明不一致或超过限制。",
            "invalid_json": "请求不是有效的严格 JSON。",
            "matelab_auth_required": "MatElab 登录已失效或账号密码不正确。",
            "matelab_contract_error": "MatElab 拒绝了请求或返回了无法识别的数据。",
            "matelab_retryable": "暂时无法连接 MatElab。",
        }
        actions = {
            "capture_not_found": "检查提交编号后重试。",
            "artifact_not_found": "检查附件编号是否与清单一致。",
            "notebook_not_found": "刷新记录本列表并重新选择。",
            "notebook_not_writable": "选择“我的”记录本, 或请记录本所有者授予编辑权限。",
            "default_notebook_not_configured": (
                "打开 Connector 的“手动提交”页面, 选择记录本并点击“设为 API 默认记录本”。"
            ),
            "notebook_selection_incomplete": "补齐记录本 ID 和名称, 或删除这两个字段。",
            "record_not_found": "检查记录 UID 和记录本后重试。",
            "record_not_writable": "在 MatElab 中确认记录状态; 不要自动覆盖已定稿内容。",
            "idempotency_conflict": "为新描述使用新的 Idempotency-Key。",
            "capture_identity_conflict": (
                "为不同内容使用新的 capture_id; 手动上传则改用新的 Idempotency-Key。"
            ),
            "artifact_conflict": "检查附件后重新创建提交。",
            "mapping_conflict": "停止重试并运行诊断, 避免覆盖远端记录。",
            "matelab_conflict": "在 MatElab 中检查同 UID 记录和附件后再处理。",
            "invalid_state": "刷新状态; 如任务失败, 可从维护接口重试。",
            "capture_incomplete": "补齐错误详情中列出的附件后再次提交。",
            "artifact_hash_mismatch": "重新选择原始文件, 不要在上传过程中修改它。",
            "artifact_size_mismatch": "重新选择文件, 或调高允许的附件上限。",
            "invalid_json": "按 /docs 中的请求示例修正 JSON。",
            "matelab_auth_required": "请在 GUI 中重新登录 MatElab。",
            "matelab_contract_error": "刷新记录本后重试; 持续失败请生成诊断包。",
            "matelab_retryable": "检查网络和 MatElab 服务, 任务会自动重试。",
        }
        return JSONResponse(
            status_code=status,
            content=_problem(
                request,
                code=exc.code,
                message=messages.get(exc.code, "连接器处理请求失败。"),
                retryable=exc.retryable,
                action=actions.get(exc.code, "请记录请求编号并运行诊断。"),
                details={"reason": redact_text(str(exc))},
            ),
        )

    @app.exception_handler(ValidationError)
    async def pydantic_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_problem(
                request,
                code="schema_validation_error",
                message="提交内容未通过格式校验。",
                retryable=False,
                action="根据 details 修正字段后重新提交。",
                details=_validation_details(exc),
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=_problem(
                request,
                code="request_validation_error",
                message="请求参数不完整或格式不正确。",
                retryable=False,
                action="根据 details 补全或修正字段。",
                details=_validation_details(exc),
            ),
        )

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = {
            401: "local_auth_required",
            404: "not_found",
            413: "payload_too_large",
        }.get(exc.status_code, "http_error")
        actions = {
            401: "提供正确的本地 API 凭证, 或从 GUI 重新操作。",
            404: "检查访问地址或资源编号。",
            413: "缩小文件或清单后重新提交。",
        }
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content=_problem(
                request,
                code=code,
                message=str(exc.detail),
                retryable=False,
                action=actions.get(exc.status_code, "修正请求后重试。"),
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        LOGGER.exception(
            "unexpected API error request_id=%s path=%s error_type=%s",
            _request_id(request),
            request.url.path,
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content=_problem(
                request,
                code="internal_error",
                message="连接器发生了未预期的内部错误。",
                retryable=False,
                action="保存请求编号并运行 matelab-bridge diagnostic-bundle。",
            ),
        )

    def require_ui_csrf(x_bridge_ui_csrf: str | None = Header(default=None)) -> None:
        if not x_bridge_ui_csrf or not secrets.compare_digest(ui_csrf, x_bridge_ui_csrf):
            raise HTTPException(status_code=403, detail="GUI 会话校验失败, 请刷新页面后重试。")

    def session_payload() -> dict[str, Any]:
        status = matelab.credential_status()
        authenticated = bool(status.get("access_valid") or status.get("refresh_valid"))
        return {
            "authenticated": authenticated,
            "matelab_url": config.matelab_url,
            "access_valid": bool(status.get("access_valid", False)),
            "access_expires_at": status.get("access_expires_at"),
            "refresh_valid": bool(status.get("refresh_valid", False)),
            "refresh_expires_at": status.get("refresh_expires_at"),
        }

    async def load_notebooks() -> list[dict[str, Any]]:
        result = await asyncio.to_thread(matelab.elns)
        try:
            notebooks = _normalize_notebooks(result)
        except ValueError as exc:
            raise MatelabContractError(str(exc)) from exc
        return notebooks

    def load_default_notebook() -> dict[str, str] | None:
        settings = DesktopSettings.load(config.data_dir, default_port=config.port)
        if not settings.default_notebook_id or not settings.default_notebook_name:
            return None
        return {
            "id": settings.default_notebook_id,
            "name": settings.default_notebook_name,
        }

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/ui", status_code=307)

    @app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
    def desktop_ui() -> HTMLResponse:
        document = ui_index.replace("__BRIDGE_CSRF__", escape(ui_csrf, quote=True)).replace(
            "__MATELAB_URL__", escape(config.matelab_url, quote=True)
        )
        return HTMLResponse(
            document,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                    "base-uri 'none'; form-action 'self'"
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/ui/styles.css", include_in_schema=False)
    def desktop_styles() -> Response:
        return Response(
            ui_styles,
            media_type="text/css",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/ui/app.js", include_in_schema=False)
    def desktop_script() -> Response:
        return Response(
            ui_script,
            media_type="application/javascript",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get(
        "/v1/ui/session",
        dependencies=[Depends(require_ui_csrf)],
        tags=["desktop-gui"],
    )
    def ui_session() -> dict[str, Any]:
        return session_payload()

    @app.post(
        "/v1/ui/login",
        dependencies=[Depends(require_ui_csrf)],
        tags=["desktop-gui"],
    )
    async def ui_login(credentials: UiLoginRequest) -> dict[str, Any]:
        await asyncio.to_thread(
            matelab.login,
            credentials.username.strip(),
            credentials.password.get_secret_value(),
        )
        notebooks = await load_notebooks()
        resumed = store.retry_auth_required()
        if resumed:
            wake_event.set()
        return {
            "session": session_payload(),
            "notebooks": notebooks,
            "resumed_tasks": resumed,
        }

    @app.post(
        "/v1/ui/logout",
        dependencies=[Depends(require_ui_csrf)],
        tags=["desktop-gui"],
    )
    def ui_logout() -> dict[str, Any]:
        matelab.clear_tokens()
        return {"logged_out": True, "session": session_payload()}

    @app.get(
        "/v1/ui/notebooks",
        dependencies=[Depends(require_ui_csrf)],
        tags=["desktop-gui"],
    )
    async def ui_notebooks() -> dict[str, Any]:
        return {"notebooks": await load_notebooks()}

    @app.put(
        "/v1/ui/default-notebook",
        response_model=DefaultNotebookStatus,
        dependencies=[Depends(require_ui_csrf)],
        tags=["desktop-gui"],
    )
    async def ui_set_default_notebook(selection: UiDefaultNotebookRequest) -> dict[str, Any]:
        notebook_id = selection.notebook_id.strip()
        notebook_name = selection.notebook_name.strip()
        notebooks = await load_notebooks()
        selected = next(
            (
                notebook
                for notebook in notebooks
                if notebook["id"] == notebook_id and notebook["name"] == notebook_name
            ),
            None,
        )
        if selected is None:
            raise NotebookNotFoundError(
                "the selected notebook ID/name pair is not in the current MatElab list"
            )
        if not selected["editable"]:
            raise NotebookNotWritableError("the selected MatElab notebook is public or read-only")
        settings = DesktopSettings.load(config.data_dir, default_port=config.port)
        replace(
            settings,
            default_notebook_id=selected["id"],
            default_notebook_name=selected["name"],
        ).save(config.data_dir)
        return {
            "configured": True,
            "notebook": {"id": selected["id"], "name": selected["name"]},
        }

    @app.get(
        "/v1/ui/captures/{capture_id}",
        response_model=SyncReceipt,
        dependencies=[Depends(require_ui_csrf)],
        tags=["desktop-gui"],
    )
    def ui_capture_status(capture_id: str) -> SyncReceipt:
        return store.get_receipt(capture_id)

    async def upload_stream(upload: UploadFile) -> AsyncIterator[bytes]:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            yield chunk

    async def perform_manual_submission(
        request: Request,
        *,
        title: str,
        content: str,
        notebook_id: str | None,
        notebook_name: str | None,
        actor_id: str | None,
        attachments: list[UploadFile] | None,
        operation: str,
        producer_kind: str,
        status_prefix: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        notebook_id = (notebook_id or "").strip() or None
        notebook_name = (notebook_name or "").strip() or None
        if bool(notebook_id) != bool(notebook_name):
            raise NotebookSelectionIncompleteError(
                "notebook_id and notebook_name must be provided together"
            )
        if idempotency_key is not None and not AUDIT_ID_RE.fullmatch(idempotency_key):
            raise HTTPException(
                status_code=400,
                detail="Idempotency-Key must be an opaque ASCII identifier",
            )
        selected_files = attachments or []
        if len(selected_files) > 64:
            raise HTTPException(status_code=413, detail="一次最多上传 64 个附件。")
        artifact_specs: list[dict[str, Any]] = []
        total_size = 0
        for index, upload in enumerate(selected_files, start=1):
            filename = (upload.filename or f"attachment-{index}").replace("\\", "/").split("/")[-1]
            file_digest = hashlib.sha256()
            size = 0
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                total_size += len(chunk)
                if size > config.max_artifact_bytes or total_size > config.max_artifact_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="附件总大小超过连接器允许的上限。",
                    )
                file_digest.update(chunk)
            await upload.seek(0)
            mime_type = upload.content_type or mimetypes.guess_type(filename)[0]
            if not mime_type or "/" not in mime_type:
                mime_type = "application/octet-stream"
            artifact_specs.append(
                {
                    "artifact_id": f"attachment-{index:03d}",
                    "role": "note_attachment",
                    "filename": filename,
                    "mime_type": mime_type,
                    "size": size,
                    "sha256": file_digest.hexdigest(),
                    "ingress": {"mode": "stream", "upload_id": f"upload-{uuid.uuid4()}"},
                    "transfer_policy": "copy",
                    "required": True,
                }
            )
        effective_actor = (actor_id or getpass.getuser()).strip()
        request_fingerprint = sha256_tag(
            canonical_json(
                {
                    "title": title.strip(),
                    "content": content,
                    "notebook_id": notebook_id,
                    "notebook_name": notebook_name,
                    "actor_id": effective_actor,
                    "attachments": [
                        {
                            "artifact_id": item["artifact_id"],
                            "filename": item["filename"],
                            "mime_type": item["mime_type"],
                            "size": item["size"],
                            "sha256": item["sha256"],
                        }
                        for item in artifact_specs
                    ],
                }
            )
        )
        async with manual_submission_lock:
            if idempotency_key is not None:
                prior = store.find_manual_capture(
                    operation=operation,
                    idempotency_key=idempotency_key,
                    request_sha256=request_fingerprint,
                )
                if prior is not None:
                    request_id, key = _audit_ids(
                        prior.capture_id,
                        request.headers.get("X-Request-ID"),
                        idempotency_key,
                    )
                    store.record_request(
                        prior.capture_id,
                        operation=operation,
                        request_id=request_id,
                        idempotency_key=key,
                    )
                    if prior.state == SyncState.RECEIVING:
                        for index, upload in enumerate(selected_files, start=1):
                            await ingress.receive(
                                prior.capture_id,
                                f"attachment-{index:03d}",
                                upload_stream(upload),
                            )
                        prior = store.commit_capture(prior.capture_id)
                        wake_event.set()
                    return {
                        "capture_id": prior.capture_id,
                        "sync_id": prior.sync_id,
                        "state": prior.state.value,
                        "status_url": f"{status_prefix}/{prior.capture_id}",
                        "duplicate": True,
                    }

            if notebook_id is None or notebook_name is None:
                default_notebook = load_default_notebook()
                if default_notebook is None:
                    raise DefaultNotebookNotConfiguredError(
                        "no default notebook is configured for API submissions"
                    )
                notebook_id = default_notebook["id"]
                notebook_name = default_notebook["name"]

            notebooks = await load_notebooks()
            selected = next(
                (
                    notebook
                    for notebook in notebooks
                    if notebook["id"] == notebook_id and notebook["name"] == notebook_name
                ),
                None,
            )
            if selected is None:
                raise NotebookNotFoundError(
                    "the selected notebook ID/name pair is not in the current MatElab list"
                )
            if not selected["editable"]:
                raise NotebookNotWritableError(
                    "the selected MatElab notebook is public or read-only"
                )

            capture_id = f"manual-{uuid.uuid4()}"
            envelope = CaptureEnvelope.model_validate(
                {
                    "schema": "capture-envelope/v1",
                    "capture_id": capture_id,
                    "capture_kind": "field_note",
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "producer": {
                        "kind": producer_kind,
                        "producer_id": socket.gethostname(),
                        "actor_id": effective_actor,
                        "run_id": None,
                    },
                    "scope": {},
                    "source": None,
                    "execution": None,
                    "summary": {"observations": [content]},
                    "artifacts": artifact_specs,
                    "routing_hints": {
                        "creation_mode": "create_update",
                        "target_notebook": selected["name"],
                        "target_notebook_id": selected["id"],
                        "target_user": selected["user"],
                    },
                    "extensions": {
                        "quick_note": {
                            "title": title.strip(),
                            "original_text": content,
                            "human_confirmed": True,
                        }
                    },
                }
            )
            canonical = canonical_json(envelope.model_dump(mode="json", by_alias=True))
            manifest_digest = sha256_tag(canonical)
            template = select_template(config, envelope.capture_kind)
            if idempotency_key is None:
                receipt, duplicate = store.create_capture(
                    envelope,
                    canonical,
                    manifest_digest,
                    template_id="matelab-manual-create/v1",
                    template_version=template.version,
                    template_sha256=template.sha256,
                )
            else:
                receipt, duplicate = store.create_manual_capture(
                    envelope,
                    canonical,
                    manifest_digest,
                    template_id="matelab-manual-create/v1",
                    template_version=template.version,
                    template_sha256=template.sha256,
                    operation=operation,
                    idempotency_key=idempotency_key,
                    request_sha256=request_fingerprint,
                )
            request_id, key = _audit_ids(
                capture_id,
                request.headers.get("X-Request-ID"),
                idempotency_key,
            )
            store.record_request(
                capture_id,
                operation=operation,
                request_id=request_id,
                idempotency_key=key,
            )
            if receipt.state == SyncState.RECEIVING:
                for index, upload in enumerate(selected_files, start=1):
                    await ingress.receive(
                        capture_id,
                        f"attachment-{index:03d}",
                        upload_stream(upload),
                    )
                receipt = store.commit_capture(capture_id)
            wake_event.set()
            return {
                "capture_id": capture_id,
                "sync_id": receipt.sync_id,
                "state": receipt.state.value,
                "status_url": f"{status_prefix}/{capture_id}",
                "duplicate": duplicate,
            }

    @app.post(
        "/v1/ui/manual-submissions",
        response_model=ManualSubmissionAccepted,
        status_code=202,
        dependencies=[Depends(require_ui_csrf)],
        tags=["desktop-gui"],
    )
    async def ui_manual_submission(
        request: Request,
        title: Annotated[str, Form(min_length=1, max_length=255)],
        content: Annotated[str, Form(min_length=1, max_length=100_000)],
        notebook_id: Annotated[str, Form(min_length=1, max_length=255)],
        notebook_name: Annotated[str, Form(min_length=1, max_length=255)],
        actor_id: Annotated[str | None, Form(max_length=255)] = None,
        attachments: Annotated[list[UploadFile] | None, File()] = None,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return await perform_manual_submission(
            request,
            title=title,
            content=content,
            notebook_id=notebook_id,
            notebook_name=notebook_name,
            actor_id=actor_id,
            attachments=attachments,
            operation="gui_manual_submission",
            producer_kind="desktop_gui",
            status_prefix="/v1/ui/captures",
            idempotency_key=idempotency_key,
        )

    @app.post(
        "/v1/manual-submissions",
        response_model=ManualSubmissionAccepted,
        status_code=202,
        dependencies=[Depends(authorizer.dependency("submit"))],
        tags=["captures"],
    )
    async def api_manual_submission(
        request: Request,
        title: Annotated[str, Form(min_length=1, max_length=255)],
        content: Annotated[str, Form(min_length=1, max_length=100_000)],
        notebook_id: Annotated[
            str | None,
            Form(max_length=255, description="可选; 与 notebook_name 同时省略时使用默认记录本"),
        ] = None,
        notebook_name: Annotated[
            str | None,
            Form(max_length=255, description="可选; 与 notebook_id 同时省略时使用默认记录本"),
        ] = None,
        actor_id: Annotated[str | None, Form(max_length=255)] = None,
        attachments: Annotated[list[UploadFile] | None, File()] = None,
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Simple multipart endpoint for local programs that submit human-readable notes."""
        return await perform_manual_submission(
            request,
            title=title,
            content=content,
            notebook_id=notebook_id,
            notebook_name=notebook_name,
            actor_id=actor_id,
            attachments=attachments,
            operation="api_manual_submission",
            producer_kind="local_api",
            status_prefix="/v1/captures",
            idempotency_key=idempotency_key,
        )

    @app.get(
        "/v1/default-notebook",
        response_model=DefaultNotebookStatus,
        dependencies=[Depends(authorizer.dependency("status"))],
        tags=["connector-console"],
    )
    def api_default_notebook() -> dict[str, Any]:
        notebook = load_default_notebook()
        return {"configured": notebook is not None, "notebook": notebook}

    @app.post(
        "/v1/records/{record_uid}/descriptions",
        response_model=RecordDescriptionReceipt,
        status_code=201,
        responses={
            200: {
                "model": RecordDescriptionReceipt,
                "description": "An idempotent replay of an already acknowledged description.",
            }
        },
        dependencies=[Depends(authorizer.dependency("submit"))],
        tags=["records"],
    )
    async def add_record_description(
        record_uid: str,
        description: RecordDescriptionRequest,
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        """Append a uniquely named rich-text description module to an existing record."""
        record_uid = record_uid.strip()
        if not AUDIT_ID_RE.fullmatch(record_uid):
            raise HTTPException(
                status_code=422,
                detail="record_uid must be an opaque ASCII identifier",
            )
        notebook_id = (description.notebook_id or "").strip() or None
        notebook_name = (description.notebook_name or "").strip() or None
        if bool(notebook_id) != bool(notebook_name):
            raise NotebookSelectionIncompleteError(
                "notebook_id and notebook_name must be provided together"
            )
        if notebook_id is None or notebook_name is None:
            default_notebook = load_default_notebook()
            if default_notebook is None:
                raise DefaultNotebookNotConfiguredError(
                    "no default notebook is configured for API submissions"
                )
            notebook_id = default_notebook["id"]
            notebook_name = default_notebook["name"]

        key = idempotency_key or f"description-{uuid.uuid4()}"
        if not AUDIT_ID_RE.fullmatch(key):
            raise HTTPException(
                status_code=400,
                detail="Idempotency-Key must be an opaque ASCII identifier",
            )
        title = (description.title or "").strip() or None
        request_fingerprint = sha256_tag(
            canonical_json(
                {
                    "record_uid": record_uid,
                    "notebook_id": notebook_id,
                    "notebook_name": notebook_name,
                    "title": title,
                    "content": description.content,
                }
            )
        )
        identity_hash = hashlib.sha256(f"{record_uid}\0{key}".encode()).hexdigest()[:24]
        description_id = f"desc-{identity_hash}"
        label = f": {title[:80]}" if title else ""
        module_name = f"Connector 补充说明{label} · {identity_hash}"
        content_sha256 = sha256_tag(description.content.encode())
        prior, duplicate = store.reserve_record_description(
            description_id=description_id,
            idempotency_key=key,
            request_sha256=request_fingerprint,
            record_uid=record_uid,
            notebook_id=notebook_id,
            notebook_name=notebook_name,
            module_name=module_name,
            content_sha256=content_sha256,
        )
        if prior is not None:
            return JSONResponse(status_code=200, content=prior.model_dump(mode="json"))

        try:
            notebooks = await load_notebooks()
            selected = next(
                (
                    notebook
                    for notebook in notebooks
                    if notebook["id"] == notebook_id and notebook["name"] == notebook_name
                ),
                None,
            )
            if selected is None:
                raise NotebookNotFoundError(
                    "the selected notebook ID/name pair is not in the current MatElab list"
                )
            if not selected["editable"]:
                raise NotebookNotWritableError(
                    "the selected MatElab notebook is public or read-only"
                )
            target_user = selected["user"]
            items = await asyncio.to_thread(
                matelab.items,
                selected["name"],
                user=target_user,
            )
            matches = [item for item in items if str(item.get("sn")) == record_uid]
            if not matches:
                raise RecordNotFoundError("record UID was not found in the selected notebook")
            if len(matches) > 1:
                raise MatelabContractError("MatElab returned duplicate records for one UID")
            if bool(matches[0].get("locked")) or bool(matches[0].get("signed")):
                raise RecordNotWritableError("locked or signed records cannot be updated safely")
            payload = build_description_update_payload(
                record_uid=record_uid,
                notebook=selected["name"],
                user=target_user,
                module_name=module_name,
                title=title,
                content=description.content,
            )
            matelab_response = await asyncio.to_thread(matelab.update_record, payload)
        except BridgeError as exc:
            store.fail_record_description(
                description_id,
                error_code=exc.code,
                message=str(exc),
            )
            raise

        receipt = store.complete_record_description(
            description_id,
            owner_user=target_user,
            matelab_response=matelab_response,
            duplicate=duplicate,
        )
        return JSONResponse(
            status_code=200 if duplicate else 201,
            content=receipt.model_dump(mode="json"),
        )

    def checked_event_types(values: list[str] | None) -> set[str] | None:
        if not values:
            return None
        event_types = {value.strip() for value in values if value.strip()}
        if not event_types or any(not EVENT_TYPE_RE.fullmatch(value) for value in event_types):
            raise HTTPException(status_code=400, detail="type contains an invalid event name")
        return event_types

    @app.get(
        "/v1/events/stream",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "A replayable Server-Sent Events stream.",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            }
        },
        dependencies=[Depends(authorizer.dependency("status"))],
        tags=["events"],
    )
    async def stream_events(
        request: Request,
        after: int = Query(default=0, ge=0),
        event_type: Annotated[list[str] | None, Query(alias="type")] = None,
        heartbeat_seconds: float = Query(default=15, ge=5, le=60),
        last_event_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        """Replay durable events after a cursor, then keep the SSE connection open."""
        cursor = after
        if last_event_id:
            try:
                resumed_cursor = int(last_event_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="Last-Event-ID must be an integer cursor"
                ) from exc
            if resumed_cursor < 0:
                raise HTTPException(status_code=400, detail="Last-Event-ID cannot be negative")
            cursor = max(cursor, resumed_cursor)
        event_types = checked_event_types(event_type)

        async def generate() -> AsyncIterator[str]:
            nonlocal cursor
            loop = asyncio.get_running_loop()
            last_output = loop.time()
            while not await request.is_disconnected():
                events = await asyncio.to_thread(
                    store.list_integration_events,
                    after=cursor,
                    limit=100,
                    event_types=event_types,
                )
                if events:
                    for event in events:
                        cursor = event.cursor
                        payload = json.dumps(
                            event.model_dump(mode="json"),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        yield f"id: {event.cursor}\nevent: {event.type}\ndata: {payload}\n\n"
                    last_output = loop.time()
                    continue
                if loop.time() - last_output >= heartbeat_seconds:
                    yield ": heartbeat\n\n"
                    last_output = loop.time()
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get(
        "/v1/events",
        response_model=IntegrationEventBatch,
        dependencies=[Depends(authorizer.dependency("status"))],
        tags=["events"],
    )
    def integration_events(
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        event_type: Annotated[list[str] | None, Query(alias="type")] = None,
    ) -> IntegrationEventBatch:
        events = store.list_integration_events(
            after=after,
            limit=limit,
            event_types=checked_event_types(event_type),
        )
        return IntegrationEventBatch(
            events=events,
            next_cursor=events[-1].cursor if events else after,
        )

    @app.get("/healthz", tags=["operations"])
    def health() -> dict[str, str | int]:
        return {
            "status": "ok",
            "service": "matelab-desktop-bridge",
            "version": __version__,
            "console_api": NATIVE_CONSOLE_API_VERSION,
        }

    @app.get(
        "/v1/console/summary",
        dependencies=[Depends(authorizer.dependency("status"))],
        tags=["connector-console"],
    )
    def console_summary() -> dict[str, Any]:
        return {
            "service": "running",
            "api_url": f"http://127.0.0.1:{config.port}",
            "matelab": session_payload(),
            "queue": store.metrics(),
            "default_notebook": load_default_notebook(),
        }

    @app.get(
        "/v1/console/tasks",
        response_model=list[SyncReceipt],
        dependencies=[Depends(authorizer.dependency("status"))],
        tags=["connector-console"],
    )
    def console_tasks(limit: int = Query(default=100, ge=1, le=500)) -> list[SyncReceipt]:
        return store.list_receipts(limit=limit)

    @app.post(
        "/v1/captures",
        response_model=CaptureAccepted,
        status_code=201,
        dependencies=[Depends(authorizer.dependency("submit"))],
        tags=["captures"],
    )
    async def create_capture(
        request: Request,
        x_request_id: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > config.max_manifest_bytes:
                from fastapi import HTTPException

                raise HTTPException(status_code=413, detail="capture manifest is too large")
        parsed = strict_json_loads(bytes(raw))
        envelope = CaptureEnvelope.model_validate(parsed)
        canonical = canonical_json(envelope.model_dump(mode="json", by_alias=True))
        digest = sha256_tag(canonical)
        template = select_template(config, envelope.capture_kind)
        request_id, idempotency_key = _audit_ids(envelope.capture_id, x_request_id, idempotency_key)
        receipt, duplicate = store.create_capture(
            envelope,
            canonical,
            digest,
            template_id=template.template_id,
            template_version=template.version,
            template_sha256=template.sha256,
        )
        store.record_request(
            envelope.capture_id,
            operation="capture_create",
            request_id=request_id,
            idempotency_key=idempotency_key,
        )
        accepted = CaptureAccepted(
            capture_id=receipt.capture_id,
            sync_id=receipt.sync_id,
            manifest_sha256=receipt.manifest_sha256,
            state=receipt.state,
            duplicate=duplicate,
        )
        return JSONResponse(
            status_code=200 if duplicate else 201,
            content=accepted.model_dump(mode="json"),
            headers={"X-Request-ID": request_id},
        )

    @app.put(
        "/v1/captures/{capture_id}/artifacts/{artifact_id}",
        dependencies=[Depends(authorizer.dependency("submit"))],
        tags=["captures"],
    )
    async def upload_artifact(
        capture_id: str,
        artifact_id: str,
        request: Request,
        response: Response,
        x_request_id: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> ArtifactReceipt:
        request_id, key = _audit_ids(capture_id, x_request_id, idempotency_key)
        receipt = await ingress.receive(capture_id, artifact_id, request.stream())
        store.record_request(
            capture_id,
            operation="artifact_upload",
            request_id=request_id,
            idempotency_key=key,
        )
        response.headers["X-Request-ID"] = request_id
        return receipt

    @app.post(
        "/v1/captures/{capture_id}/commit",
        response_model=SyncReceipt,
        status_code=202,
        dependencies=[Depends(authorizer.dependency("submit"))],
        tags=["captures"],
    )
    def commit_capture(
        capture_id: str,
        x_request_id: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        request_id, key = _audit_ids(capture_id, x_request_id, idempotency_key)
        receipt = store.commit_capture(capture_id)
        store.record_request(
            capture_id,
            operation="capture_commit",
            request_id=request_id,
            idempotency_key=key,
        )
        wake_event.set()
        return JSONResponse(
            status_code=202,
            content=receipt.model_dump(mode="json"),
            headers={"X-Request-ID": request_id},
        )

    @app.get(
        "/v1/captures/recent",
        response_model=list[RecentCandidate],
        dependencies=[Depends(authorizer.dependency("status"))],
        tags=["captures"],
    )
    def recent_captures(
        actor_id: str = Query(min_length=1, max_length=255),
        producer_id: str = Query(min_length=1, max_length=255),
        within_minutes: int = Query(default=120, ge=1, le=1440),
    ) -> list[RecentCandidate]:
        since = datetime.now(UTC) - timedelta(minutes=within_minutes)
        return store.recent_experiments(actor_id=actor_id, producer_id=producer_id, since=since)

    @app.get(
        "/v1/captures/{capture_id}",
        response_model=SyncReceipt,
        dependencies=[Depends(authorizer.dependency("status"))],
        tags=["captures"],
    )
    def capture_status(
        capture_id: str,
        response: Response,
        x_request_id: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> SyncReceipt:
        receipt = store.get_receipt(capture_id)
        request_id, key = _audit_ids(capture_id, x_request_id, idempotency_key)
        store.record_request(
            capture_id,
            operation="capture_status",
            request_id=request_id,
            idempotency_key=key,
        )
        response.headers["X-Request-ID"] = request_id
        return receipt

    @app.get(
        "/v1/captures/{capture_id}/history",
        dependencies=[Depends(authorizer.dependency("status"))],
        tags=["captures"],
    )
    def capture_history(
        capture_id: str,
        response: Response,
        x_request_id: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        events = store.history(capture_id)
        request_id, key = _audit_ids(capture_id, x_request_id, idempotency_key)
        store.record_request(
            capture_id,
            operation="capture_history",
            request_id=request_id,
            idempotency_key=key,
        )
        response.headers["X-Request-ID"] = request_id
        return {"capture_id": capture_id, "events": events}

    @app.post(
        "/v1/captures/{capture_id}/retry",
        response_model=SyncReceipt,
        dependencies=[Depends(authorizer.dependency("maintenance"))],
        tags=["operations"],
    )
    def retry_capture(
        capture_id: str,
        response: Response,
        x_request_id: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> SyncReceipt:
        request_id, key = _audit_ids(capture_id, x_request_id, idempotency_key)
        receipt = store.retry_capture(capture_id)
        store.record_request(
            capture_id,
            operation="capture_retry",
            request_id=request_id,
            idempotency_key=key,
        )
        response.headers["X-Request-ID"] = request_id
        wake_event.set()
        return receipt

    @app.get(
        "/metrics",
        response_class=PlainTextResponse,
        dependencies=[Depends(authorizer.dependency("maintenance"))],
        tags=["operations"],
    )
    def metrics() -> str:
        snapshot = store.metrics()
        lines = [
            "# HELP matelab_bridge_captures Captures by synchronization state",
            "# TYPE matelab_bridge_captures gauge",
        ]
        lines.extend(
            f'matelab_bridge_captures{{state="{state}"}} {count}'
            for state, count in sorted(snapshot["states"].items())
        )
        lines.extend(
            [
                "# TYPE matelab_bridge_oldest_pending_seconds gauge",
                f"matelab_bridge_oldest_pending_seconds {snapshot['oldest_pending_seconds']}",
                "# TYPE matelab_bridge_orphan_candidates gauge",
                f"matelab_bridge_orphan_candidates {snapshot['orphans']}",
            ]
        )
        return "\n".join(lines) + "\n"

    return app


app = create_app()
