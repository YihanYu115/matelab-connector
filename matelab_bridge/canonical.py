"""Strict JSON, canonical encoding, hashes, and stable identifiers."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from .errors import StrictJSONError


def _reject_constant(value: str) -> None:
    raise StrictJSONError(f"strict JSON does not allow {value}")


def strict_json_loads(raw: bytes | str) -> Any:
    """Decode UTF-8 JSON while rejecting BOM, NaN, and infinities."""
    if isinstance(raw, bytes):
        if raw.startswith(b"\xef\xbb\xbf"):
            raise StrictJSONError("UTF-8 BOM is not allowed")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise StrictJSONError("request is not valid UTF-8") from exc
    else:
        text = raw
        if text.startswith("\ufeff"):
            raise StrictJSONError("UTF-8 BOM is not allowed")
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise StrictJSONError(f"invalid JSON at line {exc.lineno}, column {exc.colno}") from exc


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise StrictJSONError("value cannot be represented as strict JSON") from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_tag(data: bytes) -> str:
    return f"sha256:{sha256_hex(data)}"


def deterministic_record_uid(capture_id: str) -> str:
    """Return a MatElab-safe deterministic UID shorter than 45 characters."""
    digest = hashlib.sha256(capture_id.encode("utf-8")).digest()
    suffix = base64.b32encode(digest).decode("ascii").rstrip("=")[:32]
    return f"MB{suffix}"


def deterministic_upload_name(capture_id: str, artifact_id: str) -> str:
    digest = hashlib.sha256(f"{capture_id}\0{artifact_id}".encode()).digest()
    suffix = base64.b32encode(digest).decode("ascii").rstrip("=")[:32]
    return f"MF{suffix}"
