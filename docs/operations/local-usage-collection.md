# Local usage collection and autonomous admission

Standing rule:

> Each agent Instance saves its available usage from its configured environment/
> session for later analysis. Missing associations are recorded as unknown and do
> not block work. Environment, Instance and session are separate identities; task
> and launcher associations are optional metadata.

`peoplebot usage-collect` incrementally reads one configured local structured
Codex record source. It selects only the two observed shapes:

- `codex.token_usage_record.v0`: `token_usage_record` payloads with `usage`,
  `turn_token_usage`, and `thread_token_usage`;
- `codex.token_count.v0`: older `event_msg` / `token_count` payloads with
  `last_token_usage`, `total_token_usage`, and available `rate_limits` windows.

This is explicit field mapping, not transcript analysis. The local JSONL ledger
contains only sanitized measurements and established identifiers. It never stores
raw records, prompts, responses, reasoning, or credentials. Cached input is a
subset of input, and reasoning output is not added again to total. A call increment
and cumulative turn/session snapshots remain separately labelled.

## Per-environment configuration

Each sovereign environment creates its own `peoplebot.usage-collection.v0` JSON.
Set its environment and Instance IDs, exact existing session ID, a stable local
source ID, observed source format, absolute rollout path, and absolute local
ledger/cursor/admission paths. Do not put environment-specific paths or session IDs
in generic framework code. Owners fill those values locally after mapping an
existing selected session to its rollout filename. Copying this configuration does
not copy an Instance or authority.

The existing Instance environment profile references that collector configuration,
an owner-local run-record directory, the `python -m peoplebot usage-report-run`
entry point, and its existing authorized task-branch batch synchronization
destination. Setup establishes this mapping once. An Instance does not rediscover
paths or identity, infer them from transcript timing, or analyze logs on each run.

The default `stop_threshold_remaining_percent` is **10**. This is an
assistant-selected, owner-changeable default. `allowance_freshness_seconds` is
900 in the documented setup example. Every applicable exposed
allowance window must be current and above the threshold. Missing or stale
allowance is explicitly recorded and conservatively defers new autonomous work.
A later valid reading may admit pending work under its original authority and
budget, but it does not reset a task budget or override STOP.

Run a manual or boundary collection with:

```text
python -m peoplebot usage-collect --config C:\absolute\usage-collection.json --phase manual
```

At an autonomous entry boundary use `--phase entry`; exit code 11 means defer and
the exact reason is saved to `admission_path`. Optional command execution/task IDs
identify the collection pass only. A token event receives execution/task linkage
only when the provider record itself explicitly supplies it; timing is never used
to assign ownership. At exit use `--phase exit`. Provider timestamps and explicit
model/effort values are retained; saved model preferences are not substituted for
absent observation fields.

Every sanitized token event is associated with its configured Instance through
the explicit environment/profile/session binding. Task, message, execution, turn,
and launcher associations are optional metadata. Missing optional links do not
hide the latest known per-call, turn-cumulative, or session-cumulative Instance
observation and never enter the allowance or STOP gates. If the Instance binding
itself is unknown, retain source/session observations with association `unknown`
until an ordinary later collection can apply a verified mapping.

The durable byte cursor advances only past complete newline-terminated records.
Incomplete trailing bytes remain for the next ordinary pass. Event identity is
stable across reruns, and existing ledger IDs prevent duplication if a write
completed before cursor replacement. Source truncation or prefix replacement is
a bounded error, not an invitation to restart counting.

## Automatic boundary and synchronization limits

`operations/windows/peoplebot-single-tick.ps1` accepts the optional
`-UsageConfigurationPath`. When present it collects and checks allowance before a
work/development tick, then collects again after it returns. The next ordinary
entry pass captures final provider metadata that arrived after a previous exit.
Ledger synchronization remains part of the environment's existing bounded,
authorized Git synchronization boundary; the collector itself makes no commit or
network call per event.

The active Codex Desktop hourly heartbeat is a native app schedule bound directly
to a thread. Its inspected automation record exposes the schedule, prompt, and
target thread, but no supported pre-turn local hook. Codex starts the model turn
before repository code can run, so this collector cannot prevent that initial
turn or guarantee zero-cost idle scheduling. Prompt instructions are not automatic
enforcement, and delayed local allowance samples are not an account-wide hard cap.
The working collector is therefore attached to the supported PeopleBot launcher;
automatic pre-turn enforcement for the native desktop heartbeat remains blocked
by the absent pre-turn integration.

Account allowance is separate from token accounting. Token counters do not imply
cost, credits, or remaining allowance. In the newer observed record shape no
allowance window is present, so autonomous admission is truthfully `missing` and
deferred until a supported local allowance record is available.

## Standard run operation

Before ordinary manual or scheduled work, save the run start:

```text
python -m peoplebot usage-report-run --profile C:\absolute\instance-profile.json --run-id <execution-id> --trigger manual --phase start --started-at <UTC>
```

After any success, failure, stop, or idle outcome, save completion locally before
reporting it:

```text
python -m peoplebot usage-report-run --profile C:\absolute\instance-profile.json --run-id <execution-id> --trigger manual --phase completion --finished-at <UTC> --outcome success --turn-id <explicit-provider-turn-id>
```

Omit `--turn-id` when it is not explicitly available. The ledger and compact
status still expose the latest known Instance-attributed call, turn, and session
observation; only the optional run/task aggregate remains unlinked. A multi-call
run may repeat `--turn-id`; only matching call increments are aggregated, and
stable event IDs prevent duplication.

Completion remains `provisional`: the source boundary and any incomplete trailing
bytes are saved, and unavailable final-turn data is not claimed. At the next normal
start or recovery, invoke the same command with `--phase recovery` and the same
explicit turn IDs. It updates the same run record, including a crashed run, without
rerunning the task. A collection/save/synchronization problem is reported as usage
reporting trouble and does not relabel otherwise successful project work.

The command emits `peoplebot.run-usage-status.v0`, including its local record path,
outcome, reporting problems, source boundary, provisional finality, token summary,
and `pending_batch` synchronization status. Copy this compact deterministic status
or reference into the ordinary completion reply; do not ask a model to reconstruct
the accounting.
