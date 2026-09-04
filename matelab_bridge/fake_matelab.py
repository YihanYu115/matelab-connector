"""Small stateful fake MatElab server for offline contract and recovery tests."""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, File, Form, Header, UploadFile


def create_fake_matelab_app() -> FastAPI:
    app = FastAPI(title="Fake MatElab", version="0.1.0")
    uploads: dict[str, bytearray] = defaultdict(bytearray)
    uploaded_files: dict[str, dict[str, Any]] = {}
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    next_record_id = 1000
    app.state.uploads = uploaded_files
    app.state.records = records

    def authorized(value: str | None) -> bool:
        return value == "Bearer dev-access"

    @app.post("/tokens")
    def login(username: str = Form(), password: str = Form()) -> dict[str, Any]:
        if not username or not password:
            return {"code": 1, "msg": "用户名或密码错误"}
        now = time.time()
        return {
            "code": 0,
            "userid": 94,
            "access": {"token": "dev-access", "expiredAt": (now + 3600) * 1000},
            "refresh": {"token": "dev-refresh", "expiredAt": (now + 86400) * 1000},
        }

    @app.post("/tokens/tokens_refresh")
    def refresh(refresh_token: str = Form()) -> dict[str, Any]:
        if refresh_token != "dev-refresh":
            return {"code": 1, "msg": "无效 refresh token"}
        now = time.time()
        return {
            "code": 0,
            "access": {"token": "dev-access", "expiredAt": (now + 3600) * 1000},
            "refresh": {"token": "dev-refresh", "expiredAt": (now + 86400) * 1000},
        }

    @app.post("/eln_api/upload")
    async def upload(
        uid: str = Form(),
        name: str = Form(),
        eln: str = Form(),
        last: str = Form("1"),
        hash_value: str | None = Form(default=None, alias="hash"),
        file: UploadFile = File(),  # noqa: B008
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if not authorized(authorization):
            return {"code": 1, "msg": "unauthorized"}
        if name in uploaded_files:
            existing = uploaded_files[name]
            if hash_value and existing["hash"] != hash_value:
                return {"code": 2, "errmsg": "name/hash conflict"}
            return {"code": 0, **existing}
        uploads[uid].extend(await file.read())
        if last != "1":
            return {"code": 0, "name": name}
        content = bytes(uploads.pop(uid))
        digest = hashlib.sha256(content).hexdigest()
        if hash_value and digest != hash_value:
            return {"code": 2, "errmsg": "hash mismatch"}
        receipt = {
            "id": len(uploaded_files) + 1,
            "name": name,
            "filename": file.filename or "artifact.bin",
            "url": f"upload/eln/{name}",
            "size": len(content),
            "hash": digest,
            "eln": eln,
        }
        uploaded_files[name] = receipt
        return {"code": 0, **receipt}

    @app.post("/eln_api/import")
    def import_record(
        payload: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        nonlocal next_record_id
        if not authorized(authorization):
            return {"code": 1, "msg": "unauthorized"}
        notebook = payload.get("eln")
        dataset = payload.get("dataset")
        if not isinstance(notebook, str) or not isinstance(dataset, list) or not dataset:
            return {"code": 2, "msg": "invalid import payload"}
        ids: list[int] = []
        for entry in dataset:
            uid = entry.get("uid")
            if any(record["sn"] == uid for record in records[notebook]):
                return {"code": 2, "msg": "UID already exists"}
            next_record_id += 1
            records[notebook].append(
                {
                    "id": next_record_id,
                    "sn": uid,
                    "title": entry.get("title", ""),
                    "comm": entry.get("title", ""),
                    "keywords": str(entry.get("keyword", "")).split(";"),
                    "template_id": payload.get("template"),
                    "data": entry.get("data", {}),
                    "locked": False,
                    "signed": False,
                    "version": 1,
                    "datetime_create": "2026-09-04 12:00",
                    "datetime_modify": "2026-09-04 12:00",
                }
            )
            ids.append(next_record_id)
        return {"code": 0, "eln_id": 1264, "id": ids, "msg": "数据导入成功"}

    @app.post("/eln_api/items")
    def items(
        payload: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        if not authorized(authorization):
            return {"code": 1, "msg": "unauthorized"}
        notebook = str(payload.get("eln", ""))
        return {
            "code": 0,
            "items": [
                {key: value for key, value in record.items() if key != "data"}
                for record in records[notebook]
            ],
        }

    @app.post("/eln_api/export")
    def export(
        payload: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        if not authorized(authorization):
            return {"code": 1, "msg": "unauthorized"}
        result: list[dict[str, Any]] = []
        for target in payload.get("uids", []):
            eln_value = target.get("eln")
            notebook = eln_value.get("title") if isinstance(eln_value, dict) else eln_value
            for record in records[str(notebook)]:
                if record["sn"] == target.get("uid"):
                    result.append(
                        {
                            "eln_name": notebook,
                            "id": record["id"],
                            "comm": record["comm"],
                            "uid": record["sn"],
                            "version": record["version"],
                            "data": record["data"],
                        }
                    )
        return {"code": 0, "dataset": result}

    @app.post("/eln_api/search")
    def search(
        payload: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        if not authorized(authorization):
            return {"code": 1, "msg": "unauthorized"}
        notebooks = payload.get("eln") or list(records)
        if isinstance(notebooks, str):
            notebooks = [notebooks]
        output: list[dict[str, Any]] = []
        for item in notebooks:
            notebook = item.get("title") if isinstance(item, dict) else item
            for record in records[str(notebook)]:
                result: dict[str, Any] = {"id": record["id"], "uid": record["sn"]}
                for request in payload.get("requests", []):
                    value: Any = record["data"]
                    try:
                        for path_part in request.get("path", []):
                            value = value[path_part]
                    except (KeyError, IndexError, TypeError):
                        continue
                    result[str(request.get("uid"))] = value
                output.append(result)
        return {"code": 0, "data": output}

    @app.post("/eln_api/elns")
    def elns(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not authorized(authorization):
            return {"code": 1, "msg": "unauthorized"}
        return {
            "code": 0,
            "items": [
                {"showtext": name, "sn": f"fake-{index}", "owner": True, "server": "localhost"}
                for index, name in enumerate(records, start=1)
            ],
        }

    @app.post("/eln_api/update")
    def update(
        _payload: dict[str, Any], authorization: str | None = Header(default=None)
    ) -> dict[str, Any]:
        if not authorized(authorization):
            return {"code": 1, "msg": "unauthorized"}
        return {"code": 0, "msg": "数据更新成功"}

    return app


app = create_fake_matelab_app()
