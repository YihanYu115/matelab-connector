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

    @classmethod
    def load(cls, data_dir: Path, *, default_port: int) -> DesktopSettings:
        path = data_dir / "desktop-settings.json"
        try:
            value: Any = json.loads(path.read_text(encoding="utf-8"))
            port = int(value["api_port"])
        except (OSError, ValueError, TypeError, KeyError):
            port = default_port
        if not 0 < port < 65536:
            port = default_port
        return cls(api_port=port)

    def save(self, data_dir: Path) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        destination = data_dir / "desktop-settings.json"
        temporary = data_dir / ".desktop-settings.tmp"
        content = json.dumps({"api_port": self.api_port}, ensure_ascii=False, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
