"""Read-only diagnostics plus token-free backup and support bundles."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from .auth import KeyringCredentialStore
from .config import BridgeConfig
from .matelab_client import MatelabClient
from .security import redact
from .storage import BridgeStore


def doctor(config: BridgeConfig, *, online: bool = False) -> dict[str, Any]:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    store = BridgeStore(config.database_path)
    with store.connect() as connection:
        integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
    usage = shutil.disk_usage(config.data_dir)
    credentials = KeyringCredentialStore(config.matelab_url)
    client = MatelabClient(config, credentials=credentials)
    try:
        credential_status = client.credential_status()
    except Exception as exc:
        credential_status = {"configured": False, "error_type": type(exc).__name__}
    result: dict[str, Any] = {
        "ok": integrity == "ok",
        "database": {"path": str(config.database_path), "quick_check": integrity},
        "disk": {"free_bytes": usage.free, "total_bytes": usage.total},
        "queue": store.metrics(),
        "credentials": credential_status,
        "matelab": {"checked": False},
        "production_readiness": {
            "owner_id_configured": config.owner_id != "self",
            "notebook_ids_configured": all(
                (
                    config.experiment_notebook_id,
                    config.analysis_notebook_id,
                    config.note_notebook_id,
                )
            ),
        },
    }
    if online:
        try:
            response = client.elns()
            result["matelab"] = {
                "checked": True,
                "reachable": True,
                "response_code": response.get("code"),
            }
        except Exception as exc:
            result["ok"] = False
            result["matelab"] = {
                "checked": True,
                "reachable": False,
                "error_type": type(exc).__name__,
            }
    client.close()
    return cast(dict[str, Any], redact(result))


def create_backup(config: BridgeConfig, destination: Path) -> Path:
    """Back up DB and immutable templates; credentials are deliberately excluded."""
    store = BridgeStore(config.database_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="matelab-bridge-backup-") as temp_name:
        temp = Path(temp_name)
        store.backup_to(temp / "bridge.sqlite3")
        snapshot_root = files("matelab_bridge.template_snapshots")
        for entry in snapshot_root.iterdir():
            if entry.name.endswith(".json"):
                (temp / entry.name).write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
        metadata = {
            "created_at": datetime.now(UTC).isoformat(),
            "format": "matelab-bridge-backup/v1",
            "matelab_url": config.matelab_url,
            "server_id": config.server_id,
            "credential_material_included": False,
        }
        (temp / "backup.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for entry in sorted(temp.iterdir()):
                archive.write(entry, arcname=entry.name)
    return destination


def create_diagnostic_bundle(config: BridgeConfig, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = doctor(config, online=False)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "doctor.json",
            json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
        )
        archive.writestr(
            "README.txt",
            b"This bundle excludes tokens, request bodies, manifests, and artifact contents.\n",
        )
    return destination


def verify_backup(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "bridge.sqlite3" not in names or "backup.json" not in names:
            return {"ok": False, "error": "missing required backup entries"}
        with tempfile.TemporaryDirectory(prefix="matelab-bridge-verify-") as temp_name:
            database = Path(temp_name) / "bridge.sqlite3"
            database.write_bytes(archive.read("bridge.sqlite3"))
            connection = sqlite3.connect(database)
            try:
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                connection.close()
    return {"ok": integrity == "ok", "database_quick_check": integrity}
