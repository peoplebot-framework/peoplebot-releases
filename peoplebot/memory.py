"""Bounded Git-native Instance memory checkpoints and exact resume."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from ._json import stable_json_bytes
from .execution import ExecutionRecord, ExecutionStatus, TerminalOutcome, _require_text, _utc_timestamp
from .preparation import ContextAssembly, ContextAssemblyError, ContextPolicy, assemble_context
from .provenance import (
    AttemptEvidenceStore,
    ExecutionStart,
    GitAttemptStore,
    ProvenanceError,
    ProvenanceRunResult,
    run_with_execution_provenance,
)
from .state import StateRef, StateResolutionError, resolve_state


_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_ZERO_OBJECT_ID = "0" * 40
_METADATA_PATH = "memory.json"
_CONTENT_DIRECTORY = "memory"
_MAX_ITEMS = 64
_MAX_ITEM_BYTES = 65_536
_MAX_TOTAL_BYTES = 262_144
_MAX_METADATA_BYTES = 65_536


class MemoryError(RuntimeError):
    """A classified local Instance-memory failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _checked_out_branches(output: bytes) -> frozenset[str]:
    """Parse `git worktree list --porcelain -z`, failing closed on ambiguity."""

    if not output.endswith(b"\0\0"):
        raise ValueError("worktree output is not complete NUL-delimited porcelain")
    records = output[:-2].split(b"\0\0")
    branches: set[str] = set()
    for record in records:
        fields = record.split(b"\0")
        if not fields or not fields[0].startswith(b"worktree ") or fields[0] == b"worktree ":
            raise ValueError("worktree record has no path")
        dispositions = [
            field
            for field in fields[1:]
            if field == b"bare" or field == b"detached" or field.startswith(b"branch ")
        ]
        if len(dispositions) != 1:
            raise ValueError("worktree record has ambiguous HEAD disposition")
        disposition = dispositions[0]
        if disposition.startswith(b"branch "):
            encoded = disposition.removeprefix(b"branch ")
            if not encoded.startswith(b"refs/heads/") or b"\n" in encoded or b"\r" in encoded:
                raise ValueError("worktree branch identity is invalid")
            try:
                branch = encoded.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError("worktree branch identity is not UTF-8") from error
            if branch in branches:
                raise ValueError("worktree branch is reported more than once")
            branches.add(branch)
    return frozenset(branches)


def _require_identity(value: str, field: str) -> None:
    _require_text(value, field)
    if len(value) > 256:
        raise ValueError(f"{field} exceeds 256 characters")


def _memory_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError("memory item path must contain 1-256 characters")
    if "\\" in value or "\0" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("memory item path must be a canonical printable POSIX path")
    parsed = PurePosixPath(value)
    if (
        parsed.is_absolute()
        or str(parsed) != value
        or value == "."
        or any(part in {".", ".."} for part in parsed.parts)
    ):
        raise ValueError("memory item path must be a canonical relative POSIX path")
    return value


def instance_memory_ref(environment_id: str, instance_id: str) -> str:
    """Return the deterministic discovery branch for one owning environment/Instance."""

    _require_identity(environment_id, "environment_id")
    _require_identity(instance_id, "instance_id")
    environment_digest = hashlib.sha256(
        b"peoplebot.instance-memory.environment.v0\0" + environment_id.encode("utf-8")
    ).hexdigest()
    instance_digest = hashlib.sha256(
        b"peoplebot.instance-memory.instance.v0\0"
        + environment_id.encode("utf-8")
        + b"\0"
        + instance_id.encode("utf-8")
    ).hexdigest()
    return f"refs/heads/peoplebot/instances/v0/{environment_digest}/{instance_digest}"


@dataclass(frozen=True, slots=True)
class MemoryItem:
    path: str
    content: str

    def __post_init__(self) -> None:
        _memory_path(self.path)
        if not isinstance(self.content, str):
            raise ValueError("memory item content must be UTF-8 text")
        content = self.content.encode("utf-8")
        if b"\0" in content:
            raise ValueError("memory item content must not contain NUL")
        if len(content) > _MAX_ITEM_BYTES:
            raise ValueError(f"memory item content exceeds {_MAX_ITEM_BYTES} bytes")


