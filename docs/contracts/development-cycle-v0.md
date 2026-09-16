# Private development cycle v0

`peoplebot.development` connects one exact structured task to an admitted editing
Instance, an exact candidate commit, a distinct admitted project-review Instance,
and a bounded acceptance, finding, or unresolved result. The supported command is
`peoplebot development-cycle-tick`. It composes with `run_work_cycle_tick`; the
cycle coordinator, implementer, and reviewer must have three distinct Instance
identities, so the coordinator retains ownership throughout both child calls
without reentrant admission.

Authority is the strict local `peoplebot.development-authority.v0` file plus the
exact `peoplebot.development-task.v0` State. A selected message must contain both
the exact coordination-request State and task State and must match the bound
message, task, proposal, and repository identities. Message prose cannot change
paths, branch, commands, effects, Instances, timeouts, or allowances. The editing
Adapter creates a unique ordinary `codex/peoplebot-attempts/` branch and isolated
worktree at the explicit accepted base before Codex CLI `workspace-write`; this runtime
control is not described as a complete sandbox. Host code independently checks
the actual changed paths, runs fixed shell-free verification commands, creates the
candidate commit, and compare-and-swaps only the approved private candidate ref.
It rejects symbolic candidate refs, worktree HEAD movement and precommitted model
changes. The host records the exact staged tree before verification and refuses any
verification-time byte change. A final base-to-candidate path and tree check
precedes the direct-ref CAS. It never updates main or pushes. A successful attempt
advances both its retained attempt branch and the separately authorized candidate
ref to the verified candidate commit.

Workflow progress is appended to a deterministic local direct Git ref. An
invocation reservation is committed before each Adapter launch and is never
removed, including for failure or interruption. A fresh process encountering an
in-flight reservation stops unresolved rather than repeating an effect. The same
finite total invocation budget covers initial implementation, review, and any
configured corrections; elapsed time is measured from the first durable progress
record and remaining time is checked before every reservation, review and
correction. Each provider and verification process receives no more than the lower
of its configured limit and remaining authority. Verification reuses the bounded
direct-child output and lifetime owner. Each returned implementer or reviewer call
also appends a structured invocation report to development progress. It binds
environment/machine, task, Execution, role, actual start/finish and elapsed time,
runtime/model when known, child launch and exit, provider-response observation,
terminal outcome, exact evidence, and provider counters emitted by the existing
structured Codex stream. Missing counters remain explicitly unknown; cached input
is reported separately and is not added to input. V0
makes no currency-limit or exactly-once external-effect claim.

A completed nonzero editing child produces a bounded
`peoplebot.development-process-diagnostic.v0` record before the workflow returns.
It keeps process exit status separate from provider-processing events, a verified
final provider response, and provider-reported usage. The record may retain only
allowlisted failure classification, software-authored summary and recovery advice,
recognized failure-event types, bounded event counts, and captured-stream byte
counts and SHA-256 fingerprints. Raw stdout/stderr, event payloads, prompts,
reasoning, commands, tool data, credentials, thread identifiers, URLs, and unknown
values are not retained. A known authentication, configuration, model, network,
service-limit, or runtime cause is reported only when bounded structured-event or
stderr inspection identifies it; otherwise the cause remains `unknown`.

After a child is confirmed stopped, an ordinary failed implementation is terminal
for that task rather than global reader uncertainty. Its attempt branch and worktree
remain; allowed partial edits are checkpointed to that branch when feasible,
otherwise the unchanged base branch and intact worktree remain the recovery surface.
The failed State is never promoted as a candidate. A distinct authorized task can
start from its own explicit accepted base without changing or reconciling the failed
attempt. Process ownership uncertainty, STOP, candidate-destination conflict, and
uncertain shared publication retain their existing halt behavior.

Review uses the existing supplied-context read-only project reviewer. A bounded
review-packet State supplies the approved task, coordination request, host-derived
base/candidate diff, implementation observation, and the task's explicitly selected
exact candidate source files with a digest/State manifest. Its Execution pins the exact
candidate, packet and task/request States; a review
whose candidate does not equal current durable progress is stale and cannot
accept. A permitted correction receives the validated findings and exact review
evidence in its captured provider request. Findings stop when the correction
allowance is zero or exhausted.

`run_development_cycle_command` imports only one caller-selected exact structured
task through the environment-owned serialized publisher, discovers it through one
matching approved peer source, runs the work cycle with an explicit Instance-memory
checkpoint, and publishes the correlated sovereign reply. It then renders and
normally publishes a bounded append-only coordination Markdown reply to one exact
configured checkout, branch and remote. Matching completed imports and exports
reconcile; conflicting or uncertain content stops without automatic retry.
Workflow progress, memory, transport and coordination export remain distinct.
Known candidate/review facts and invocation status are returned when a later
progress save fails, while the durable reservation prevents redispatch. Remote
provisioning, authentication, scheduler activation, model invocation, main
integration, and deployment are outside this implementation.

An imported new request cannot adopt an earlier reader barrier or reply as its
own result. If the work-cycle status selects a message State other than the exact
new import, the command returns `bridge.cycle_message_mismatch`, performs no
reply rendering/export for that request, and reports the actual provider-invoked
fact from the halted tick.

`peoplebot development-cycle-reconcile` remains the explicit compatibility operation
for one historical evidence-proven stopped failure recorded as unresolved before the
ordinary failure correction. It is inactive by default and acquires the
same logical work-cycle and coordinator admission locks as a tick. It requires
the exact current reader-progress State and sole unresolved task, byte-identical
prior status, exact development-progress State, one consumed reservation, null
candidate/review evidence, and matching failed terminal/Adapter evidence. A
changed status/ref, another barrier, STOP control, live owner, or mismatched
evidence refuses the transition. The operation appends a typed reconciliation
record to the next reader-progress commit, retaining the complete prior task,
prior reader State, terminal classification, timestamp, and exact evidence
States, while changing only that task's current reader disposition from
`unresolved` to `failed`. The previous progress commit remains its parent.
Matching repetition after an uncertain local return is idempotent; it does not
repeat the failure or refresh task/provider budgets. Workspace cleanup is
independent and cannot establish progress reconciliation.

For a failed editing child, durable progress carries the same sanitized process
diagnostic used by terminal companion evidence. The sovereign reply and bounded
coordination Markdown report include the terminal code, implementer-process stage,
reservation count, provider-processing/verified-response observations, safe
diagnostic summary, and explicitly label the implementation evidence as local-only.
They do not publish the raw local attempt, memory, or progress histories.

If a verification process cannot establish shutdown, its existing
`ProcessOwnershipUnresolved` authority propagates and the exact isolated worktree is
retained. The worktree is removed only after provider and verification ownership is
resolved; normal successful cleanup remains unchanged.
