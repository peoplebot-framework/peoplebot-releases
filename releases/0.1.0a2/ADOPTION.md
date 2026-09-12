# Adoption in a consuming sovereign environment

## Boundary

The consuming environment owns and supplies its project name, environment identity,
Git-host identity and repositories, credentials, runtime configuration, Instances,
memory, communications, project history, paths, and task context. Installing
PeopleBot does not create, select, transfer, or authorize any of these resources.

PeopleBot is an explicitly pinned framework dependency. A copied Blueprint does not
copy an Instance, credentials, private context, memory, or standing authority. The
destination environment creates and owns a new Instance.

## Minimal verified installation

1. Download the wheel, matching source distribution, public provenance, and
   `SHA256SUMS` from the `v0.1.0a2` prerelease.
2. Verify all three entries in `SHA256SUMS` before installation.
3. Retain the matching source distribution beside the wheel.
4. Create a fresh Python 3.11-or-newer virtual environment.
5. Install the exact wheel with dependency resolution disabled:
   `python -m pip install --no-deps ./peoplebot-0.1.0a2-py3-none-any.whl`.
6. Record the release tag, artifact digests, public provenance digest, Python and
   Git versions, and the caller-supplied environment identity in that environment's
   own State.
7. Run only an explicitly authorized utility or synthetic demonstration.

Git-backed operations require Git 2.45 or newer. The v0 single-Execution admission
implementation is Windows-specific. The package declares no runtime dependencies.

## Pinned use and updates

The consuming environment adopts an exact reviewed artifact and source identity.
Mutable labels are discovery aids, not exact State. Active Executions retain their
pinned version until completion. Compatible updates may be adopted at an Execution
boundary; incompatible updates require an explicit migration procedure. Failed
adoption preserves the prior usable State.

Framework adoption is separate from Instance-memory synchronization. Only the
owning environment executes an Instance and advances its canonical memory State.
Every launch path must share the same atomic one-active-Execution-per-Instance
admission mechanism.

## Present limits

`alpha-setup`, `alpha-adopt`, and `alpha-resume` are trusted synthetic local
demonstrations, not project setup or general package-update workflows. A real
project task still requires separately authorized environment provisioning,
externally managed credentials, an explicitly bounded project-task Adapter, and
truthful terminal provenance under the owning environment's authority.