@dataclass(frozen=True, slots=True)
class MemoryCheckpointRequest:
    repository: str
    environment_id: str
    instance_id: str
    blueprint: StateRef
    expected_state: StateRef
    items: tuple[MemoryItem, ...]
    saved_at: str
    initial: bool = False

    def __post_init__(self) -> None:
        _require_text(self.repository, "repository")
        _require_identity(self.environment_id, "environment_id")
        _require_identity(self.instance_id, "instance_id")
        if not isinstance(self.blueprint, StateRef):
            raise ValueError("blueprint must be a StateRef")
        if not isinstance(self.expected_state, StateRef) or self.expected_state.path is not None:
            raise ValueError("expected_state must be a repository-level StateRef")
        if self.repository != self.expected_state.repository:
            raise ValueError("expected State repository must match the memory repository")
        if not isinstance(self.items, tuple) or not all(
            isinstance(item, MemoryItem) for item in self.items
        ):
            raise ValueError("items must be a tuple of MemoryItem")
        if not isinstance(self.initial, bool):
            raise ValueError("initial must be a boolean")
        if len(self.items) > _MAX_ITEMS:
            raise ValueError(f"memory checkpoint exceeds {_MAX_ITEMS} items")
        paths = [item.path for item in self.items]
        if len(paths) != len(set(paths)):
            raise ValueError("memory item paths must be unique")
        total = sum(len(item.content.encode("utf-8")) for item in self.items)
        if total > _MAX_TOTAL_BYTES:
            raise ValueError(f"memory content exceeds {_MAX_TOTAL_BYTES} bytes")
        _utc_timestamp(self.saved_at, "saved_at")


@dataclass(frozen=True, slots=True)
class MemoryCheckpointResult:
    state: StateRef
    ref_name: str
    created: bool
    changed: bool
    locally_committed: bool = True
    remote_synchronized: bool = False


@dataclass(frozen=True, slots=True)
class MemoryExecutionResult:
    checkpoint: MemoryCheckpointResult | None
    provenance: ProvenanceRunResult


