"""Detached Git worktree preparation and deterministic context manifests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._json import stable_json_bytes
from .state import (
    StateRef,
    StateResolutionError,
    _GIT_GLOBAL_OPTIONS,
    _git_environment,
    _run_git,
    _validate_git_path,
    resolve_state,
)


_MAX_POLICY_ENTRIES = 4_096
_MAX_POLICY_BLOB_BYTES = 16 * 1_024 * 1_024
_MAX_POLICY_TOTAL_BLOB_BYTES = 64 * 1_024 * 1_024
_MAX_POLICY_EXCLUSIONS = 256
_MAX_REQUESTED_PATHS = 256
_OBJECT_ID = re.compile(r"^[0-9a-f]{40,64}$")
_WORKTREE_MARKER = "peoplebot-preparation-token"
_MAX_ADMINISTRATIVE_ENTRIES = 512
_MAX_ADMINISTRATIVE_BYTES = 64 * 1_024 * 1_024
_INITIAL_ADMINISTRATIVE_FILES = frozenset(
    {"HEAD", "commondir", "gitdir", "index", "logs/HEAD"}
)
_INITIAL_ADMINISTRATIVE_DIRECTORIES = frozenset({"logs", "refs"})


def _require_text(value: str, field: str, *, maximum: int = 512) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty and have no surrounding whitespace")
    if len(value) > maximum:
        raise ValueError(f"{field} must contain at most {maximum} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must not contain control characters")


def _validate_context_path(value: str) -> None:
    _validate_git_path(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("context paths must not contain control characters")


def _require_bounded_integer(value: int, field: str, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise ValueError(f"{field} must be an integer from 0 through {maximum}")


def _path_sort_key(value: str) -> bytes:
    return value.encode("utf-8")


@dataclass(frozen=True, slots=True)
class ContextPathExclusion:
    """One policy rule excluding a tracked path and all of its descendants."""

    path: str
    reason: str

    def __post_init__(self) -> None:
        _validate_context_path(self.path)
        _require_text(self.reason, "exclusion reason")

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """Explicit, addressable content-selection policy for manifest v0."""

    identity: StateRef
    max_entries: int
    max_blob_bytes: int
    max_total_blob_bytes: int
    exclusions: tuple[ContextPathExclusion, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, StateRef) or self.identity.path is None:
            raise ValueError("policy identity must be an exact path-specific StateRef")
        _require_bounded_integer(self.max_entries, "max_entries", _MAX_POLICY_ENTRIES)
        if self.max_entries == 0:
            raise ValueError("max_entries must be at least 1")
        _require_bounded_integer(
            self.max_blob_bytes,
            "max_blob_bytes",
            _MAX_POLICY_BLOB_BYTES,
        )
        _require_bounded_integer(
            self.max_total_blob_bytes,
            "max_total_blob_bytes",
            _MAX_POLICY_TOTAL_BLOB_BYTES,
        )
        if not isinstance(self.exclusions, tuple) or not all(
            isinstance(rule, ContextPathExclusion) for rule in self.exclusions
        ):
            raise ValueError("exclusions must be a tuple of ContextPathExclusion values")
        if len(self.exclusions) > _MAX_POLICY_EXCLUSIONS:
            raise ValueError(f"exclusions must contain at most {_MAX_POLICY_EXCLUSIONS} rules")
        ordered = tuple(sorted(self.exclusions, key=lambda rule: _path_sort_key(rule.path)))
        if len({rule.path for rule in ordered}) != len(ordered):
            raise ValueError("exclusion paths must be unique")
        object.__setattr__(self, "exclusions", ordered)

    @classmethod
    def from_dict(cls, identity: StateRef, value: Mapping[str, Any]) -> ContextPolicy:
        """Parse exact policy content loaded from ordinary versioned State."""

        if not isinstance(value, Mapping):
            raise ValueError("policy content must be an object")
        expected = {
            "exclusions",
            "format",
            "max_blob_bytes",
            "max_entries",
            "max_total_blob_bytes",
        }
        if set(value) != expected:
            raise ValueError("policy content fields do not match context-policy v0")
        if value["format"] != "peoplebot.context-policy.v0":
            raise ValueError("policy format must be peoplebot.context-policy.v0")
        raw_exclusions = value["exclusions"]
        if not isinstance(raw_exclusions, list):
            raise ValueError("policy exclusions must be an array")
        exclusions: list[ContextPathExclusion] = []
        for item in raw_exclusions:
            if not isinstance(item, Mapping) or set(item) != {"path", "reason"}:
                raise ValueError("each policy exclusion must contain only path and reason")
            exclusions.append(ContextPathExclusion(item["path"], item["reason"]))
        return cls(
            identity=identity,
            max_entries=value["max_entries"],
            max_blob_bytes=value["max_blob_bytes"],
            max_total_blob_bytes=value["max_total_blob_bytes"],
            exclusions=tuple(exclusions),
        )

    def content_dict(self) -> dict[str, Any]:
        return {
            "exclusions": [rule.to_dict() for rule in self.exclusions],
            "format": "peoplebot.context-policy.v0",
            "max_blob_bytes": self.max_blob_bytes,
            "max_entries": self.max_entries,
            "max_total_blob_bytes": self.max_total_blob_bytes,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content_dict(), "identity": self.identity.to_dict()}


@dataclass(frozen=True, slots=True)
class ContextEntry:
    """One tracked leaf entry selected from the pinned Git tree."""

    path: str
    mode: str
    object_type: str
    object_id: str
    size: int | None
    kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "mode": self.mode,
            "object_id": self.object_id,
            "path": self.path,
            "size": self.size,
            "type": self.object_type,
        }


@dataclass(frozen=True, slots=True)
class ExcludedContextEntry:
    """One requested leaf omitted by an explicit, reproducible policy decision."""

    entry: ContextEntry
    reason_code: str
    reason: str
    rule_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = self.entry.to_dict()
        value["exclusion"] = {
            "code": self.reason_code,
            "reason": self.reason,
            "rule_path": self.rule_path,
        }
        return value


@dataclass(frozen=True, slots=True)
class ContextManifest:
    """Deterministic description of context selected from exact Git objects."""

    source_state: StateRef
    root_tree: str
    policy: ContextPolicy
    requested_paths: tuple[str, ...]
    selected: tuple[ContextEntry, ...]
    excluded: tuple[ExcludedContextEntry, ...]
    selected_blob_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "excluded": [entry.to_dict() for entry in self.excluded],
            "format": "peoplebot.context-manifest.v0",
            "policy": self.policy.to_dict(),
            "requested_paths": list(self.requested_paths),
            "root_tree": self.root_tree,
            "selected": [entry.to_dict() for entry in self.selected],
            "selected_blob_bytes": self.selected_blob_bytes,
            "source_state": self.source_state.to_dict(),
        }

    def to_json_bytes(self) -> bytes:
        return stable_json_bytes(self.to_dict())


@dataclass(frozen=True, slots=True)
class ContextDocument:
    """One exact UTF-8 text blob assembled from pinned Git State."""

    source: StateRef
    mode: str
    object_id: str
    size: int
    kind: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "encoding": "utf-8",
            "kind": self.kind,
            "mode": self.mode,
            "object_id": self.object_id,
            "size": self.size,
            "source": self.source.to_dict(),
            "type": "blob",
        }

    def to_source_bytes(self) -> bytes:
        """Return the original blob bytes for this supported UTF-8 document."""

        return self.content.encode("utf-8")


@dataclass(frozen=True, slots=True)
class ContextAssembly:
    """Deterministic useful content plus its exact selection manifest."""

    manifest: ContextManifest
    documents: tuple[ContextDocument, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "documents": [document.to_dict() for document in self.documents],
            "format": "peoplebot.context-assembly.v0",
            "manifest": self.manifest.to_dict(),
        }

    def to_json_bytes(self) -> bytes:
        return stable_json_bytes(self.to_dict())


class ContextManifestError(RuntimeError):
    """A bounded, classified failure to inspect pinned context."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class ContextAssemblyError(ContextManifestError):
    """A bounded, classified failure to assemble pinned text content."""


