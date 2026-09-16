# Experimental Admission/Execution Provenance Contract v0

Status: implementation experiment, not a settled public schema.

This contract connects the Windows Instance-admission observation to the existing
Execution record without adding a PeopleBot primitive. Durable records describe
what was observed; they never acquire, release, transfer, extend, or override the
live operating-system lock.

## Inputs and ownership

One attempt receives an explicit owning-environment identity, Instance identity,
attempted Execution identity, objective, start timestamp, starting State,
Blueprint State, Adapter State, and exact procedure, input-State, and input-Message
references. The caller also supplies the local Git checkout that owns the evidence
and the separately configured environment-local admission root.

The starting State is resolved from local Git objects before evidence is written.
Its repository identity must equal the evidence repository identity. These opaque
identities and Git references record provenance but do not authenticate the caller
or establish live ownership.

## Ordering and lifecycle

The synchronous admitted path has one required order:

1. acquire the live Instance admission handle;
2. commit an `admitted_start` observation on an isolated attempt ref;
3. start the protected task only after that commit succeeds;
4. obtain and validate the terminal `ExecutionRecord`;
5. commit that terminal record while the live admission handle is still held; and
6. release the exact admission handle only after protected task and owner-side
   evidence writes have stopped.

An authorized task-specific owner may propagate an unresolved child-lifetime
exception carrying exact in-process recovery authority. In that case the wrapper
does not perform ordinary release; it attaches the exact admission handle to that
authority and preserves incomplete start evidence. This narrow guard does not let a
durable record, PID, pathname, or unrelated exception retain or release ownership.

If the start commit fails, the task is not started. If the task raises, a
caller-supplied deterministic failure-record factory produces the truthful failed
`ExecutionRecord`; failure normalization and terminal persistence still occur under
the admission handle. There is no retry. A task failure and a subsequent terminal
write failure are returned as two distinct observations.

A contended attempt does not wait for the active owner and never starts the task.
It commits a `rejected` attempt observation on its own isolated ref. This write does
not advance or modify the active Instance branch. Failure to save the rejection is
reported alongside the returned rejection observation; it does not turn rejection
into admission.

## Records and completion meaning

`peoplebot.execution-attempt.v0` contains the attempted environment, Instance, and
Execution identities; objective; exact start timestamp; exact starting State,
Blueprint, Adapter, procedures, input States, and input Messages; admission result;
and phase (`rejected` or `admitted_start`). It records `task_started: false`, which
is truthful at the instant either record is committed.

An `admitted_start` commit proves only that admission was acquired and the task was
eligible to start next. Without a later terminal evidence commit, it is incomplete
evidence. It is not a completed, no-change, blocked, or failed Execution and does
not prove that retry is safe.

Terminal evidence is the existing `peoplebot.execution.v0` `ExecutionRecord`.
Before persistence, its environment, Instance, Execution, objective, start time,
starting State, Blueprint, Adapter, procedures, input States, and input Messages
must exactly match the committed start observation. Its existing validation
distinguishes `completed`, `no_change`, `blocked`, and `failed` outcomes. No fake
completion, resulting State, model invocation, or usage measurement is synthesized.

Rejected attempts have no terminal `ExecutionRecord`. They preserve the attempted
Execution and relevant exact inputs plus the `instance.already_running` admission
result. They do not claim completion and do not contain model usage.

Every normal task callback return is validated, including `null` or a value of the
wrong type. An absent required result is an explicit
`provenance.execution_record_unavailable` validation failure, not a reason to skip
validation. The exact admitted-start evidence remains incomplete, no terminal
record is manufactured, the callback is not repeated, and admission is released
only after owner-side handling stops.

## Git-native local persistence

V0 writes only Git objects and one isolated ref per attempt:

`refs/peoplebot/attempts/v0/<domain-separated-attempt-digest>`

The attempt commit is parented by the exact starting commit and contains
`attempt.json`. For an admitted attempt, the terminal commit is parented by the
attempt commit, contains `execution.json`, and atomically advances only that exact
attempt ref from the known start commit. A task-specific terminal companion may
also be placed in that tree. The read-only and project-review Adapters use
`adapter-observation.json`; the store adds a locally available same-repository
Adapter commit as another parent. This keeps adopted code/configuration reachable without
writing a self-referential commit hash into the tree. Initial publication similarly
requires the ref not to exist. Existing refs, checked-out branches, indexes,
working files, and untracked files are not modified.

Adapter companions may include the bounded event-classification diagnostic defined
by the adopted Adapter: counts, positions, allowlisted event/item names, value
shape/length metadata, and SHA-256 fingerprints only. They exclude raw provider
streams, unknown type names, prompts, reasoning, commands, tool arguments/results,
authorization material, and payloads. A companion persistence failure remains
separate from the in-process observation, available usage, and process outcome.

