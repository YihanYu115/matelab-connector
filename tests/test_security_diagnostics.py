from __future__ import annotations

import json
import zipfile
from pathlib import Path

from matelab_bridge.config import BridgeConfig
from matelab_bridge.diagnostics import create_backup, create_diagnostic_bundle, verify_backup
from matelab_bridge.security import redact, redact_text, safe_child
from matelab_bridge.storage import BridgeStore


def test_redaction_and_path_boundary(tmp_path: Path) -> None:
    assert redact_text("Authorization: Bearer abc.def") == "Authorization: Bearer [REDACTED]"
    assert redact({"password": "secret", "nested": {"access_token": "x"}}) == {
        "password": "[REDACTED]",
        "nested": {"access_token": "[REDACTED]"},
    }
    assert safe_child(tmp_path, "a", "b").is_relative_to(tmp_path.resolve())
    try:
        safe_child(tmp_path, "..", "outside")
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal accepted")


def test_backup_and_diagnostic_exclude_credentials(config: BridgeConfig, tmp_path: Path) -> None:
    BridgeStore(config.database_path)
    backup = create_backup(config, tmp_path / "backup.zip")
    assert verify_backup(backup)["ok"] is True
    with zipfile.ZipFile(backup) as archive:
        names = set(archive.namelist())
        assert "bridge.sqlite3" in names
        assert not any("token" in name.lower() for name in names)
        metadata = json.loads(archive.read("backup.json"))
        assert metadata["credential_material_included"] is False
    diagnostic = create_diagnostic_bundle(config, tmp_path / "diagnostic.zip")
    with zipfile.ZipFile(diagnostic) as archive:
        assert set(archive.namelist()) == {"doctor.json", "README.txt"}
