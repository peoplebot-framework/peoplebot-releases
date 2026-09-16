# PeopleBot releases

This public repository distributes reviewed PeopleBot framework source and release
artifacts without publishing the private development repository's Git history or
operational environment.

## Current experimental framework: 0.1.0a4

PeopleBot `0.1.0a4` consolidates the deterministic foundation and bounded
project-review agent from earlier prereleases with experimental Git messaging,
finite work-cycle and development-cycle utilities, sanitized diagnostic
corrections, and per-Instance usage reporting.

It is not stable 1.0, a general autonomous runtime, or a functioning Architect.
Installation does not create an Instance, bind a provider chat, configure
credentials, enable a schedule, or grant project authority.

Download the wheel, matching source archive, public provenance, and `SHA256SUMS`
from the [0.1.0a4 prerelease](https://github.com/peoplebot-framework/peoplebot-releases/releases/tag/v0.1.0a4).
Read the [release notes](releases/0.1.0a4/RELEASE_NOTES.md),
[adoption boundary](releases/0.1.0a4/ADOPTION.md), and
[Instance operating model](docs/operations/instance-operating-model.md) before use.

Python 3.11 or newer and Git 2.45 or newer are required. Windows-specific
single-Instance admission and the supplied Codex CLI adapters/usage formats are
the implemented platform paths. No active runtime binding is distributed.

## Other releases

Framework versions `0.1.0a1`, `0.1.0a2`, and `0.1.0a3` remain immutable
historical prereleases. The separately named `syslog-onboarding-v0.1.0` release is
a different component, not a newer framework package. Public documentation and
syslog material already on `main` are preserved alongside this update.

## License

PeopleBot is licensed `GPL-3.0-only`. The complete `LICENSE` and creator
acknowledgment in `LICENSING.md` accompany the source and artifacts.
