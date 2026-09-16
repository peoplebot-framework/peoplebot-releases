# PeopleBot

PeopleBot V2 is a clean rebuild of a Git-native framework for durable, efficient agent work.

Its central rule is:

> Git handles what is known. AI handles what must be inferred.

The founding repository State was documentation-only. The current experimental foundation adds dependency-free deterministic State resolution, Execution provenance records, detached no-checkout worktree preparation, context manifests, useful UTF-8 context assembled from pinned Git objects, Windows environment-local single-Execution admission, an ordered local Git evidence path connecting admission to terminal Execution records, bounded local Instance-memory checkpoints with exact resume and explicit synchronization/recovery, a synthetic deterministic setup/compatible-adoption proof, bounded owner-publishes/peer-reads Git messaging, one finite locally policy-bound work-cycle tick, and one bounded production editing/review-cycle experiment. It does not contain a general model runtime, adopted public schema, broker, generic autonomous workflow engine, or website.

## Core primitives

PeopleBot has exactly five core primitives:

1. **Blueprint** — durable, reusable agent behavior and configuration.
2. **Instance** — a concrete actor hosted by a sovereign environment.
3. **State** — an exact repository snapshot identified by repository identity and a full Git commit, optionally narrowed by path.
4. **Message** — a small, actionable, normally append-only object that references exact State and artifacts.
5. **Execution** — one bounded invocation or work attempt from pinned State to resulting State or a recorded terminal outcome.

Roles are metadata or Blueprint/Instance descriptions. Runtime Adapters are required infrastructure, not another primitive. An environment is the sovereign hosting and ownership context and may host multiple Instances and Executions.

## Architectural direction

- Git provides durable State, history, lineage, provenance, isolation, synchronization, and recovery.
- Deterministic software traverses and validates known State; AI is reserved for inference.
- Executions receive only relevant context while complete permitted artifacts remain addressable.
- Learned procedures keep stable identities and continuous Git lineages; every Execution pins the exact procedure commit it used.
- Each sovereign environment owns its GitHub identity, repositories, and outbound messages repository. Authorized peers read; replies are published from the replying environment's repository.
- Runtime Adapters are thin, deterministic, versioned drivers.
- Work stays local where practical and synchronizes remotely at meaningful boundaries.
- Usage observations retain their source and confidence; unavailable measurements remain unknown.
- Credentials, private reasoning, private transcripts, and authority do not travel with shared knowledge.

The public contracts under [`docs/contracts`](docs/contracts) define the current
experimental guarantees and limits. Repository-only project state, working rules,
historical handoffs, and private planning are deliberately excluded from source
distributions.

## Experimental deterministic foundation

The current implementation targets Python 3.11 or newer and uses only the standard library plus Git 2.45 or newer.

The `0.1.0a4` release candidate uses ordinary Python wheel and source distribution
formats and adds no runtime dependencies. It consolidates the accepted public
foundation with bounded messaging, finite work-cycle and development-cycle code,
sanitized diagnostics, and local usage reporting. Installation, exact source
pinning, the public inclusion list, and deliberate limits are documented in
[the 0.1.0a4 notes](docs/releases/release-a-0.1.0a4.md).

The [Instance operating and setup model](docs/operations/instance-operating-model.md)
defines the reusable owner responsibilities around chats/sessions, attempt branches,
failure handling, usage, STOP, scheduling, messaging, memory, and credentials.

Run its tests:

```text
python -m unittest discover -s tests -v
```

Resolve an exact local State:

```text
python -m peoplebot resolve-state --repository <stable-repository-id> --checkout <local-checkout> --commit <full-40-hex-commit> [--path <relative-git-path>]
```

The experimental record contract and its deliberate limits are documented in [docs/contracts/state-execution-v0.md](docs/contracts/state-execution-v0.md).

Prepare an exact detached worktree without materializing its working files, then build a metadata-only context manifest:

```python
from peoplebot import (
    ContextPolicy,
    StateRef,
    build_context_manifest,
    cleanup_prepared_worktree,
    prepare_detached_worktree,
)

source = StateRef("example.test/owner/repo", "0" * 40)
policy = ContextPolicy(
    identity=StateRef("example.test/owner/repo", "0" * 40, "config/context-policy.json"),
    max_entries=256,
    max_blob_bytes=1_048_576,
    max_total_blob_bytes=4_194_304,
)
prepared = prepare_detached_worktree("/local/checkout", source, "/new/destination")
assert prepared.working_files_materialized is False
try:
    manifest = build_context_manifest(
        prepared.destination,
        source,
        ("README.md", "peoplebot"),
        policy,
    )
finally:
    cleanup_prepared_worktree(prepared)
```

