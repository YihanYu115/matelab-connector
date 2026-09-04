"""Secret redaction and local path safety helpers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|access[_-]?token|refresh[_-]?token|authorization|private[_-]?key)"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
KEY_VALUE_RE = re.compile(
    r"(?i)\b(password|passwd|access[_-]?token|refresh[_-]?token)\b\s*[:=]\s*([^\s,;]+)"
)


def redact_text(value: str, *, limit: int = 500) -> str:
    value = BEARER_RE.sub("Bearer [REDACTED]", value)
    value = KEY_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return value[:limit]


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if SECRET_KEY_RE.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def contains_secret_material(value: Any) -> bool:
    """Detect credential-shaped content before a manifest reaches durable storage."""
    if isinstance(value, dict):
        return any(
            SECRET_KEY_RE.search(str(key)) is not None or contains_secret_material(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret_material(item) for item in value)
    if isinstance(value, str):
        return BEARER_RE.search(value) is not None or KEY_VALUE_RE.search(value) is not None
    return False


def safe_child(root: Path, *parts: str) -> Path:
    """Resolve a child path and fail closed on traversal or symlink escape."""
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise ValueError("path escapes configured root")
    return candidate
