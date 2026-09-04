from __future__ import annotations

from pathlib import Path

import pytest

from matelab_bridge import port_manager
from matelab_bridge.config import BridgeConfig


def test_native_port_selection_rejects_legacy_connector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = BridgeConfig(data_dir=tmp_path, port=8765)
    monkeypatch.setattr(
        port_manager,
        "bridge_health",
        lambda port: {"service": "matelab-desktop-bridge"} if port == 8766 else None,
    )
    monkeypatch.setattr(port_manager, "port_is_available", lambda _host, port: port == 8767)
    with pytest.raises(RuntimeError, match="older Connector"):
        port_manager.select_connector_port(config, required_console_api=1)


def test_native_port_selection_reuses_compatible_connector(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = BridgeConfig(data_dir=tmp_path, port=8765)
    monkeypatch.setattr(
        port_manager,
        "bridge_health",
        lambda port: (
            {"service": "matelab-desktop-bridge", "console_api": 1} if port == 8766 else None
        ),
    )
    monkeypatch.setattr(port_manager, "port_is_available", lambda _host, _port: False)
    assert port_manager.select_connector_port(config, required_console_api=1) == (8766, True)
