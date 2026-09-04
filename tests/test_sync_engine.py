from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from matelab_bridge.canonical import canonical_json, sha256_tag
from matelab_bridge.config import BridgeConfig
from matelab_bridge.errors import MatelabAuthRequiredError, MatelabRetryableError
from matelab_bridge.models import CaptureEnvelope, SyncState
from matelab_bridge.reconciliation import Reconciler
from matelab_bridge.storage import BridgeStore
from matelab_bridge.sync_engine import SyncEngine
from matelab_bridge.templates import select_template


class FakeMatelabClient:
    def __init__(self) -> None:
        self.record: dict[str, Any] | None = None
        self.payload: dict[str, Any] | None = None
        self.uploads = 0

    def upload_file(self, path: Path, **kwargs: Any) -> dict[str, Any]:
        self.uploads += 1
        return {
            "name": kwargs["name"],
            "filename": path.name,
            "size": path.stat().st_size,
            "hash": kwargs["expected_sha256"],
        }

    def items(self, _notebook: str) -> list[dict[str, Any]]:
        return [self.record] if self.record else []

    def import_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payload = payload
        entry = payload["dataset"][0]
        self.record = {
            "id": 123,
            "sn": entry["uid"],
            "version": 1,
            "locked": False,
            "signed": False,
            "datetime_create": "2026-09-04 12:00",
            "datetime_modify": "2026-09-04 12:01",
        }
        return {"code": 0, "eln_id": 1264, "id": [123]}

    def export_record(self, _notebook: str, _uid: str, **_kwargs: Any) -> dict[str, Any]:
        assert self.payload is not None
        return {"code": 0, "dataset": self.payload["dataset"]}

    def close(self) -> None:
        pass


class RetryOnceClient(FakeMatelabClient):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    def items(self, notebook: str) -> list[dict[str, Any]]:
        if self.fail:
            self.fail = False
            raise MatelabRetryableError("offline")
        return super().items(notebook)


class AuthFailureClient(FakeMatelabClient):
    def items(self, _notebook: str) -> list[dict[str, Any]]:
        raise MatelabAuthRequiredError("expired")