Requests are explicit canonical Git paths. Directories expand recursively to tracked leaves; overlaps are deduplicated. Policy exclusions and size decisions are explicit in stable JSON. Symlinks and gitlinks are described but never followed, and missing local objects never trigger a fetch. The exact selection, failure, and guarded-cleanup behavior is documented in [docs/contracts/context-preparation-v0.md](docs/contracts/context-preparation-v0.md).

Assemble useful content directly from an exact local Git State without creating another worktree:

```python
from peoplebot import ContextPolicy, StateRef, assemble_context

repository = "https://github.com/peoplebot-framework/peoplebot"
commit = "<full-40-hex-commit>"
state = StateRef(repository, commit)
policy = ContextPolicy(
    identity=StateRef(
        repository,
        commit,
        "config/context-retrieval-policy-v0.json",
    ),
    max_entries=16,
    max_blob_bytes=131_072,
    max_total_blob_bytes=262_144,
)
assembly = assemble_context(
    ".",
    state,
    ("LICENSING.md", "docs/contracts/context-preparation-v0.md"),
    policy,
)
print(assembly.to_json_bytes().decode("utf-8"), end="")
```

Replace the placeholder with the exact commit to retrieve the licensing decision and cleanup procedure from that pinned State. Output documents retain complete supported UTF-8 source content plus exact repository, commit, path, mode, size, and blob provenance. Limits count original Git blob bytes, and exclusions remain in the embedded manifest. See [docs/contracts/context-assembly-v0.md](docs/contracts/context-assembly-v0.md).

Admit one synchronous task for an Instance through an explicitly configured absolute local runtime directory:

```python
from pathlib import Path

from peoplebot import run_with_admission


def perform_task() -> str:
    return "task result"


result = run_with_admission(
    Path(r"C:\PeopleBotRuntime"),
    "environment:example",
    "instance:example",
    "execution:example",
    perform_task,
)
if not result.task_started:
    assert result.rejection_code == "instance.already_running"
```

