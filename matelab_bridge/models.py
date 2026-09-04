"""Versioned public protocol models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CAPTURE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,126}[A-Za-z0-9]$|^[A-Za-z0-9]$"
ARTIFACT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,126}[A-Za-z0-9]$|^[A-Za-z0-9]$"
SHA256_PATTERN = r"^sha256:[0-9a-fA-F]{64}$"
BLOCKED_ARTIFACT_SUFFIXES = {".com", ".dll", ".exe", ".msi", ".scr"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CaptureKind(StrEnum):
    EXPERIMENT_RUN = "experiment_run"
    ANALYSIS_RUN = "analysis_run"
    DERIVATION = "derivation"
    FIELD_NOTE = "field_note"
    CORRECTION = "correction"


class SyncState(StrEnum):
    RECEIVING = "receiving"
    VALIDATING = "validating"
    ACCEPTED = "accepted"
    READY = "ready"
    UPLOADING_FILES = "uploading_files"
    CREATING_RECORD = "creating_record"
    POPULATING_RECORD = "populating_record"
    VERIFYING_EXPORT = "verifying_export"
    MATELAB_VERIFIED = "matelab_verified"
    COMPLETE = "complete"
    RETRY_WAIT = "retry_wait"
    AUTH_REQUIRED = "auth_required"
    NEEDS_ATTENTION = "needs_attention"
    ABANDONED = "abandoned"


class Producer(StrictModel):
    kind: str = Field(min_length=1, max_length=64)
    producer_id: str = Field(min_length=1, max_length=255)
    actor_id: str = Field(min_length=1, max_length=255)
    run_id: str | None = Field(default=None, max_length=255)


class Scope(StrictModel):
    sample: str | None = Field(default=None, max_length=255)
    device: str | None = Field(default=None, max_length=255)
    components: list[str] = Field(default_factory=list, max_length=256)
    topic: str | None = Field(default=None, max_length=255)


class Source(StrictModel):
    kind: str = Field(min_length=1, max_length=64)
    locator: str = Field(min_length=1, max_length=4096)
    immutable_revision: str | None = Field(default=None, max_length=1024)
    content_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)


class Execution(StrictModel):
    entrypoint: str | None = Field(default=None, max_length=1024)
    script_snapshot_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    git_commit: str | None = Field(default=None, max_length=128)
    dirty_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    environment_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    parameters: dict[str, Any] = Field(default_factory=dict)
    random_seed: int | str | None = None
    status: str | None = Field(default=None, max_length=64)


class Summary(StrictModel):
    actions: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    interpretations: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)
    rationale: str | None = Field(default=None, max_length=100_000)
    limitations: list[str] = Field(default_factory=list)


class InputReference(StrictModel):
    kind: str = Field(min_length=1, max_length=64)
    locator: str = Field(min_length=1, max_length=4096)
    content_hash: str = Field(pattern=SHA256_PATTERN)
    immutable_revision: str | None = Field(default=None, max_length=1024)


class GenerationDetails(StrictModel):
    machine_generated: bool
    model: str | None = Field(default=None, max_length=255)
    prompt_template_version: str | None = Field(default=None, max_length=255)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    human_confirmed: bool = False

    @model_validator(mode="after")
    def require_generator_identity(self) -> GenerationDetails:
        if self.machine_generated and (not self.model or not self.prompt_template_version):
            raise ValueError("machine-generated work requires model and prompt_template_version")
        return self


class AnalysisDetails(StrictModel):
    analysis_run_id: str = Field(min_length=1, max_length=255)
    inputs: list[InputReference] = Field(min_length=1)
    method_name: str = Field(min_length=1, max_length=255)
    method_version: str = Field(min_length=1, max_length=255)
    bounded_log: str = Field(max_length=100_000)
    generation: GenerationDetails


class DerivationDetails(StrictModel):
    problem: str = Field(min_length=1, max_length=100_000)
    assumptions: list[str] = Field(min_length=1)
    symbols_and_units: dict[str, str]
    inputs: list[InputReference] = Field(default_factory=list)
    result: str = Field(min_length=1, max_length=100_000)
    applicability: str = Field(min_length=1, max_length=100_000)
    generation: GenerationDetails


class ArtifactIngress(StrictModel):
    mode: Literal["stream", "none"]
    upload_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_upload_id(self) -> ArtifactIngress:
        if self.mode == "stream" and not self.upload_id:
            raise ValueError("stream ingress requires upload_id")
        if self.mode == "none" and self.upload_id is not None:
            raise ValueError("none ingress cannot include upload_id")
        return self


class ArtifactDescriptor(StrictModel):
    artifact_id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    role: str = Field(min_length=1, max_length=64)
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0, le=4 * 1024**3)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    ingress: ArtifactIngress
    transfer_policy: Literal["copy", "manifest_only", "preview_only"]
    required: bool = True

    @field_validator("filename")
    @classmethod
    def plain_filename(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value or "\0" in value:
            raise ValueError("filename must not contain a path")
        from pathlib import PurePath

        if PurePath(value).suffix.lower() in BLOCKED_ARTIFACT_SUFFIXES:
            raise ValueError("executable artifact extension is not allowed")
        return value

    @field_validator("mime_type")
    @classmethod
    def safe_mime_type(cls, value: str) -> str:
        if "\r" in value or "\n" in value or "/" not in value:
            raise ValueError("mime_type must be a single valid media type")
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> ArtifactDescriptor:
        if self.transfer_policy == "manifest_only" and self.ingress.mode != "none":
            raise ValueError("manifest_only artifacts must use ingress.mode=none")
        if self.transfer_policy != "manifest_only" and self.ingress.mode != "stream":
            raise ValueError("copied artifacts must use ingress.mode=stream")
        return self


class CaptureEnvelope(StrictModel):
    schema_: Literal["capture-envelope/v1"] = Field(alias="schema")
    capture_id: str = Field(pattern=CAPTURE_ID_PATTERN)
    capture_kind: CaptureKind
    occurred_at: datetime
    producer: Producer
    scope: Scope = Field(default_factory=Scope)
    source: Source | None = None
    execution: Execution | None = None
    summary: Summary = Field(default_factory=Summary)
    artifacts: list[ArtifactDescriptor] = Field(default_factory=list, max_length=1024)
    routing_hints: dict[str, Any] = Field(default_factory=dict)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a UTC offset")
        return value

    @model_validator(mode="after")
    def validate_capture_semantics(self) -> CaptureEnvelope:
        from .security import contains_secret_material

        if contains_secret_material(self.model_dump(mode="json", by_alias=True)):
            raise ValueError("capture manifests cannot contain credential-shaped material")
        ids = [item.artifact_id for item in self.artifacts]
        if len(ids) != len(set(ids)):
            raise ValueError("artifact_id values must be unique")
        if self.capture_kind == CaptureKind.EXPERIMENT_RUN and self.source is None:
            raise ValueError("experiment_run requires source")
        if self.execution and self.execution.dirty_hash:
            source_roles = {item.role for item in self.artifacts}
            if "source_snapshot" not in source_roles:
                raise ValueError("dirty_hash requires a source_snapshot artifact")
        if self.capture_kind in {CaptureKind.ANALYSIS_RUN, CaptureKind.DERIVATION}:
            if self.execution is None:
                raise ValueError("analysis and derivation captures require execution")
            if not self.artifacts:
                raise ValueError("analysis and derivation captures require at least one artifact")
        if self.capture_kind == CaptureKind.ANALYSIS_RUN:
            details = AnalysisDetails.model_validate(self.extensions.get("analysis"))
            if details.analysis_run_id != self.producer.run_id:
                raise ValueError("analysis_run_id must equal producer.run_id")
            required_execution = {
                "script_snapshot_hash",
                "git_commit",
                "environment_hash",
                "parameters",
                "random_seed",
                "status",
            }
            if self.execution is None or not required_execution.issubset(
                self.execution.model_fields_set
            ):
                raise ValueError(
                    "analysis execution requires explicit script/Git/environment/"
                    "parameters/seed/status"
                )
            roles = {item.role for item in self.artifacts}
            if not roles.intersection({"source_snapshot", "script", "notebook"}):
                raise ValueError("analysis requires a script or Notebook snapshot artifact")
            if not roles.intersection({"analysis_output", "result", "figure", "numerical_result"}):
                raise ValueError("analysis requires an output Artifact")
        if self.capture_kind == CaptureKind.DERIVATION:
            DerivationDetails.model_validate(self.extensions.get("derivation"))
            roles = {item.role for item in self.artifacts}
            if not roles.intersection(
                {"derivation_source", "source_snapshot", "notebook", "calculation_code"}
            ):
                raise ValueError("derivation requires a derivation file or calculation code")
        if self.capture_kind == CaptureKind.FIELD_NOTE:
            note = self.extensions.get("quick_note", {})
            if not isinstance(note, dict) or not str(note.get("original_text", "")).strip():
                raise ValueError("field_note requires extensions.quick_note.original_text")
        return self


class MatelabRecordRef(StrictModel):
    server_id: str
    owner_id: str
    notebook_id: str
    notebook_name_snapshot: str
    record_id: str
    record_uid: str
    record_version: int = Field(ge=1)
    locked: bool
    signed: bool
    created_at: str | None = None
    modified_at: str | None = None
    export_sha256: str = Field(pattern=SHA256_PATTERN)
    attachment_hashes: list[str] = Field(default_factory=list)
    mutable_source: bool = True

    @property
    def locator(self) -> str:
        from urllib.parse import quote

        parts = [self.server_id, self.owner_id, self.notebook_id, self.record_uid]
        encoded = [quote(part, safe="") for part in parts]
        return f"matelab://{'/'.join(encoded[:3])}/{encoded[3]}@{self.record_version}"


class SyncReceipt(StrictModel):
    sync_id: str
    capture_id: str
    source_locator: str | None = None
    source_hash: str | None = None
    manifest_sha256: str
    matelab_ref: MatelabRecordRef | None = None
    state: SyncState
    attempt: int
    last_error: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class CaptureAccepted(StrictModel):
    capture_id: str
    sync_id: str
    manifest_sha256: str
    state: SyncState
    duplicate: bool = False


class ArtifactReceipt(StrictModel):
    artifact_id: str
    size: int
    sha256: str
    state: Literal["received"] = "received"


class RecentCandidate(StrictModel):
    capture_id: str
    occurred_at: datetime
    actor_id: str
    producer_id: str


class RecordDescriptionRequest(StrictModel):
    content: str = Field(min_length=1, max_length=100_000)
    title: str | None = Field(default=None, max_length=120)
    notebook_id: str | None = Field(default=None, min_length=1, max_length=255)
    notebook_name: str | None = Field(default=None, min_length=1, max_length=255)


class RecordDescriptionReceipt(StrictModel):
    description_id: str
    record_uid: str
    notebook_id: str
    notebook_name: str
    module_name: str
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    status: Literal["matelab_acknowledged"] = "matelab_acknowledged"
    event_cursor: int = Field(ge=1)
    duplicate: bool = False


class IntegrationEvent(StrictModel):
    cursor: int = Field(ge=1)
    id: str
    type: str = Field(pattern=r"^[a-z][a-z0-9]*(?:\.[a-z0-9_]+)+$")
    occurred_at: datetime
    subject: str
    data: dict[str, Any]


class IntegrationEventBatch(StrictModel):
    events: list[IntegrationEvent]
    next_cursor: int = Field(ge=0)
