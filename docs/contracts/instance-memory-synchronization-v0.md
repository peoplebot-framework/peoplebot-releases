# Experimental Instance-Memory Synchronization Contract v0

Status: accepted implementation experiment integrated on `main`, not an accepted public contract.

This slice publishes or recovers one exact Instance-memory commit without invoking
a model. It adds no primitive, credential store, permission system, queue, or
general backup service. The owning environment supplies authority and externally
managed Git credentials; the implementation validates only the explicit technical
boundary described here.

## Explicit authority and binding

Every operation identifies the repository, environment, Instance, adopted Blueprint
State, canonical Instance-memory ref, configured remote name, expected resolved push
destination, expected remote State or explicit absence, and bounded limits. A sync
also pins the exact local memory State. The caller explicitly authorizes a pinned
baseline and all history reachable from it for this one destination.

The baseline is an authority boundary, not proof of ownership. The implementation
requires the selected memory commit to form a single-parent, non-merge chain above
that baseline. Every post-baseline commit must be valid Instance-memory v0 State
with the same repository/environment/Instance/Blueprint binding. An expected remote
memory State must lie on that same chain. Names, repository privacy, current tree
contents, or remote accessibility never create publication authority.

## Destination resolution and inspection

The configured remote's resolved push URLs are obtained from Git. Exactly one must
exist and equal the explicitly expected destination. Inspection and transport use
that exact resolved URL, never the remote's possibly different fetch URL. Embedded
URL credentials are unsupported and rejected; durable records bind the destination
by a SHA-256 digest rather than retaining credential-bearing text.

Unsupported ambiguity or transport overrides fail closed. Synchronization inspects
only the full canonical memory ref. It compares the observation with the explicitly
expected commit or absence before transport. This preflight is an observation, not
an atomic guarantee against another writer.

V0 conservatively rejects any effective `url.*.insteadOf` or
`url.*.pushInsteadOf` configuration before resolving a destination. Passing a
resolved URL to another Git command can apply rewriting again, so validation alone
would otherwise permit inspection and publication to diverge. System, global,
local, worktree, and other effective Git configuration visible to the operation are
included by the query. Authentication configuration remains externally managed and
is not copied, erased, displayed, or bypassed. Coordinated configuration must not
change during the bounded operation.

## Exact publication

The source ref must still be the canonical direct local ref at the pinned commit.
Transport uses the exact commit as source and one full source-to-destination
refspec. Hooks, follow-tags, configured/default/wildcard/mirror pushes, and automatic
submodule pushes are disabled. Publication is a normal fast-forward push: force,
force-with-lease, deletion, and history rewriting are unsupported.

For every commit after the authorized baseline, v0 requires the exact supported
metadata fields and canonical sorted unique item paths, regular-file object IDs,
declared byte sizes, and SHA-256 content digests. Reconstructing the expected Git
tree from those objects must produce the commit's root tree exactly. This rejects
undeclared files, symlinks, gitlinks, executable or other unsupported modes, extra
trees, and malformed or missing content in both the tip and transferred history.
The baseline and its ancestors remain explicitly authorized history and are not
retrospectively required to use the memory-only tree shape.

Only the selected `refs/heads/peoplebot/instances/v0/...` ref is published.
`refs/peoplebot/attempts/v0/...`, terminal evidence, tags, other refs, working files,
and index State remain local. This is memory synchronization, not complete backup.

## Outcomes and reconciliation

Returned and durable sanitized observations distinguish `local_only`,
`remote_verified`, `failed`, and `uncertain`. They retain exact commit/ref identities,
operation kind, destination digest, observed remote object when available, and a
classified code; they exclude credentials, raw command output, diagnostics, and
machine paths.

A timeout, disconnect, or unreadable transport result can occur after publication
and is uncertain. There is no automatic retry. A separate reconciliation inspects
the exact same destination/ref:

- the attempted commit establishes the current remote ref value and verifies it;
- the prior value does not prove publication never occurred and remains uncertain;
- an intervening value is a conflict requiring deliberate reconciliation; and
- unavailable inspection remains uncertain.

