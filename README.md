# PeopleBot releases

This repository distributes reviewed PeopleBot release artifacts without exposing
the private development repository's Git history or operational environment.

## Current corrected prerelease: 0.1.0a2

PeopleBot `0.1.0a2` is an experimental prerelease of Git-native framework
utilities. It is not a functioning Architect or a ready-made project-task agent.
It supersedes `0.1.0a1` for new downloads because project-specific documentation
was removed and the consuming-environment boundary was made explicitly neutral.

Release page:

`https://github.com/peoplebot-framework/peoplebot-releases/releases/tag/v0.1.0a2`

Download and verify:

- `peoplebot-0.1.0a2-py3-none-any.whl`
- `peoplebot-0.1.0a2.tar.gz`
- `peoplebot-0.1.0a2-public-provenance.json`
- `SHA256SUMS`

```text
sha256sum -c SHA256SUMS
```

Install only the verified wheel, without resolving runtime dependencies:

```text
python -m pip install --no-deps ./peoplebot-0.1.0a2-py3-none-any.whl
peoplebot --help
python -m peoplebot --help
```

Python 3.11 or newer is required. Git-backed operations require Git 2.45 or newer.
The v0 single-Execution admission implementation is Windows-specific. The package
declares no runtime dependencies.

The matching source is the verified `peoplebot-0.1.0a2.tar.gz` asset and is
available without private development access. See
[`RELEASE_NOTES.md`](releases/0.1.0a2/RELEASE_NOTES.md),
[`ADOPTION.md`](releases/0.1.0a2/ADOPTION.md), and
[`CORRECTION.md`](releases/0.1.0a2/CORRECTION.md).

## Historical 0.1.0a1

Version `0.1.0a1` is superseded. Its public Git history and published downloads are
historical public content; the correction does not claim to erase them.

## License

PeopleBot is licensed `GPL-3.0-only`. The complete `LICENSE` and creator
acknowledgment in `LICENSING.md` accompany the distribution and artifacts.
