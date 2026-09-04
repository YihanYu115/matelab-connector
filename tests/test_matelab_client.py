from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx
import pytest

from matelab_bridge.auth import MemoryCredentialStore, TokenBundle
from matelab_bridge.config import BridgeConfig
from matelab_bridge.errors import (
    MatelabAuthRequiredError,
    MatelabConflictError,
    MatelabContractError,
    MatelabRetryableError,
)
from matelab_bridge.matelab_client import MatelabClient


def test_authenticated_items_request(config: BridgeConfig) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer access"
        return httpx.Response(200, json={"code": 0, "items": [{"sn": "uid"}]})

    credentials = MemoryCredentialStore(
        TokenBundle(access_token="access", access_expires_at=time.time() + 600)
    )
    http = httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler))
    client = MatelabClient(config, credentials=credentials, http_client=http)
    assert client.items("notebook") == [{"sn": "uid"}]
    client.close()


def test_refresh_accepts_millisecond_expiry(config: BridgeConfig) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("tokens_refresh"):
            future_ms = (time.time() + 3600) * 1000
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "access": {"token": "new-access", "expiredAt": future_ms},
                    "refresh": {"token": "new-refresh", "expiredAt": future_ms + 1000},
                },
            )
        assert request.headers["Authorization"] == "Bearer new-access"
        return httpx.Response(200, json={"code": 0, "items": []})

    credentials = MemoryCredentialStore(
        TokenBundle(
            access_token="old",
            access_expires_at=time.time() - 1,
            refresh_token="refresh",
            refresh_expires_at=time.time() + 600,
        )
    )
    http = httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler))
    client = MatelabClient(config, credentials=credentials, http_client=http)
    assert client.items("notebook") == []
    assert calls == ["/tokens/tokens_refresh", "/eln_api/items"]
    assert credentials.load().access_token == "new-access"  # type: ignore[union-attr]
    client.close()


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({"code": 1, "msg": "no"}, MatelabAuthRequiredError),
        ({"code": 2, "msg": "bad"}, MatelabContractError),
        ({"code": "mystery"}, MatelabContractError),
    ],
)
def test_error_classification(config: BridgeConfig, body: dict, error: type[Exception]) -> None:
    credentials = MemoryCredentialStore(
        TokenBundle(access_token="access", access_expires_at=time.time() + 600)
    )
    http = httpx.Client(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body)),
    )
    client = MatelabClient(config, credentials=credentials, http_client=http)
    with pytest.raises(error):
        client.items("notebook")
    client.close()


def test_login_persists_tokens(config: BridgeConfig) -> None:
    future_ms = (time.time() + 3600) * 1000
    credentials = MemoryCredentialStore()
    http = httpx.Client(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "code": 0,
                    "access": {"token": "access", "expiredAt": future_ms},
                    "refresh": {"token": "refresh", "expiredAt": future_ms + 1000},
                },
            )
        ),
    )
    client = MatelabClient(config, credentials=credentials, http_client=http)
    assert client.login("user", "ephemeral-password").access_token == "access"
    assert client.credential_status()["configured"] is True
    client.clear_tokens()
    assert client.credential_status() == {"configured": False}
    client.close()


def test_serial_chunk_upload_and_local_hash_guard(config: BridgeConfig, tmp_path: Path) -> None:
    content = b"abcdef"
    digest = hashlib.sha256(content).hexdigest()
    path = tmp_path / "data.bin"
    path.write_bytes(content)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "code": 0,
                "name": "stable-name",
                "filename": "data.bin",
                "size": len(content),
                "hash": digest,
            },
        )

    chunked = BridgeConfig(data_dir=config.data_dir, upload_chunk_bytes=3)
    credentials = MemoryCredentialStore(
        TokenBundle(access_token="access", access_expires_at=time.time() + 600)
    )
    http = httpx.Client(base_url="https://example.invalid", transport=httpx.MockTransport(handler))
    client = MatelabClient(chunked, credentials=credentials, http_client=http)
    result = client.upload_file(
        path,
        uid="stable-name",
        name="stable-name",
        notebook="book",
        mime_type="application/octet-stream",
        expected_sha256=digest,
    )
    assert result["hash"] == digest
    assert calls == 2
    with pytest.raises(MatelabConflictError, match="changed"):
        client.upload_file(
            path,
            uid="stable-name",
            name="stable-name",
            notebook="book",
            mime_type="application/octet-stream",
            expected_sha256="0" * 64,
        )
    client.close()


@pytest.mark.parametrize("status", [429, 500])
def test_http_retryable_status(config: BridgeConfig, status: int) -> None:
    credentials = MemoryCredentialStore(
        TokenBundle(access_token="access", access_expires_at=time.time() + 600)
    )
    http = httpx.Client(
        base_url="https://example.invalid",
        transport=httpx.MockTransport(lambda _request: httpx.Response(status)),
    )
    client = MatelabClient(config, credentials=credentials, http_client=http)
    with pytest.raises(MatelabRetryableError):
        client.elns()
    client.close()
