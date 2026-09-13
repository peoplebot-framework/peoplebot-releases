# Experimental Instance Admission Contract v0

Status: implementation experiment, not a settled public schema.

This contract proves environment-local admission for at most one active Execution of an Instance. It adds no PeopleBot primitive, invokes no model, writes no live ownership claim to Git, and does not turn a human-readable identity or filesystem path into authority.

## Inputs and local resource identity

`try_acquire_execution` accepts:

- an explicitly configured absolute environment-local runtime root;
- an opaque owning-environment identity;
- an opaque Instance identity; and
- an opaque attempted-Execution identity.

V0 accepts non-empty UTF-8 identities without surrounding whitespace or control characters, each bounded to 256 characters. A domain-separated SHA-256 digest of the environment identity selects an environment directory beneath the runtime root. A separately domain-separated digest of the environment and Instance identities selects a stable lock file. Raw identities and the Execution identity are not written into the lock resource.

Relative runtime paths are rejected so an incidental current directory cannot become operational state. The digest-derived path only selects the shared operating-system resource. It does not authenticate the caller, grant authority, prove ownership, or replace environment access controls. The caller is responsible for supplying a runtime root whose filesystem permissions are controlled by the owning environment.

## Atomic acquisition and immediate rejection

V0 is a Windows implementation using the standard-library `msvcrt.locking` interface. Each contender opens the same stable file and directly attempts one nonblocking one-byte operating-system lock with `LK_NBLCK`. Windows permits locking beyond the current end of a file, so a newly created resource remains empty and requires no initialization write before ownership. Existing nonempty lock files from earlier runs remain usable without migration or destructive cleanup. The operating system arbitrates the overlapping byte range atomically across processes. Windows also rejects a second overlapping lock through another descriptor in the same process; V0 tests that behavior directly through its public API.

The operation never checks a status file before locking. A successful lock returns `admission.acquired` and an opaque `ExecutionAdmission` handle. Lock contention closes only the contender's descriptor and returns `instance.already_running` immediately. The rejection path performs no task callback, model invocation, waiting, polling, queueing, or automatic retry.

Different Instance identities select different lock resources and can be admitted independently. The same Instance identity in different owning environments also selects different resources. All launch paths for an Instance must call this one admission operation; V0 does not provide a global coordinator or cross-environment running-status service.

## Supported execution lifetime and release

The admitted handle retains the exact open file descriptor that owns the operating-system lock. Low-level callers must retain that handle and release it only after their supported task code has stopped. `run_with_admission` is the bounded admission-only lifecycle wrapper: it calls task code only after acquisition and holds that descriptor until the synchronous callback returns or raises. It releases in `finally`, so ordinary completion and task exceptions both relinquish admission. The callback's return value or exception is not by itself an Execution terminal record. The separate `execution-provenance-v0` experiment adds ordered local evidence while retaining this exact live-lock authority boundary.

`ExecutionAdmission.release` unlocks and closes only its own retained descriptor. Its first successful call returns `admission.released`. A repeated or stale call returns `admission.not_owner` and cannot affect a later owner's different descriptor. An unlock or close failure is reported as `admission.release_failed`; V0 does not silently claim release.

An `ExecutionAdmission` is intentionally neither shallow-copyable, deep-copyable, nor serializable. Copying the integer descriptor value would not copy live OS ownership or the handle's authoritative release state, and Windows may reuse that descriptor number after release. Callers may copy ordinary environment, Instance, and Execution identifiers, but those identifiers cannot reconstruct a live admission handle.

Routine release never deletes or replaces the lock file or its parent directories. Repeated contenders therefore continue to address the same filesystem resource rather than racing through unlink-and-recreate behavior. No API in this slice force-unlocks, deletes, or cleans the runtime root.

The supported task lifetime is synchronous code executing in the lock-owning process. V0 does not transfer a handle to a child worker, release from a durable Execution record, or infer that a task stopped from a timestamp, PID, branch name, status string, or prose.

## Interruption boundary

Windows releases a byte-range lock when the owning process terminates and the operating system closes its file descriptors. A later successful nonblocking acquisition of the same byte range therefore proves that the previous lock-owning process no longer holds admission for the supported in-process callback lifetime. The process-termination regression starts only a test-owned child, confirms its acquisition, terminates that exact child, waits for operating-system termination, and then proves reacquisition.

Admission becoming available again does not prove that the previous task completed successfully. It does not create a completed, failed, blocked, or interrupted Execution record. It also does not prove that retrying external effects is safe. Child processes, background threads, detached workers, remote services, and effects committed before termination can outlive the supported callback or remain uncertain. Those cases retain recoverable State for explicit reconciliation; V0 supplies no timer-based expiry, PID takeover, force-unlock, automatic retry, or broad recovery subsystem.

## Reusable admission lessons

- Every launch path for one protected identity must contend on the same operating-system-owned atomic resource. A parallel status record is not a substitute.
- Rejection happens before protected task code and returns immediately. Waiting, polling, queueing, retrying, or asking a model changes the contract rather than implementing admission.
- Ownership covers the task's actual supported lifetime. A timestamp, PID observation, branch, filename, human-readable name, or durable status record cannot release live work.
- A release operation acts through the exact retained owner handle. Remembering a path or identity is insufficient and must not let an earlier owner disturb a later claim.
- Copying an identifier or descriptor number does not duplicate live ownership. Ownership handles must either share one authoritative release state or prohibit copying; V0 prohibits shallow copy, deep copy, and serialization because descriptor numbers may be reused after release.
- Acquire ownership through the atomic primitive before performing initialization that can conflict with an owner. V0 needs no lock-file initialization and directly locks byte zero even when it is beyond end-of-file.
- Routine release keeps the shared resource stable. Deleting and recreating its pathname can split contenders across different underlying resources.
- Operating-system cleanup after owner termination can prove admission is available again for the supported lifetime. It does not prove successful completion, authorize a retry, or reconcile uncertain external effects.
- When the primitive cannot establish acquisition or release, report bounded uncertainty and preserve recoverable State instead of weakening exclusivity or force-unlocking.

## Outcomes and failures

The public outcomes are:

- `admission.acquired`: the returned handle currently owns the OS lock;
- `instance.already_running`: an overlapping nonblocking lock attempt encountered the active owner;
- `admission.released`: this handle unlocked and closed its own descriptor; and
- `admission.not_owner`: this handle no longer owns a releasable descriptor.

`AdmissionError` reports bounded operational failures:

- `admission.unsupported` when the Windows locking primitive is unavailable;
- `admission.runtime_unavailable` when the configured runtime root, environment directory, or stable resource cannot be prepared safely;
- `admission.lock_unavailable` when locking fails for a reason other than ordinary contention; and
- `admission.release_failed` when the owning handle cannot truthfully establish release.

No failure silently substitutes a weaker check or a read-then-write ownership protocol.

## Deliberate limits

This slice is tested on Windows only. No portability claim is made from source inspection. V0 does not:

- authenticate environment, Instance, or Execution identities;
- harden an improperly shared runtime root against an authorized local administrator or arbitrary external tampering;
- persist live locks, PIDs, or running flags in Git;
- by itself create durable Execution provenance or infer terminal outcomes (the separate provenance wrapper requires explicit exact inputs and an existing validated Execution record);
- prove safe retry of external actions;
- manage production child workers or remote operations;
- invoke a model, Adapter, message transport, scheduler, or Blueprint maintainer;
- implement copying, migration, UI, learning, or external-effect reconciliation; or
- provide global or cross-environment admission state.
