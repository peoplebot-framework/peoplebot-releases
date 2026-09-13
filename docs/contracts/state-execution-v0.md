# Experimental State and Execution Contract v0

Status: implementation experiment, not a settled public schema.

This contract is the smallest executable proof of exact Git State and bounded Execution provenance. It adds no core primitive and does not settle the long-term storage layout or serialization standard.

## State reference

A `StateRef` contains:

- `repository`: a stable, non-empty repository identity supplied by the caller;
- `commit`: a lowercase full 40-hex Git commit ID;
- `path`: an optional canonical relative POSIX Git path.

The repository identity is intentionally opaque in v0. The local resolver proves that the exact commit and optional path exist in a supplied checkout; it does not yet prove that the checkout corresponds to the claimed external repository identity.

Resolution does not consult a branch or the working tree. It returns the commit's root tree and either the commit itself or the selected path object. Missing repositories, commits, trees, paths, or selected objects and unavailable/unsupported Git produce bounded classified failures.

The resolver requires Git 2.45 or newer and feature-probes the documented `--no-lazy-fetch` global option before inspecting a repository. Every inspection uses:

- `--no-replace-objects`, so replacement refs cannot change the meaning of a pinned commit;
- `--no-lazy-fetch`, so missing promisor objects remain local classified failures rather than triggering remote transport;
- `--no-optional-locks`, so read-only resolution does not take optional Git locks.

The subprocess environment removes ambient repository/worktree/object routing, namespace, discovery, and command-scope `GIT_CONFIG_COUNT`/`GIT_CONFIG_PARAMETERS` overrides. Normal system, global, and repository configuration remain available, credentials are not changed, and linked worktrees continue to resolve through their `.git` linkage file. Remote synchronization is a separate explicit operation outside `resolve_state`.

## Execution record

An `ExecutionRecord` requires:

- caller-supplied Execution, environment, and Instance identities;
- an objective and start/finish timestamps in the v0 RFC 3339 UTC subset;
- exact starting State, adopted Blueprint, and runtime Adapter references;
- exact procedure, input State, and input Message references where applicable;
- status of `completed`, `no_change`, `blocked`, or `failed`;
- resulting State for completed/no-change work, or a terminal outcome for blocked/failed work;
- exact artifact and reusable-learning references where produced;
- usage observations with explicit source and confidence.

The timestamp subset is exactly `YYYY-MM-DDTHH:MM:SS[.fraction]Z`: extended calendar date, uppercase `T` and `Z`, required seconds, and an optional fraction of one through six digits. Calendar values are validated. Basic dates, ISO week dates, offsets, lowercase separators, omitted seconds, and fractions beyond microsecond precision are rejected. Accepted timestamps are compared without truncating supported precision.

The v0 implementation never generates identities or timestamps implicitly. Unknown usage has a null value and both source and confidence set to `unknown`.

### Saved partial progress

A blocked or failed Execution retains its truthful terminal status, keeps `resulting_state` null, and may reference a recoverable partial commit through the existing `artifacts` tuple. At most one artifact with the same repository identity as `starting_state` and no path is permitted for a terminal Execution; that exact repository-level `StateRef` is its recoverable partial State. Other path-specific artifact references remain ordinary outputs. No partial State is required when no useful progress was committed.

This convention uses Git's existing commit/recovery behavior and adds no checkpoint primitive or subsystem.

### Admission-bound persistence

The separate experimental admission/Execution-provenance contract uses this
unchanged `ExecutionRecord` as terminal evidence. It commits an admitted-start
observation before task code, validates the terminal record's exact pre-task fields,
and commits that record while the live admission handle remains held. A rejected
attempt and an incomplete admitted start are not terminal `ExecutionRecord`
statuses. See `docs/contracts/execution-provenance-v0.md` for ordering, storage, and
failure semantics.

## Stable JSON encoding

Records use UTF-8 JSON with:

- lexicographically sorted object keys;
- compact separators;
- no non-finite numbers;
- Unicode emitted directly;
- one trailing newline.

Usage values are non-negative integers, plain decimal strings, or null. Decimal strings avoid cross-language floating-point rendering differences.

The format labels `peoplebot.resolved-state.v0` and `peoplebot.execution.v0` make the experimental encoding explicit. They are not a promise that the eventual public schema will retain these exact fields.

## Deliberate limits

This slice does not implement:

- external repository-identity verification;
- content projection or artifact retention;
- a production runtime Adapter or model invocation;
- messaging, cursors, acknowledgements, or remote synchronization;
- procedure divergence decisions, promotion, adoption, or reconciliation;
- usage collection;
- a license choice.
