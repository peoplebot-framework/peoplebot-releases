# PeopleBot release candidate — 0.1.0a3

Status: local candidate preparation pending read-only review and separate public
publication authorization. It does not activate an Instance or create a general
Architect or autonomous project maintainer.

## Candidate change

This candidate builds on the published project-neutral `0.1.0a2` framework and
adds the accepted bounded project-review Blueprint and its purpose-specific Codex
Adapter. It also includes the reviewed corrections for a truthful `no_findings`
outcome, strict response Unicode handling with provider-usage retention, and exact
Blueprint Boolean/integer type validation.

The accepted private implementation State and the eventual public distribution
State are distinct Git identities. Candidate evidence records both after the
public-history candidate commit exists. An archive digest does not substitute for
a Git `StateRef`.

## Included project-review boundary

The project-review operation accepts a caller-owned objective, pinned project
State, explicit context paths, exact context-policy State, owning environment and
Instance identities, runtime/authentication inputs, evidence store, and finish
time. It performs at most one configured read-only Codex invocation and returns
bounded cited findings, a truthful `no_findings` response, or an
`insufficient_evidence` response.

The Adapter configuration remains pinned to `codex-cli 0.153.4`, model
`gpt-5.6-luna`, and read-only sandbox mode. Tool-event rejection is detection, not
proof that every external effect is prevented. No live CLI/model acceptance check
is part of candidate preparation.

## Installation and adoption boundary

Use Python 3.11 or newer. Git-backed operations require Git 2.45 or newer, and the
v0 Instance-admission implementation is Windows-specific. Verify `SHA256SUMS`,
retain the matching source archive, create a fresh environment, and install the
exact wheel with dependency resolution disabled.

Installation does not create or activate an Instance. The consuming sovereign
environment separately owns and supplies its environment, Instance, project,
context policy, runtime, authentication, evidence, and memory bindings. Review,
memory saving, memory synchronization, and fresh-process recovery remain separate
explicit operations.

## Publication boundary

This candidate is local only. A later authorized publication must push the exact
reviewed public-history commit normally, create an annotated `v0.1.0a3` tag at
that distribution commit, create a draft prerelease titled `PeopleBot 0.1.0a3`,
upload only the reviewed wheel, source distribution, public provenance, and
`SHA256SUMS`, verify them without authentication, and only then publish the
prerelease. No such public operation is authorized by this document.
