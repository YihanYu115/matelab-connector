# Security policy

- Never put MatElab usernames, passwords, Access Tokens, Refresh Tokens, local bridge tokens, or signature passwords in issues, manifests, fixtures, logs, diagnostics, or backups.
- Use `matelab-bridge auth login` or `auth set-token` so MatElab tokens are saved by the operating-system credential store.
- Keep the gateway on `127.0.0.1` unless an approved HTTPS/mTLS proxy and scoped tokens are deployed.
- Treat all submitted Artifact files as opaque data. The bridge hashes and transports them but never executes them.
- Report a suspected credential leak by revoking the affected MatElab token/account first, then preserving only redacted diagnostics.

