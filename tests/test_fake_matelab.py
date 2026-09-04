from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from matelab_bridge.fake_matelab import create_fake_matelab_app


def test_fake_matelab_contract_roundtrip() -> None:
    headers = {"Authorization": "Bearer dev-access"}
    app = create_fake_matelab_app()
    with TestClient(app) as client:
        unauthorized = client.post("/eln_api/items", json={"eln": "book"})
        assert unauthorized.json()["code"] == 1
        login = client.post("/tokens", data={"username": "user", "password": "pass"})
        assert login.json()["access"]["token"] == "dev-access"
        assert (
            client.post("/tokens/tokens_refresh", data={"refresh_token": "wrong"}).json()["code"]
            == 1
        )
        assert (
            client.post("/tokens/tokens_refresh", data={"refresh_token": "dev-refresh"}).json()[
                "code"
            ]
            == 0
        )

        content = b"abcdef"
        digest = hashlib.sha256(content).hexdigest()
        first = client.post(
            "/eln_api/upload",
            data={"uid": "u1", "name": "n1", "eln": "book", "last": "0", "hash": digest},
            files={"file": ("a.txt", content[:3], "text/plain")},
            headers=headers,
        )
        assert first.json() == {"code": 0, "name": "n1"}
        final = client.post(
            "/eln_api/upload",
            data={"uid": "u1", "name": "n1", "eln": "book", "last": "1", "hash": digest},
            files={"file": ("a.txt", content[3:], "text/plain")},
            headers=headers,
        )
        assert final.json()["hash"] == digest
        duplicate_upload = client.post(
            "/eln_api/upload",
            data={"uid": "u1", "name": "n1", "eln": "book", "last": "1", "hash": digest},
            files={"file": ("a.txt", content, "text/plain")},
            headers=headers,
        )
        assert duplicate_upload.json()["id"] == final.json()["id"]

        payload = {
            "eln": "book",
            "template": "template",
            "dataset": [
                {
                    "uid": "record-1",
                    "title": "title",
                    "keyword": "matelab-bridge;experiment_run",
                    "data": {"Connector Metadata": {"Capture ID": "capture-1"}},
                }
            ],
        }
        imported = client.post("/eln_api/import", json=payload, headers=headers)
        assert imported.json()["id"] == [1001]
        assert client.post("/eln_api/import", json=payload, headers=headers).json()["code"] == 2
        items = client.post("/eln_api/items", json={"eln": "book"}, headers=headers).json()
        assert items["items"][0]["sn"] == "record-1"
        exported = client.post(
            "/eln_api/export",
            json={"uids": [{"eln": "book", "uid": "record-1"}]},
            headers=headers,
        ).json()
        assert exported["dataset"][0]["data"]["Connector Metadata"]["Capture ID"] == "capture-1"
        searched = client.post(
            "/eln_api/search",
            json={
                "eln": ["book"],
                "requests": [{"uid": "capture", "path": ["Connector Metadata", "Capture ID"]}],
            },
            headers=headers,
        ).json()
        assert searched["data"][0]["capture"] == "capture-1"
        elns = client.post("/eln_api/elns", headers=headers).json()
        assert elns["items"][0]["showtext"] == "book"
        assert client.post("/eln_api/update", json={}, headers=headers).json()["code"] == 0

        created = client.post(
            "/eln_api/create",
            json={"eln": "book", "uid": "manual-1", "title": "manual", "path": []},
            headers=headers,
        )
        assert created.json()["code"] == 0
        updated = client.post(
            "/eln_api/update",
            json={
                "eln": "book",
                "uid": "manual-1",
                "addModule": [{"name": "记录内容", "type": "richtext", "data": "正文"}],
            },
            headers=headers,
        )
        assert updated.json()["code"] == 0
        manual_export = client.post(
            "/eln_api/export",
            json={"uids": [{"eln": "book", "uid": "manual-1"}]},
            headers=headers,
        ).json()
        assert manual_export["dataset"][0]["data"]["记录内容"] == "正文"


def test_fake_rejects_bad_upload_hash_and_import_shape() -> None:
    headers = {"Authorization": "Bearer dev-access"}
    with TestClient(create_fake_matelab_app()) as client:
        upload = client.post(
            "/eln_api/upload",
            data={"uid": "u", "name": "n", "eln": "book", "last": "1", "hash": "0" * 64},
            files={"file": ("a.txt", b"content", "text/plain")},
            headers=headers,
        )
        assert upload.json()["code"] == 2
        assert client.post("/eln_api/import", json={}, headers=headers).json()["code"] == 2