class WorktreePreparationError(RuntimeError):
    """A bounded worktree preparation or cleanup failure."""

    def __init__(
        self,
        code: str,
        detail: str,
        recoverable_paths: tuple[str, ...] = (),
    ) -> None:
        self.code = code
        self.detail = detail
        self.recoverable_paths = recoverable_paths
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class PreparedWorktree:
    """Operation-owned registration for an unmaterialized detached worktree."""

    source_checkout: Path
    destination: Path
    state: StateRef
    root_tree: str
    registration_git_dir: Path
    working_files_materialized: bool
    _token: str = field(repr=False, compare=False)
    _registration_identity: str = field(repr=False, compare=False)
    _administrative_digest: str = field(repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination": os.fspath(self.destination),
            "detached": True,
            "format": "peoplebot.prepared-worktree.v0",
            "root_tree": self.root_tree,
            "source_checkout": os.fspath(self.source_checkout),
            "source_state": self.state.to_dict(),
            "working_files_materialized": self.working_files_materialized,
        }


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    path: str
    mode: str
    object_type: str
    object_id: str


@dataclass(frozen=True, slots=True)
class _WorktreeRecord:
    path: Path
    head: str | None
    detached: bool


def _diagnostic(result: subprocess.CompletedProcess[Any]) -> str:
    value = result.stderr
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    else:
        text = value or ""
    compact = " ".join(text.split())
    return compact[:400] or "Git command failed"


