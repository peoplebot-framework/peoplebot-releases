# Instance usage-profile adoption checklist

Use this checklist independently in every sovereign environment and for every
existing Instance/chat binding. Installing the package prepares instructions only;
it does not launch or modify an environment.

For each environment, its owner must update the existing Instance profile once:

| Example | Required owner-local mapping |
|---|---|
| Environment A / Instance 1 | Existing Instance/session ID; observed Codex source format and exact rollout locator; local ledger/cursor/admission/run-record paths; authorized repository/path batch destination. |
| Environment A / Instance 2 | Same fields, with its own session mapping; do not copy another Instance's identity or authority. |
| Environment B / Instance 1 | Values from that sovereign environment; unsupported provider formats remain unsupported. |

Adopt the exact corrected standing rule in `local-usage-collection.md`. Keep the
environment, individual agent Instance, and provider session as separate fields;
task and launcher links are optional. Invoke the one profile-backed
`usage-report-run` operation at every manual/scheduled start and at
completion for success, failure, stop, or idle. Reconcile provisional late data at
the next normal start/recovery. Include the compact saved status/reference in the
ordinary completion reply and synchronize sanitized records in an existing
authorized task-branch batch, never one commit/network call per token event.

Before activation, deterministically verify profile/collector environment,
Instance, and session identities match; source and destination paths are absolute
and owner-local; the source format is one of the two observed mappings; and a
synthetic no-provider run proves start/completion/recovery without duplicate
events. Adoption of instructions is not proof of a provider or scheduler hook.
