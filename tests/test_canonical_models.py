from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from matelab_bridge.canonical import (
    canonical_json,
    deterministic_record_uid,
    deterministic_upload_name,
    strict_json_loads,
)
from matelab_bridge.errors import StrictJSONError
from matelab_bridge.models import CaptureEnvelope, MatelabRecordRef


def test_strict_json_rejects_bom_and_non_finite() -> None:
    with pytest.raises(StrictJSONError, match="BOM"):
        strict_json_loads(b"\xef\xbb\xbf{}")
    with pytest.raises(StrictJSONError, match="NaN"):
        strict_json_loads('{"value": NaN}')
    with pytest.raises(StrictJSONError):
        canonical_json({"value": math.inf})


def test_canonical_json_and_deterministic_ids() -> None:
    assert canonical_json({"b": 1, "a": "汉字"}) == b'{"a":"\xe6\xb1\x89\xe5\xad\x97","b":1}'
    assert deterministic_record_uid("capture-a") == deterministic_record_uid("capture-a")
    assert deterministic_record_uid("capture-a") != deterministic_record_uid("capture-b")
    assert len(deterministic_record_uid("capture-a")) <= 45
    assert len(deterministic_upload_name("capture-a", "file-a")) <= 45


def test_capture_semantics_require_source_and_dirty_snapshot(manifest: dict) -> None:
    invalid = {**manifest, "source": None}
    with pytest.raises(ValidationError, match="requires source"):
        CaptureEnvelope.model_validate(invalid)
    dirty = {
        **manifest,
        "execution": {**manifest["execution"], "dirty_hash": f"sha256:{'2' * 64}"},
    }
    with pytest.raises(ValidationError, match="source_snapshot"):
        CaptureEnvelope.model_validate(dirty)


def test_artifact_rejects_executable_filename(manifest: dict) -> None:
    invalid = {
        **manifest,
        "artifacts": [
            {
                "artifact_id": "unsafe",
                "role": "result",
                "filename": "payload.exe",
                "mime_type": "application/octet-stream",
                "size": 1,
                "sha256": "1" * 64,
                "ingress": {"mode": "stream", "upload_id": "unsafe"},
                "transfer_policy": "copy",
            }
        ],
    }
    with pytest.raises(ValidationError, match="executable"):
        CaptureEnvelope.model_validate(invalid)


def test_field_note_preserves_original_text() -> None:
    capture = CaptureEnvelope.model_validate(
        {
            "schema": "capture-envelope/v1",
            "capture_id": "note-1",
            "capture_kind": "field_note",
            "occurred_at": "2026-09-04T12:00:00+08:00",
            "producer": {"kind": "desktop", "producer_id": "pc", "actor_id": "a"},
            "extensions": {"quick_note": {"original_text": "原始文本"}},
        }
    )
    assert capture.extensions["quick_note"]["original_text"] == "原始文本"


def test_manifest_rejects_credential_shaped_material() -> None:
    with pytest.raises(ValidationError, match="credential-shaped"):
        CaptureEnvelope.model_validate(
            {
                "schema": "capture-envelope/v1",
                "capture_id": "note-secret",
                "capture_kind": "field_note",
                "occurred_at": "2026-09-04T12:00:00+08:00",
                "producer": {"kind": "desktop", "producer_id": "pc", "actor_id": "a"},
                "extensions": {
                    "quick_note": {"original_text": "note"},
                    "access_token": "must-not-persist",
                },
            }
        )


def test_locator_uses_lossless_percent_encoding() -> None:
    ref = MatelabRecordRef(
        server_id="group/a",
        owner_id="owner name",
        notebook_id="记录本",
        notebook_name_snapshot="name",
        record_id="1",
        record_uid="uid@1",
        record_version=2,
        locked=False,
        signed=False,
        export_sha256=f"sha256:{'a' * 64}",
    )
    assert ref.locator == "matelab://group%2Fa/owner%20name/%E8%AE%B0%E5%BD%95%E6%9C%AC/uid%401@2"


def test_analysis_bundle_requires_auditable_minimum() -> None:
    content_hash = "3" * 64
    descriptor = lambda artifact_id, role: {  # noqa: E731
        "artifact_id": artifact_id,
        "role": role,
        "filename": f"{artifact_id}.txt",
        "mime_type": "text/plain",
        "size": 1,
        "sha256": content_hash,
        "ingress": {"mode": "stream", "upload_id": f"upload-{artifact_id}"},
        "transfer_policy": "copy",
    }
    payload = {
        "schema": "capture-envelope/v1",
        "capture_id": "analysis-1",
        "capture_kind": "analysis_run",
        "occurred_at": "2026-09-04T12:00:00+08:00",
        "producer": {
            "kind": "agent",
            "producer_id": "codex",
            "actor_id": "researcher",
            "run_id": "analysis-run-1",
        },
        "execution": {
            "entrypoint": "analysis.py",
            "script_snapshot_hash": f"sha256:{content_hash}",
            "git_commit": "abc123",
            "environment_hash": f"sha256:{content_hash}",
            "parameters": {},
            "random_seed": 7,
            "status": "success",
        },
        "artifacts": [
            descriptor("script", "source_snapshot"),
            descriptor("result", "analysis_output"),
        ],
        "extensions": {
            "analysis": {
                "analysis_run_id": "analysis-run-1",
                "inputs": [
                    {
                        "kind": "matelab_record",
                        "locator": "matelab://main/1/2/uid@1",
                        "content_hash": f"sha256:{content_hash}",
                    }
                ],
                "method_name": "fit",
                "method_version": "1",
                "bounded_log": "ok",
                "generation": {
                    "machine_generated": True,
                    "model": "model-id",
                    "prompt_template_version": "prompt-v1",
                    "tool_versions": {"fit": "1"},
                    "human_confirmed": False,
                },
            }
        },
    }
    assert CaptureEnvelope.model_validate(payload).producer.run_id == "analysis-run-1"
    payload["extensions"]["analysis"]["analysis_run_id"] = "different"
    with pytest.raises(ValidationError, match="must equal"):
        CaptureEnvelope.model_validate(payload)
