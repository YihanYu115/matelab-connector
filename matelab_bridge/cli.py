"""Command-line interface for service, submitters, quick-note, and operations."""

from __future__ import annotations

import getpass
import hashlib
import json
import mimetypes
import os
import socket
import threading
import time
import uuid
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from .auth import KeyringCredentialStore
from .canonical import strict_json_loads
from .config import BridgeConfig
from .diagnostics import create_backup, create_diagnostic_bundle, doctor, verify_backup
from .fake_matelab import create_fake_matelab_app
from .matelab_client import MatelabClient
from .models import CaptureKind
from .reconciliation import Reconciler
from .sdk import BridgeClient
from .service import serve as run_service
from .storage import BridgeStore
from .sync_engine import SyncEngine

app = typer.Typer(help="Reliable desktop submission gateway for MatElab.", no_args_is_help=True)
capture_app = typer.Typer(help="Submit or inspect Capture Envelopes.")
analysis_app = typer.Typer(help="Submit analysis bundles.")
derivation_app = typer.Typer(help="Submit derivation bundles.")
auth_app = typer.Typer(help="Manage MatElab tokens in the operating-system credential store.")
app.add_typer(capture_app, name="capture")
app.add_typer(analysis_app, name="analysis")
app.add_typer(derivation_app, name="derivation")
app.add_typer(auth_app, name="auth")


