from __future__ import annotations

from pathlib import Path

import pytest

from matelab_bridge import cli
from matelab_bridge.config import BridgeConfig


def test_gui_port_skips_unrelated_service(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = BridgeConfig(data_dir=tmp_path, port=8765)
    monkeypatch.setattr(cli, "_is_running_bridge", lambda _port: False)
    monkeypatch.setattr(cli, "_port_is_available", lambda _host, port: port == 8766)
    assert cli._select_gui_port(config) == (8766, False)


def test_gui_port_reuses_running_connector(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = BridgeConfig(data_dir=tmp_path, port=8765)
    monkeypatch.setattr(cli, "_is_running_bridge", lambda port: port == 8766)
    monkeypatch.setattr(cli, "_port_is_available", lambda _host, _port: False)
    assert cli._select_gui_port(config) == (8766, True)