No Git ref observation establishes task completion or external-effect safety.
Subprocesses are bounded and admission is not released until owned activity is
confirmed stopped; unresolved ownership retains the admission and exact recovery
authority rather than merely raising a normal timeout.

Transport observation and Execution-record construction are separate facts. A
supported post-push inspection timeout produces an uncertain observation. If later
Execution-record construction fails, the already captured remote-verified or
uncertain observation is not replaced; a constructible failed Execution records the
record failure separately and commits the original sanitized companion.

The same separation applies to recovery. If canonical recovery succeeds but later
terminal-record construction fails, a constructible failed Execution keeps
`resulting_state` null and cites the exact recovered repository-level State as its
recoverable partial-State artifact. The successful recovery observation remains the
companion evidence and transport/publication is not repeated. If no terminal record
can be constructed, the returned recovery result and classified record failure
remain available without fabricating a timestamp. Recovery-ref retention is true,
false, or unknown according to direct inspection; an exception does not prove that
an owner or quarantine ref is absent.

## Fresh-checkout recovery

Recovery is invoked only after the original Execution is stopped and under the same
owning environment's Instance admission boundary. It retrieves objects for only the
authorized memory ref without supplying a local destination refspec, writing
`FETCH_HEAD`, fetching tags, applying configured tracking refspecs, or recursing
into submodules. Retrieval alone authorizes no canonical publication. After the
exact expected commit is available, recovery establishes a unique operation-owned
quarantine ref, verifies the authorized lineage and complete identity binding, then
guardedly creates or fast-forwards the canonical local memory ref. It never forces
a canonical ref, changes a checkout, stages files, prunes refs, or overwrites newer
local memory. Uncertain quarantine State remains recoverable.

An operation-specific owner ref is guardedly created before fetch and binds the
operation identity, destination digest/ref, and expected commit. Recovery uses the
existing prepared `update-ref --stdin` transaction protocol with `no-deref` and
symbolic inspection under the transaction locks. Initial ownership is claimed only
while both owner and quarantine identities are eligible. Quarantine is established
only while the owner marker remains exact and quarantine remains absent. Canonical
publication verifies owner, quarantine, and expected canonical State in one native
transaction. Cleanup verifies and removes quarantine and owner together, while also
verifying the canonical recovered State. A competing owner, dangling or resolved
symbolic ref, checked-out destination, replacement, ambiguity, validation failure,
or newer/conflicting local State is preserved and classified. Cleanup failure does
not undo an already verified canonical recovery and cannot remove only one recovery
ref within the transaction.

An uncertain recovery attempt retains its exact owner and any quarantine ref. It
is not retried automatically. Deliberate continuation must explicitly opt into
resuming the same operation identity, reproduce the exact deterministic owner
marker, and find either no quarantine ref or the exact expected recovery State.
Any other retained identity or value is preserved and refused.

A fresh checkout in the same sovereign environment is not a new environment.
Copying memory across sovereign environments creates a new Instance and is outside
this contract. Recovery performs no model call and does not start another live task.

## Deliberate limits

V0 supports one manually requested Instance-memory ref and synthetic deterministic
transport tests. It does not create accounts or repositories, infer permissions,
publish live runtime memory, synchronize Execution evidence, implement cross-
environment copying, migrate incompatible memory, retry, schedule, message, or
maintain Blueprints. Framework adoption is a separate operation.

The process-lifetime proof covers the directly launched Git process, its I/O
workers, and transport helpers that retain inherited output pipes: normal completion
requires those pipes to close, while a pipe-holding descendant keeps admission and
in-process recovery authority. V0 does not prove control of a detached helper that
sheds those pipes, survive abrupt owner-process death, or cancel remote-side work.
Such transports or behaviors are outside the supported lifetime claim and uncertain
effects require reconciliation.

These native transactions make each declared local transition atomic for the exact
refs and object IDs it verifies. They do not form a distributed lock, prevent direct
external Git mutation between transitions, authenticate a process that recreates an
identical object ID, or prove comprehensive safety against arbitrary non-PeopleBot
mutation. Ordinary supported recovery remains serialized by Instance admission;
outside interference is preserved or refused when it changes a guarded identity.
