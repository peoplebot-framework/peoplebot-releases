# Experimental Alpha Bootstrap and Compatible Adoption Contract v0

Status: synthetic implementation experiment pending read-only review, not a
published release, general updater, or accepted public contract.

This slice composes existing exact State, Windows Instance admission, Execution
provenance, Instance memory, synchronization, and recovery. It adds no PeopleBot
primitive, registry, daemon, scheduler, package manager, migration system, model
call, or external account operation.

## Deterministic environment setup

`setup_alpha_environment` accepts an existing local framework checkout, one exact
repository-level framework State, exact Blueprint, compatibility-anchor, and
fixture-source paths, and an existing empty or otherwise isolated environment Git
repository. It resolves every supplied object locally with replacement refs,
ambient Git routing, and lazy fetching disabled.

Setup and adoption compare the resolved Git common directories and refuse when the
framework and environment share an object database, including linked worktrees of
the same repository. This is a concrete local separation check, not external
repository-identity authentication.

The setup records `peoplebot.alpha-selection.v0` at the deterministic discovery
ref returned by `alpha_selection_ref`. Its selection commit has no parent. The
environment's initial memory checkpoint uses that root commit as its authorized
baseline, so neither environment history nor memory can inherit PeopleBot
development ancestry accidentally. Repeating setup with identical exact inputs
and timestamp in another object-format-compatible repository produces the same
selection bytes, tree, and commit.

An operational installation also prepares one owner-local usage collection
configuration as described in `docs/operations/local-usage-collection.md`. Its
default is a changeable 10% remaining allowance threshold with conservative
missing/stale admission. Environment-specific source paths and session IDs are
configuration, not deterministic framework selection content, and the local
ledger is synchronized only at existing authorized Git boundaries.

Setup also binds that collector through the environment's existing Instance
profile, including the exact session, source kind/locator, owner-local run-record
path, deterministic operation, and authorized batched synchronization destination.
The adopted Instance workflow invokes it at start, completion, and the next normal
recovery for late data; unsupported sources remain explicitly unsupported.

The selection records exact framework, Blueprint, compatibility-anchor, and source
States plus their observed Git object IDs; it also records the source byte count
and SHA-256 digest. A branch or fixture label is not exact State. The stable ref is
only a discovery address and is guarded against absence or the exact prior commit
by the existing prepared native Git reference transaction.

## Narrow fixture compatibility rule

This experiment accepts B only when all of the following are established from
local Git objects before selection publication:

- B is a distinct descendant of A;
- the external framework repository identity is unchanged;
- the Blueprint, compatibility-anchor, and source paths are unchanged;
- the exact Blueprint and compatibility-anchor blob object IDs are unchanged;
- all three selected entries are regular non-executable blobs;
- the B source is locally available, valid UTF-8 Python, syntactically valid, and
  no larger than 65,536 bytes; and
- loading B with empty builtins and a fixed read-only `progress.md` probe returns
  exactly a non-empty string fixture identity and the unchanged probe text within
  a five-second child-process bound.

The exact adopted Blueprint State remains A even though B contains the same blob.
Adoption therefore does not silently rewrite a Blueprint or memory binding. The
anchor's exact unchanged content is an objective fixture constraint, not a version
string or self-declared compatibility flag. This rule proves compatibility only
for the deliberately tiny synthetic fixture; it makes no claim about arbitrary
PeopleBot revisions or hostile code.

## Admission, publication, and failure

`run_alpha_framework_adoption` runs through the existing single-Instance admission
and Execution-provenance lifecycle. A busy Instance is rejected before candidate
inspection or publication. The Execution pins A's exact fixture source as its thin
Adapter and the candidate framework, Blueprint copy, interface anchor, and source
as exact input States. An admitted execution commits that start evidence, validates
B, writes a child selection commit, and advances only the exact selection ref from
A to B with the prepared expected-State transaction. Existing Executions retain
their already pinned inputs.

Missing, non-descendant, changed-Blueprint, changed-anchor, changed-path, invalid-
mode, oversized, or invalid-source candidates fail before publication. Ref movement,
symbolic redirection, or indeterminate ref identity is preserved and refused.
These paths do not alter A, Instance identity, Blueprint, or memory. As with memory
publication, selection publication and later terminal-evidence publication are
separate facts; a later evidence failure must not be described as a failed
pre-publication adoption.

If successful-record construction fails after the selection ref advances, the
returned adoption still identifies the exact published selection State. A
constructible failed record uses `alpha.record_failed_after_adoption`, keeps
`resulting_state` null, and records that State as its single recoverable partial-
State artifact. It neither rolls back nor repeats selection publication. If failed-
record construction also fails, the returned adoption remains separate from the
classified record failure and exact incomplete admitted-start evidence; no terminal
evidence is fabricated.

