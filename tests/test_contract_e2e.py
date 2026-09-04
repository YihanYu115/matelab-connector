from __future__ import annotations

import hashlib
import time

import httpx
from fastapi.testclient import TestClient

from matelab_bridge.api import create_app
from matelab_bridge.auth import MemoryCredentialStore, TokenBundle
from matelab_bridge.config import BridgeConfig
from matelab_bridge.fake_matelab import create_fake_matelab_app
from matelab_bridge.matelab_client import MatelabClient
from matelab_bridge.storage import BridgeStore
from matelab_bridge.sync_engine import SyncEngine

from .conftest import artifact_descriptor


class TestClientTransport(httpx.BaseTransport):
    """Forward httpx requests, including multipart bytes, into a TestClient."""

    __test__ = False

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        forwarded = self.client.request(
            request.method,
            request.url.path,
            headers=dict(request.headers),
            content=request.read(),
        )
        return httpx.Response(
            forwarded.status_code,
            headers=dict(forwarded.headers),
            content=forwarded.content,
            request=request,
        )


def test_gateway_to_fake_matelab_and_independent_discovery(
    config: BridgeConfig, manifest: dict
) -> None:
    artifact = b"print('e2e')\n"
    manifest["artifacts"] = [artifact_descriptor(artifact)]
    store = BridgeStore(config.database_path)
    credentials = MemoryCredentialStore(
        TokenBundle(access_token="dev-access", access_expires_at=time.time() + 3600)
    )
    with TestClient(create_fake_matelab_app()) as fake:
        remote_http = httpx.Client(
            base_url="http://fake-matelab", transport=TestClientTransport(fake)
        )
        matelab = MatelabClient(config, credentials=credentials, http_client=remote_http)
        engine = SyncEngine(config, store, matelab)
        with TestClient(create_app(config, store=store, engine=engine)) as gateway:
            assert gateway.post("/v1/captures", json=manifest).status_code == 201
            uploaded = gateway.put(
                f"/v1/captures/{manifest['capture_id']}/artifacts/script",
                content=artifact,
            )
            assert uploaded.json()["sha256"] == hashlib.sha256(artifact).hexdigest()
            assert gateway.post(f"/v1/captures/{manifest['capture_id']}/commit").status_code == 202
            assert engine.process_once() == manifest["capture_id"]
            receipt = gateway.get(f"/v1/captures/{manifest['capture_id']}").json()
            assert receipt["state"] == "complete"
            uid = receipt["matelab_ref"]["record_uid"]

            auth = {"Authorization": "Bearer dev-access"}
            items = fake.post(
                "/eln_api/items", json={"eln": config.experiment_notebook}, headers=auth
            ).json()
            assert [item["sn"] for item in items["items"]] == [uid]
            searched = fake.post(
                "/eln_api/search",
                json={
                    "eln": [config.experiment_notebook],
                    "requests": [
                        {
                            "uid": "capture_id",
                            "path": ["Connector Metadata", "Capture ID"],
                        }
                    ],
                },
                headers=auth,
            ).json()
            assert searched["data"][0]["capture_id"] == manifest["capture_id"]
            exported = fake.post(
                "/eln_api/export",
                json={"uids": [{"eln": config.experiment_notebook, "uid": uid}]},
                headers=auth,
            ).json()
            metadata = exported["dataset"][0]["data"]["Connector Metadata"]
            assert metadata["Capture ID"] == manifest["capture_id"]
            assert metadata["Capture Hash"] == receipt["manifest_sha256"]
