"""MatElab token persistence using the operating-system credential store."""

from __future__ import annotations

import json
from contextlib import suppress
from typing import Protocol

import keyring
from pydantic import BaseModel, ConfigDict


class TokenBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    access_expires_at: float
    refresh_token: str | None = None
    refresh_expires_at: float | None = None


class CredentialStore(Protocol):
    def load(self) -> TokenBundle | None: ...

    def save(self, bundle: TokenBundle) -> None: ...

    def clear(self) -> None: ...


class KeyringCredentialStore:
    """Persist token JSON under an endpoint-specific keyring entry."""

    service_name = "MatElab Desktop Bridge"

    def __init__(self, endpoint: str) -> None:
        self.account = endpoint.rstrip("/")

    def load(self) -> TokenBundle | None:
        raw = keyring.get_password(self.service_name, self.account)
        if not raw:
            return None
        return TokenBundle.model_validate_json(raw)

    def save(self, bundle: TokenBundle) -> None:
        keyring.set_password(self.service_name, self.account, bundle.model_dump_json())

    def clear(self) -> None:
        with suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(self.service_name, self.account)


class MemoryCredentialStore:
    """In-memory credential storage for tests and the fake service."""

    def __init__(self, bundle: TokenBundle | None = None) -> None:
        self.bundle = bundle

    def load(self) -> TokenBundle | None:
        if self.bundle is None:
            return None
        return TokenBundle.model_validate(json.loads(self.bundle.model_dump_json()))

    def save(self, bundle: TokenBundle) -> None:
        self.bundle = bundle.model_copy(deep=True)

    def clear(self) -> None:
        self.bundle = None
