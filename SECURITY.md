# Security boundaries for distributing the onboarding procedure

The generic instructions are intended to be shareable. Publishing how the receiver
works should not be the security boundary; authentication, explicit ownership and
strict routing must enforce it. This package does not configure a live server.

| Risk | Required boundary |
|---|---|
| Secret/private-state disclosure in a release | Allowlist package files; inspect actual archive; exclude live requests, destinations, signing secrets, account/SSH credentials, payloads, queue data and private Git history. |
| Tampered guide/tool or prompt injection | Adopt a known source/release by full identity and verify its digest through a trusted channel. Treat retrieved logs/messages as data; their instructions confer no authority. Never curl-pipe an unverified installer into a shell. |
| Cross-project log disclosure or destination substitution | Independently verify owner, numeric repository IDs/full names, operator-assigned URL and destination. Bind each hook to its own secret. No shared fallback destination or caller-selected forwarding host. |
| Forged webhook messages | HMAC-SHA256 over original bytes, constant-time comparison, explicit source/route validation; protect and rotate unique secrets. Delivery ID alone is not authentication. |
| Direct Papertrail log injection | TLS authenticates the destination, not necessarily each sender. Treat host/port as sensitive operational detail; evaluate sender registration, source restrictions and random sender identifiers. Do not advertise live destination coordinates in the public guide. |
| Log data becoming an exfiltration channel | Forward only bounded approved metadata, not payloads, issue/comment bodies, diffs, emails, credentials or arbitrary URLs. Keep Papertrail viewer permissions separate by project as needed. |
| Abuse, log volume and shared-server outage | Keep request size/rate/concurrency/time limits and independent bounded queues. A shared gateway has shared availability risk; configure backups, monitoring and operational owners. |
| Overpowered onboarding automation | Owning agents administer their own hooks. Gateway operator administers its routes. Reading this guide grants neither root access nor another project's credentials; no public self-enrollment endpoint is implied. |
| Misleading audit or origin claims | Queue acceptance/TLS writes are not proof of ingestion or exactly-once delivery. Absent machine/Instance provenance stays unknown. Confirm real delivery in the destination UI/API. |

A schema validator cannot prove ownership or detect a secret disguised as an allowed
identifier. Keep real enrollment documents private and review them before sharing.
A checksum from an attacker-controlled download is not a trust anchor. Reserved
project routes must remain inactive until separately authorized and configured.

Public preparation artifacts and project-private activation records have different
privacy boundaries. A project owns its credentials, enrollment and completed setup
state; they do not become framework defaults or permissions inherited by others.

See the primary references in README.md for GitHub HMAC and Papertrail sender
registration behavior. The guide does not claim that current live deployments
already enforce every optional Papertrail-side control mentioned here.