class LostImportResponseClient(FakeMatelabClient):
    def __init__(self) -> None:
        super().__init__()
        self.import_calls = 0

    def import_record(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.import_calls += 1
        result = super().import_record(payload)
        if self.import_calls == 1:
            raise MatelabRetryableError("response lost after remote commit")
        return result


def persist_ready(config: BridgeConfig, manifest: dict) -> BridgeStore:
    store = BridgeStore(config.database_path)
    envelope = CaptureEnvelope.model_validate(manifest)
    body = canonical_json(envelope.model_dump(mode="json", by_alias=True))
    template = select_template(config, envelope.capture_kind)
    store.create_capture(
        envelope,
        body,
        sha256_tag(body),
        template_id=template.template_id,
        template_version=template.version,
        template_sha256=template.sha256,
    )
    store.commit_capture(envelope.capture_id)
    return store


def test_sync_to_verified_mapping(config: BridgeConfig, manifest: dict) -> None:
    store = persist_ready(config, manifest)
    fake = FakeMatelabClient()
    engine = SyncEngine(config, store, fake)  # type: ignore[arg-type]
    assert engine.process_once() == manifest["capture_id"]
    receipt = store.get_receipt(manifest["capture_id"])
    assert receipt.state == SyncState.COMPLETE
    assert receipt.attempt == 1
    assert receipt.matelab_ref is not None
    assert receipt.matelab_ref.record_uid.startswith("MB")
    assert receipt.matelab_ref.notebook_id == "1264"
    assert Path(
        store.history(manifest["capture_id"])[-3]["details"].get("unused", config.data_dir)
    ).exists()
    assert (config.data_dir / "exports" / manifest["capture_id"] / "v1.json").exists()


def test_retry_and_manual_wakeup(config: BridgeConfig, manifest: dict) -> None:
    store = persist_ready(config, manifest)
    fake = RetryOnceClient()
    engine = SyncEngine(config, store, fake)  # type: ignore[arg-type]
    engine.process_once()
    assert store.get_receipt(manifest["capture_id"]).state == SyncState.RETRY_WAIT
    store.retry_capture(manifest["capture_id"])
    engine.process_once()
    assert store.get_receipt(manifest["capture_id"]).state == SyncState.COMPLETE


def test_auth_failure_preserves_queue(config: BridgeConfig, manifest: dict) -> None:
    store = persist_ready(config, manifest)
    engine = SyncEngine(config, store, AuthFailureClient())  # type: ignore[arg-type]
    engine.process_once()
    receipt = store.get_receipt(manifest["capture_id"])
    assert receipt.state == SyncState.AUTH_REQUIRED
    assert receipt.last_error["code"] == "matelab_auth_required"


def test_restart_recovers_inflight_state(config: BridgeConfig, manifest: dict) -> None:
    store = persist_ready(config, manifest)
    assert store.claim_next() == manifest["capture_id"]
    assert store.recover_inflight() == 1
    assert store.get_receipt(manifest["capture_id"]).state == SyncState.READY


def test_lost_import_response_reconciles_without_duplicate(
    config: BridgeConfig, manifest: dict
) -> None:
    configured = replace(config, experiment_notebook_id="1264")
    store = persist_ready(configured, manifest)
    fake = LostImportResponseClient()
    engine = SyncEngine(configured, store, fake)  # type: ignore[arg-type]
    engine.process_once()
    assert store.get_receipt(manifest["capture_id"]).state == SyncState.RETRY_WAIT
    store.retry_capture(manifest["capture_id"])
    engine.process_once()
    assert store.get_receipt(manifest["capture_id"]).state == SyncState.COMPLETE
    assert fake.import_calls == 1


def test_new_capture_version_does_not_overwrite_old_mapping(
    config: BridgeConfig, manifest: dict
) -> None:
    store = persist_ready(config, manifest)
    fake = FakeMatelabClient()
    SyncEngine(config, store, fake).process_once()  # type: ignore[arg-type]
    original = store.get_receipt(manifest["capture_id"]).matelab_ref
    assert original is not None
    newer = copy.deepcopy(original)
    newer.record_version = 2
    newer.export_sha256 = f"sha256:{'f' * 64}"
    export_path = config.data_dir / "exports" / manifest["capture_id"] / "v2.json"
    export_path.write_text(json.dumps({"version": 2}), encoding="utf-8")
    store.save_mapping(manifest["capture_id"], newer, export_json_path=export_path)
    assert store.get_receipt(manifest["capture_id"]).matelab_ref.record_version == 2  # type: ignore[union-attr]
    with store.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM mappings").fetchone()[0]
    assert count == 2


def test_reconciliation_appends_remote_version(config: BridgeConfig, manifest: dict) -> None:
    store = persist_ready(config, manifest)
    fake = FakeMatelabClient()
    SyncEngine(config, store, fake).process_once()  # type: ignore[arg-type]
    assert fake.record is not None
    fake.record["version"] = 2
    report = Reconciler(config, store, fake).run()  # type: ignore[arg-type]
    assert report["new_versions"] == 1
    assert store.get_receipt(manifest["capture_id"]).matelab_ref.record_version == 2  # type: ignore[union-attr]
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM mappings").fetchone()[0] == 2


def test_reconciliation_reports_same_version_drift(config: BridgeConfig, manifest: dict) -> None:
    store = persist_ready(config, manifest)
    fake = FakeMatelabClient()
    SyncEngine(config, store, fake).process_once()  # type: ignore[arg-type]
    assert fake.payload is not None
    fake.payload["dataset"][0]["data"]["Summary"] += "changed"
    report = Reconciler(config, store, fake).run()  # type: ignore[arg-type]
    assert report["conflicts"] == 1
    assert report["results"][0]["reason"] == "same_version_content_changed"
