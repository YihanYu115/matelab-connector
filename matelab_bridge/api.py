"""Local Submission Gateway HTTP API."""

from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError

from .artifact_ingress import ArtifactIngressService
from .canonical import canonical_json, sha256_tag, strict_json_loads
from .config import BridgeConfig
from .errors import BridgeError
from .matelab_client import MatelabClient
from .models import (
    ArtifactReceipt,
    CaptureAccepted,
    CaptureEnvelope,
    RecentCandidate,
    SyncReceipt,
)
from .storage import BridgeStore
from .sync_engine import SyncEngine, run_worker
from .templates import select_template

ScopeName = Literal["submit", "status", "maintenance"]
AUDIT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


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
        version="0.1.0",
        description="Durable local Capture Envelope submission gateway",
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.store = store
    app.state.engine = engine
    app.state.wake_event = wake_event

    @app.exception_handler(BridgeError)
    async def bridge_error_handler(_request: Request, exc: BridgeError) -> JSONResponse:
        status = {
            "capture_not_found": 404,
            "artifact_not_found": 404,
            "capture_identity_conflict": 409,
            "artifact_conflict": 409,
            "invalid_state": 409,
            "capture_incomplete": 422,
            "artifact_hash_mismatch": 422,
            "artifact_size_mismatch": 422,
            "invalid_json": 400,
        }.get(exc.code, 400)
        return JSONResponse(
            status_code=status,
            content={"error": {"code": exc.code, "message": str(exc)}},
        )

    @app.exception_handler(ValidationError)
    async def pydantic_error_handler(_request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "schema_validation_error",
                    "message": "capture payload failed schema validation",
                    "details": _validation_details(exc),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "request_validation_error",
                    "message": "request parameters failed validation",
                    "details": _validation_details(exc),
                }
            },
        )

    @app.get("/healthz", tags=["operations"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
