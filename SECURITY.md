# Security Policy

## Supported versions

Security fixes are provided for the latest published release.

## Local security boundary

MatElab Connector is a community-developed desktop client, not an official MatElab product.
It is designed for one trusted user on one computer and listens on `127.0.0.1` by default.
Do not expose its API to a LAN or the public Internet without enabling scoped API tokens and
placing it behind controls appropriate for that network.

The default loopback-only configuration does not authenticate local API calls. While the
Connector is running, only visit trusted websites and run trusted local software. Close the
Connector when it is not needed. Tokens are stored in the operating-system credential store;
passwords are used only for the login exchange and are not persisted by the application.

Operational rules:

- Never put MatElab usernames, passwords, Access Tokens, Refresh Tokens, local bridge tokens,
  or signature passwords in issues, manifests, fixtures, logs, diagnostics, or backups.
- Use `matelab-bridge auth login` or `auth set-token` so MatElab tokens are saved by the
  operating-system credential store.
- Treat submitted Artifact files as opaque data. The bridge hashes and transports them but
  never executes them.
- If a credential may have leaked, revoke the affected MatElab token or account first, then
  preserve only redacted diagnostics.

## Reporting a vulnerability

Use GitHub's private vulnerability-reporting feature when it is available on the repository.
Do not post passwords, access tokens, refresh tokens, private experiment records, or diagnostic
bundles in a public issue. If private reporting is unavailable, open a public issue containing
only a request for private contact and no sensitive details.
