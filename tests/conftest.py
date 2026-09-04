from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from matelab_bridge.config import BridgeConfig


@pytest.fixture
def config(tmp_path: Path) -> BridgeConfig:
    return BridgeConfig(data_dir=tmp_path, worker_enabled=False)


@pytest.fixture
def manifest() -> dict[str, Any]:
    return {
        "schema": "capture-envelope/v1",
        "capture_id": "capture-test-001",
        "capture_kind": "experiment_run",
        "occurred_at": "2026-09-04T08:21:13+08:00",
        "producer": {
            "kind": "qualab",
            "producer_id": "control-pc-01",
            "actor_id": "operator-01",
            "run_id": "run-001",
        },
        "scope": {"sample": "chip-A", "components": ["q1"], "topic": "calibration"},
        "source": {
            "kind": "datavault_dataset",
            "locator": "datavault://session/dataset-1",
            "immutable_revision": "1",
            "content_hash": f"sha256:{'1' * 64}",
        },
        "execution": {
            "entrypoint": "spectroscopy_auto",
            "git_commit": "abc123",
            "parameters": {"power_dbm": -20},
            "status": "success",
        },
        "summary": {"observations": ["resonance"]},
        "artifacts": [],
        "routing_hints": {},
        "extensions": {},
    }


def artifact_descriptor(content: bytes, artifact_id: str = "script") -> dict[str, Any]:
    return {
        "artifact_id": artifact_id,
        "role": "source_snapshot",
        "filename": "script.py",
        "mime_type": "text/x-python",
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "ingress": {"mode": "stream", "upload_id": f"upload-{artifact_id}"},
        "transfer_policy": "copy",
    }