class GitMemoryStore:
    """Internal storage collaborator for the admitted memory Execution entry point."""

    def __init__(self, checkout: str | Path, repository: str) -> None:
        self._plumbing = GitAttemptStore(checkout, repository)
        self.checkout = self._plumbing.checkout
        self.repository = self._plumbing.repository

    def _write_tree(self, entries: Mapping[str, str]) -> str:
        root: dict[str, Any] = {}
        for path, blob in entries.items():
            if not _OBJECT_ID.fullmatch(blob):
                raise ValueError("memory blob must be a Git object ID")
            parts = PurePosixPath(path).parts
            node = root
            for part in parts[:-1]:
                existing = node.setdefault(part, {})
                if not isinstance(existing, dict):
                    raise ValueError("memory paths must not overlap files")
                node = existing
            if parts[-1] in node:
                raise ValueError("memory tree paths must be unique")
            node[parts[-1]] = blob

        def write(node: Mapping[str, Any]) -> str:
            serialized = bytearray()
            for name, value in sorted(node.items()):
                if isinstance(value, dict):
                    object_id = write(value)
                    entry = f"040000 tree {object_id}\t{name}".encode("utf-8")
                else:
                    entry = f"100644 blob {value}\t{name}".encode("utf-8")
                serialized.extend(entry + b"\0")
            return self._plumbing._object_id(
                self._plumbing._git("mktree", "-z", input_bytes=bytes(serialized)),
                "write the Instance memory tree",
            )

        return write(root)

    def _metadata(self, request: MemoryCheckpointRequest, blobs: Mapping[str, str]) -> bytes:
        ref_name = instance_memory_ref(request.environment_id, request.instance_id)
        value = {
            "blueprint": request.blueprint.to_dict(),
            "environment_id": request.environment_id,
            "format": "peoplebot.instance-memory.v0",
            "instance_id": request.instance_id,
            "items": [
                {
                    "bytes": len(item.content.encode("utf-8")),
                    "git_path": f"{_CONTENT_DIRECTORY}/{item.path}",
                    "object_id": blobs[item.path],
                    "path": item.path,
                    "sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                }
                for item in sorted(request.items, key=lambda item: item.path)
            ],
            "ref_name": ref_name,
            "repository": request.repository,
        }
        content = stable_json_bytes(value)
        if len(content) > _MAX_METADATA_BYTES:
            raise MemoryError(
                "memory.metadata_limit_exceeded",
                f"memory metadata exceeds {_MAX_METADATA_BYTES} bytes",
            )
        return content

    def _require_unused_branch(self, ref_name: str) -> None:
        inspected = self._plumbing._git("worktree", "list", "--porcelain", "-z")
        if inspected.returncode != 0:
            raise MemoryError(
                "memory.worktree_inspection_failed",
                "Git could not inspect structured worktree branch ownership",
            )
        try:
            checked_out = _checked_out_branches(inspected.stdout)
        except ValueError as error:
            raise MemoryError(
                "memory.worktree_inspection_failed",
                "Git returned ambiguous structured worktree branch ownership",
            ) from error
        if ref_name in checked_out:
            raise MemoryError(
                "memory.destination_checked_out",
                "the Instance memory branch is checked out and was preserved",
            )

    def _checkpoint(self, request: MemoryCheckpointRequest) -> MemoryCheckpointResult:
        if not isinstance(request, MemoryCheckpointRequest):
            raise ValueError("request must be a MemoryCheckpointRequest")
        if request.repository != self.repository:
            raise MemoryError(
                "memory.repository_mismatch",
                "memory request repository does not match this store",
            )
        try:
            expected = resolve_state(self.checkout, request.expected_state)
            if not request.initial:
                metadata = read_instance_memory_metadata(self.checkout, request.expected_state)
                _validate_metadata_binding(
                    metadata,
                    request.repository,
                    request.environment_id,
                    request.instance_id,
                    request.blueprint,
                )
            blobs = {
                item.path: self._plumbing._write_blob(item.content.encode("utf-8"))
                for item in request.items
            }
            metadata_bytes = self._metadata(request, blobs)
            entries = {
                f"{_CONTENT_DIRECTORY}/{path}": object_id for path, object_id in blobs.items()
            }
            entries[_METADATA_PATH] = self._plumbing._write_blob(metadata_bytes)
            tree = self._write_tree(entries)
            ref_name = instance_memory_ref(request.environment_id, request.instance_id)
            if not request.initial and tree == expected.root_tree:
                self._plumbing._update_ref(
                    ref_name,
                    request.expected_state.commit,
                    request.expected_state.commit,
                    reflog_message="peoplebot instance memory v0 unchanged verification",
                    conflict_code="memory.stale_expected_state",
                    conflict_detail="Instance memory destination does not match expected State",
                    symbolic_code="memory.destination_symbolic",
                    symbolic_detail=(
                        "the Instance memory destination is symbolic and was preserved"
                    ),
                    inspection_code="memory.ref_inspection_failed",
                    inspection_detail=(
                        "Git could not establish that the Instance memory destination is direct"
                    ),
                    persistence_code="memory.persistence_failed",
                    persistence_detail="Git could not verify the Instance memory checkpoint",
                )
                return MemoryCheckpointResult(
                    request.expected_state,
                    ref_name,
                    created=False,
                    changed=False,
                )
            self._require_unused_branch(ref_name)
            commit = self._plumbing._write_commit(
                tree,
                (request.expected_state.commit,),
                request.saved_at,
                "PeopleBot Instance memory checkpoint",
            )
            self._plumbing._update_ref(
                ref_name,
                commit,
                _ZERO_OBJECT_ID if request.initial else request.expected_state.commit,
                reflog_message="peoplebot instance memory v0",
                conflict_code=(
                    "memory.destination_exists"
                    if request.initial
                    else "memory.stale_expected_state"
                ),
                conflict_detail=(
                    "initial Instance memory destination already exists or changed"
                    if request.initial
                    else "Instance memory destination does not match expected State"
                ),
                symbolic_code="memory.destination_symbolic",
                symbolic_detail="the Instance memory destination is symbolic and was preserved",
                inspection_code="memory.ref_inspection_failed",
                inspection_detail=(
                    "Git could not establish that the Instance memory destination is direct"
                ),
                persistence_code="memory.persistence_failed",
                persistence_detail="Git could not publish the Instance memory checkpoint",
            )
        except MemoryError:
            raise
        except StateResolutionError as error:
            raise MemoryError(error.code, error.detail) from error
        except ProvenanceError as error:
            raise MemoryError(error.code, error.detail) from error
        return MemoryCheckpointResult(
            StateRef(self.repository, commit),
            ref_name,
            created=request.initial,
            changed=True,
        )


def read_instance_memory_metadata(
    checkout: str | Path,
    state: StateRef,
) -> dict[str, Any]:
    if not isinstance(state, StateRef) or state.path is not None:
        raise ValueError("state must be a repository-level StateRef")
    policy = ContextPolicy(
        StateRef(state.repository, state.commit, _METADATA_PATH),
        max_entries=1,
        max_blob_bytes=_MAX_METADATA_BYTES,
        max_total_blob_bytes=_MAX_METADATA_BYTES,
    )
    try:
        assembly = assemble_context(checkout, state, (_METADATA_PATH,), policy)
        value = json.loads(assembly.documents[0].content)
    except ContextAssemblyError as error:
        raise MemoryError(error.code, error.detail) from error
    except (IndexError, json.JSONDecodeError) as error:
        raise MemoryError("memory.metadata_invalid", "memory metadata is invalid") from error
    if not isinstance(value, dict) or value.get("format") != "peoplebot.instance-memory.v0":
        raise MemoryError("memory.metadata_invalid", "memory metadata format is invalid")
    return value


def _validate_metadata_binding(
    metadata: Mapping[str, Any],
    repository: str,
    environment_id: str,
    instance_id: str,
    blueprint: StateRef,
) -> None:
    if (
        metadata.get("repository") != repository
        or metadata.get("environment_id") != environment_id
        or metadata.get("instance_id") != instance_id
        or metadata.get("blueprint") != blueprint.to_dict()
        or metadata.get("ref_name") != instance_memory_ref(environment_id, instance_id)
    ):
        raise MemoryError(
            "memory.identity_mismatch",
            (
                "expected memory State is bound to another repository, environment, "
                "Instance, or Blueprint State"
            ),
        )


def assemble_instance_memory_context(
    checkout: str | Path,
    state: StateRef,
    environment_id: str,
    instance_id: str,
    blueprint: StateRef,
    requested_paths: Sequence[str],
    policy: ContextPolicy,
) -> ContextAssembly:
    """Retrieve explicitly selected memory items from one exact checkpoint State."""

    metadata = read_instance_memory_metadata(checkout, state)
    if not isinstance(blueprint, StateRef):
        raise ValueError("blueprint must be a StateRef")
    _validate_metadata_binding(
        metadata,
        state.repository,
        environment_id,
        instance_id,
        blueprint,
    )
    available = {
        item.get("path")
        for item in metadata.get("items", ())
        if isinstance(item, Mapping)
    }
    canonical = tuple(_memory_path(path) for path in requested_paths)
    if any(path not in available for path in canonical):
        raise MemoryError(
            "memory.path_unavailable",
            "requested memory path is not present in the exact checkpoint",
        )
    try:
        return assemble_context(
            checkout,
            state,
            tuple(f"{_CONTENT_DIRECTORY}/{path}" for path in canonical),
            policy,
        )
    except ContextAssemblyError as error:
        raise MemoryError(error.code, error.detail) from error


def run_instance_memory_execution(
    runtime_root: str | Path,
    evidence_store: AttemptEvidenceStore,
    memory_store: GitMemoryStore,
    start: ExecutionStart,
    request: MemoryCheckpointRequest,
    finished_at: Callable[[], str],
) -> MemoryExecutionResult:
    """Checkpoint memory inside existing admission and Execution provenance."""

    if (
        start.environment_id != request.environment_id
        or start.instance_id != request.instance_id
        or start.starting_state != request.expected_state
        or start.blueprint != request.blueprint
    ):
        raise ValueError("Execution start does not match the memory checkpoint request")
    if not callable(finished_at):
        raise ValueError("finished_at must be callable")
    checkpoint: MemoryCheckpointResult | None = None

    def record(
        status: ExecutionStatus,
        *,
        resulting_state: StateRef | None = None,
        outcome: TerminalOutcome | None = None,
        artifacts: tuple[StateRef, ...] = (),
    ) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=start.execution_id,
            environment_id=start.environment_id,
            instance_id=start.instance_id,
            objective=start.objective,
            started_at=start.started_at,
            finished_at=finished_at(),
            starting_state=start.starting_state,
            blueprint=start.blueprint,
            adapter=start.adapter,
            status=status,
            procedures=start.procedures,
            input_states=start.input_states,
            input_messages=start.input_messages,
            resulting_state=resulting_state,
            terminal_outcome=outcome,
            artifacts=artifacts,
        )

    def task() -> ExecutionRecord:
        nonlocal checkpoint
        checkpoint = memory_store._checkpoint(request)
        return record(
            ExecutionStatus.COMPLETED if checkpoint.changed else ExecutionStatus.NO_CHANGE,
            resulting_state=checkpoint.state,
        )

    def failure(error: Exception) -> ExecutionRecord:
        if checkpoint is not None:
            outcome = TerminalOutcome(
                "memory.record_failed_after_checkpoint",
                (
                    "terminal record construction failed after the memory checkpoint "
                    f"with {type(error).__module__}.{type(error).__qualname__}"
                ),
            )
            artifacts = (checkpoint.state,)
        elif isinstance(error, MemoryError):
            outcome = TerminalOutcome(error.code, error.detail)
            artifacts = ()
        else:
            outcome = TerminalOutcome(
                "memory.checkpoint_failed",
                f"memory checkpoint raised {type(error).__module__}.{type(error).__qualname__}",
            )
            artifacts = ()
        return record(ExecutionStatus.FAILED, outcome=outcome, artifacts=artifacts)

    provenance = run_with_execution_provenance(
        runtime_root,
        evidence_store,
        start,
        task,
        failure,
    )
    return MemoryExecutionResult(checkpoint, provenance)
