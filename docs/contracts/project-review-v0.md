# Experimental Project-review Blueprint and Adapter v0

Status: candidate implementation in a local public-distribution State. It is not
remotely resolvable until that exact reviewed distribution commit is published.

This contract defines one genuine reusable agent type and one thin Codex Adapter
for a bounded review of explicitly supplied pinned project text. It is not a
general Architect, bootstrapper, project maintainer, or autonomous editor. It adds
no PeopleBot primitive.

## Blueprint State and boundary

The versioned Blueprint artifact is
`peoplebot/blueprints/project_review/blueprint.json` with format
`peoplebot.project-review-blueprint.v0`. The loader requires that exact path,
reads its exact Git blob, rejects duplicate JSON keys, validates every required
field, exact JSON scalar type, and v0 value, and records its content digest.
Booleans are actual JSON Booleans, and integers are actual JSON integers rather
than Booleans or numerically equal floats. Possessing a syntactically valid
`StateRef` to an arbitrary blob does not make that blob a project-review Blueprint.

The Blueprint contains only stable agent-type information: purpose, required
input categories, bounded cited output, read-only supplied-context authority,
stopping/failure behavior, and explicit exclusions. It grants no authority to
modify the project, PeopleBot framework, or Blueprint; schedule work; publish;
send messages; retry itself; or save memory automatically. Environment, Instance,
project, repository, objective, decisions, history, credentials, paths, and memory
policy are caller-owned inputs outside the Blueprint.

The response is one through eight findings, `no_findings`, or
`insufficient_evidence`. `no_findings` requires empty findings and null
`insufficient_evidence`; it means no issue was identified within the supplied
scope after an adequate review, not that the project is correct or complete.
`insufficient_evidence` requires empty findings and one bounded explanation that
the supplied evidence cannot support the requested review. Each actual finding
contains a bounded severity, title, short explanation, suggested next action, and
one through four exact repository/commit/path citations.

## Exact invocation inputs

`run_project_review_execution` receives separate framework and project checkouts,
an `ExecutionStart`, an adopted `ProjectReviewAdapter`, explicit project paths, an
exact `ContextPolicy`, the existing evidence store/runtime root, and an explicit
finish-time callback. Before admission it:

1. loads and validates the exact Blueprint State from the framework checkout;
2. verifies the exact Adapter State selected by the Execution;
3. reads the context-policy blob from its exact path-specific project State and
   requires its strict content to equal the supplied policy;
4. assembles only requested UTF-8 project blobs from the pinned starting commit
   through the existing context-selection and byte-limit implementation; and
5. requires `ExecutionStart.input_states` to contain the policy State followed by
   every selected document State in deterministic order.

The caller supplies and controls the repository identities, exact commits, local
checkouts, owning environment and Instance identities, objective, context policy
and paths, runtime root, Codex executable/home, evidence destination, timestamps,
and any later memory operation. V0 records but does not authenticate opaque
repository or identity strings.

The prompt binds the exact Blueprint State/content digest, Adapter State and
canonical configuration digest, complete context assembly/digest, objective and
objective digest. Terminal companion evidence repeats the Blueprint, Adapter,
policy, project source, selected document, and objective identities. The Adapter
observation also records the exact configuration blob, project-review driver, and
shared process-owner source States and digests. Matching pinned, source-at-import,
and current source files is reported narrowly; executing Python objects or
bytecode are not independently attested.

## Invocation, validation, and meaning

The pinned private-review configuration uses installed `codex-cli 0.153.4`, model
`gpt-5.6-luna`, read-only sandboxing, and the documented `codex exec` flags already
used by the licensing Adapter. Current installed `codex exec --help` was inspected
for those flags. The licensing Adapter configuration and response protocol remain
unchanged.

At most one `exec` invocation occurs. Version inspection is not a model call.
There is no retry and no model-driven tool path. The prompt asks for supplied
context only and no tools or external actions; supported event inspection rejects
an observed tool/command event. As with the existing Adapter, this is detection,
not proof that all external behavior is prevented.

PeopleBot independently parses the response with duplicate-key detection and
validates strict UTF-8 encodability, exact fields, JSON types, allowed
outcomes/severities, item and string bounds, outcome consistency, citation
uniqueness, and citation membership in the assembled document States. Raw response
text is checked before hashing, and every parsed JSON string is checked before an
observation can serialize it. It performs no keyword matching or semantic scoring.
A valid response proves protocol conformance only. Neither findings nor
`no_findings` prove factual correctness or completeness.

`no_findings` and insufficient-evidence responses are conformant no-change
Executions, not invented findings. Invalid response Unicode, bad citation, runtime
failure, output limit, timeout, or observed restriction violation is a truthful
failed Execution. No successful answer or timestamp is fabricated.

## Ownership, evidence, and memory

The project-review Adapter calls the licensing Adapter's factored bounded JSON
transport, which retains the established direct-process, pipe-worker, whole-I/O
deadline, operation-workspace, cleanup, and exact recovery-authority behavior.
The public wrapper uses the existing Instance admission/provenance lifecycle.
Contention is refused before runtime probing or provider execution. Admission
remains held while an owned child/worker is unresolved; exact recovery authority
must establish shutdown before release.

Provider usage that can be parsed independently survives response-validation and
later terminal-persistence failures in returned observations and any constructible
Execution record, including when malformed Unicode is rejected before response
hashing or after JSON parsing. A terminal companion persistence failure leaves
exact admitted start evidence incomplete; it does not rerun the provider or recast
returned success as durable evidence. Process, terminal-persistence, and
admission-release failures remain distinct.

Accepting a review does not save memory. A caller may separately and explicitly
pass this exact Blueprint State to `MemoryCheckpointRequest`. The memory commit
retains the existing project-memory ancestry and records the external Blueprint
reference in metadata; it does not import framework ancestry, credentials, runtime
paths, or unrelated project State.

## Public packaging and adoption dependency

The wheel/source layout includes:

- `peoplebot/blueprints/project_review/blueprint.json`;
- `peoplebot/adapters/project_review.py`;
- `peoplebot/adapters/project_review/adapter.json`;
- `peoplebot/adapters/codex_read_only.py`, the shared process owner; and
- this contract and `docs/examples/project_review_fake.py` in the source archive.

The installed driver/configuration bytes and adopted Git objects must agree. A
future public-only consumer must use the public distribution repository identity,
one reviewed full distribution commit, and the paths above for its Blueprint and
Adapter States. The development-source commit and the later public-distribution
commit are intentionally different identities even when file bytes match.

The candidate public-distribution commit is deliberately separate from private
development ancestry. Adoption requires review and publication of that exact
commit plus artifacts whose installed bytes match its selected Git objects. A
wheel or source-archive checksum authenticates an archive but is not itself a
publicly resolvable Git `StateRef`.

## Deliberate limits

V0 does not verify factual correctness or completeness, infer context relevance,
read unselected project data, support binary/non-UTF-8 content, prevent every
possible tool or remote side effect, cancel submitted remote inference, survive
abrupt owner-process death, stop detached descendants, save memory automatically,
checkpoint selection, synchronize memory, message, schedule, publish, modify a
project, maintain Blueprints/framework code, implement a registry/inheritance
system, provision a consumer, or create an Architect.
