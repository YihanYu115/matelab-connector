"""Environment-first bridge configuration."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"MATELAB_BRIDGE_{name}", default)


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    data_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    auth_mode: str = "none"
    submit_token: str | None = None
    status_token: str | None = None
    maintenance_token: str | None = None
    matelab_url: str = "https://eln.iphy.ac.cn:61262"
    server_id: str = "main"
    owner_id: str = "self"
    experiment_notebook: str = "自动实验记录"
    analysis_notebook: str = "分析与推导"
    note_notebook: str = "工作进展"
    experiment_notebook_id: str | None = None
    analysis_notebook_id: str | None = None
    note_notebook_id: str | None = None
    experiment_template: str = "matelab-auto-experiment/v1"
    analysis_template: str = "matelab-analysis-derivation/v1"
    note_template: str = "matelab-work-progress/v1"
    max_artifact_bytes: int = 4 * 1024**3
    max_manifest_bytes: int = 2 * 1024**2
    upload_chunk_bytes: int = 8 * 1024**2
    worker_poll_seconds: float = 1.0
    worker_enabled: bool = True
    max_attempts: int = 20

    @classmethod
    def from_env(cls) -> BridgeConfig:
        load_dotenv(encoding="utf-8")
        default_data = Path(os.environ.get("LOCALAPPDATA", str(Path.cwd()))) / "MatElabBridge"
        config = cls(
            data_dir=Path(_env("DATA_DIR", str(default_data)) or default_data),
            host=_env("HOST", "127.0.0.1") or "127.0.0.1",
            port=int(_env("PORT", "8765") or 8765),
            auth_mode=_env("AUTH_MODE", "none") or "none",
            submit_token=_env("SUBMIT_TOKEN"),
            status_token=_env("STATUS_TOKEN"),
            maintenance_token=_env("MAINTENANCE_TOKEN"),
            matelab_url=(_env("MATELAB_URL", "https://eln.iphy.ac.cn:61262") or "").rstrip("/"),
            server_id=_env("SERVER_ID", "main") or "main",
            owner_id=_env("OWNER_ID", "self") or "self",
            experiment_notebook=_env("EXPERIMENT_NOTEBOOK", "自动实验记录") or "",
            analysis_notebook=_env("ANALYSIS_NOTEBOOK", "分析与推导") or "",
            note_notebook=_env("NOTE_NOTEBOOK", "工作进展") or "",
            experiment_notebook_id=_env("EXPERIMENT_NOTEBOOK_ID"),
            analysis_notebook_id=_env("ANALYSIS_NOTEBOOK_ID"),
            note_notebook_id=_env("NOTE_NOTEBOOK_ID"),
            experiment_template=_env("EXPERIMENT_TEMPLATE", "matelab-auto-experiment/v1") or "",
            analysis_template=_env("ANALYSIS_TEMPLATE", "matelab-analysis-derivation/v1") or "",
            note_template=_env("NOTE_TEMPLATE", "matelab-work-progress/v1") or "",
            max_artifact_bytes=int(_env("MAX_ARTIFACT_BYTES", str(4 * 1024**3)) or 0),
            max_manifest_bytes=int(_env("MAX_MANIFEST_BYTES", str(2 * 1024**2)) or 0),
            upload_chunk_bytes=int(_env("UPLOAD_CHUNK_BYTES", str(8 * 1024**2)) or 0),
            worker_poll_seconds=float(_env("WORKER_POLL_SECONDS", "1") or 1),
            worker_enabled=(_env("WORKER_ENABLED", "true") or "true").lower()
            not in {"0", "false", "no"},
            max_attempts=int(_env("MAX_ATTEMPTS", "20") or 20),
        )
        config.validate()
        return config

    @property
    def database_path(self) -> Path:
        return self.data_dir / "bridge.sqlite3"

    @property
    def artifact_root(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def export_root(self) -> Path:
        return self.data_dir / "exports"

    def validate(self) -> None:
        if self.auth_mode not in {"none", "token"}:
            raise ValueError("auth_mode must be 'none' or 'token'")
        try:
            is_loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            is_loopback = self.host.lower() == "localhost"
        if not is_loopback and self.auth_mode != "token":
            raise ValueError("non-loopback binding requires auth_mode=token")
        if self.auth_mode == "token" and not all(
            (self.submit_token, self.status_token, self.maintenance_token)
        ):
            raise ValueError("token mode requires submit, status, and maintenance tokens")
        local_tokens = (self.submit_token, self.status_token, self.maintenance_token)
        if self.auth_mode == "token" and len(set(local_tokens)) != 3:
            raise ValueError("submit, status, and maintenance tokens must be distinct")
        if not 0 < self.upload_chunk_bytes <= 20 * 1024**2:
            raise ValueError("upload_chunk_bytes must be between 1 and 20 MB")
        if self.max_artifact_bytes <= 0 or self.max_artifact_bytes > 4 * 1024**3:
            raise ValueError("max_artifact_bytes must be between 1 byte and 4 GB")
        if self.max_manifest_bytes <= 0:
            raise ValueError("max_manifest_bytes must be positive")
        if not 0 < self.port < 65536:
            raise ValueError("port must be between 1 and 65535")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.export_root.mkdir(parents=True, exist_ok=True)

    def notebook_and_template(self, kind: str) -> tuple[str, str]:
        if kind == "experiment_run":
            return self.experiment_notebook, self.experiment_template
        if kind in {"analysis_run", "derivation", "correction"}:
            return self.analysis_notebook, self.analysis_template
        return self.note_notebook, self.note_template
