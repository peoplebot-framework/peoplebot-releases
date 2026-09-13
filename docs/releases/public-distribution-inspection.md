# Public distribution inspection

Use this procedure before proposing any public PeopleBot framework distribution.
It applies to the files actually proposed for publication, not only to the source
checkout.

## 1. Fix the source identity

Start from one clean committed State. Record the canonical repository, full source
commit, version, and expected artifact names. Confirm that the version, tag, and
release identity are unused before any public write.

## 2. Define public and private inputs

The public set may contain framework source, generic contracts and examples,
required configuration, license materials, release notes, checksums, and public-safe
provenance. Exclude repository working instructions, private project state,
historical handoffs, consuming-project plans, credentials, runtime memory,
Execution evidence, machine-specific paths, and authentication details.

PeopleBot and its canonical origin are valid framework provenance. A consuming
project's identity, repositories, paths, credentials, task context, and inherited
operational instructions are not valid public framework defaults or examples.

## 3. Inspect actual outputs

After building from the fixed commit:

1. enumerate every wheel and source-archive member;
2. reject unsafe paths, duplicate names, links, devices, and unexpected files;
3. extract to an operation-owned temporary directory;
4. scan all public files and archive members for known consuming-project names,
   repository bindings, host-specific paths, credential signatures, private-control
   configuration, and other operational context;
5. inspect wheel `METADATA`, source `PKG-INFO`, package data, generated manifests,
   and the wheel `RECORD` rather than trusting source text alone;
6. verify every public relative documentation link against the extracted source;
7. confirm required code, configuration, `LICENSE`, `LICENSING.md`, and matching
   source are present; and
8. verify inventories, member digests, artifact sizes, artifact SHA-256 values,
   `SHA256SUMS`, and every wheel `RECORD` entry.

Use byte-oriented deterministic checks. Account explicitly for working-tree versus
Git-blob line endings when repository files are copied to a distribution tree.

## 4. Exercise the install boundary

Install the exact verified wheel into a fresh virtual environment with dependency
resolution disabled. Run `peoplebot --help` and `python -m peoplebot --help` from an
unrelated directory. Run focused behavioral regressions only when implementation,
runtime defaults, schemas, or fixtures changed.

## 5. Separate evidence by audience

Private evidence records the build host, tool versions, commands, temporary paths,
source commit, inventories, and all validation results. Public provenance contains
only enough information to identify the exact public artifacts, public source, and
their relationship to the reviewed source State; it must not require private
repository access or expose machine-specific details.

Any correction to an existing public release must distinguish current files from
historical Git objects and already published downloads. Updating current documents
or deleting release assets does not erase content retained in public history.
