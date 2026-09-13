# Experimental Context Assembly Contract v0

Status: implementation experiment, not a settled public schema.

This contract turns explicitly selected pinned Git blobs into deterministic useful text. It reuses `StateRef`, `ContextPolicy`, and the context-manifest selection algorithm. It adds no PeopleBot primitive, performs no relevance inference, and invokes no model.

## Inputs and selection

`assemble_context` accepts:

- a local non-bare repository location;
- an exact repository-level `StateRef`;
- one or more explicit canonical committed paths; and
- a `ContextPolicy` whose path-specific identity uses the same repository identity and full commit as the source State.

Selection is exactly `peoplebot.context-manifest.v0`: requested paths are canonically ordered and deduplicated, directories expand to tracked leaves, overlaps collapse by exact path, and policy exclusions and size decisions remain explicit. The assembly embeds the complete manifest, including normalized policy identity/content and every exclusion. V0 does not independently load the policy blob to prove that supplied policy content matches its path-specific identity.

## Size accounting

`max_entries`, `max_blob_bytes`, and `max_total_blob_bytes` retain their context-policy meanings. Byte limits count the raw byte lengths of selected Git blob objects before UTF-8 decoding or JSON escaping. A blob exactly at a limit is eligible. An over-limit blob is not read into the assembly and remains listed in the embedded manifest with `policy.max_blob_bytes` or `policy.max_total_blob_bytes`.

The stable JSON envelope may be larger than the selected source bytes because JSON syntax and escaping are not part of source-byte accounting. No selected source content is truncated to fit an output-size approximation.

## Content and encoding

Only regular-file and executable blobs are supported as assembled documents. Every selected blob must be valid UTF-8 and contain no NUL byte. UTF-8 BOMs, line endings, final-newline presence, and other valid source characters are not normalized. Encoding each document's `content` value as UTF-8 reproduces the original Git blob bytes; `ContextDocument.to_source_bytes()` performs that round trip.

Selected unsupported content fails the complete assembly with a classified outcome:

- `context.encoding_unsupported` for invalid UTF-8;
- `context.nul_unsupported` for NUL-containing content;
- `context.symlink_unsupported` for a selected symlink, which is not followed; and
- `context.gitlink_unsupported` for a selected gitlink, which is not traversed or fetched.

A policy may explicitly exclude such an entry before assembly; the embedded manifest then reports the exclusion and no content read is attempted for it.

## Deterministic output

`peoplebot.context-assembly.v0` contains:

- the complete deterministic context manifest;
- documents in canonical Git-path order; and
- for each document, exact repository/commit/path State, Git mode, kind, blob object ID, raw size, `utf-8` encoding label, and complete text content.

Stable JSON uses sorted keys, compact UTF-8, and one trailing newline. Machine paths, timestamps, mutable branch names, working-tree content, and incidental diagnostics are absent. Equivalent inputs and locally available pinned objects produce identical bytes.

## Local-only behavior and failures

State and tree resolution retain `--no-replace-objects`, `--no-lazy-fetch`, sanitized Git routing/configuration environment, `shell=False`, and 15-second subprocess bounds. Selected blobs are read from the local object database in one bounded `git cat-file --batch` operation. Missing paths and required objects remain classified as `context.path_unavailable` and `context.object_unavailable`.

Assembly does not create, register, materialize, move, or remove a worktree. Explicit synchronization belongs outside this operation. It does not execute hooks, checkout filters, submodules, or repository content.

## Deliberate limits

This slice does not:

- infer relevance or dependencies;
- discover procedures automatically;
- verify external repository identity or policy content against the policy artifact;
- support non-UTF-8 or binary content;
- assemble symlink targets or submodule content;
- invoke a model or runtime Adapter;
- implement Instance admission, messaging, copying, learning, or Blueprint maintenance; or
- prove model compliance, autonomous behavior, or an efficiency improvement.
