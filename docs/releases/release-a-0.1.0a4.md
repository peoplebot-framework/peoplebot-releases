# PeopleBot experimental distribution — 0.1.0a4

`0.1.0a4` consolidates reusable PeopleBot framework and Blueprint work completed
after `0.1.0a3`. It remains an experimental prerelease. It does not create or
activate an agent, provide a general autonomous runtime, provision another
provider, or transfer environment authority.

## Included changes

- corrected Codex event classification and bounded sanitized diagnostics;
- bounded owner-publishes/authorized-peers-read Git messaging;
- one finite work-cycle tick with durable claims, STOP/unresolved barriers,
  correlated replies, and preservation of failed terminal evidence;
- one bounded development implementer/reviewer composition with isolated attempt
  branches/worktrees, exact review packets, ordinary-failure preservation, and no
  automatic retry;
- deterministic local usage collection and per-run reporting which preserves known
  Instance/session observations when optional task or launcher links are absent;
- project-neutral setup and operating guidance for Instance/chat binding, usage,
  scheduling limits, messaging ownership, and Git-backed knowledge recovery.

## Explicit public inclusion list

The source archive includes the established foundation plus these new public
surfaces:

- `peoplebot/messaging.py`, `peoplebot/work_cycle.py`,
  `peoplebot/development.py`, and `peoplebot/usage.py`;
- the matching `peoplebot/__init__.py`, `peoplebot/__main__.py`, shared Adapter,
  project-review Adapter, and provenance corrections;
- `peoplebot/blueprints/development/blueprint.json` and
  `peoplebot/adapters/development/adapter.json`;
- the messaging, work-cycle, and development-cycle contracts;
- the neutral Instance operating model, usage/adoption procedures, Windows
  single-tick procedure, disabled scheduler template, and deterministic fake
  examples;
- affected tests, license/attribution files, packaging metadata, this release
  note, and the public distribution inspection procedure.

It excludes private repository guidance and project state, coordination inbox and
reply messages, machine/session bindings, runtime memory, local usage ledgers and
run records, private trial/handoff evidence, credentials, authentication state,
raw provider streams/transcripts, and private reasoning.

## Supported boundary

Python 3.11 or newer and Git 2.45 or newer are required. Windows-specific
single-Instance admission and the supplied Codex CLI adapters/usage record formats
are the implemented platform paths. The scheduler template is disabled and no
schedule is installed. Native provider-app idle checks may consume a model turn.

The development Blueprint and Adapter are experimental callable components, not a
claim of a functioning general Architect or autonomous project-task agent. The
first historical live development attempt failed and remains preserved; package
publication does not erase or recast it. No live model acceptance invocation is
required for this release because the public claims are limited to the tested
deterministic boundaries.

## Adoption

Verify all release assets and retain the matching source archive. Pin the public
distribution commit rather than a mutable branch. A consuming environment must
separately supply Instance/chat bindings, credentials, project authority,
message endpoints, admission roots, usage profile, memory/synchronization policy,
budgets, and STOP controls. See
`docs/operations/instance-operating-model.md` before enabling any schedule or
provider-backed operation.
