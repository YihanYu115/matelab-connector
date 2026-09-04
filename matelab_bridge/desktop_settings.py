"""Small durable preference store for the native Connector application."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class DesktopSettings:
    api_port: int
    default_notebook_id: str | None = None
    default_notebook_name: str | None = None
    matelab_username: str | None = None

    @classmethod
    def load(cls, data_dir: Path, *, default_port: int) -> DesktopSettings:
        path = data_dir / "desktop-settings.json"
        try:
            value: Any = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise TypeError("desktop settings must be an object")
            port = int(value["api_port"])
            notebook_id = cls._optional_text(value.get("default_notebook_id"))
            notebook_name = cls._optional_text(value.get("default_notebook_name"))
            matelab_username = cls._optional_text(value.get("matelab_username"))
        except (OSError, ValueError, TypeError, KeyError):
            port = default_port
            notebook_id = None
            notebook_name = None
            matelab_username = None
        if not 0 < port < 65536:
            port = default_port
        if not notebook_id or not notebook_name:
            notebook_id = None
            notebook_name = None
        return cls(
            api_port=port,
            default_notebook_id=notebook_id,
            default_notebook_name=notebook_name,
            matelab_username=matelab_username,
        )

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 255:
            return None
        return normalized

    def save(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        destination = data_dir / "desktop-settings.json"
        temporary = data_dir / ".desktop-settings.tmp"
        content = (
            json.dumps(
                {
                    "api_port": self.api_port,
                    "default_notebook_id": self.default_notebook_id,
                    "default_notebook_name": self.default_notebook_name,
                    "matelab_username": self.matelab_username,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