## Fresh-process resume

`alpha-resume` reads the exact selection and memory commits, re-resolves all
recorded objects, assembles only explicit memory paths through the existing bounded
memory context path, and loads the exact selected fixture source blob. The fixture
executes with an empty read-only builtins mapping and receives only a read-only
mapping of selected memory text. Its result must be a stable JSON object.

Running the command in a new Python process prevents an earlier imported fixture
module from masquerading as B. The deterministic observation reports the exact
framework, Blueprint, selection, source, and memory States; source Git object and
SHA-256 identities; requested paths; memory digest; and fixture result. This is
exact source-object loading for a trusted synthetic fixture, not hostile-process,
bytecode, sandbox, or code-attestation proof.

The v0 fixture accepts exactly one requested memory path, `progress.md`, and its
loaded `resume` callable must reproduce that text in the fixed result shape. This
purpose-specific behavioral probe is part of the narrow rule, not a general
framework compatibility test.

Fixture execution uses the existing bounded direct-process owner documented in the
read-only Adapter contract. Ownership begins immediately after successful spawn and
includes the child, all started non-daemon stdin/stdout/stderr workers, and their
streams. One monotonic five-second deadline covers prompt delivery, bounded output
collection, and process execution; accepted stdout is limited to 65,536 bytes and
collection also has the existing absolute stdout/stderr caps. Normal completion,
timeout, output overflow, pipe/setup failure, `KeyboardInterrupt`, and `SystemExit`
all enter the same bounded cleanup guard. The fixture child must be confirmed stopped
and all started workers joined before ordinary admission release.

If terminate/kill/wait and worker joins cannot establish shutdown, the existing
`ProcessOwnershipUnresolved` path retains the exact process owner. During admitted
adoption it also retains the exact admission handle until explicit recovery confirms
shutdown and the owner explicitly releases admission. The original interruption is
preserved separately from cleanup failures and is not normalized to
`alpha.source_invalid`. These rules reuse **Bounds, deadline, and process lifetime**
and **Reusable Adapter lessons** in `read-only-adapter-v0.md`, plus **Ordering and
lifecycle** and **Reusable provenance lessons** in `execution-provenance-v0.md`.

The proof does not establish operating-system sandboxing, detached-descendant
control, or safety for untrusted framework source; only the repository-owned
synthetic fixture is in scope.

## Callable commands

The public functions are `setup_alpha_environment`,
`run_alpha_framework_adoption`, and `resume_alpha_instance`. The same paths are
available as commands; placeholders must be replaced with full exact commits and
explicit paths:

```text
python -m peoplebot alpha-setup --environment-checkout <environment-repo> --environment-repository <environment-id> --environment-id <owner-id> --instance-id <instance-id> --framework-checkout <framework-repo> --framework-repository <framework-id> --framework-commit <A-commit> --framework-blueprint-path blueprint.json --framework-compatibility-path alpha-interface.json --framework-source-path alpha_runtime.py --created-at 2026-09-10T12:01:00Z
```

```text
python -m peoplebot alpha-adopt --environment-checkout <environment-repo> --environment-repository <environment-id> --environment-id <owner-id> --instance-id <instance-id> --candidate-checkout <framework-repo> --candidate-repository <framework-id> --candidate-commit <B-commit> --candidate-blueprint-path blueprint.json --candidate-compatibility-path alpha-interface.json --candidate-source-path alpha_runtime.py --current-selection-commit <A-selection-commit> --runtime-root <absolute-runtime-root> --execution-id <unique-id> --started-at 2026-09-10T12:02:00Z --finished-at 2026-09-10T12:02:01Z
```

```text
python -m peoplebot alpha-resume --environment-checkout <environment-repo> --environment-repository <environment-id> --environment-id <owner-id> --instance-id <instance-id> --framework-checkout <framework-repo> --selection-commit <B-selection-commit> --memory-checkout <environment-repo> --memory-commit <exact-memory-commit> --memory-path progress.md
```

Memory checkpoint, synchronization, and recovery remain their existing callable
APIs rather than being duplicated in this command surface. The integration test
executes the complete setup/save/synchronize/recover/adopt/fresh-process-resume/
descendant-save sequence against isolated working and bare repositories.

## Deliberate limits

A local bare repository proves only synthetic Git transport. This slice does not
publish or adopt a reviewed PeopleBot release, authenticate a remote Git host,
provision a consuming sovereign environment, invoke a model, run a project-task
Adapter, enforce operating-system sandboxing for fixture code, migrate incompatible
State, or implement messaging, schedules, automatic learning, Blueprint
maintenance, release discovery, package installation, or general compatibility
inference.
