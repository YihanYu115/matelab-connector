"""Read-back reconciliation that appends versions without replacing old mappings."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .canonical import canonical_json, sha256_tag
from .config import BridgeConfig
from .matelab_client import MatelabClient
from .models import CaptureEnvelope, MatelabRecordRef
from .security import safe_child
from .storage import BridgeStore
from .templates import select_template


class Reconciler:
    def __init__(self, config: BridgeConfig, store: BridgeStore, matelab: MatelabClient) -> None:
        self.config = config
        self.store = store
        self.matelab = matelab

    def run(self) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for row in self.store.latest_mappings():
            try:
                results.append(self._reconcile(row))
            except Exception as exc:
                results.append(
                    {
                        "capture_id": row["capture_id"],
                        "status": "error",
                        "error_type": type(exc).__name__,
                    }
                )
        return {
            "checked": len(results),
            "new_versions": sum(item["status"] == "new_version" for item in results),
            "conflicts": sum(item["status"] == "conflict" for item in results),
            "errors": sum(item["status"] == "error" for item in results),
            "results": results,
        }

    def _reconcile(self, row: Any) -> dict[str, Any]:
        capture_id = str(row["capture_id"])
        envelope = CaptureEnvelope.model_validate_json(row["manifest_json"])
        selection = select_template(self.config, envelope.capture_kind)
        matches = [
            item
            for item in self.matelab.items(selection.notebook)
            if item.get("sn") == row["record_uid"]
        ]
        if len(matches) != 1:
            return {
                "capture_id": capture_id,
                "status": "conflict",
                "reason": "record_uid_not_unique_or_missing",
            }
        item = matches[0]
        remote_version = int(item.get("version") or 1)
        local_version = int(row["record_version"])
        exported = self.matelab.export_record(
            selection.notebook, row["record_uid"], user=self.config.owner_id
        )
        export_bytes = canonical_json(exported)
        export_sha256 = sha256_tag(export_bytes)
        if not self._contains(exported, capture_id) or not self._contains(
            exported, row["manifest_sha256"]
        ):
            return {
                "capture_id": capture_id,
                "status": "conflict",
                "reason": "capture_marker_mismatch",
            }
        if remote_version < local_version:
            return {
                "capture_id": capture_id,
                "status": "conflict",
                "reason": "remote_version_regressed",
            }
        if remote_version == local_version:
            status = "unchanged" if export_sha256 == row["export_sha256"] else "conflict"
            return {
                "capture_id": capture_id,
                "status": status,
                "reason": None if status == "unchanged" else "same_version_content_changed",
            }
        previous = MatelabRecordRef.model_validate_json(row["record_ref_json"])
        updated = previous.model_copy(
            update={
                "record_id": str(item.get("id") or previous.record_id),
                "record_version": remote_version,
                "locked": bool(item.get("locked", False)),
                "signed": bool(item.get("signed", False)),
                "created_at": item.get("datetime_create") or previous.created_at,
                "modified_at": item.get("datetime_modify") or previous.modified_at,
                "export_sha256": export_sha256,
            }
        )
        path = self._write_export(capture_id, remote_version, export_bytes)
        self.store.save_mapping(capture_id, updated, export_json_path=path)
        return {
            "capture_id": capture_id,
            "status": "new_version",
            "record_version": remote_version,
            "export_sha256": export_sha256,
        }

    def _write_export(self, capture_id: str, version: int, content: bytes) -> Path:
        root = self.config.export_root
        directory = safe_child(root, capture_id)
        directory.mkdir(parents=True, exist_ok=True)
        destination = safe_child(directory, f"v{version}.json")
        temporary = safe_child(directory, f".v{version}.tmp")
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return destination

    @classmethod
    def _contains(cls, value: Any, expected: str) -> bool:
        if value == expected:
            return True
        if isinstance(value, str):
            return expected in value
        if isinstance(value, dict):
            return any(cls._contains(child, expected) for child in value.values())
        if isinstance(value, list):
            return any(cls._contains(child, expected) for child in value)
        return False