def _git_text(checkout: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _run_git(
        [
            "git",
            *_GIT_GLOBAL_OPTIONS,
            "-C",
            os.fspath(checkout),
            *arguments,
        ]
    )


def _run_git_bytes(
    checkout: Path,
    arguments: Sequence[str],
    *,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            [
                "git",
                *_GIT_GLOBAL_OPTIONS,
                "-C",
                os.fspath(checkout),
                *arguments,
            ],
            input=input_bytes,
            capture_output=True,
            check=False,
            env=_git_environment(),
            shell=False,
            timeout=15,
        )
    except FileNotFoundError as error:
        raise ContextManifestError("git.unavailable", "Git executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise ContextManifestError("git.timeout", "Git operation exceeded 15 seconds") from error


def _parse_tree_entries(output: bytes) -> tuple[_TreeEntry, ...]:
    entries: list[_TreeEntry] = []
    for record in output.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = header.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as error:
            raise ContextManifestError(
                "context.tree_format_unsupported",
                "Git tree contains a path or entry not representable by manifest v0",
            ) from error
        if not _OBJECT_ID.fullmatch(object_id):
            raise ContextManifestError(
                "context.tree_format_unsupported",
                "Git tree entry has an unsupported object ID",
            )
        entries.append(_TreeEntry(path, mode, object_type, object_id))
    return tuple(entries)


def _ls_tree(checkout: Path, commit: str, path: str, *, recursive: bool) -> tuple[_TreeEntry, ...]:
    arguments = ["ls-tree", "-z"]
    if recursive:
        arguments.append("-r")
    arguments.extend(["--full-tree", commit, "--", f":(literal){path}"])
    result = _run_git_bytes(checkout, arguments)
    if result.returncode != 0:
        raise ContextManifestError(
            "context.tree_unavailable",
            f"tree entries for requested path {path!r} could not be read locally",
        )
    return _parse_tree_entries(result.stdout)


def _entry_kind(entry: _TreeEntry) -> str:
    kinds = {
        ("100644", "blob"): "file",
        ("100755", "blob"): "executable",
        ("120000", "blob"): "symlink",
        ("160000", "commit"): "gitlink",
        ("040000", "tree"): "directory",
    }
    try:
        return kinds[(entry.mode, entry.object_type)]
    except KeyError as error:
        raise ContextManifestError(
            "context.entry_unsupported",
            f"tracked path {entry.path!r} has unsupported mode/type {entry.mode}/{entry.object_type}",
        ) from error


def _blob_sizes(checkout: Path, entries: Sequence[_TreeEntry]) -> dict[str, int]:
    blob_ids = sorted(
        {entry.object_id for entry in entries if entry.object_type == "blob"}
    )
    if not blob_ids:
        return {}
    request = ("\n".join(blob_ids) + "\n").encode("ascii")
    result = _run_git_bytes(
        checkout,
        ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
        input_bytes=request,
    )
    if result.returncode != 0:
        raise ContextManifestError(
            "context.object_unavailable",
            "one or more required blob objects are unavailable locally",
        )
    sizes: dict[str, int] = {}
    for line in result.stdout.splitlines():
        parts = line.decode("ascii", "replace").split(" ")
        if len(parts) != 3 or parts[1] != "blob" or not parts[2].isdigit():
            missing = parts[0] if parts else "unknown"
            raise ContextManifestError(
                "context.object_unavailable",
                f"required blob object {missing!r} is unavailable locally",
            )
        sizes[parts[0]] = int(parts[2])
    if set(sizes) != set(blob_ids):
        raise ContextManifestError(
            "context.object_unavailable",
            "Git did not report every required blob object",
        )
    return sizes


def _matching_exclusion(
    path: str,
    exclusions: Sequence[ContextPathExclusion],
) -> ContextPathExclusion | None:
    matches = [
        rule
        for rule in exclusions
        if path == rule.path or path.startswith(f"{rule.path}/")
    ]
    if not matches:
        return None
    return max(matches, key=lambda rule: (len(rule.path.encode("utf-8")), rule.path))


def build_context_manifest(
    checkout: str | Path,
    source_state: StateRef,
    requested_paths: Sequence[str],
    policy: ContextPolicy,
) -> ContextManifest:
    """Build stable metadata for explicit context paths from pinned local Git objects."""

    if not isinstance(source_state, StateRef) or source_state.path is not None:
        raise ValueError("source_state must be a repository-level StateRef")
    if isinstance(requested_paths, (str, bytes)) or not isinstance(requested_paths, Sequence):
        raise ValueError("requested_paths must be a sequence of canonical Git paths")
    if not requested_paths:
        raise ValueError("requested_paths must contain at least one path")
    if len(requested_paths) > _MAX_REQUESTED_PATHS:
        raise ValueError(f"requested_paths must contain at most {_MAX_REQUESTED_PATHS} paths")
    for path in requested_paths:
        _validate_context_path(path)
    normalized_requests = tuple(sorted(set(requested_paths), key=_path_sort_key))

    checkout_path = Path(checkout)
    resolved = resolve_state(checkout_path, source_state)
    candidates: dict[str, _TreeEntry] = {}
    for path in normalized_requests:
        exact = _ls_tree(checkout_path, source_state.commit, path, recursive=False)
        if len(exact) != 1 or exact[0].path != path:
            raise ContextManifestError(
                "context.path_unavailable",
                f"requested path {path!r} is unavailable at the exact commit",
            )
        entry = exact[0]
        kind = _entry_kind(entry)
        expanded = (
            _ls_tree(checkout_path, source_state.commit, path, recursive=True)
            if kind == "directory"
            else (entry,)
        )
        for candidate in expanded:
            if _entry_kind(candidate) == "directory":
                continue
            candidates[candidate.path] = candidate
        if len(candidates) > policy.max_entries:
            raise ContextManifestError(
                "context.entry_limit_exceeded",
                f"expanded context exceeds the policy maximum of {policy.max_entries} entries",
            )

    ordered_candidates = tuple(
        candidates[path] for path in sorted(candidates, key=_path_sort_key)
    )
    sizes = _blob_sizes(checkout_path, ordered_candidates)
    selected: list[ContextEntry] = []
    excluded: list[ExcludedContextEntry] = []
    selected_blob_bytes = 0

    for candidate in ordered_candidates:
        kind = _entry_kind(candidate)
        size = sizes.get(candidate.object_id) if candidate.object_type == "blob" else None
        entry = ContextEntry(
            candidate.path,
            candidate.mode,
            candidate.object_type,
            candidate.object_id,
            size,
            kind,
        )
        rule = _matching_exclusion(candidate.path, policy.exclusions)
        if rule is not None:
            excluded.append(
                ExcludedContextEntry(
                    entry,
                    "policy.path_excluded",
                    rule.reason,
                    rule.path,
                )
            )
            continue
        if size is not None and size > policy.max_blob_bytes:
            excluded.append(
                ExcludedContextEntry(
                    entry,
                    "policy.max_blob_bytes",
                    "blob size exceeds max_blob_bytes",
                )
            )
            continue
        if size is not None and selected_blob_bytes + size > policy.max_total_blob_bytes:
            excluded.append(
                ExcludedContextEntry(
                    entry,
                    "policy.max_total_blob_bytes",
                    "canonical-order selection would exceed max_total_blob_bytes",
                )
            )
            continue
        selected.append(entry)
        if size is not None:
            selected_blob_bytes += size

    return ContextManifest(
        source_state=source_state,
        root_tree=resolved.root_tree,
        policy=policy,
        requested_paths=normalized_requests,
        selected=tuple(selected),
        excluded=tuple(excluded),
        selected_blob_bytes=selected_blob_bytes,
    )


def _selected_blob_contents(
    checkout: Path,
    entries: Sequence[ContextEntry],
) -> tuple[bytes, ...]:
    if not entries:
        return ()
    request = b"".join(entry.object_id.encode("ascii") + b"\n" for entry in entries)
    result = _run_git_bytes(checkout, ["cat-file", "--batch"], input_bytes=request)
    if result.returncode != 0:
        raise ContextAssemblyError(
            "context.object_unavailable",
            "one or more selected blob objects could not be read locally",
        )

    contents: list[bytes] = []
    cursor = 0
    for entry in entries:
        header_end = result.stdout.find(b"\n", cursor)
        if header_end < 0:
            raise ContextAssemblyError(
                "context.object_format_unsupported",
                "Git did not return a complete selected-object header",
            )
        header = result.stdout[cursor:header_end]
        try:
            fields = header.decode("ascii").split(" ")
        except UnicodeDecodeError as error:
            raise ContextAssemblyError(
                "context.object_format_unsupported",
                "Git returned a non-ASCII selected-object header",
            ) from error
        if len(fields) == 2 and fields[1] == "missing":
            raise ContextAssemblyError(
                "context.object_unavailable",
                f"selected blob object {entry.object_id!r} is unavailable locally",
            )
        if (
            len(fields) != 3
            or fields[0] != entry.object_id
            or fields[1] != "blob"
            or not fields[2].isdigit()
            or int(fields[2]) != entry.size
        ):
            raise ContextAssemblyError(
                "context.object_format_unsupported",
                "Git selected-object metadata does not match the context manifest",
            )
        content_start = header_end + 1
        content_end = content_start + entry.size
        if (
            content_end >= len(result.stdout)
            or result.stdout[content_end : content_end + 1] != b"\n"
        ):
            raise ContextAssemblyError(
                "context.object_format_unsupported",
                "Git did not return the complete selected blob content",
            )
        contents.append(result.stdout[content_start:content_end])
        cursor = content_end + 1
    if cursor != len(result.stdout):
        raise ContextAssemblyError(
            "context.object_format_unsupported",
            "Git returned unexpected trailing selected-object data",
        )
    return tuple(contents)


def assemble_context(
    checkout: str | Path,
    source_state: StateRef,
    requested_paths: Sequence[str],
    policy: ContextPolicy,
) -> ContextAssembly:
    """Assemble exact UTF-8 text content from explicitly selected pinned objects."""

    if not isinstance(policy, ContextPolicy):
        raise ValueError("policy must be a ContextPolicy")
    if not isinstance(source_state, StateRef) or source_state.path is not None:
        raise ValueError("source_state must be a repository-level StateRef")
    if (
        policy.identity.repository != source_state.repository
        or policy.identity.commit != source_state.commit
    ):
        raise ValueError("policy identity must share the source repository and commit")
    try:
        manifest = build_context_manifest(
            checkout,
            source_state,
            requested_paths,
            policy,
        )
    except ContextManifestError as error:
        raise ContextAssemblyError(error.code, error.detail) from error

    for entry in manifest.selected:
        if entry.kind == "symlink":
            raise ContextAssemblyError(
                "context.symlink_unsupported",
                f"selected path {entry.path!r} is a symlink and was not followed",
            )
        if entry.kind == "gitlink":
            raise ContextAssemblyError(
                "context.gitlink_unsupported",
                f"selected path {entry.path!r} is a gitlink and was not traversed",
            )
        if entry.object_type != "blob" or entry.size is None:
            raise ContextAssemblyError(
                "context.entry_kind_unsupported",
                f"selected path {entry.path!r} is not a supported text blob",
            )

    try:
        contents = _selected_blob_contents(Path(checkout), manifest.selected)
    except ContextManifestError as error:
        if isinstance(error, ContextAssemblyError):
            raise
        raise ContextAssemblyError(error.code, error.detail) from error

    documents: list[ContextDocument] = []
    for entry, content in zip(manifest.selected, contents, strict=True):
        if b"\0" in content:
            raise ContextAssemblyError(
                "context.nul_unsupported",
                f"selected path {entry.path!r} contains NUL bytes",
            )
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ContextAssemblyError(
                "context.encoding_unsupported",
                f"selected path {entry.path!r} is not valid UTF-8",
            ) from error
        documents.append(
            ContextDocument(
                source=StateRef(
                    source_state.repository,
                    source_state.commit,
                    entry.path,
                ),
                mode=entry.mode,
                object_id=entry.object_id,
                size=entry.size,
                kind=entry.kind,
                content=text,
            )
        )

    return ContextAssembly(manifest=manifest, documents=tuple(documents))


def _absolute_new_destination(source: Path, destination: str | Path) -> Path:
    raw = os.fspath(destination)
    if not raw:
        raise ValueError("destination must be a non-empty path")
    proposed = Path(os.path.abspath(raw))
    try:
        parent = proposed.parent.resolve(strict=True)
    except FileNotFoundError as error:
        raise WorktreePreparationError(
            "worktree.parent_unavailable",
            "destination parent directory does not exist",
        ) from error
    if not parent.is_dir():
        raise WorktreePreparationError(
            "worktree.parent_unavailable",
            "destination parent is not a directory",
        )
    normalized = parent / proposed.name
    if os.path.lexists(normalized):
        raise WorktreePreparationError(
            "worktree.destination_exists",
            "destination already exists and was not modified",
            (os.fspath(normalized),),
        )
    try:
        normalized.relative_to(source)
    except ValueError:
        pass
    else:
        raise WorktreePreparationError(
            "worktree.destination_inside_source",
            "destination must not be inside the source checkout",
        )
    return normalized


def _path_identity(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(path))))


