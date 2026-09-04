# ADR 0001: Desktop bridge MVP defaults

Status: proposed pending Gate 0  
Date: 2026-09-04

## Decisions

- The bridge is a standalone Python 3.11+ service with FastAPI, SQLite WAL, a local Artifact spool, a single logical synchronizer, a thin SDK, and a Typer CLI.
- It binds to loopback by default. Non-loopback operation requires separately scoped local bearer tokens and an operator-provided HTTPS/mTLS boundary.
- The first release never calls MatElab lock/delete and never receives a digital-signature password.
- Upload names and record UIDs are deterministic hashes of caller identities. Capture payloads are RFC-style canonical JSON (sorted keys, UTF-8, no whitespace, NaN, Infinity, or BOM) hashed with SHA-256.
- A MatElab export is authoritative only for the version it describes. Each `(capture_id, record_uid, version)` mapping is append-only; it is never overwritten by a later version.
- Templates have fixed module and field names. Snapshot hashes are included in both the local row and MatElab machine-readable fields.
- `manifest_only` is the default escape hatch for oversized or unsuitable raw data. There is no staged-path mode in v1; adding it requires a separate allowlist/path-boundary ADR.
- MatElab main versus group server, exact notebook IDs, service-account ownership, signing policy, web-edit policy, and pilot dataset remain unresolved until Gate 0.

## Consequences

The connector can be fully tested offline and safely queues work during network or authentication outages. Production rollout remains intentionally blocked on real MatElab fixtures and notebook/template provisioning.

