"""Controlled template snapshots and MatElab import payload mapping."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, cast
from urllib.parse import quote

from .canonical import canonical_json, sha256_tag
from .config import BridgeConfig
from .models import CaptureEnvelope, CaptureKind


@dataclass(frozen=True, slots=True)
class TemplateSelection:
    template_id: str
    version: str
    sha256: str
    notebook: str


def _snapshot_filename(kind: CaptureKind) -> str:
    if kind == CaptureKind.EXPERIMENT_RUN:
        return "matelab-auto-experiment-v1.json"
    if kind in {CaptureKind.ANALYSIS_RUN, CaptureKind.DERIVATION, CaptureKind.CORRECTION}:
        return "matelab-analysis-derivation-v1.json"
    return "matelab-work-progress-v1.json"


def load_snapshot(kind: CaptureKind) -> dict[str, Any]:
    path = files("matelab_bridge.template_snapshots").joinpath(_snapshot_filename(kind))
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("template snapshot root must be an object")
    return cast(dict[str, Any], value)


def select_template(config: BridgeConfig, kind: CaptureKind) -> TemplateSelection:
    snapshot = load_snapshot(kind)
    if kind == CaptureKind.EXPERIMENT_RUN:
        template_id = config.experiment_template
        notebook = config.experiment_notebook
    elif kind in {CaptureKind.ANALYSIS_RUN, CaptureKind.DERIVATION, CaptureKind.CORRECTION}:
        template_id = config.analysis_template
        notebook = config.analysis_notebook
    else:
        template_id = config.note_template
        notebook = config.note_notebook
    return TemplateSelection(
        template_id=template_id,
        version=str(snapshot["version"]),
        sha256=sha256_tag(canonical_json(snapshot)),
        notebook=notebook,
    )


def build_import_payload(
    config: BridgeConfig,
    envelope: CaptureEnvelope,
    capture_row: Any,
    artifact_rows: list[Any],
) -> dict[str, Any]:
    selection = select_template(config, envelope.capture_kind)
    source = envelope.source
    scope = envelope.scope
    metadata = {
        "Connector Schema": envelope.schema_,
        "Capture ID": envelope.capture_id,
        "Capture Hash": capture_row["manifest_sha256"],
        "Capture Kind": envelope.capture_kind.value,
        "Occurred At": envelope.occurred_at.isoformat(),
        "Received At": capture_row["received_at"],
        "Producer Kind": envelope.producer.kind,
        "Producer ID": envelope.producer.producer_id,
        "Actor ID": envelope.producer.actor_id,
        "Run ID": envelope.producer.run_id or "",
        "Source Kind": source.kind if source else "",
        "Source Locator": source.locator if source else "",
        "Source Revision": source.immutable_revision or "" if source else "",
        "Source Hash": source.content_hash or "" if source else "",
        "Sample": scope.sample or "",
        "Device": scope.device or "",
        "Components": json.dumps(scope.components, ensure_ascii=False, separators=(",", ":")),
        "Topic": scope.topic or "",
        "Template ID": selection.template_id,
        "Template Version": selection.version,
        "Template Hash": selection.sha256,
    }
    envelope_json = capture_row["manifest_json"]
    summary_json = json.dumps(
        envelope.summary.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
    )
    artifact_table: dict[str, list[Any]] = {
        "Artifact ID": [],
        "Role": [],
        "Filename": [],
        "MIME": [],
        "Size": [],
        "SHA256": [],
        "Transfer Policy": [],
        "File": [],
    }
    for row in artifact_rows:
        descriptor = json.loads(row["descriptor_json"])
        receipt = json.loads(row["matelab_receipt_json"]) if row["matelab_receipt_json"] else {}
        artifact_table["Artifact ID"].append(row["artifact_id"])
        artifact_table["Role"].append(descriptor["role"])
        artifact_table["Filename"].append(descriptor["filename"])
        artifact_table["MIME"].append(descriptor["mime_type"])
        artifact_table["Size"].append(descriptor["size"])
        artifact_table["SHA256"].append(descriptor["sha256"])
        artifact_table["Transfer Policy"].append(descriptor["transfer_policy"])
        file_name = receipt.get("name")
        artifact_table["File"].append(f"#file{{{file_name}}}" if file_name else "")

    keywords = ["matelab-bridge", envelope.capture_kind.value]
    keywords.extend(value for value in (scope.topic, scope.sample, scope.device) if value)
    safe_keywords = [value.replace(";", "_") for value in keywords]
    title = _record_title(envelope)
    data = {
        "Connector Metadata": metadata,
        "Capture Envelope JSON": f"<pre>{html.escape(envelope_json)}</pre>",
        "Summary": f"<pre>{html.escape(summary_json)}</pre>",
        "Artifacts": artifact_table,
    }
    return {
        "eln": selection.notebook,
        "template": selection.template_id,
        "dataset": [
            {
                "uid": capture_row["record_uid"],
                "title": title,
                "keyword": ";".join(safe_keywords),
                "data": data,
            }
        ],
    }


def build_manual_create_payload(
    envelope: CaptureEnvelope,
    capture_row: Any,
    *,
    notebook: str,
    user: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "eln": notebook,
        "path": [],
        "title": _record_title(envelope),
        "uid": capture_row["record_uid"],
        "describe": "由 MatElab Desktop Connector 手动提交",
    }
    if user and user != "self":
        payload["user"] = user
    return payload


def build_manual_update_payload(
    envelope: CaptureEnvelope,
    capture_row: Any,
    artifact_rows: list[Any],
    *,
    notebook: str,
    user: str | None,
) -> dict[str, Any]:
    note = cast(dict[str, Any], envelope.extensions["quick_note"])
    title = str(note.get("title") or _record_title(envelope))
    body = str(note["original_text"])
    metadata = {
        "Connector Schema": envelope.schema_,
        "Capture ID": envelope.capture_id,
        "Capture Hash": capture_row["manifest_sha256"],
        "Capture Kind": envelope.capture_kind.value,
        "Occurred At": envelope.occurred_at.isoformat(),
        "Received At": capture_row["received_at"],
        "Producer ID": envelope.producer.producer_id,
        "Actor ID": envelope.producer.actor_id,
        "Target Notebook": notebook,
    }
    artifact_lines: list[str] = []
    for row in artifact_rows:
        descriptor = json.loads(row["descriptor_json"])
        receipt = json.loads(row["matelab_receipt_json"]) if row["matelab_receipt_json"] else {}
        remote_hash = str(receipt.get("hash") or descriptor["sha256"])
        remote_size = int(receipt.get("size") or descriptor["size"])
        filename = str(receipt.get("filename") or descriptor["filename"])
        href = (
            f"elnurl://h={quote(remote_hash, safe='')}&s={remote_size}&f={quote(filename, safe='')}"
        )
        artifact_lines.append(
            f'<li><a href="{html.escape(href, quote=True)}">{html.escape(filename)}</a>'
            f"(SHA-256: {html.escape(descriptor['sha256'])})</li>"
        )
    artifacts_html = (
        "<ul>" + "".join(artifact_lines) + "</ul>"
        if artifact_lines
        else "<p>本条记录没有附件。</p>"
    )
    modules = [
        {
            "name": "记录内容",
            "type": "richtext",
            "data": f"<h2>{html.escape(title)}</h2><pre>{html.escape(body)}</pre>",
        },
        {
            "name": "附件",
            "type": "richtext",
            "data": artifacts_html,
        },
        {
            "name": "Connector 元数据",
            "type": "richtext",
            "data": "<pre>"
            + html.escape(json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2))
            + "</pre>",
        },
    ]
    payload: dict[str, Any] = {
        "eln": notebook,
        "uid": capture_row["record_uid"],
        "addModule": modules,
    }
    if user and user != "self":
        payload["user"] = user
    return payload


def _record_title(envelope: CaptureEnvelope) -> str:
    note = envelope.extensions.get("quick_note")
    if envelope.capture_kind == CaptureKind.FIELD_NOTE and isinstance(note, dict):
        title = str(note.get("title", "")).strip()
        if title:
            return title[:255]
        text = str(note.get("original_text", "")).strip().replace("\n", " ")
        return text[:80] or envelope.capture_id
    scope = envelope.scope
    parts = [envelope.capture_kind.value]
    parts.extend(value for value in (scope.sample, scope.device, scope.topic) if value)
    parts.append(envelope.capture_id)
    return " | ".join(parts)[:255]