def _worktree_records(checkout: Path) -> tuple[_WorktreeRecord, ...]:
    try:
        result = _git_text(checkout, "worktree", "list", "--porcelain")
    except StateResolutionError as error:
        raise WorktreePreparationError(error.code, error.detail) from error
    if result.returncode != 0:
        raise WorktreePreparationError(
            "worktree.registry_unavailable",
            "Git worktree registry could not be read",
        )
    records: list[_WorktreeRecord] = []
    current_path: Path | None = None
    current_head: str | None = None
    detached = False
    for line in [*result.stdout.splitlines(), ""]:
        if line.startswith("worktree "):
            if current_path is not None:
                records.append(_WorktreeRecord(current_path, current_head, detached))
            current_path = Path(
                os.path.abspath(
                    os.path.normpath(line.removeprefix("worktree "))
                )
            )
            current_head = None
            detached = False
        elif line.startswith("HEAD "):
            current_head = line.removeprefix("HEAD ")
        elif line == "detached":
            detached = True
        elif line == "" and current_path is not None:
            records.append(_WorktreeRecord(current_path, current_head, detached))
            current_path = None
            current_head = None
            detached = False
    return tuple(records)


def _find_worktree(checkout: Path, destination: Path) -> _WorktreeRecord | None:
    wanted = _path_identity(destination)
    return next(
        (record for record in _worktree_records(checkout) if _path_identity(record.path) == wanted),
        None,
    )


