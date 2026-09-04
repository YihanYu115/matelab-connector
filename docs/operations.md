# Operations

## Configuration

Configuration is environment-first and may be loaded from a UTF-8 `.env` file. MatElab tokens are never read from `.env`; they are stored under the endpoint-specific “MatElab Desktop Bridge” entry in the operating-system credential store.

The default listener is `127.0.0.1:8765` with local authentication disabled. Any non-loopback bind fails startup unless `MATELAB_BRIDGE_AUTH_MODE=token` and three distinct scope tokens are configured: submit, status, and maintenance. Put these in the service environment or a protected launcher configuration, not in version control. SDK/CLI clients use `MATELAB_BRIDGE_CLIENT_SUBMIT_TOKEN`, `MATELAB_BRIDGE_CLIENT_STATUS_TOKEN`, and `MATELAB_BRIDGE_CLIENT_MAINTENANCE_TOKEN`; the single `CLIENT_TOKEN` compatibility variable is intended only for loopback development.

## First setup

For ordinary desktop use, launch `MatElabConnector.exe` on Windows or `MatElab Connector.app` on macOS, log in from the native Chinese console, select a notebook, and submit a test note. The Windows source checkout launcher `start-matelab-bridge.cmd` prefers the already-built executable. Manual console records use MatElab `/create` and `/update`, so they do not require pre-installed Connector templates.

For local API callers, select a writable notebook on the native console's manual submission page and click `设为 API 默认记录本`. The selection is stored in `desktop-settings.json`. `POST /v1/manual-submissions` may then omit both `notebook_id` and `notebook_name`; explicit ID/name pairs remain supported as per-request overrides. See [中文 API 快速接入](api-quickstart.zh-CN.md).

Existing records can receive additional descriptions through `POST /v1/records/{record_uid}/descriptions`. The caller should take the UID from a completed sync receipt and reuse the same `Idempotency-Key` and JSON body after an uncertain response. This update is synchronous and is not part of the Capture retry worker.

Local integrations can resume from `GET /v1/events?after=<cursor>` and then stay connected to `GET /v1/events/stream?after=<cursor>`. Both endpoints use the status credential in token mode. Consumers own their committed cursor and should process each event idempotently. The database backup includes the full integration-event log and description idempotency ledger.

For structured submissions from Data Vault or analysis programs:

1. Create the three MatElab templates from `matelab_bridge/template_snapshots/` and configure their exact names, stable notebook IDs, notebooks, and numeric owner ID.
2. Log in from the native console or run `matelab-bridge auth login USERNAME`; the password is never stored.
3. Run `matelab-bridge doctor --online`.
4. Keep the native Connector running, or start `matelab-bridge serve` under the chosen service supervisor.
5. Submit a synthetic capture and verify it independently through MatElab `/items`, `/search`, and `/export`.

For a hardened Windows deployment, run the command from a dedicated unprivileged service account and use an approved supervisor such as WinSW or NSSM. Configure automatic restart, a private data directory ACL, and stdout/stderr rotation. This repository does not download or silently install a third-party supervisor.

## HTTP submission sequence

1. `POST /v1/captures` with strict UTF-8 JSON and `Content-Type: application/json`.
2. For every non-`manifest_only` Artifact, `PUT /v1/captures/{capture_id}/artifacts/{artifact_id}` with the raw bytes.
3. `POST /v1/captures/{capture_id}/commit`. `202` means durable local acceptance.
4. Poll `GET /v1/captures/{capture_id}` until `complete`, `auth_required`, or `needs_attention`.

The same `capture_id` and canonical payload returns the original `sync_id`. Reusing the ID with a different payload returns HTTP 409.

For the simpler multipart `POST /v1/manual-submissions` endpoint, generate one opaque ASCII `Idempotency-Key` (maximum 128 characters) per logical note. Reuse it when retrying after a timeout. The same key plus identical fields and attachment bytes returns the original `capture_id` and `"duplicate": true`, even if MatElab is temporarily unavailable; reusing the key with changed data returns HTTP 409. Omitting the header intentionally creates a new note on every request.

The native application holds a per-user data-directory process lock. A second launch reports that the Connector is already running and exits instead of attaching its window to another process's API lifetime.

The checked-in machine contract is `docs/openapi.json`. Regenerate it after API changes with `.\.venv\Scripts\python.exe tools\generate_openapi.py`.

## Recovery and maintenance

- `matelab-bridge capture retry CAPTURE_ID` moves an attention/retry item back to `ready` without deleting any remote object.
- `matelab-bridge doctor` checks SQLite, disk, queue, and token lifetime without printing token values. Add `--online` to call `/eln_api/elns`.
- `matelab-bridge backup PATH.zip` creates a consistent SQLite backup plus template snapshots; credentials and Artifacts are excluded.
- `matelab-bridge verify-backup PATH.zip` checks archive structure and SQLite integrity.
- `matelab-bridge diagnostic-bundle PATH.zip` creates a redacted support bundle without manifests, request bodies, tokens, or Artifact contents.
- `matelab-bridge reconcile` compares every latest local mapping to MatElab, appends newly observed versions, and reports same-version content drift without overwriting history.
- `/metrics` exposes state counts, oldest pending age, and unresolved orphan count in Prometheus text format.

Artifact bytes are excluded from the small operational backup because they may be very large. Back up the configured `artifacts/` and `exports/` directories with an access-controlled filesystem backup if full disaster recovery is required.