def _client(token: str | None = None) -> BridgeClient:
    url = os.environ.get("MATELAB_BRIDGE_CLIENT_URL", "http://127.0.0.1:8765")
    token = token or os.environ.get("MATELAB_BRIDGE_CLIENT_TOKEN")
    return BridgeClient(
        url,
        token=token,
        submit_token=os.environ.get("MATELAB_BRIDGE_CLIENT_SUBMIT_TOKEN"),
        status_token=os.environ.get("MATELAB_BRIDGE_CLIENT_STATUS_TOKEN"),
        maintenance_token=os.environ.get("MATELAB_BRIDGE_CLIENT_MAINTENANCE_TOKEN"),
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    value = strict_json_loads(path.read_bytes())
    if not isinstance(value, dict):
        raise typer.BadParameter("manifest root must be a JSON object")
    return value


def _artifact_mapping(values: list[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        artifact_id, separator, path = value.partition("=")
        if not separator or not artifact_id or not path:
            raise typer.BadParameter("artifact must use ARTIFACT_ID=PATH")
        result[artifact_id] = Path(path)
    return result


def _submit(
    manifest_path: Path,
    artifact: list[str],
    expected_kind: CaptureKind | None = None,
) -> None:
    manifest = _load_manifest(manifest_path)
    if expected_kind and manifest.get("capture_kind") != expected_kind.value:
        raise typer.BadParameter(f"bundle capture_kind must be {expected_kind.value}")
    with _client() as client:
        receipt = client.submit_bundle(manifest, _artifact_mapping(artifact))
    typer.echo(receipt.model_dump_json(indent=2))


@capture_app.command("submit")
def capture_submit(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    artifact: Annotated[
        list[str] | None,
        typer.Option("--artifact", "-a", help="ARTIFACT_ID=PATH; repeat as needed"),
    ] = None,
) -> None:
    _submit(manifest, artifact or [])


@analysis_app.command("submit")
def analysis_submit(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    artifact: Annotated[list[str] | None, typer.Option("--artifact", "-a")] = None,
) -> None:
    _submit(manifest, artifact or [], CaptureKind.ANALYSIS_RUN)


@derivation_app.command("submit")
def derivation_submit(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
    artifact: Annotated[list[str] | None, typer.Option("--artifact", "-a")] = None,
) -> None:
    _submit(manifest, artifact or [], CaptureKind.DERIVATION)


@capture_app.command("status")
def capture_status(capture_id: str) -> None:
    with _client() as client:
        typer.echo(client.status(capture_id).model_dump_json(indent=2))


@capture_app.command("retry")
def capture_retry(capture_id: str) -> None:
    with _client() as client:
        typer.echo(client.retry(capture_id).model_dump_json(indent=2))


@app.command("note")
def note(
    text: Annotated[str, typer.Argument(help="Original note text; it is preserved verbatim.")],
    attach: Annotated[
        list[Path] | None, typer.Option("--attach", "-a", exists=True, dir_okay=False)
    ] = None,
    attach_recent: Annotated[bool, typer.Option("--attach-recent")] = False,
    actor: Annotated[str | None, typer.Option("--actor")] = None,
    producer: Annotated[str | None, typer.Option("--producer")] = None,
) -> None:
    actor_id = actor or getpass.getuser()
    producer_id = producer or socket.gethostname()
    artifacts: list[dict[str, Any]] = []
    paths: dict[str, Path] = {}
    for index, path in enumerate(attach or [], start=1):
        artifact_id = f"attachment-{index}"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "role": "note_attachment",
                "filename": path.name,
                "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                "size": path.stat().st_size,
                "sha256": digest,
                "ingress": {"mode": "stream", "upload_id": f"upload-{uuid.uuid4()}"},
                "transfer_policy": "copy",
            }
        )
        paths[artifact_id] = path
    routing: dict[str, Any] = {}
    with _client() as client:
        if attach_recent:
            candidates = client.recent(actor_id, producer_id)
            if len(candidates) == 1:
                routing["related_capture_id"] = candidates[0]["capture_id"]
                routing["routing_status"] = "unique_recent_match"
            else:
                routing["routing_status"] = "unrouted"
                routing["candidate_count"] = len(candidates)
        manifest = {
            "schema": "capture-envelope/v1",
            "capture_id": f"note-{uuid.uuid4()}",
            "capture_kind": "field_note",
            "occurred_at": datetime.now(UTC).isoformat(),
            "producer": {
                "kind": "desktop_quick_note",
                "producer_id": producer_id,
                "actor_id": actor_id,
                "run_id": None,
            },
            "scope": {},
            "source": None,
            "execution": None,
            "summary": {"observations": [text]},
            "artifacts": artifacts,
            "routing_hints": routing,
            "extensions": {"quick_note": {"original_text": text, "human_confirmed": True}},
        }
        receipt = client.submit_bundle(manifest, paths)
    typer.echo(receipt.model_dump_json(indent=2))


@app.command("serve")
def serve(verbose: bool = False) -> None:
    run_service(BridgeConfig.from_env(), verbose=verbose)


@app.command("gui")
def gui(
    open_browser: Annotated[
        bool,
        typer.Option("--open-browser/--no-browser", help="启动后自动打开中文 GUI。"),
    ] = True,
    verbose: bool = False,
) -> None:
    """启动本地 API, 并打开中文桌面操作界面。"""
    config = BridgeConfig.from_env()
    if open_browser:
        url = f"http://127.0.0.1:{config.port}/ui"

        def open_when_ready() -> None:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", config.port), timeout=0.4):
                        webbrowser.open(url)
                        return
                except OSError:
                    time.sleep(0.2)

        threading.Thread(target=open_when_ready, daemon=True).start()
    run_service(config, verbose=verbose)


@app.command("worker")
def worker(once: Annotated[bool, typer.Option("--once")] = False) -> None:
    config = BridgeConfig.from_env()
    store = BridgeStore(config.database_path)
    store.recover_inflight()
    matelab = MatelabClient(config)
    engine = SyncEngine(config, store, matelab)
    try:
        if once:
            processed = engine.process_once()
            typer.echo(json.dumps({"processed": processed}, ensure_ascii=False))
            return
        while True:
            if engine.process_once() is None:
                time.sleep(config.worker_poll_seconds)
    finally:
        matelab.close()


@app.command("fake-matelab")
def fake_matelab(host: str = "127.0.0.1", port: int = 8766) -> None:
    import uvicorn

    uvicorn.run(create_fake_matelab_app(), host=host, port=port)


@app.command("doctor")
def doctor_command(online: bool = False) -> None:
    report = doctor(BridgeConfig.from_env(), online=online)
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("backup")
def backup_command(destination: Path | None = None) -> None:
    config = BridgeConfig.from_env()
    destination = destination or Path(f"backup-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip")
    typer.echo(str(create_backup(config, destination).resolve()))


@app.command("verify-backup")
def verify_backup_command(
    path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    typer.echo(json.dumps(verify_backup(path), ensure_ascii=False, indent=2))


@app.command("diagnostic-bundle")
def diagnostic_bundle(destination: Path | None = None) -> None:
    config = BridgeConfig.from_env()
    destination = destination or Path(
        f"diagnostics-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.zip"
    )
    typer.echo(str(create_diagnostic_bundle(config, destination).resolve()))


@app.command("reconcile")
def reconcile_command() -> None:
    config = BridgeConfig.from_env()
    store = BridgeStore(config.database_path)
    matelab = MatelabClient(config)
    try:
        report = Reconciler(config, store, matelab).run()
        typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        matelab.close()


@auth_app.command("login")
def auth_login(username: str) -> None:
    password = typer.prompt("MatElab password", hide_input=True)
    config = BridgeConfig.from_env()
    client = MatelabClient(config)
    try:
        bundle = client.login(username, password)
        typer.echo(
            json.dumps(
                {"saved": True, "access_expires_at": bundle.access_expires_at},
                ensure_ascii=False,
            )
        )
    finally:
        client.close()


@auth_app.command("set-token")
def auth_set_token() -> None:
    access_token = typer.prompt("Access token", hide_input=True)
    refresh_token = typer.prompt("Refresh token (optional)", hide_input=True, default="")
    access_minutes = typer.prompt("Access token lifetime in minutes", default=60, type=int)
    refresh_hours = typer.prompt("Refresh token lifetime in hours", default=24, type=int)
    config = BridgeConfig.from_env()
    client = MatelabClient(config)
    now = time.time()
    try:
        client.set_tokens(
            access_token,
            refresh_token or None,
            access_expires_at=now + access_minutes * 60,
            refresh_expires_at=now + refresh_hours * 3600 if refresh_token else None,
        )
        typer.echo("tokens saved in the operating-system credential store")
    finally:
        client.close()


@auth_app.command("status")
def auth_status() -> None:
    config = BridgeConfig.from_env()
    client = MatelabClient(config)
    try:
        typer.echo(json.dumps(client.credential_status(), ensure_ascii=False, indent=2))
    finally:
        client.close()


@auth_app.command("clear")
def auth_clear() -> None:
    config = BridgeConfig.from_env()
    KeyringCredentialStore(config.matelab_url).clear()
    typer.echo("MatElab tokens removed from the operating-system credential store")


if __name__ == "__main__":
    app()
