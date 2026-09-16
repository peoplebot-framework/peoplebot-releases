# PeopleBot 0.1.0a4

Status: experimental prerelease.

This release adds project-neutral, bounded framework utilities completed after
`0.1.0a3`:

- corrected Codex event classification and bounded sanitized diagnostics;
- owner-publishes/authorized-peers-read Git messaging;
- finite work-cycle and development-cycle paths with isolated attempt
  branches/worktrees, preserved ordinary failures, exact review packets, STOP and
  unresolved barriers, and no automatic retry;
- deterministic local usage collection and per-run reporting which keeps known
  Instance/session observations even when optional task/launcher links are absent;
- neutral setup guidance covering Instance/chat bindings, messaging ownership,
  usage versus allowance, scheduling limits, and Git-backed knowledge recovery.

The distribution remains experimental. It does not create a functioning
Architect, configure a provider, register a schedule, transfer credentials, or
prove Claude or non-Windows runtime support. The included Windows launcher and
Task Scheduler template are inactive. Native provider-app idle heartbeats may
consume a model turn before repository code runs.

Matching source is published alongside the wheel. Verify the three entries in
`SHA256SUMS`, verify `SHA256SUMS` against the digest in the GitHub release body,
and retain the public provenance file.
