# Experimental Read-Only Codex Adapter Contract v0

Status: one bounded implementation experiment, not a general Adapter standard.

The bounded direct-process/JSONL transport in this implementation is also reused
by the separate project-review v0 Adapter. That factoring does not change this
licensing Adapter's model, configuration, prompt format, response schema, rendered
answer, or public callable behavior.

This contract connects exact context assembly, Windows Instance admission, one
Codex CLI invocation, and the existing Git-backed Execution provenance lifecycle.
Adapter remains thin runtime infrastructure and is not a sixth PeopleBot primitive.

## Exact inputs and adopted configuration

The licensing demonstration receives an exact repository-level starting `StateRef`,
an `ExecutionStart`, the versioned Adapter State, a local evidence store, an
environment-local admission root, and an explicit finish-time callback. Its sole
context input is `LICENSING.md` at the starting commit. Context selection uses a
one-entry, 32 KiB blob/total policy and the existing deterministic context assembly.

The Adapter State selects the `peoplebot/adapters` tree. That tree contains the
driver and `codex_read_only/adapter.json`, which fixes the runtime/version, model,
sandbox, duration, prompt/transport/response/rendered-answer limits, and JSON
response schema. Construction loads an immutable deep configuration snapshot from
the exact Git object. At module import the driver records a digest of its source
file. Adapter adoption requires the pinned driver blob to match both that
source-at-import digest and a fresh digest of the current source file. Public
configuration/schema views cannot replace the effective values. This proves only
that those three source-byte observations agree. It does not independently prove
the identity of executing Python objects or bytecode and is not a defense against
hostile arbitrary mutation in the owning process.

The canonical stable configuration JSON digest, the digest of the original pinned
configuration content, and the driver State/source digest remain distinct. The
observation records the precise evidence label and records
`executing_code_identity_verified: false`; it does not rename source agreement into
an executing-implementation guarantee. Merely loading old configuration does not
imply that old driver code was executed. The `ExecutionStart` must pin both that
Adapter State and the exact selected `LICENSING.md` State before admission.

The invocation request embeds the complete context assembly plus SHA-256 digests of
its stable JSON bytes and the exact Adapter configuration bytes. This binds the
bytes sent to the runtime to reproducible Git inputs and adopted configuration; the
digests do not replace those exact States.

## One synchronous invocation

V0 uses installed `codex-cli 0.153.4` through `codex exec` with model
`gpt-5.6-luna`. The installed help was inspected before implementation. The direct
command uses the documented local flags `--sandbox read-only`, `--ephemeral`,
`--ignore-user-config`, `--ignore-rules`, `--skip-git-repo-check`,
`--output-schema`, `--json`, `--color never`, an empty temporary `--cd`, and stdin.
It does not enable search. API-key/access-token environment variables are removed
from the child environment; authentication comes from the explicitly supplied,
externally managed Codex home. Credential values and its machine path are not
recorded.

The child receives only a fixed allowlist of ordinary Windows runtime variables and
the explicitly supplied `CODEX_HOME`; arbitrary inherited variables are omitted.
The prompt directs the model to use only supplied context, call no tools, perform no
external action, and return one schema-constrained JSON object. The installed CLI
offers no stronger purpose-specific no-tools switch used by this experiment.
Therefore event inspection detects and rejects an observed tool event after the
fact; prompt wording and detection do not prove prevention of external effects.

The model response contains only exact structured facts: `GPL-3.0-only`, distribution
scope, required preservation/source booleans, advertising false, and the exact
citation. Free-form explanation fields, extra fields, duplicate keys, malformed
JSON, unsupported constants, and citation mismatch are rejected. PeopleBot renders
the displayed explanation deterministically from validated fields and labels it
software-rendered; it does not claim the model independently wrote or reasoned
through that prose. The wording limits obligations to distribution of covered
material, does not require publication of undistributed private work, preserves
existing credit/origin, and creates no advertising requirement.

There is no retry. A rejected admission, failed admitted-start write, input-limit
failure, runtime/version failure, timeout, output-limit failure, tool event,
malformed response, or nonzero process status never becomes a successful answer.

## Bounds, deadline, and process lifetime

The adopted limits are 32 KiB prompt bytes, 64 KiB accepted stdout, 16 KiB accepted
stderr, 4 KiB serialized structured response, 2,000 software-rendered answer
characters, schema-level individual string bounds, 128 parsed events, and 120
seconds. The direct driver additionally caps capture at 256 KiB stdout and 64 KiB
stderr. Concurrent non-daemon workers deliver stdin and drain both output streams;
the owner applies one monotonic deadline to prompt delivery, output collection, and
process execution. Output is bounded during collection rather than accumulated by
an unrestricted `communicate()` call.

`run_read_only_licensing_execution` assembles context before admission. Once
admitted, provenance commits admitted-start evidence before the Adapter is called.
Ownership begins immediately after a successful spawn, before worker construction
or startup. The exact process handle, every I/O worker that actually started, its
streams, and the exact invocation workspace are one owned lifetime. Partial worker
construction or startup, normal return, deadline, overflow, I/O failure,
`KeyboardInterrupt`, and `SystemExit` all enter the same cleanup guard. The direct
child must stop and all started workers must join before ordinary admission release.
Cleanup has a separate bounded allowance after the execution deadline. Process
creation itself may exceed the requested duration on operating systems where
creation is not interruptible. Streams are closed only after their workers stop;
workers are not abandoned as daemons.

