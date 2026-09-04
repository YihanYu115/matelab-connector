from __future__ import annotations

from pathlib import Path

import pytest

from matelab_bridge.config import BridgeConfig
from matelab_bridge.embedded_service import (
    ConnectorAlreadyRunningError,
    EmbeddedBridgeService,
)
from matelab_bridge.single_instance import SingleInstanceLock


def test_single_instance_lock_excludes_second_owner(tmp_path: Path) -> None:
    path = tmp_path / "native-instance.lock"
    first = SingleInstanceLock(path)
    second = SingleInstanceLock(path)

    assert first.acquire() is True
    assert first.acquire() is True
    assert second.acquire() is False

    first.release()
    assert second.acquire() is True
    second.release()


def test_single_instance_release_is_idempotent(tmp_path: Path) -> None:
    lock = SingleInstanceLock(tmp_path / "native-instance.lock")
    assert lock.acquire() is True
    lock.release()
    lock.release()
    assert lock.acquired is False


def test_embedded_service_refuses_a_second_native_instance(tmp_path: Path) -> None:
    guard = SingleInstanceLock(tmp_path / "native-instance.lock")
    assert guard.acquire() is True
    service = EmbeddedBridgeService(BridgeConfig(data_dir=tmp_path, worker_enabled=False))
    try:
        with pytest.raises(ConnectorAlreadyRunningError, match="已经在运行"):
            service.start()
        assert service.owned is False
    finally:
        service.stop()
        guard.release()
