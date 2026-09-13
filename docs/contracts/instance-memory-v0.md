# Experimental Instance Memory Contract v0

Status: implementation experiment, not a settled public schema.

This contract proves that one Instance can checkpoint explicitly supplied,
authorized task memory in local Git and that a later Execution can retrieve
selected bytes from the exact saved State. Memory remains Instance-owned State;
this adds no PeopleBot primitive, database, autonomous selection, or model call.

## Identity and inputs

`MemoryCheckpointRequest` supplies an external repository identity, owning
environment identity, Instance identity, adopted Blueprint State, exact expected
repository-level State, explicit memory items, a commit timestamp, and an explicit
`initial` flag. Repository and ownership identities are trusted caller/environment
configuration; v0 records but cannot independently authenticate them.

Each `MemoryItem` has a unique canonical relative POSIX path and complete UTF-8
text. V0 permits at most 64 items, 65,536 UTF-8 bytes per item, 262,144 UTF-8 bytes
in total, and 65,536 bytes of generated metadata. NUL content and noncanonical
paths are rejected. The operation never discovers files from a working directory
and never decides what should be remembered.

## Git layout and exact State

One owning environment/Instance maps deterministically to:

`refs/heads/peoplebot/instances/v0/<environment-digest>/<instance-digest>`

The ref is a discovery address. An exact `StateRef` containing the full memory
commit is the saved State. The commit tree contains `memory.json` and one regular
blob at `memory/<item-path>`. Metadata binds the State to format, repository,
environment, Instance, adopted Blueprint State, deterministic ref name, and the
canonical item list with Git object ID, raw UTF-8 byte count, and SHA-256 digest.
Execution IDs, save timestamps, logs, mutable machine paths, and incidental
diagnostics are not memory content.

Initial creation must set `initial=true`, supplies an exact repository commit as
the new memory commit's parent, and requires the destination ref to be absent.
Later checkpoints set `initial=false`, supply the exact prior memory State, validate
its identity and Blueprint binding, and create a child commit only when the memory
tree changes. Reordered equivalent items and a later save timestamp do not change
the tree. An unchanged save verifies the exact direct ref with the same guarded
reference transaction and returns the existing State with `changed=false`; it does
not manufacture a memory commit.

Publication prepares and locks only the derived ref, guards its absent or expected
object value, and checks symbolic identity while locked. Symbolic destinations,
inspection failures, existing initial destinations, and stale expected States are
classified and preserved. Before changed or initial publication, v0 parses Git's
NUL-delimited porcelain worktree inventory and refuses a destination branch selected
by the calling or any linked worktree, including an absent branch selected as an
unborn `HEAD`. Inspection failure or ambiguous structured output also refuses
publication. An unchanged save remains permitted because its same-object guarded
transaction leaves `HEAD`, index, working files, untracked files, and registrations
unchanged. Another Instance's ref is not touched. Git object writes
and reference publication do not stage, check out, scan, or change the caller's
branch, index, working files, or untracked files.

## Execution lifecycle and interruption boundary

`run_instance_memory_execution` is the supported mutation entry point;
`GitMemoryStore` is its internal storage collaborator rather than an independent
authority-bearing API. The entry point binds the request to the same environment,
Instance, Blueprint, and starting State as `ExecutionStart`, then reuses the
existing nonblocking admission and local provenance lifecycle. A busy Instance
does not execute the memory callback. An admitted attempt commits exact start
evidence before checkpointing and commits a truthful `completed`, `no_change`, or
failed terminal `ExecutionRecord` before releasing admission.

Memory and terminal evidence are separate publications. A successful memory
checkpoint remains reachable from its Instance ref even if terminal evidence later
fails. The returned `MemoryExecutionResult` retains that checkpoint beside the
provenance persistence failure; it does not roll back useful State, fabricate
terminal evidence, rerun the callback, or infer that retry is safe. Exact expected
State prevents a later retry from silently replacing newer memory.

If successful-record construction fails after checkpoint publication, a later
truthful failed record keeps `resulting_state=null` and includes the exact saved
repository-level State as its single recoverable partial-State artifact. If even
failed-record construction is impossible, the returned result still retains the
checkpoint beside the classified record failure; no timestamp or terminal evidence
is fabricated.

`locally_committed=true` means the memory commit is attached to the local Instance
ref. `remote_synchronized=false` is always reported by v0. Pushing the framework's
implementation branch does not push runtime Instance-memory refs.

## Exact resume

`assemble_instance_memory_context` accepts an exact repository-level memory State,
the expected environment, Instance, and Blueprint State, explicit logical item
paths, and an explicit `ContextPolicy` pinned to that same memory commit. It first
validates the memory metadata binding, maps requested logical paths to
`memory/<path>`, and delegates content retrieval to the existing deterministic
context assembly. Earlier checkpoints remain readable by commit after the
discovery ref advances.

Resolution reads only local Git objects with replacement refs, ambient repository
routing, configuration overrides, and lazy fetching disabled. It uses bounded
`shell=False` subprocesses, creates no worktree, follows no host symlink, fetches no
submodule, and invokes no hook or checkout filter. A missing path or object fails
the complete request rather than being silently omitted or fetched.

## Reusable lessons

- Ref-name identity and expected object State must be protected by the same
  prepared transaction; a mutable discovery name is not exact State or authority.
- Guarded ref publication alone does not preserve a checkout whose `HEAD` names
  that ref. Inspect structured worktree ownership and refuse checked-out branches;
  this coordinated preflight still cannot eliminate a concurrent external checkout
  race without a broader locking design.
- Meaningful content, not an Execution ID or wall-clock checkpoint cadence,
  determines whether memory advances.
- A useful State publication and its terminal Execution evidence are separate
  facts. A later reporting failure must retain an exact artifact reference to
  already saved useful progress whenever a truthful failed record can be built.
- Exact resume means deterministic retrieval from a pinned commit after process
  memory is gone; it does not restore hidden model state.
- Blueprint reuse and Instance memory are separate lineages. A routine Instance
  checkpoint records its adopted Blueprint but does not rewrite that Blueprint or
  invoke its maintainer.

## Deliberate limits

V0 does not implement autonomous checkpoint triggers, content selection or
summarization, relevance inference, reconciliation, remote memory synchronization,
messaging, scheduling, model invocation, credential handling, UI, copying,
migration, Blueprint maintenance, a general graph engine, or retention policy.
The precise long-term record schema, ref encoding, Blueprint-adoption transition,
remote publication policy, and interrupted-attempt reconciliation remain open.
The worktree pre-publication inspection assumes coordinated repository use and does
not prevent another process from selecting the branch after inspection but before
the guarded ref transaction. V0 does not detach, repair, or manage user worktrees.