If terminate/kill/wait and started-worker joins cannot establish shutdown, the
interruption propagates with exact in-process child, workspace, and admission
authority retained for explicit recovery. The workspace is not removed while an
unresolved child may still use it. Recovery first establishes child/worker shutdown,
then attempts removal of only that operation-created directory. If removal still
fails, that handle permanently refuses further programmatic removal. The exact path
and sanitized secondary failure remain recovery information for deliberate manual
inspection; the path does not authorize deletion of whatever may occupy it later.
Moved originals, replacement directories, and newly added work are therefore not
removed by a retained handle. Because the child and workers are stopped, admission
may then be released. A workspace remnant never rewrites an unresolved primary
failure or fabricates terminal evidence. Incomplete admitted-start evidence remains
truthful, and cleanup uncertainty is separate from the original failure. A callback
finishing or an exception being raised is never treated as proof that its child
stopped.

This establishes only the lifetime of the directly launched CLI process while the
owning Python process remains able to run cleanup. It does not survive abrupt owner
death, prove cancellation of an already submitted remote inference request, or
terminate a detached descendant not owned by that process. Timeout is therefore a
truthful failed Execution, never a safe-retry or completed claim. V0 is not a
general process supervisor.

## Structured observations and provenance

Adapter observations retain only validated structured facts and software-rendered
answer, runtime/version/model, exact context/configuration/blob/driver/response
identities, the exact driver-evidence scope, explicit non-verification of executing
code identity, direct-process and workspace-cleanup dispositions, exit code,
sanitized stage/reason, and available provider-reported token fields. Unsupported event types are rejected
deliberately. Failures distinguish missing completion, unsupported event, invalid
JSON, citation mismatch, field/size failure, restriction observation, process
failure, and invalid usage. Reliably parsed usage is retained even when answer
validation or subsequent workspace removal fails. Workspace-removal failure remains
an explicit failed Adapter observation and cannot turn a validated answer into a
successful Execution. Raw JSONL, thread identifier, diagnostics, credentials, and
provider transcript are discarded.

A validated answer returns a truthful `no_change` `ExecutionRecord` whose resulting
State equals starting State. A classified Adapter failure returns a truthful failed
record and terminal outcome. While admission remains held, the terminal commit
stores `execution.json` plus `adapter-observation.json`. The companion binds exact
context/configuration States, validated result or failure, digest semantics,
process disposition, and available usage. The terminal commit has the admitted-start
commit and same-repository Adapter commit as Git parents, so the evidence ref keeps
both source ancestry and adopted Adapter State reachable through Git's object graph;
a commit hash written only inside JSON is not treated as reachability.

If companion construction or persistence fails, the in-process task result remains
separate, terminal evidence is incomplete, and success is not reported as durable.
After discarding returned objects or changing working files/branches, the companion
is retrievable by its exact terminal commit and path. A rejected attempt invokes
neither version probe nor model. Successful local evidence remains
`remote_synchronized: false`; this slice does not push attempt refs.

## Reusable Adapter lessons

- Child lifetime differs from callback lifetime; release ownership only after the
  supported child and I/O workers are observed stopped.
- A deadline covers blocking input delivery and output collection, not only the
  final process wait. Cleanup needs a separate explicit bound.
- Exceptions describe control flow, not cleanup evidence. Unresolved ownership
  retains exact live authority rather than inferring it from a PID or record.
- Put resource creation inside its cleanup guard immediately. Partial initialization
  owns only resources that actually started, and every exit path must close or
  retain exact recovery authority for them.
- Cleanup failures are secondary evidence, not replacements for the primary
  interruption or ownership state. A stopped child permits admission release even
  when its exact operation-owned workspace remains for deliberate recovery.
- A retained cleanup path is recovery information, not continuing authority to
  delete its future contents. After one removal failure, the retained handle refuses
  further programmatic deletion and leaves inspection/removal to the owner.
- A secondary cleanup failure must not discard independently available, validated
  usage evidence. Process captured output before reporting the separate cleanup
  disposition, without accepting an invalid answer.
- Keyword presence does not establish meaning. Validate small structured facts and
  render deterministic prose in software.
- Effective inputs must match recorded State. Freeze nested configuration and state
  the driver guarantee narrowly: matching pinned, source-at-import, and current-file
  bytes do not independently verify executing Python object or bytecode identity.
- A returned observation is not durable until committed into reachable Git
  evidence. JSON references do not create Git reachability.

Implementation behavior was checked against the installed Python version and the
official Python `subprocess` documentation, including its process-creation timeout
limitation, plus Microsoft's anonymous-pipe blocking semantics:

- https://docs.python.org/3/library/subprocess.html
- https://learn.microsoft.com/en-us/windows/win32/ipc/anonymous-pipe-operations

## Deliberate limits

V0 is one Windows-hosted licensing demonstration. It does not authenticate the
caller or repository identity, prevent all tool behavior or enforce model internals,
prove the model consulted no latent knowledge, provide remote-request cancellation,
survive abrupt owner death, stop detached descendants, stream accepted results,
support arbitrary objectives or context, choose models dynamically, invoke paid API
billing, publish messages, copy agents, schedule work, maintain Blueprints, discover
procedures, or implement automatic learning. The fixed response validator is
specific to this licensing objective. It also does not provide independent
executing-code/bytecode attestation or automatic cleanup of a reported workspace
remnant. Retained workspace removal is deliberately manual after the first cleanup
failure; no claim is made that pre-removal checks could eliminate all path-replacement
races. General Adapter portability and policy remain unresolved.
