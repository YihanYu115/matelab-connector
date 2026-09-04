"""Narrow MatElab API client based on the public ELN documentation."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import httpx

from .auth import CredentialStore, KeyringCredentialStore, TokenBundle
from .config import BridgeConfig
from .errors import (
    MatelabAuthRequiredError,
    MatelabConflictError,
    MatelabContractError,
    MatelabRetryableError,
)
from .security import redact_text


def _expiry_seconds(value: Any) -> float:
    number = float(value)
    return number / 1000 if number > 100_000_000_000 else number


class MatelabClient:
    """Only exposes calls needed by the submission gateway."""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        credentials: CredentialStore | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.credentials = credentials or KeyringCredentialStore(config.matelab_url)
        self.http = http_client or httpx.Client(
            base_url=config.matelab_url,
            timeout=httpx.Timeout(connect=10, read=60, write=60, pool=10),
            follow_redirects=False,
        )

    def close(self) -> None:
        self.http.close()

    def login(self, username: str, password: str) -> TokenBundle:
        response = self._transport_post(
            "/tokens",
            data={"username": username, "password": password},
            authenticated=False,
        )
        bundle = self._parse_token_response(response)
        self.credentials.save(bundle)
        return bundle

    def set_tokens(
        self,
        access_token: str,
        refresh_token: str | None,
        *,
        access_expires_at: float,
        refresh_expires_at: float | None,
    ) -> None:
        self.credentials.save(
            TokenBundle(
                access_token=access_token,
                access_expires_at=access_expires_at,
                refresh_token=refresh_token,
                refresh_expires_at=refresh_expires_at,
            )
        )

    def credential_status(self) -> dict[str, Any]:
        bundle = self.credentials.load()
        if bundle is None:
            return {"configured": False}
        now = time.time()
        return {
            "configured": True,
            "access_valid": bundle.access_expires_at > now,
            "access_expires_at": bundle.access_expires_at,
            "refresh_valid": bool(
                bundle.refresh_token
                and bundle.refresh_expires_at
                and bundle.refresh_expires_at > now
            ),
            "refresh_expires_at": bundle.refresh_expires_at,
        }

    def clear_tokens(self) -> None:
        self.credentials.clear()

    def elns(self) -> dict[str, Any]:
        return self._api_post("/eln_api/elns", json_data={})

    def items(self, notebook: str, *, user: str | None = None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"eln": notebook}
        if user and user != "self":
            payload["user"] = user
        result = self._api_post("/eln_api/items", json_data=payload)
        items = result.get("items", [])
        if not isinstance(items, list):
            raise MatelabContractError("MatElab items response did not contain a list")
        return [item for item in items if isinstance(item, dict)]

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_post("/eln_api/search", json_data=payload)

    def import_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_post("/eln_api/import", json_data=payload)

    def export_record(
        self, notebook: str, record_uid: str, *, user: str | None = None
    ) -> dict[str, Any]:
        eln: str | dict[str, Any] = notebook
        if user and user != "self":
            eln = {"title": notebook, "user": user}
        return self._api_post(
            "/eln_api/export", json_data={"uids": [{"eln": eln, "uid": record_uid}]}
        )

    def update_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_post("/eln_api/update", json_data=payload)

    def upload_file(
        self,
        path: Path,
        *,
        uid: str,
        name: str,
        notebook: str,
        mime_type: str,
        expected_sha256: str,
    ) -> dict[str, Any]:
        """Upload chunks serially; MatElab explicitly forbids parallel chunks."""
        if self.config.upload_chunk_bytes > 20 * 1024**2:
            raise MatelabContractError("upload_chunk_bytes exceeds MatElab's 20 MB maximum")
        actual_hash = hashlib.sha256()
        final_result: dict[str, Any] | None = None
        with path.open("rb") as handle:
            chunk = handle.read(self.config.upload_chunk_bytes)
            if chunk == b"":
                final_result = self._upload_chunk(
                    chunk,
                    filename=path.name,
                    mime_type=mime_type,
                    uid=uid,
                    name=name,
                    notebook=notebook,
                    last=True,
                    expected_sha256=expected_sha256,
                )
            while chunk:
                actual_hash.update(chunk)
                following = handle.read(self.config.upload_chunk_bytes)
                final_result = self._upload_chunk(
                    chunk,
                    filename=path.name,
                    mime_type=mime_type,
                    uid=uid,
                    name=name,
                    notebook=notebook,
                    last=not following,
                    expected_sha256=expected_sha256,
                )
                chunk = following
        if actual_hash.hexdigest() != expected_sha256.lower():
            raise MatelabConflictError("local artifact changed after ingress verification")
        if final_result is None:
            raise MatelabContractError("MatElab did not return a final upload receipt")
        remote_hash = str(final_result.get("hash", "")).lower()
        if remote_hash and remote_hash != expected_sha256.lower():
            raise MatelabConflictError("MatElab upload receipt hash differs from the artifact")
        return final_result

    def _upload_chunk(
        self,
        chunk: bytes,
        *,
        filename: str,
        mime_type: str,
        uid: str,
        name: str,
        notebook: str,
        last: bool,
        expected_sha256: str,
    ) -> dict[str, Any]:
        return self._api_post(
            "/eln_api/upload",
            data={
                "uid": uid,
                "name": name,
                "eln": notebook,
                "last": "1" if last else "0",
                "hash": expected_sha256,
            },
            files={"file": (filename, chunk, mime_type)},
        )

    def _access_token(self) -> str:
        bundle = self.credentials.load()
        if bundle is None:
            raise MatelabAuthRequiredError("MatElab credentials are not configured")
        if bundle.access_expires_at <= time.time() + 60:
            bundle = self._refresh(bundle)
        return bundle.access_token

    def _refresh(self, bundle: TokenBundle) -> TokenBundle:
        if (
            not bundle.refresh_token
            or bundle.refresh_expires_at is None
            or bundle.refresh_expires_at <= time.time()
        ):
            raise MatelabAuthRequiredError("MatElab refresh token is unavailable or expired")
        response = self._transport_post(
            "/tokens/tokens_refresh",
            data={"refresh_token": bundle.refresh_token},
            authenticated=False,
        )
        refreshed = self._parse_token_response(response)
        self.credentials.save(refreshed)
        return refreshed

    def _parse_token_response(self, result: dict[str, Any]) -> TokenBundle:
        if result.get("code") != 0:
            raise MatelabAuthRequiredError("MatElab rejected the authentication request")
        try:
            access = result["access"]
            refresh = result.get("refresh")
            return TokenBundle(
                access_token=str(access["token"]),
                access_expires_at=_expiry_seconds(access["expiredAt"]),
                refresh_token=str(refresh["token"]) if refresh else None,
                refresh_expires_at=_expiry_seconds(refresh["expiredAt"]) if refresh else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MatelabContractError("MatElab returned an invalid token response") from exc

    def _api_post(
        self,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        refresh_attempted: bool = False,
    ) -> dict[str, Any]:
        result = self._transport_post(
            path,
            json_data=json_data,
            data=data,
            files=files,
            access_token=self._access_token(),
        )
        code = result.get("code")
        if code == 0:
            return result
        if code == "refresh" and not refresh_attempted:
            bundle = self.credentials.load()
            if bundle is None:
                raise MatelabAuthRequiredError("MatElab credentials disappeared")
            self._refresh(bundle)
            return self._api_post(
                path,
                json_data=json_data,
                data=data,
                files=files,
                refresh_attempted=True,
            )
        message = redact_text(
            str(result.get("msg") or result.get("errmsg") or "MatElab request failed")
        )
        if code in {1, "refresh"}:
            raise MatelabAuthRequiredError(message)
        if code == 3:
            raise MatelabRetryableError(message)
        if code in {2, 4}:
            raise MatelabContractError(message)
        raise MatelabContractError("MatElab returned an undocumented response code")

    def _transport_post(
        self,
        path: str,
        *,
        json_data: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        access_token: str | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if authenticated:
            token = access_token or self._access_token()
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = self.http.post(path, json=json_data, data=data, files=files, headers=headers)
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise MatelabRetryableError("MatElab is unreachable or timed out") from exc
        if response.status_code == 401:
            raise MatelabAuthRequiredError("MatElab rejected the access token")
        if response.status_code == 429 or response.status_code >= 500:
            raise MatelabRetryableError(f"MatElab returned HTTP {response.status_code}")
        if response.status_code >= 400:
            raise MatelabContractError(f"MatElab returned HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise MatelabContractError("MatElab returned non-JSON content") from exc
        if not isinstance(result, dict):
            raise MatelabContractError("MatElab returned a non-object JSON response")
        return result
