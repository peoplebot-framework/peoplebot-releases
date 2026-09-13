# Experimental Context Preparation Contract v0

Status: implementation experiment, not a settled public schema.

This contract prepares isolated Git metadata and describes explicitly requested context. It adds no PeopleBot primitive, does not materialize working files or context content, and does not infer relevance.

## Detached worktree preparation

`prepare_detached_worktree` accepts a path within a local non-bare repository checkout, a repository-level `StateRef`, and a destination whose parent already exists. Git resolves the checkout's actual worktree root before safety comparisons. The destination itself must not exist, must not already be registered as a worktree, and must not be inside that source worktree.

Preparation first resolves the exact commit locally with the State resolver. It then runs `git worktree add --detach --no-checkout` at that full commit. The operation retains the existing `--no-replace-objects`, `--no-lazy-fetch`, `--no-optional-locks`, timeout, sanitized Git-routing environment, and `shell=False` controls. It supplies a command-local nonexistent `core.hooksPath`, so repository hooks cannot run. Because no checkout occurs, attributes, smudge filters, submodule checkout, and working-file conversion do not run.

The registered worktree contains only its `.git` linkage file. Its report explicitly records `detached: true` and `working_files_materialized: false`. Report paths are operational diagnostics and do not enter a context manifest. Creating a temporary Execution branch and materializing files remain later work.

The source checkout's HEAD, branch, index, tracked changes, and untracked files are not changed.

## Context policy

`peoplebot.context-policy.v0` is ordinary JSON configuration content paired with a path-specific `StateRef` identifying the exact versioned policy artifact. The manifest embeds both that identity and the complete normalized policy content. `ContextPolicy.from_dict` accepts only these fields:

- `format`: exactly `peoplebot.context-policy.v0`;
- `max_entries`: 1 through 4,096 expanded tracked leaf entries;
- `max_blob_bytes`: 0 through 16,777,216 bytes;
- `max_total_blob_bytes`: 0 through 67,108,864 bytes;
- `exclusions`: at most 256 canonical path/reason rules.

Each exclusion applies to its exact path and descendants. When rules overlap, the most-specific matching path supplies the reason. Duplicate rule paths are invalid. The framework adds no policy registry, policy service, or new primitive.

An invocation accepts 1 through 256 explicit canonical relative Git paths. Root selection, absolute paths, traversal, backslashes, empty components, trailing separators, NULs, and control characters are invalid in v0.

## Selection algorithm

Selection uses only the pinned commit's Git trees and objects:

1. Deduplicate and order requested paths by their UTF-8 bytes.
2. Resolve every request exactly. A blob, executable, symlink, or gitlink selects that leaf. A tree recursively selects all tracked leaves below it. A missing request is a classified failure.
3. Union overlapping selections by exact path. If the expanded union exceeds `max_entries`, fail without emitting a partial manifest.
4. Resolve the size of every candidate blob locally. Any missing required blob is a classified failure, including a blob that a later policy rule would exclude. This keeps all applicable blob sizes explicit. Gitlinks do not require their referenced commit locally and have null size.
5. Visit candidates in canonical path order. Apply the most-specific path exclusion first, then `max_blob_bytes`, then the remaining `max_total_blob_bytes` budget. A blob that does not fit the remaining total budget is explicitly excluded; later smaller blobs may still fit. Gitlinks consume zero blob bytes.

Regular files use mode `100644`, executables `100755`, symlinks `120000`, and gitlinks `160000`. Symlinks are represented as blobs whose size is the stored link-target byte count; they are never followed into the host filesystem. Gitlinks are represented as commit object IDs with null size; submodules are never fetched or traversed.

## Deterministic manifest

`peoplebot.context-manifest.v0` contains:

- the exact repository-level source State and root tree;
- exact policy State identity and normalized policy content;
- normalized requested paths;
- selected tracked leaves with path, mode, kind, Git type, object ID, and applicable size;
- excluded tracked leaves with the same metadata plus a stable reason code, reason, and applicable rule path;
- total selected blob bytes.

Stable JSON uses the existing sorted-key, compact UTF-8 encoding with one trailing newline. Destination paths, source checkout paths, timestamps, ownership tokens, subprocess diagnostics, and other machine-specific facts are absent, so equivalent repository objects, requests, and policy produce identical bytes.

The manifest describes metadata only. It does not copy blob content into the destination or assemble a model prompt.

