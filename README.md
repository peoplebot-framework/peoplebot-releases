# PeopleBot releases

This repository distributes reviewed PeopleBot release artifacts without exposing
the private development repository's Git history or operational environment.

## PeopleBot 0.1.0a1

PeopleBot `0.1.0a1` is an experimental prerelease of Git-native framework
utilities. It is not a general Architect or a ready-made project-task agent.

Publication status during preparation: **not published**. Every GitHub URL below
is proposed until the repository, tag, release, and unauthenticated downloads are
independently verified.

Proposed release page:

`https://github.com/peoplebot-framework/peoplebot-releases/releases/tag/v0.1.0a1`

The accepted development source is exact commit
`53088efed1df83401d3d11628f765339fd416c2a` in the canonical PeopleBot development
repository. The public distribution repository has independent history, so its
commit is intentionally different. See the public provenance record for the exact
connection.

## Verify and install

Download these four files from the proposed release page:

- `peoplebot-0.1.0a1-py3-none-any.whl`
- `peoplebot-0.1.0a1.tar.gz`
- `peoplebot-0.1.0a1-public-provenance.json`
- `SHA256SUMS`

Verify the files before installation:

```text
sha256sum -c SHA256SUMS
```

On PowerShell, compare `Get-FileHash -Algorithm SHA256 <file>` with the matching
line in `SHA256SUMS`.

Install only the verified local wheel, without resolving runtime dependencies:

```text
python -m pip install --no-deps ./peoplebot-0.1.0a1-py3-none-any.whl
peoplebot --help
python -m peoplebot --help
```

Python 3.11 or newer is required. Git-backed operations require Git 2.45 or newer.
The v0 single-Execution admission implementation is Windows-specific. The package
declares no runtime dependencies.

The matching source is the verified `peoplebot-0.1.0a1.tar.gz` asset. Installation
and source review do not require access to the private development repository. The
canonical private development URL embedded in package metadata is provenance, not
an installation dependency.

See [`releases/0.1.0a1/RELEASE_NOTES.md`](releases/0.1.0a1/RELEASE_NOTES.md) for
scope and findings and [`releases/0.1.0a1/ADOPTION.md`](releases/0.1.0a1/ADOPTION.md)
for the separate-sovereign-environment boundary.

## License

PeopleBot is licensed `GPL-3.0-only`. The complete `LICENSE` and applicable
`LICENSING.md` declaration are included at repository root and in the artifacts.
