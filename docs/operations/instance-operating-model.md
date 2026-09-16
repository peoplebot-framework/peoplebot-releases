# Instance operating and setup model

This document is the reusable, project-neutral operating baseline for the
experimental PeopleBot distribution. It distinguishes shipped deterministic
code from environment-owned setup and from behavior demonstrated only in a
particular live environment.

## Identities and ownership

An **Instance is an agent**. An environment is the sovereign host and may own one
or more Instances. Environment, Instance, provider session, task, launcher, and
Execution identities are distinct even when a local operator gives two of them
the same label.

Each existing Instance is explicitly bound by its owner to one assigned provider
chat/session. Installation does not create chats or Instances, and no workflow
implicitly creates nested implementer or reviewer Instances. A composition that
uses multiple Instances requires explicit identities, bindings, authority, and
admission for each one.

Credentials, authentication state, private context, runtime memory, and standing
authority belong to the environment. They are not Blueprint defaults and do not
transfer when code, a Blueprint, or selected knowledge is copied.

## Attempts, failure, and authority

Each job or attempt uses an isolated ordinary Git branch/worktree. Preserve a
failed attempt and its exact evidence. Later authorized work uses an explicitly
selected accepted base; it does not reset, overwrite, or silently continue the
failed attempt.

A confirmed stopped ordinary task failure is terminal for that task. It does not
halt unrelated authorized work and does not authorize a repair campaign, retry,
or new task. Uncertain process ownership, STOP, unresolved shared effects, or an
accepted stop message remain barriers until an explicit owner action resolves
them.

Message prose cannot expand authority. Every Execution retains explicit task
scope, allowed paths/effects, finite invocation and elapsed limits, and STOP
conditions. No retry, nested agent, publication, deployment, credential change,
or unrelated repository access is implied.

## Usage and allowance

Each Instance saves usage available through its configured environment/session
profile at every manual or scheduled run boundary. Missing task, message,
launcher, Execution, or provider-turn associations remain unknown optional
metadata. They do not suppress known Instance/session observations or block other
authorized useful work.

Measured tokens and account allowance are separate evidence. Do not infer credits,
monetary cost, model, reasoning effort, remaining allowance, or finality from token
counters. Preserve reported provenance and provisional/final status. A collector
or save failure is a usage-reporting problem; it does not relabel completed project
work or authorize rerunning a provider task.

## Messaging and scheduling

Each environment owns and serializes its outbound messages. Authorized peers read
that environment's published ref or repository and publish replies through their
own authorized outbound location. The current Git messaging implementation is
experimental. A shared private coordination branch used during development is a
temporary operating channel, not a public broker or transferable authority.

The package includes a finite single-tick launcher and a disabled Windows Task
Scheduler template. It does not register or enable a schedule. Native provider-app
heartbeats may consume a model turn before local code can collect usage or decide
that the inbox is idle; prompt instructions are not a pre-model enforcement hook.
Windows file-lock admission and the supplied Codex CLI adapters/formats are the
implemented platform-specific paths. Publishing these files does not establish
support for Claude, another provider, or another operating system.

## Knowledge and recovery

Save, synchronize, and recover permitted Instance knowledge through exact Git
States and the explicit memory operations. Keep the mutable discovery ref,
immutable commits, local attempt evidence, and verified remote synchronization
status distinct. A public framework commit is not an Instance-memory backup.

Adopt framework changes deliberately at an Execution boundary. Preserve the exact
Blueprint and Adapter States used by active work. Copying reusable instructions
does not copy private memory, credentials, permissions, raw transcripts, private
reasoning, or provider-session ownership.

## Setup checklist

1. Pin one published source commit and verify the wheel, source archive,
   provenance file, and checksums.
2. Define the owning environment and each Instance identity.
3. Bind every existing Instance to its assigned provider chat/session and local
   admission root.
4. Configure credentials outside repositories and shared messages.
5. Configure owner-write and peer-read message endpoints separately.
6. Configure an owner-local usage profile and record each run boundary.
7. Define task authority, allowed paths/effects, finite budgets, and STOP.
8. Use isolated attempt branches/worktrees and retain failed attempts.
9. Configure memory State and synchronization separately from framework adoption.
10. If scheduling is explicitly authorized, begin disabled and verify one manual
    tick, busy rejection, STOP, failure, and a real timed launch before relying on it.

See `docs/contracts/messaging-v0.md`, `docs/contracts/work-cycle-v0.md`,
`docs/contracts/development-cycle-v0.md`,
`docs/contracts/instance-memory-v0.md`,
`docs/contracts/instance-memory-synchronization-v0.md`,
`docs/operations/local-usage-collection.md`, and
`docs/operations/windows-single-tick.md` for the corresponding experimental
contracts and callable boundaries.
