# Windows finite single-tick launcher preparation

The checked-in launcher and Task Scheduler XML are inactive preparation artifacts. This work does not register, enable, or run a scheduled task.

## Required per-environment inputs

Before opting in, choose and review these exact values:

- Windows account SID and whether the account will remain logged on. The template uses `InteractiveToken`, least privilege, so it can run at a locked desktop but not after logoff.
- Absolute Python executable from the intended PeopleBot installation, absolute source/module root, bindings JSON, policy JSON, launcher, status, runtime, local checkout, and stop-control paths. A `development-cycle-tick` also requires the reviewed authority and operations JSON paths.
- Exact owner outbound remote/ref and every permitted peer source/ref/sender. The scheduled account must already have prompt-free least-privilege Git authentication for those repositories.
- One reviewed per-environment `peoplebot.usage-collection.v0` configuration. The default setup records a 10% remaining threshold, 15-minute freshness, and conservative deferral when allowance is missing or stale; see `local-usage-collection.md`.
- Timer start boundary and whether hourly, `StartWhenAvailable`, network-only, no wake, battery allowed, 35-minute outer limit, hidden execution, and `IgnoreNew` overlap are suitable. The proposed outer limit covers the 30-minute durable cycle budget plus bounded shutdown/evidence handling; it is not additional provider authority. `IgnoreNew` supplements rather than replaces PeopleBot per-Instance admission.
- The real local handler registry. The CLI's optional `fixture.complete` handler is offline validation only.

## Direct offline verification

Use synthetic local repositories and no credentials or provider:

```powershell
python docs/examples/message_work_cycle_fake.py
```

The production-development command has a separate end-to-end offline exercise. It uses the actual read-only wrappers with fake provider executables, synthetic owner/coordination remotes, an explicit memory callback, and a second fresh process that proves completed work is not dispatched again:

```powershell
python docs/examples/development_cycle_fake.py
```

For a stopped-path launcher check, create valid local bindings whose `stop_path` exists, then invoke:

```powershell
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File .\operations\windows\peoplebot-single-tick.ps1 -PythonExecutable C:\absolute\python.exe -ModuleRoot C:\absolute\peoplebot -BindingsPath C:\absolute\cycle-bindings.json -PolicyPath C:\absolute\task-policy.json -StatusPath C:\absolute\cycle-status.json -UsageConfigurationPath C:\absolute\usage-collection.json
```

For the real production-development path, explicitly select it and supply both additional reviewed configurations:

```powershell
powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File .\operations\windows\peoplebot-single-tick.ps1 -PythonExecutable C:\absolute\python.exe -ModuleRoot C:\absolute\peoplebot -BindingsPath C:\absolute\development-cycle-bindings.json -PolicyPath C:\absolute\development-cycle-policy.json -StatusPath C:\absolute\cycle-status.json -PeopleBotCommand development-cycle-tick -AuthorityPath C:\absolute\development-authority.json -OperationsPath C:\absolute\development-cycle-operations.json -UsageConfigurationPath C:\absolute\usage-collection.json
```

No active environment/session binding is distributed. The generic scheduler XML
remains disabled; scheduler start time, registration identity, local paths,
credentials, and live behavioral evidence remain owner-supplied later decisions.

The wrapper remains attached to Python until the finite tick exits. Before launch it requires `-StatusPath` to equal the path in the loaded bindings. After launch it emits persisted status only when the file has the expected format and the exact execution ID generated for that invocation; a stale or mismatched file is rejected rather than presented as durable evidence. If Python emits a bounded current-run diagnostic because status persistence failed, that output remains explicitly non-durable and the wrapper also reports the missing exact status. It preserves a nonzero PeopleBot exit code when status verification fails. It never launches detached work. The stop control is the presence of the exact `stop_path`; status must show `cycle.stopped`. Remove that file only as a deliberate operator action. Persisted accepted stop messages and unresolved outcomes are separate durable barriers and are not cleared by removing this filesystem control.

When `-UsageConfigurationPath` is supplied, the wrapper runs the deterministic
collector before the tick and defers with exit code 11 when current allowance is
not safely above the configured threshold. It runs the collector again after the
tick; any final provider record not yet present is picked up at the next entry.
The collector writes only local ledger/cursor/admission files and makes no remote
call. The native Codex Desktop heartbeat has no observed supported pre-turn local
hook, so this launcher boundary does not prevent that schedule's initial model
turn.

For `development-cycle-tick`, the supported Python path creates and retains a unique
ordinary attempt branch for each coding Execution and stores structured timing, outcome,
evidence and available provider usage in development progress. An idle tick returns
`cycle.idle` with `provider_invoked: false`; the launcher does not create a model-written
idle narrative.

## Explicit opt-in installation and live verification

Do not perform these steps until separately authorized:

1. Copy the XML template, replace every placeholder, inspect the resulting XML, and retain `<Enabled>false</Enabled>` for initial registration.
2. In the selected account's real logon context, run prompt-disabled `git ls-remote` against each exact read/write ref and run the selected Python import/version check. Any prompt or ambiguous identity is a blocker.
3. Register the reviewed XML under a unique task name, still disabled. Inspect its principal, action, working directory, triggers, overlap, power, network, missed-trigger, hidden, and time-limit settings from Task Scheduler.
4. Enable only with separate authorization. First use Task Scheduler's manual **Run**, inspect Last Run Result plus the compact PeopleBot status/ref evidence, and confirm no console window and no duplicate work.
5. Leave the desktop locked for the next real timer trigger and verify the same Git/status evidence. Separately exercise the agreed sleep/power and missed-trigger cases; this template does not wake the computer and starts a missed trigger when the machine becomes available.
6. Exercise overlap with a controlled held admission and confirm `IgnoreNew` plus `cycle.busy`. Exercise the stop file and one deliberate failing fixture, verifying framework status rather than relying only on scheduler code 0.

Task XML generation or direct shell success is not evidence that Task Scheduler ran. Actual scheduled execution, account authentication, locked-desktop behavior, power behavior, and live handlers remain unverified until those opt-in checks occur.
