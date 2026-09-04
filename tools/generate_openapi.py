"""Regenerate the checked-in OpenAPI contract from the FastAPI application."""

from __future__ import annotations

import json
from pathlib import Path

from matelab_bridge.api import create_app
from matelab_bridge.config import BridgeConfig


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    config = BridgeConfig(data_dir=root / ".contract-data", worker_enabled=False)
    schema = create_app(config).openapi()
    destination = root / "docs" / "openapi.json"
    destination.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