The v0 implementation directly locks byte zero of one stable Windows file per environment/Instance identity; a new resource remains empty and needs no pre-lock initialization. Contention is nonblocking and rejected callbacks never run. Live handles cannot be shallow-copied, deep-copied, or serialized. Admission is held only for the synchronous in-process callback; availability after interruption is not proof of successful completion or safe retry of external effects. Live locks and running flags are not Git State. See the [experimental admission lifecycle and reusable lessons](docs/contracts/instance-admission-v0.md#reusable-admission-lessons).

## Durable local admission and Execution evidence

`run_with_execution_provenance` wraps that same synchronous admission boundary. A
rejected attempt records its exact attempted inputs on an isolated local Git ref
without starting task code or advancing the active branch. An admitted attempt
must first commit start evidence, then runs its task, validates the returned
`ExecutionRecord` against the exact start inputs, and commits terminal evidence
while the live admission handle is still held. A start commit without terminal
evidence is deliberately incomplete.

`GitAttemptStore` writes blobs, trees, commits, and only the attempt-specific
`refs/peoplebot/attempts/v0/*` ref. It neither touches working files, the index, or
branches nor executes hooks or checkout filters. It does not fetch objects, retry
failures, or push. Returned observations, exact locally committed evidence States,
and remote synchronization are distinct. See the [experimental admission/Execution
provenance contract](docs/contracts/execution-provenance-v0.md).

## Local Instance-memory checkpoint and exact resume

`run_instance_memory_execution` checkpoints explicitly supplied bounded UTF-8
memory through the existing admission and provenance lifecycle. The deterministic
Instance discovery ref is guarded against its exact expected commit and symbolic
redirection. Changed content creates a descendant commit; equivalent content
returns the existing State while the new Execution still receives separate
provenance. The operation does not scan working files, change a Blueprint, invoke a
model, or decide what to remember.

`assemble_instance_memory_context` validates the repository, environment,
Instance, and adopted Blueprint binding in a pinned memory commit, then retrieves
only explicitly selected items through the existing local-only context assembly.
Earlier checkpoints remain readable by exact commit after the discovery ref moves.
Memory publication and terminal evidence are separate: a saved checkpoint remains
reported if terminal-evidence persistence later fails.

See the [experimental Instance-memory contract](docs/contracts/instance-memory-v0.md)
and [callable checkpoint/resume example](docs/examples/instance_memory_checkpoint.py).
The implementation always reports runtime memory as locally committed and not
remotely synchronized. Pushing this framework branch is not runtime-memory sync.

## Synthetic alpha setup and compatible adoption

The bounded alpha-bootstrap experiment records a pinned framework A in an
environment-owned root commit with no PeopleBot-development parent, uses that State
as the independent memory baseline, and adopts a distinct descendant B only under
the existing Instance admission/provenance boundary. Compatibility is the exact
unchanged Blueprint and interface-anchor blob identity plus fixed paths and Git
ancestry, not a version label. The Blueprint State, Instance identity, and memory
lineage remain unchanged across adoption.

`python -m peoplebot alpha-setup`, `alpha-adopt`, and `alpha-resume` expose the
callable paths. The fresh-process resume loads the exact selected fixture source
blob and reports its source State and digest. This proves only trusted synthetic
local mechanics; it is not a release, installer, general updater, sandbox, or real
remote-host/environment-isolation proof. Commands, guarantees, and limits are in the
[experimental alpha-bootstrap contract](docs/contracts/alpha-bootstrap-v0.md).

## Explicit Instance-memory synchronization and recovery

`run_memory_synchronization_execution` manually publishes one exact pinned local
memory commit to its one canonical Instance ref under the same admission and local
provenance lifecycle. The experimental request binds an authorized baseline,
repository/environment/Instance/Blueprint identity, configured remote, exact push
destination, expected remote State or absence, and bounded limits. Every
post-baseline commit is a direct non-merge descendant whose complete tree is
reconstructed from strict memory metadata; undeclared paths, modes, links,
gitlinks, object identities, sizes, or digests are refused before publication.

V0 rejects effective `url.*.insteadOf` and `url.*.pushInsteadOf` configuration,
multiple push URLs, credential-bearing URLs, mirror remotes, and receive-pack
overrides. Inspection, the explicit normal push, verification, reconciliation, and
recovery use the same validated push URL. No default/wildcard push, force, tag,
submodule, hook, attempt-evidence, or unrelated-ref publication occurs. A preflight
is not an atomic remote lock; concurrency can still make the transport outcome
uncertain.

`run_memory_recovery_execution` fetches objects for only the authorized memory ref
without a local fetch refspec, tags, configured tracking updates, or `FETCH_HEAD`.
It then uses prepared native Git transactions to establish the operation-owned
quarantine only with the exact owner marker, publish the canonical local ref only
with exact owner/quarantine/canonical identities, and remove owner and quarantine
together. It refuses dangling or resolved symbolic refs, checked-out destinations,
newer/conflicting State, ambiguity, and competing replacement. Working files,
index, registrations, unrelated refs, and the remote remain unchanged by recovery.
After an uncertain recovery transport result, continuation is never automatic: an
explicit resume request must reproduce the operation identity and exact owner
marker, and it accepts only an absent quarantine ref or the exact expected State.

Sanitized companion evidence distinguishes remote-verified, failed, and uncertain
transport/recovery observations without retaining URLs, credentials, diagnostics,
or raw output. Retained recovery refs are reported true, false, or unknown according
to what direct-ref inspection established. A late record failure keeps a successful
recovery as an exact partial-State artifact while `resulting_state` remains null; if
record construction remains impossible, the returned recovery fact remains separate
from incomplete start evidence. A timeout or disconnect never retries automatically. Git transport
uses the existing bounded direct-process and pipe-worker owner: normal completion
requires the Git child and inherited output pipes to close, and a pipe-holding
helper retains admission until explicit recovery. Detached helpers that shed those
pipes, abrupt owner-process death, and remote-side cancellation are outside the v0
lifetime proof. See the [experimental synchronization and recovery contract](docs/contracts/instance-memory-synchronization-v0.md).

## First read-only Adapter demonstration

`run_read_only_licensing_execution` connects the existing pinned context,
Windows admission, and provenance paths to one thin experimental Codex CLI
Adapter. It supplies only `LICENSING.md` from exact starting State, loads fixed
runtime configuration from exact Adapter State, commits admitted-start evidence,
performs at most one schema-constrained read-only invocation, and records a
truthful `no_change` or failed terminal Execution before releasing admission.

The model returns only exact structured facts; software validates those fields and
deterministically renders the displayed explanation. The Adapter binds immutable
effective configuration to pinned State and records the narrower fact that pinned
driver source matches source observed at import and the current source file; it
does not claim independent executing-code identity. Prompt delivery, bounded output
drains, and execution share one deadline. Ownership begins at child creation,
includes every started I/O worker and the operation-created workspace, and admission
is not released until direct activity is confirmed stopped. A cleanup failure can
leave an explicitly reported recoverable workspace remnant without masking the
primary failure; after the first removal failure, its handle treats the path as
manual-recovery information and will not delete later contents. Validated usage
from already captured output is preserved even when workspace removal fails.
Sanitized observations, including bounded event classification counts and hashed
shape metadata, are stored beside terminal evidence in Git. Raw provider JSONL,
unknown event values, prompts, reasoning, command/tool content, authorization data,
credentials, payloads, and transcripts are not retained. The guarantee
ends at the directly launched child and does not prove tool prevention,
detached-descendant termination, or remote cancellation.
See the [experimental read-only Adapter contract](docs/contracts/read-only-adapter-v0.md).

## Bounded project-review agent type

The project-review implementation provides a genuine project-neutral Blueprint at
`peoplebot/blueprints/project_review/blueprint.json` and its thin Adapter at
`peoplebot/adapters/project_review.py`, configured by
`peoplebot/adapters/project_review/adapter.json`. The public callable
`run_project_review_execution` loads and validates the exact Blueprint blob,
assembles only explicit project paths from one pinned commit under an exact
caller-supplied context policy, admits one Instance Execution, performs at most one
read-only Codex invocation, and records bounded cited findings, an explicit
`no_findings` result after an adequate supplied-scope review, or an explicit
`insufficient_evidence` result when the supplied material cannot support the
requested review.

Response validation is deterministic: duplicate keys, types, allowed values,
bounds, strict UTF-8 encodability, outcome consistency, and citation membership are
checked without trying to score the model's reasoning. Blueprint Boolean and
integer fields require their exact JSON scalar types. A conformant response,
including `no_findings`, is not a claim of factual correctness or completeness.
Memory saving remains a separate explicit caller operation.

The exact inputs, lifecycle reuse, public packaging layout, adoption dependency,
and deliberate limits are documented in the [project-review v0 contract](docs/contracts/project-review-v0.md).
Run the genuine Blueprint with a deterministic fake provider (no model call) from
a committed Git checkout:

```text
python docs/examples/project_review_fake.py --framework-checkout .
```

Private development State is not a public adoption reference. A public consumer
must pin the published distribution repository, commit, and paths matching the
installed artifacts.

## Git messages and one finite work-cycle tick

`peoplebot.messaging` publishes bounded immutable messages to an environment-owned
direct Git ref and reads explicitly configured peer refs. Owner destinations and
peer sources are separate types, replies require exact correlation, exact State
references resolve only through caller-supplied checkouts, and owner publication
holds one local publisher admission while pushing and verifying the exact appended
commit against the configured write endpoint. Outcomes remain verified, failed,
or uncertain without automatic retry.

`peoplebot.work_cycle` adds durable per-Instance reader progress and performs at
most one locally allowlisted task. It records a claim before dispatch, uses the
existing admission boundary while retaining logical tick ownership through all
post-handler work, optionally composes with the explicit Instance-memory API,
publishes a correlated reply, and records a compact execution-bound sanitized
status. Fresh-process recovery does not redispatch completed or interrupted
claims; claimed, unresolved, and accepted-stop records remain explicit durable
barriers. Terminal persistence failures preserve the original outcome and known
States without retrying effects or claiming failed writes succeeded. The Windows
launcher and inactive Task Scheduler template are preparation only; no task is
registered or enabled.

See [messaging v0](docs/contracts/messaging-v0.md), [finite work cycle v0](docs/contracts/work-cycle-v0.md), and the [Windows single-tick procedure](docs/operations/windows-single-tick.md). Run the no-provider, two-environment deterministic fixture with:

```text
python docs/examples/message_work_cycle_fake.py
```

## Bounded development-cycle experiment

`peoplebot.development` supplies a handler that the finite work cycle can use for
one explicitly approved coding task. A strict local authority file
and exact task State—not message prose—bind the repository, base commit, allowed
paths, verification commands, candidate ref, three distinct Instances, and shared
finite limits. The editing Adapter works in an isolated worktree; deterministic host
code rejects symbolic refs and HEAD movement, verifies the actual base diff, bounds
verification processes by remaining time, and commits only an unchanged verified
tree. The existing supplied-context project reviewer receives an exact requirements,
request, diff and host-verification packet for that candidate. Durable
pre-launch reservations prevent restart from refreshing or repeating model calls.

The supported command serially publishes and discovers only a selected structured
task, explicitly checkpoints Instance memory, publishes its sovereign reply, and
verifies a bounded append-only coordination reply. Permitted corrections receive
the preceding validated findings and exact review evidence. The shipped
package contains no active environment/session binding. Installing it does not
register a scheduler, invoke a provider, push a candidate, or change credentials.
See the [contract](docs/contracts/development-cycle-v0.md) and
[activation checklist](docs/operations/development-cycle-acceptance.md). Run the
complete production composition with fake process responses and a real synthetic
Git candidate using:

```text
python docs/examples/development_cycle_fake.py
```

## License and origin

PeopleBot is licensed under the GNU General Public License, version 3 only (`GPL-3.0-only`). Commercial use and forks are permitted under its terms. See the complete [LICENSE](LICENSE) and the central [licensing declaration, creator acknowledgment, scope boundaries, and future website requirement](LICENSING.md).

Canonical Git history, exact origin references, applicable legal notices, and contributor attribution must be preserved.
