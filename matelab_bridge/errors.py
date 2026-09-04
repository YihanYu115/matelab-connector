"""Domain-specific failures with safe, structured error categories."""

from __future__ import annotations


class BridgeError(Exception):
    """Base class for expected bridge failures."""

    code = "bridge_error"
    retryable = False


class StrictJSONError(BridgeError):
    code = "invalid_json"


class CaptureConflictError(BridgeError):
    code = "capture_identity_conflict"


class CaptureNotFoundError(BridgeError):
    code = "capture_not_found"


class ArtifactNotFoundError(BridgeError):
    code = "artifact_not_found"


class ArtifactConflictError(BridgeError):
    code = "artifact_conflict"


class ArtifactHashMismatchError(BridgeError):
    code = "artifact_hash_mismatch"


class ArtifactSizeMismatchError(BridgeError):
    code = "artifact_size_mismatch"


class IncompleteCaptureError(BridgeError):
    code = "capture_incomplete"


class InvalidStateError(BridgeError):
    code = "invalid_state"


class NotebookNotFoundError(BridgeError):
    code = "notebook_not_found"


class MappingConflictError(BridgeError):
    code = "mapping_conflict"


class MatelabError(BridgeError):
    code = "matelab_error"


class MatelabRetryableError(MatelabError):
    code = "matelab_retryable"
    retryable = True


class MatelabAuthRequiredError(MatelabError):
    code = "matelab_auth_required"


class MatelabConflictError(MatelabError):
    code = "matelab_conflict"


class MatelabContractError(MatelabError):
    code = "matelab_contract_error"
