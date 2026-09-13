# Sovereign-environment adoption v0

Status: experimental public contract for adopting a pinned PeopleBot release in a
consuming environment. It defines ownership and input boundaries; it does not
provision an environment or grant authority.

## Ownership boundary

A consuming environment is sovereign. It owns its project identity, Git-host
identity and repositories, credentials, runtime configuration, Instances, memory,
communications, project history, and task context. None of those resources belongs
to the PeopleBot development or distribution environment, and installing PeopleBot
does not create, select, transfer, or authorize them.

Copying a Blueprint or other authorized framework material into another sovereign
environment does not copy an Instance. The destination creates and owns a new
Instance with its own identity and State. Credentials, private context, memory, and
standing authority never transfer merely because names or framework versions
match.

## Caller-supplied setup

All consuming-project inputs are explicit and caller-supplied at setup or command
invocation. This includes:

- the consuming environment and project identifiers;
- repository identities, exact commits, refs, and local checkout paths;
- externally managed credentials and authentication context;
- Blueprint and Instance identities;
- runtime-root and other host paths; and
- task context and external-action authority.

PeopleBot must not carry a default consuming-project name, repository, host path,
credential location, task, or inherited operational control configuration. Public
examples use placeholders or clearly fictional identities such as
`example.test/consumer/project`.

## Pinned adoption

The consuming environment deliberately adopts one reviewed PeopleBot distribution
by exact version, artifact digest, and source identity. Mutable labels are discovery
aids, not exact State. Active Executions retain their pinned adopted version until
completion. A compatible update may be adopted at an Execution boundary; an
incompatible update requires an explicit migration procedure. Failed adoption
preserves the prior usable State.

Framework adoption is separate from Instance-memory synchronization. Only the
owning environment executes an Instance and advances its canonical memory State.

## Blueprint, Instance, and Execution boundary

A Blueprint remains reusable agent-type behavior. A consuming environment creates
a concrete Instance from reviewed Blueprint State and supplies that Instance's
authorized project context. One active Execution is admitted per Instance through
the environment-local atomic mechanism shared by every launch path. A concurrent
request exits immediately without model invocation, waiting, queueing, polling, or
automatic retry.

Names, branches, prose, copied files, and package installation do not establish
authority. External actions require explicit authority from the owning consuming
environment.

## First supervised task gates

Before a real consuming-project task, the owning environment must separately:

1. provision and verify its identity, repositories, runtime, and credentials;
2. adopt a reviewed PeopleBot release by exact identity;
3. create its Instance and memory configuration;
4. validate authorized remote memory transport, if required;
5. supply a bounded project-task Adapter with explicit context, permissions, and
   terminal provenance; and
6. define the task and external-action boundary.

The synthetic `alpha-setup`, `alpha-adopt`, and `alpha-resume` commands demonstrate
trusted local mechanics only. They do not perform these setup steps, discover a
release, create a project agent, or authorize a task.