Publication uses Git's reference-transaction protocol. It prepares and locks the
exact attempt ref with the expected absent or direct-object state, inspects that
same ref for symbolic-ref identity while the transaction lock is held, and commits
only when Git specifically reports that the ref is not symbolic. For the inspection
command, exit status zero is a symbolic-ref finding and status one is the sole
eligible negative finding; every other status is an inspection failure. A symbolic
finding or inspection failure aborts the prepared transaction without replacing the
ref or advancing its target. The identity check is therefore not a separate
preflight followed by a raceable write. A logical ref name alone does not prove the
destination of a write; destination identity and expected object state must remain
protected by the same transaction.

The same transaction implementation can narrowly group multiple direct-ref verify,
create, update, and delete operations. Every named ref is locked and checked for
symbolic identity before commit; any mismatch aborts the whole group. Instance-
memory recovery uses this capability for its related owner, quarantine, and
canonical-ref transitions without introducing a second transaction protocol.

Git plumbing receives stable JSON through standard input and uses a fixed local
recording identity plus the whole-second value derived from the explicit record
timestamp for commit metadata (the JSON retains the exact supplied timestamp). It does
not check out files, run hooks or filters, consult replacement objects, honor
ambient repository/config routing, or automatically fetch missing objects. A
partially written object that was never attached to the attempt ref is not reported
as committed evidence.

Each successful persistence result distinguishes:

- the observation returned by the current process;
- an exact path-specific State reference to evidence committed in the local Git
  repository;
- the isolated movable attempt ref used to discover its current tip; and
- `remote_synchronized: false`.

V0 never pushes. Remote synchronization is a later explicit operation and cannot
be inferred from a local commit or returned record. Exact evidence retrieval uses
the path-specific commit State, not the movable ref or working files.

## Failure semantics

Failures are classified and returned without automatic retry:

- unavailable or invalid local starting objects prevent any evidence claim;
- an existing attempt ref prevents overwrite;
- an unexpected symbolic attempt ref prevents publication without changing it or
  its target;
- failure to establish whether the attempt ref is symbolic aborts publication with
  `provenance.ref_inspection_failed`; unknown identity is not treated as eligible;
- start-record failure prevents task start;
- a task exception is preserved separately from any normalization or persistence
  failure;
- a terminal-record failure leaves the exact start evidence incomplete and returns
  the in-process terminal observation separately from committed evidence; and
- a required terminal-companion or reachability failure likewise leaves start
  evidence incomplete rather than reporting the task result as durably persisted;
- unresolved task-owned child cleanup retains exact process and admission authority
  instead of releasing from an exception path; and
- secondary cleanup failures remain attached to the primary process/workspace
  ownership evidence and never replace the original interruption; a recoverable
  operation-owned workspace remnant does not retain admission once all task-owned
  child activity is confirmed stopped; and
- an admission-release failure reports uncertainty and retains the live handle for
  explicit owner-side recovery rather than pretending release.

The absence or freedom of a live lock is not completion evidence. A durable record
cannot free a lock, and a free lock cannot fill in a missing terminal record or
authorize retry of uncertain effects.

## Reusable provenance lessons

- A logical ref name does not prove its write destination. Protect ref identity
  and expected object state within the same Git transaction; a preflight check plus
  an unguarded or dereferencing update is insufficient.
- An absent required result is an explicit validation failure. It must preserve
  incomplete start evidence rather than silently bypassing validation or inventing
  a terminal Execution.
- An inspection error is not a negative finding. Mutation requires the specific
  successful observation that establishes eligibility; unknown state must preserve
  existing resources.
- Callback completion and exceptions do not prove owned children stopped. A narrow
  task lifetime guard may retain the exact admission handle only when it also
  retains exact in-process recovery authority.
- Cleanup must preserve causal ordering: first prove activity stopped, then dispose
  of its exact owned workspace. Secondary disposal failure is reported without
  masking the primary failure or extending execution ownership after activity stops.
- Independently validated usage remains provenance even when a later workspace
  disposal step fails; secondary cleanup cannot erase available execution evidence.
- Returned observations are not automatically durable, and commit IDs embedded in
  JSON do not make their objects reachable. Store sanitized task evidence in the
  terminal tree and connect required same-repository State through Git ancestry.

## Deliberate limits

This slice is local and synchronous. It does not implement model invocation,
messaging, external-effect reconciliation, retries, timers, PID takeover,
asynchronous workers, remote synchronization, UI, copying, migration, or automated
knowledge branching. Attempt-ref naming and record paths are experimental. The
caller-supplied external repository identity is recorded but not authenticated.