def _is_unmaterialized_destination(destination: Path) -> bool:
    if destination.is_symlink() or not destination.is_dir():
        return False
    entries = tuple(destination.iterdir())
    return (
        len(entries) == 1
        and entries[0].name == ".git"
        and not entries[0].is_symlink()
        and entries[0].is_file()
    )


def _observed_recoverable_path(source: Path, destination: Path) -> tuple[str, ...]:
    if os.path.lexists(destination):
        return (os.fspath(destination),)
    try:
        record = _find_worktree(source, destination)
    except WorktreePreparationError:
        return (os.fspath(destination),)
    return (os.fspath(destination),) if record is not None else ()


def _linked_path(value: str, relative_to: Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = relative_to / path
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _small_single_line(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > 4_096:
            return None
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    if content.endswith("\n"):
        content = content[:-1]
        if content.endswith("\r"):
            content = content[:-1]
    if "\n" in content or "\r" in content:
        return None
    return content


def _destination_git_dir(gitfile: Path) -> Path | None:
    content = _small_single_line(gitfile)
    if content is None:
        return None
    prefix = "gitdir: "
    if not content.startswith(prefix):
        return None
    value = content.removeprefix(prefix)
    return _linked_path(value, gitfile.parent) if value else None


def _administrative_gitfile(admin_dir: Path) -> Path | None:
    gitdir_file = admin_dir / "gitdir"
    content = _small_single_line(gitdir_file)
    if not content:
        return None
    return _linked_path(content, admin_dir)


def _require_disposable_initial_state(
    admin_dir: Path,
    destination: Path,
    commit: str,
    *,
    marker_expected: bool,
) -> None:
    """Reject an initial baseline that already contains unproven work."""

    if admin_dir.is_symlink() or not admin_dir.is_dir():
        raise WorktreePreparationError(
            "worktree.initial_state_unavailable",
            "initial worktree administrative directory is unavailable",
        )

    def raise_walk_error(error: OSError) -> None:
        raise error

    allowed_files = set(_INITIAL_ADMINISTRATIVE_FILES)
    if marker_expected:
        allowed_files.add(_WORKTREE_MARKER)
    try:
        observed_files: set[str] = set()
        observed_directories: set[str] = set()
        for current, directory_names, file_names in os.walk(
            admin_dir,
            topdown=True,
            onerror=raise_walk_error,
            followlinks=False,
        ):
            current_path = Path(current)
            retained_directories: list[str] = []
            for name in directory_names:
                path = current_path / name
                relative = path.relative_to(admin_dir).as_posix()
                if path.is_symlink() or relative not in _INITIAL_ADMINISTRATIVE_DIRECTORIES:
                    raise WorktreePreparationError(
                        "worktree.initial_state_not_disposable",
                        "initial worktree administrative state contains an unexpected entry",
                    )
                observed_directories.add(relative)
                retained_directories.append(name)
            directory_names[:] = retained_directories
            for name in file_names:
                path = current_path / name
                relative = path.relative_to(admin_dir).as_posix()
                if path.is_symlink() or not path.is_file() or relative not in allowed_files:
                    raise WorktreePreparationError(
                        "worktree.initial_state_not_disposable",
                        "initial worktree administrative state contains an unexpected entry",
                    )
                observed_files.add(relative)
    except OSError as error:
        raise WorktreePreparationError(
            "worktree.initial_state_unavailable",
            "initial worktree administrative state could not be inspected",
        ) from error
    required = {"HEAD", "commondir", "gitdir"}
    if (
        not required.issubset(observed_files)
        or not observed_files.issubset(allowed_files)
        or not observed_directories.issubset(_INITIAL_ADMINISTRATIVE_DIRECTORIES)
        or ((_WORKTREE_MARKER in observed_files) != marker_expected)
        or _small_single_line(admin_dir / "HEAD") != commit
    ):
        raise WorktreePreparationError(
            "worktree.initial_state_not_disposable",
            "initial worktree administrative state is not a known disposable baseline",
        )
    index = admin_dir / "index"
    if index.exists():
        result = _git_text(
            destination,
            "diff-index",
            "--cached",
            "--quiet",
            commit,
            "--",
        )
        if result.returncode == 1:
            raise WorktreePreparationError(
                "worktree.initial_state_not_disposable",
                "initial worktree index contains staged differences",
            )
        if result.returncode != 0:
            raise WorktreePreparationError(
                "worktree.initial_state_unavailable",
                "initial worktree index could not be verified against pinned State",
            )


def _registration_identity(admin_dir: Path, gitfile: Path) -> str:
    value = (
        os.fsencode(_path_identity(admin_dir))
        + b"\0"
        + os.fsencode(_path_identity(gitfile))
    )
    return hashlib.sha256(value).hexdigest()


class _AdministrativeStateError(RuntimeError):
    pass


def _administrative_digest(admin_dir: Path) -> str:
    if admin_dir.is_symlink() or not admin_dir.is_dir():
        raise _AdministrativeStateError("administrative directory is unavailable")
    digest = hashlib.sha256()
    entry_count = 0
    content_bytes = 0
    for current, directory_names, file_names in os.walk(
        admin_dir,
        topdown=True,
        followlinks=False,
    ):
        directory_names.sort()
        file_names.sort()
        current_path = Path(current)
        retained_directories: list[str] = []
        for name in directory_names:
            path = current_path / name
            relative = os.fsencode(path.relative_to(admin_dir).as_posix())
            entry_count += 1
            if path.is_symlink():
                try:
                    target = os.fsencode(os.readlink(path))
                except (OSError, UnicodeError) as error:
                    raise _AdministrativeStateError(
                        "administrative link could not be inspected"
                    ) from error
                digest.update(b"L\0" + relative + b"\0" + target + b"\0")
            else:
                digest.update(b"D\0" + relative + b"\0")
                retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in file_names:
            path = current_path / name
            relative = os.fsencode(path.relative_to(admin_dir).as_posix())
            entry_count += 1
            if entry_count > _MAX_ADMINISTRATIVE_ENTRIES:
                raise _AdministrativeStateError("administrative entry limit exceeded")
            if path.is_symlink():
                try:
                    target = os.fsencode(os.readlink(path))
                except (OSError, UnicodeError) as error:
                    raise _AdministrativeStateError(
                        "administrative link could not be inspected"
                    ) from error
                digest.update(b"L\0" + relative + b"\0" + target + b"\0")
                continue
            try:
                size = path.stat().st_size
            except OSError as error:
                raise _AdministrativeStateError(
                    "administrative file could not be inspected"
                ) from error
            if not path.is_file():
                raise _AdministrativeStateError("administrative entry type is unsupported")
            content_bytes += size
            if content_bytes > _MAX_ADMINISTRATIVE_BYTES:
                raise _AdministrativeStateError("administrative byte limit exceeded")
            digest.update(b"F\0" + relative + b"\0" + str(size).encode("ascii") + b"\0")
            try:
                with path.open("rb") as stream:
                    while chunk := stream.read(64 * 1_024):
                        digest.update(chunk)
            except OSError as error:
                raise _AdministrativeStateError(
                    "administrative file could not be read"
                ) from error
            digest.update(b"\0")
    if entry_count > _MAX_ADMINISTRATIVE_ENTRIES:
        raise _AdministrativeStateError("administrative entry limit exceeded")
    return digest.hexdigest()


def _write_registration_marker(path: Path, token: str, identity: str) -> None:
    content = stable_json_bytes(
        {
            "format": "peoplebot.worktree-ownership.v0",
            "registration_identity": identity,
            "token": token,
        }
    )
    with path.open("xb") as stream:
        stream.write(content)


def _registration_marker_matches(path: Path, token: str, identity: str) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        if path.stat().st_size > 4_096:
            return False
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict) or set(value) != {
        "format",
        "registration_identity",
        "token",
    }:
        return False
    return (
        value["format"] == "peoplebot.worktree-ownership.v0"
        and isinstance(value["registration_identity"], str)
        and secrets.compare_digest(value["registration_identity"], identity)
        and isinstance(value["token"], str)
        and secrets.compare_digest(value["token"], token)
    )


def prepare_detached_worktree(
    checkout: str | Path,
    source_state: StateRef,
    destination: str | Path,
) -> PreparedWorktree:
    """Register a detached worktree at exact State without checking out its files."""

    if not isinstance(source_state, StateRef) or source_state.path is not None:
        raise ValueError("source_state must be a repository-level StateRef")
    source_input = Path(checkout)
    resolved = resolve_state(source_input, source_state)
    source_result = _git_text(source_input, "rev-parse", "--show-toplevel")
    if source_result.returncode != 0:
        raise WorktreePreparationError(
            "worktree.source_invalid",
            "source must be a non-bare Git worktree checkout",
        )
    source = Path(source_result.stdout.strip()).resolve(strict=True)
    target = _absolute_new_destination(source, destination)
    if _find_worktree(source, target) is not None:
        raise WorktreePreparationError(
            "worktree.destination_registered",
            "destination is already present in the Git worktree registry",
            (os.fspath(target),),
        )

    token = secrets.token_hex(32)
    disabled_hooks = target.parent / f".peoplebot-no-hooks-{token}"
    command = [
        "git",
        *_GIT_GLOBAL_OPTIONS,
        "-c",
        f"core.hooksPath={os.fspath(disabled_hooks)}",
        "-C",
        os.fspath(source),
        "worktree",
        "add",
        "--detach",
        "--no-checkout",
        os.fspath(target),
        source_state.commit,
    ]
    try:
        result = _run_git(command)
    except StateResolutionError as error:
        remaining = _observed_recoverable_path(source, target)
        raise WorktreePreparationError(error.code, error.detail, remaining) from error
    if result.returncode != 0:
        remaining = _observed_recoverable_path(source, target)
        raise WorktreePreparationError(
            "worktree.add_failed",
            _diagnostic(result),
            remaining,
        )

    try:
        record = _find_worktree(source, target)
        if record is None or record.head != source_state.commit or not record.detached:
            raise WorktreePreparationError(
                "worktree.registration_mismatch",
                "new worktree registration is not detached at the exact commit",
            )
        if not _is_unmaterialized_destination(target):
            raise WorktreePreparationError(
                "worktree.materialization_unexpected",
                "new worktree contains entries other than its Git linkage file",
            )
        gitfile = target / ".git"
        registration_git_dir = _destination_git_dir(gitfile)
        if (
            registration_git_dir is None
            or registration_git_dir.is_symlink()
            or not registration_git_dir.is_dir()
        ):
            raise WorktreePreparationError(
                "worktree.registration_unavailable",
                "new worktree administrative directory could not be resolved",
            )
        administrative_gitfile = _administrative_gitfile(registration_git_dir)
        if (
            administrative_gitfile is None
            or _path_identity(administrative_gitfile) != _path_identity(gitfile)
        ):
            raise WorktreePreparationError(
                "worktree.registration_mismatch",
                "new worktree linkage is not bidirectional",
            )
        _require_disposable_initial_state(
            registration_git_dir,
            target,
            source_state.commit,
            marker_expected=False,
        )
        registration_identity = _registration_identity(registration_git_dir, gitfile)
        marker = registration_git_dir / _WORKTREE_MARKER
        _write_registration_marker(marker, token, registration_identity)
        _require_disposable_initial_state(
            registration_git_dir,
            target,
            source_state.commit,
            marker_expected=True,
        )
        administrative_digest = _administrative_digest(registration_git_dir)
    except Exception as error:
        remaining = _observed_recoverable_path(source, target)
        if isinstance(error, WorktreePreparationError):
            raise WorktreePreparationError(error.code, error.detail, remaining) from error
        raise WorktreePreparationError(
            "worktree.preparation_failed",
            "worktree registration could not be ownership-marked",
            remaining,
        ) from error

    return PreparedWorktree(
        source_checkout=source,
        destination=target,
        state=source_state,
        root_tree=resolved.root_tree,
        registration_git_dir=registration_git_dir,
        working_files_materialized=False,
        _token=token,
        _registration_identity=registration_identity,
        _administrative_digest=administrative_digest,
    )


def cleanup_prepared_worktree(prepared: PreparedWorktree) -> None:
    """Remove only an unchanged worktree carrying this operation's ownership marker."""

    if not isinstance(prepared, PreparedWorktree):
        raise ValueError("prepared must be a PreparedWorktree returned by preparation")
    gitfile = prepared.destination / ".git"
    linked_admin_dir = _destination_git_dir(gitfile)
    if (
        linked_admin_dir is None
        or _path_identity(linked_admin_dir) != _path_identity(prepared.registration_git_dir)
    ):
        raise WorktreePreparationError(
            "worktree.cleanup_registration_changed",
            "destination no longer links to the operation-owned registration",
            (os.fspath(prepared.destination),),
        )
    if (
        prepared.registration_git_dir.is_symlink()
        or not prepared.registration_git_dir.is_dir()
    ):
        raise WorktreePreparationError(
            "worktree.cleanup_registration_changed",
            "operation-owned administrative directory is unavailable",
            (os.fspath(prepared.destination),),
        )
    administrative_gitfile = _administrative_gitfile(prepared.registration_git_dir)
    if (
        administrative_gitfile is None
        or _path_identity(administrative_gitfile) != _path_identity(gitfile)
    ):
        raise WorktreePreparationError(
            "worktree.cleanup_registration_changed",
            "administrative registration no longer links to the original destination",
            (os.fspath(prepared.destination),),
        )
    current_identity = _registration_identity(prepared.registration_git_dir, gitfile)
    if not secrets.compare_digest(current_identity, prepared._registration_identity):
        raise WorktreePreparationError(
            "worktree.cleanup_registration_changed",
            "current worktree identity does not match the prepared registration",
            (os.fspath(prepared.destination),),
        )
    marker = prepared.registration_git_dir / _WORKTREE_MARKER
    if not _registration_marker_matches(
        marker,
        prepared._token,
        prepared._registration_identity,
    ):
        raise WorktreePreparationError(
            "worktree.cleanup_not_owned",
            "operation ownership marker is unavailable or does not match",
            (os.fspath(prepared.destination),),
        )
    record = _find_worktree(prepared.source_checkout, prepared.destination)
    if record is None or record.head != prepared.state.commit or not record.detached:
        raise WorktreePreparationError(
            "worktree.cleanup_registration_changed",
            "worktree registration changed; cleanup was not attempted",
            (os.fspath(prepared.destination),),
        )
    if not _is_unmaterialized_destination(prepared.destination):
        raise WorktreePreparationError(
            "worktree.cleanup_not_empty",
            "worktree contains user or materialized files; cleanup was not attempted",
            (os.fspath(prepared.destination),),
        )
    try:
        current_digest = _administrative_digest(prepared.registration_git_dir)
    except _AdministrativeStateError as error:
        raise WorktreePreparationError(
            "worktree.cleanup_metadata_changed",
            "per-worktree administrative state cannot be proven unchanged",
            (os.fspath(prepared.destination),),
        ) from error
    if not secrets.compare_digest(current_digest, prepared._administrative_digest):
        raise WorktreePreparationError(
            "worktree.cleanup_metadata_changed",
            "per-worktree administrative state changed; cleanup was not attempted",
            (os.fspath(prepared.destination),),
        )
    try:
        result = _git_text(
            prepared.source_checkout,
            "worktree",
            "remove",
            "--force",
            "--",
            os.fspath(prepared.destination),
        )
    except StateResolutionError as error:
        raise WorktreePreparationError(
            error.code,
            error.detail,
            (os.fspath(prepared.destination),),
        ) from error
    if result.returncode != 0:
        raise WorktreePreparationError(
            "worktree.cleanup_failed",
            _diagnostic(result),
            (os.fspath(prepared.destination),),
        )
    if os.path.lexists(prepared.destination):
        raise WorktreePreparationError(
            "worktree.cleanup_incomplete",
            "Git removed the registration but destination state remains recoverable",
            (os.fspath(prepared.destination),),
        )
