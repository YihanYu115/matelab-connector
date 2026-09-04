"""ASGI process entry helpers."""

from __future__ import annotations

import json
import logging
from typing import Any

import uvicorn

from .api import create_app
from .config import BridgeConfig
from .security import redact


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False, separators=(",", ":"))


def configure_logging(verbose: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def serve(config: BridgeConfig, *, verbose: bool = False) -> None:
    configure_logging(verbose)
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_config=None)