## Safe cleanup and failure

Successful preparation first establishes that the initial per-worktree administrative state is eligible for disposal. V0 accepts Git's structural `HEAD`, `commondir`, `gitdir`, optional `logs/HEAD`, and empty `refs` directory. An optional index is accepted only when `git diff-index --cached` proves it has no staged difference from the pinned commit. Symlinks, additional references, locks, staged differences, and other unexpected entries make the initial state ineligible. This check runs before and after ownership-marker creation; refusal preserves the destination, registration, index, and other observed state for recovery.

Preparation then writes a random ownership token and its registration identity only in the linked worktree's Git administrative directory. The returned handle retains the matching values privately. It also retains a bounded content digest of that known-disposable per-worktree administrative directory, including `HEAD`, `gitdir`, the ownership marker, and whether an index or other per-worktree metadata exists. File modification times are not part of the digest. The v0 fingerprint accepts at most 512 administrative entries and 67,108,864 content bytes; inability to reproduce it within those bounds prevents cleanup.

Cleanup proceeds only when:

- the destination's `.git` file points to the original administrative directory without following a changed link;
- that directory's `gitdir` file points back to the original destination;
- the token and registration identity match the marker in that same administrative directory;
- the registry still describes the original path as detached at the exact commit;
- the complete bounded administrative-state digest is unchanged, including index existence and content; and
- the destination still contains exactly its `.git` linkage file.

Git treats an intentionally unmaterialized worktree as having missing tracked files, so cleanup uses `git worktree remove --force` only after those checks prove that the same operation-owned registration contains no visible or metadata-only work. An added or changed index, staged-only content, extra destination entry, moved or replacement registration, changed linkage, changed administrative state, absent marker, or mismatched marker prevents removal and returns a classified failure with the recoverable destination. Cleanup never recursively deletes an arbitrary caller-supplied directory.

If `git worktree add` fails or times out, a registration observed afterward is not treated as owned merely because its path, commit, and detached status match the request. If later preparation fails before a complete identity-bound marker and administrative baseline are returned, the operation likewise does not infer cleanup authority. Observed or uncertain state is reported as recoverable and left untouched for explicit inspection. This deliberately favors retention over automatic rollback and adds no custom locking or coordination system.

The supported lifecycle has one coordinated owner from `prepare_detached_worktree` through disposal. A token and digest narrow destructive authority, but they do not make arbitrary external concurrent mutation safe. There is no PeopleBot lock around validation and Git removal; another process could still mutate a worktree after the final validation. Git's worktree lock is not treated as a general mutual-exclusion mechanism. Callers must coordinate concurrent cleanup externally, and refused or interrupted cleanup remains a manual recovery decision.

### Reusable cleanup lessons

These lessons apply whenever an operation may delete, roll back, unregister, overwrite, or otherwise make a local or remote resource difficult to recover. Read-only inspection that cannot discard state does not need an ownership token, but it must still avoid claiming authority it has not established.

- A path or an earlier absence check identifies a location, not the owner of a resource later found there.
- An ownership token authorizes cleanup only when it is bound to, and revalidated against, the current resource identity rather than merely found at a remembered location.
- Preservation checks cover durable metadata and index state as well as visible working files; absence of materialized files does not mean absence of work.
- Observing an initial state and later finding it unchanged is insufficient unless the initial state was itself established as eligible for disposal.
- Failed, timed-out, or interrupted operations do not assume that subsequently observed resources were created by that operation.
- When current ownership and preservation cannot both be established, leave the state recoverable and report the uncertainty instead of deleting it.

The implementation guarantees these checks for handles returned by `prepare_detached_worktree` in one process. It does not provide cross-process locking, automatic recovery, durable handle reconstruction, or a general resource-ownership framework.

The current worktree registry reader parses Git's line-delimited porcelain output. A later compatibility improvement should use Git's null-delimited machine-readable porcelain form so unusual paths are represented structurally; it should not add filename-pattern exceptions. This preference does not weaken the ownership and preservation checks above.

## Deliberate limits

This slice does not:

- verify that an external repository identity names the supplied checkout;
- verify policy content against its identity in another checkout;
- infer relevant paths or traverse a dependency graph;
- materialize worktree files or context blob content;
- create an Execution branch;
- invoke a model or runtime Adapter;
- implement messaging, knowledge divergence, or a license choice.
