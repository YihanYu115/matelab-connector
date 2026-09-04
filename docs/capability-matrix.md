# MatElab capability matrix

Checked against the public MatElab help on 2026-09-04. “Documented” is not equivalent to “verified on the pilot server”.

| Capability | Public documentation | Offline fake contract | Pilot server | MVP decision |
| --- | --- | --- | --- | --- |
| Access token login | Documented | Passed | Pending | Interactive login only; password never stored. |
| Refresh-token rotation | Documented; lifetime wording conflicts | Passed for seconds/milliseconds | Pending | Trust `expiredAt`; refresh once, then `auth_required`. |
| Serial chunk upload | ≤20 MB per chunk, ≤4 GB file | Passed | Pending | Default 8 MiB, stable UID/name/hash, never parallel. |
| Template import | One template/notebook, ≤100 records | Passed | Pending | One deterministic-UID record per request. |
| Duplicate import UID | Document says error | Passed | Pending | Query `/items` before import; different content is attention, never overwrite. |
| Record list | Documented; pagination unspecified | Passed for fake | Pending | Use only for exact UID reconciliation; pagination is Gate 0. |
| Search | ≤24 requested fields | Passed for fixed metadata path | Pending | Fixed machine fields; independent-client acceptance test included. |
| UID export | Main server documented; group server unsupported | Passed for fake | Pending | Production server choice is Gate 0; otherwise `needs_attention`. |
| Historical version export | Not documented clearly | Version mappings tested locally | Pending | `mutable_source=true`; append observed versions, preserve old export snapshots. |
| Collaborative update durability | Warning documented | Acknowledgement modeled; active-editor save not emulated | Pending | Capture submission does not call update; description receipts explicitly stop at MatElab acknowledgement. |
| Append record description | `update.addModule` rich text documented; collaborative-save caveat | Passed with stable module name and idempotency ledger | Pending | Synchronous MatElab acknowledgement; caller retries with the same key. |
| Downstream change notification | No MatElab callback contract used | Durable cursor replay and SSE passed locally | Pending | Publish verified record sync and acknowledged description events; no outbound webhook. |
| Lock/signature | Endpoint documented; private password required | Not used | Pending | Never automatic; connector never stores signature password. |
| Read-only account access | Not specified | Not modeled | Pending | Required Gate 0 permission test. |
| Service/restricted token | Not documented | Not modeled | Pending | Prefer least-privilege integration account if MatElab supports one. |

Go/No-Go remains **No-Go for production** until the Pilot server column is complete. Offline implementation and local integration testing may proceed.
