# MatElab API contract and Gate 0

Source checked on 2026-09-04: [MatElab ELN help](https://matelab.iphy.ac.cn/eln/#/doc), section “实验记录本 → 数据 API 接口”. The public examples point API traffic to `https://eln.iphy.ac.cn:61262`.

## Implemented documented contract

| Capability | Endpoint | Implemented behavior |
| --- | --- | --- |
| Login | `POST /tokens` | Form username/password; password exists only for the call; returned tokens go to OS keyring. |
| Refresh | `POST /tokens/tokens_refresh` | Form refresh token; rotated token bundle replaces the prior keyring entry. |
| Upload | `POST /eln_api/upload` | Serial chunks, default 8 MiB and hard maximum 20 MiB; stable `uid` and `name`; final response hash checked. |
| Import | `POST /eln_api/import` | One deterministic-UID record per call, one notebook and one controlled template. |
| Items | `POST /eln_api/items` | Used before import to reconcile an uncertain prior create. |
| Export | `POST /eln_api/export` | Exact UID request; complete response saved as canonical JSON and hashed. |
| Search | `POST /eln_api/search` | Exposed by the narrow client for contract verification. |
| Update | `POST /eln_api/update` | Exposed by the client, but not used by automatic submission. |
| ELNs | `POST /eln_api/elns` | Used by `doctor --online`. |

MatElab response `code` values are classified as: `0` success, `refresh` refresh-and-retry once, `1` authentication/authorization attention, `2` and `4` contract/input attention, and `3` retryable server failure. HTTP 429/5xx and network timeouts are retryable.

The official authentication page says both 60 minutes and 30 minutes for the Access Token and describes expiry as seconds while examples use millisecond-scale values. The client therefore trusts `expiredAt`, accepts either seconds or milliseconds, and never assumes a fixed lifetime.

## Gate 0: required real-environment results

Do not mark the production connector accepted until every line has a recorded fixture and result:

- [ ] Server choice, integration account, isolated personnel group, and three target notebooks.
- [ ] Exact template names and imported field/module compatibility for all three snapshots.
- [ ] Access/refresh lifetime, expiry unit, rotation, 401 behavior, and `code="refresh"` behavior.
- [ ] Upload retry for identical `uid/name/hash`, partial upload cleanup, zero-byte file, and final hash.
- [ ] Atomicity of create/import/update during timeout and response loss.
- [ ] `/items` pagination, stable ordering, creator identity, and historical versions.
- [ ] UID export on main versus group server; exact `record_uid + version` historical export behavior.
- [ ] Read-only account access to items/search/export.
- [ ] Locked record update/delete behavior and signature/certificate/timestamp fields in export.
- [ ] Notebook/template rename behavior and template-content drift.
- [ ] Collaboration edit behavior and whether an API update reaches durable storage.
- [ ] Attachment preview/download permission and URL lifetime.

Until historical version export is proven, receipts deliberately set `mutable_source=true`.

