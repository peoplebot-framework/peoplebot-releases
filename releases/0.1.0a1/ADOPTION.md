# Adoption in a separate sovereign environment

These instructions were prepared before publication. The release was subsequently
published and its unauthenticated asset downloads were verified.

Version `0.1.0a1` is superseded by `0.1.0a2` for new installations. The historical
package remains public; use the corrected release when available.

## Boundary

Create the consuming environment on its own machine with its own Codex
installation, GitHub identity, repositories, credentials, Instances, memory, and
project history. Do not copy PeopleBot development credentials, private Git
history, runtime memory, Execution evidence, or workspace control configuration.

PeopleBot is a pinned dependency. Installing it does not create an Architect,
grant authority, provision repositories, or create any consuming-project resource.

## Minimal verified installation

1. Download the wheel, source distribution, public provenance, and `SHA256SUMS`
   from the proposed public `v0.1.0a1` prerelease.
2. Verify all hashes locally before installation.
3. Retain the source distribution beside the installed wheel so the exact matching
   source remains available.
4. Create a fresh Python 3.11-or-newer virtual environment.
5. Install the exact wheel with `python -m pip install --no-deps <wheel>`.
6. Record the release tag, wheel hash, source-distribution hash, public provenance
   hash, Python version, Git version, and environment identity in the consuming
   environment's own State.
7. Run only an explicitly authorized utility or synthetic demonstration. Do not
   treat `alpha-setup`, `alpha-adopt`, or `alpha-resume` as real package upgrade or
   project-task workflows.

## Present capability

The package exposes experimental State, context, admission, provenance, memory,
synchronization/recovery, read-only Adapter, and synthetic-alpha utilities. It can
support supervised deterministic experiments when their explicit inputs and
limits are respected.

## Remaining gates for the first real project task

The first supervised task still requires:

1. separately authorized provisioning of the consuming sovereign environment;
2. real GitHub runtime-memory transport using only that environment's externally
   managed credentials;
3. one narrow project-task Adapter with explicit State/context and a bounded result
   schema; and
4. end-to-end evidence that the task ran under single-Instance admission, produced
   truthful terminal provenance, saved only authorized memory, and preserved
   project/environment boundaries.

The next bounded implementation milestone is item 3: implement one
purpose-specific project-task Adapter by reusing the accepted read-only Adapter's
process-ownership/deadline pattern and the existing context, admission, provenance,
and memory APIs. It should make at most one supervised model invocation, validate
one narrow structured response, and avoid general Architect, messaging, scheduling,
or migration scope.
