"""One finite, policy-bound, message-driven PeopleBot work-cycle tick."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from ._json import stable_json_bytes
from .admission import try_acquire_execution
from .execution import _require_text, _utc_timestamp
from .messaging import (
    Message,
    MessageError,
    MessageKind,
    OutboundDestination,
    OutboundMessageStore,
    PeerSource,
    PublicationDisposition,
    PublishedMessage,
    append_and_publish_owned_message,
    read_peer_messages,
)
from .provenance import GitAttemptStore, ProvenanceError
from .state import StateRef, StateResolutionError, resolve_state


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_PROGRESS_REF_PREFIX = "refs/peoplebot/message-readers/v0/"
_ZERO_OBJECT_ID = "0" * 40
_MAX_RECORDS = 256
_MAX_SOURCES = 8
_MAX_CONFIGURATION_BYTES = 65_536


def _identifier(value: str, field: str) -> None:
    _require_text(value, field)
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} is not a bounded v0 identifier")


def reader_progress_ref(environment_id: str, instance_id: str) -> str:
    _identifier(environment_id, "environment_id")
    _identifier(instance_id, "instance_id")
    digest = hashlib.sha256(
        b"peoplebot.reader-progress.v0\0"
        + environment_id.encode("utf-8")
        + b"\0"
        + instance_id.encode("utf-8")
    ).hexdigest()
    return f"{_PROGRESS_REF_PREFIX}{digest}"


class TaskDisposition(StrEnum):
    CLAIMED = "claimed"
    COMPLETED = "completed"
    FAILED = "failed"
    UNRESOLVED = "unresolved"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class SourceCursor:
    repository: str
    ref_name: str
    commit: str

    def __post_init__(self) -> None:
        _require_text(self.repository, "cursor repository")
        _require_text(self.ref_name, "cursor ref")
        if not _OBJECT_ID.fullmatch(self.commit):
            raise ValueError("cursor commit must be a full object ID")

    def to_dict(self) -> dict[str, str]:
        return {
            "commit": self.commit,
            "ref_name": self.ref_name,
            "repository": self.repository,
        }


@dataclass(frozen=True, slots=True)
class TaskProgress:
    message_state: StateRef
    message_id: str
    task_id: str
    disposition: TaskDisposition
    reply_state: StateRef | None = None
    memory_state: StateRef | None = None

    def __post_init__(self) -> None:
        _identifier(self.message_id, "message_id")
        _identifier(self.task_id, "task_id")
        if self.message_state.path != "message.json":
            raise ValueError("message_state must select message.json")
        if not isinstance(self.disposition, TaskDisposition):
            raise ValueError("disposition must be a TaskDisposition")
        for name, value in (("reply_state", self.reply_state), ("memory_state", self.memory_state)):
            if value is not None and not isinstance(value, StateRef):
                raise ValueError(f"{name} must be a StateRef or null")

    def to_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "memory_state": self.memory_state.to_dict() if self.memory_state else None,
            "message_id": self.message_id,
            "message_state": self.message_state.to_dict(),
            "reply_state": self.reply_state.to_dict() if self.reply_state else None,
            "task_id": self.task_id,
        }


@dataclass(frozen=True, slots=True)
class ReaderReconciliation:
    reconciliation_id: str
    prior_progress_state: StateRef
    prior_task: TaskProgress
    terminal_code: str
    evidence_states: tuple[StateRef, ...]
    reconciled_at: str

    def __post_init__(self) -> None:
        _identifier(self.reconciliation_id, "reconciliation_id")
        if self.prior_progress_state.path != "reader-progress.json":
            raise ValueError("prior_progress_state must select reader-progress.json")
        if self.prior_task.disposition is not TaskDisposition.UNRESOLVED:
            raise ValueError("only a known unresolved task can be reconciled")
        _identifier(self.terminal_code, "terminal_code")
        if not self.evidence_states or len(self.evidence_states) > 8:
            raise ValueError("reconciliation requires one to eight evidence States")
        if len(set(self.evidence_states)) != len(self.evidence_states):
            raise ValueError("reconciliation evidence States must be unique")
        _utc_timestamp(self.reconciled_at, "reconciled_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_states": [item.to_dict() for item in self.evidence_states],
            "prior_progress_state": self.prior_progress_state.to_dict(),
            "prior_task": self.prior_task.to_dict(),
            "reconciled_at": self.reconciled_at,
            "reconciliation_id": self.reconciliation_id,
            "terminal_code": self.terminal_code,
        }


@dataclass(frozen=True, slots=True)
class ReaderProgress:
    environment_id: str
    instance_id: str
    cursors: tuple[SourceCursor, ...] = ()
    tasks: tuple[TaskProgress, ...] = ()
    reconciliations: tuple[ReaderReconciliation, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.environment_id, "environment_id")
        _identifier(self.instance_id, "instance_id")
        if len(self.cursors) > _MAX_SOURCES:
            raise ValueError("reader progress exceeds eight sources")
        if len(self.tasks) > _MAX_RECORDS:
            raise ValueError("reader progress exceeds 256 tasks")
        if len({(item.repository, item.ref_name) for item in self.cursors}) != len(
            self.cursors
        ):
            raise ValueError("reader cursors must be unique")
        if len({item.message_id for item in self.tasks}) != len(self.tasks):
            raise ValueError("reader task messages must be unique")
        if len(self.reconciliations) > _MAX_RECORDS:
            raise ValueError("reader progress exceeds 256 reconciliations")
        if len({item.reconciliation_id for item in self.reconciliations}) != len(
            self.reconciliations
        ):
            raise ValueError("reader reconciliations must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cursors": [item.to_dict() for item in self.cursors],
            "environment_id": self.environment_id,
            "format": "peoplebot.reader-progress.v0",
            "instance_id": self.instance_id,
            "reconciliations": [item.to_dict() for item in self.reconciliations],
            "tasks": [item.to_dict() for item in self.tasks],
        }

    def to_json_bytes(self) -> bytes:
        return stable_json_bytes(self.to_dict())


def _state(value: object) -> StateRef | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"repository", "commit", "path"}:
        raise ValueError("progress State fields are invalid")
    return StateRef(value["repository"], value["commit"], value["path"])  # type: ignore[arg-type]


def _task_progress(value: object) -> TaskProgress:
    if not isinstance(value, Mapping) or set(value) != {
        "disposition", "memory_state", "message_id", "message_state", "reply_state", "task_id"
    }:
        raise ValueError("task progress fields are invalid")
    try:
        disposition = TaskDisposition(value["disposition"])
    except (TypeError, ValueError) as error:
        raise ValueError("task progress disposition is invalid") from error
    message_state = _state(value["message_state"])
    if message_state is None:
        raise ValueError("task progress message State is required")
    return TaskProgress(
        message_state,
        value["message_id"],  # type: ignore[arg-type]
        value["task_id"],  # type: ignore[arg-type]
        disposition,
        _state(value["reply_state"]),
        _state(value["memory_state"]),
    )


def _progress(value: object) -> ReaderProgress:
    legacy_fields = {"cursors", "environment_id", "format", "instance_id", "tasks"}
    if not isinstance(value, Mapping) or set(value) not in {
        frozenset(legacy_fields), frozenset((*legacy_fields, "reconciliations"))
    }:
        raise ValueError("reader progress fields are invalid")
    if value.get("format") != "peoplebot.reader-progress.v0":
        raise ValueError("reader progress format is invalid")
    cursors = value.get("cursors")
    tasks = value.get("tasks")
    if not isinstance(cursors, list) or not isinstance(tasks, list):
        raise ValueError("reader progress collections are invalid")
    parsed_cursors = []
    for item in cursors:
        if not isinstance(item, Mapping) or set(item) != {"commit", "ref_name", "repository"}:
            raise ValueError("reader cursor fields are invalid")
        parsed_cursors.append(
            SourceCursor(item["repository"], item["ref_name"], item["commit"])  # type: ignore[arg-type]
        )
    parsed_tasks = [_task_progress(item) for item in tasks]
    reconciliations = value.get("reconciliations", [])
    if not isinstance(reconciliations, list):
        raise ValueError("reader reconciliations must be an array")
    parsed_reconciliations = []
    for item in reconciliations:
        if not isinstance(item, Mapping) or set(item) != {
            "evidence_states", "prior_progress_state", "prior_task", "reconciled_at",
            "reconciliation_id", "terminal_code",
        }:
            raise ValueError("reader reconciliation fields are invalid")
        prior_progress = _state(item["prior_progress_state"])
        evidence = item.get("evidence_states")
        if prior_progress is None or not isinstance(evidence, list):
            raise ValueError("reader reconciliation States are invalid")
        parsed_evidence = tuple(_state(state) for state in evidence)
        if any(state is None for state in parsed_evidence):
            raise ValueError("reader reconciliation evidence State is required")
        parsed_reconciliations.append(ReaderReconciliation(
            item["reconciliation_id"],  # type: ignore[arg-type]
            prior_progress,
            _task_progress(item["prior_task"]),
            item["terminal_code"],  # type: ignore[arg-type]
            parsed_evidence,  # type: ignore[arg-type]
            item["reconciled_at"],  # type: ignore[arg-type]
        ))
    return ReaderProgress(
        value.get("environment_id"),  # type: ignore[arg-type]
        value.get("instance_id"),  # type: ignore[arg-type]
        tuple(parsed_cursors),
        tuple(parsed_tasks),
        tuple(parsed_reconciliations),
    )


@dataclass(frozen=True, slots=True)
class ProgressState:
    progress: ReaderProgress
    state: StateRef
    ref_name: str


class ReaderProgressStore:
    def __init__(self, checkout: str | Path, repository: str, ref_name: str) -> None:
        if not ref_name.startswith(_PROGRESS_REF_PREFIX):
            raise ValueError("reader progress ref is invalid")
        self._plumbing = GitAttemptStore(checkout, repository)
        self.checkout = self._plumbing.checkout
        self.repository = repository
        self.ref_name = ref_name

    def _current(self) -> str | None:
        symbolic = self._plumbing._git("symbolic-ref", "--quiet", "--no-recurse", self.ref_name)
        if symbolic.returncode == 0:
            raise CycleError("cycle.progress_ref_symbolic", "reader progress ref is symbolic")
        if symbolic.returncode != 1:
            raise CycleError("cycle.progress_inspection_failed", "reader progress identity is unknown")
        value = self._plumbing._git("rev-parse", "--verify", "--quiet", self.ref_name)
        if value.returncode == 1:
            return None
        commit = value.stdout.decode("ascii", "replace").strip()
        if value.returncode != 0 or not _OBJECT_ID.fullmatch(commit):
            raise CycleError("cycle.progress_inspection_failed", "reader progress ref is invalid")
        return commit

    def load(self) -> ProgressState | None:
        commit = self._current()
        if commit is None:
            return None
        state = StateRef(self.repository, commit, "reader-progress.json")
        try:
            resolved = resolve_state(self.checkout, state)
        except StateResolutionError as error:
            raise CycleError(error.code, error.detail) from error
        tree = self._plumbing._git("ls-tree", "-rz", "--full-tree", commit)
        expected_tree = (
            f"100644 blob {resolved.selected_object}\treader-progress.json\0".encode("ascii")
        )
        if tree.returncode != 0 or tree.stdout != expected_tree:
            raise CycleError(
                "cycle.progress_invalid", "reader progress commit tree is not progress-only"
            )
        blob = self._plumbing._git("cat-file", "blob", resolved.selected_object)
        if blob.returncode != 0 or len(blob.stdout) > _MAX_CONFIGURATION_BYTES:
            raise CycleError("cycle.progress_invalid", "reader progress blob is unavailable or oversized")
        try:
            value = json.loads(blob.stdout)
            progress = _progress(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise CycleError("cycle.progress_invalid", "reader progress is malformed") from error
        return ProgressState(progress, state, self.ref_name)

    def save(
        self,
        progress: ReaderProgress,
        expected: ProgressState | None,
        saved_at: str,
    ) -> ProgressState:
        _utc_timestamp(saved_at, "saved_at")
        if expected is not None and expected.state.repository != self.repository:
            raise ValueError("expected progress belongs to another repository")
        parent = expected.state.commit if expected else None
        try:
            blob = self._plumbing._write_blob(progress.to_json_bytes())
            tree = self._plumbing._object_id(
                self._plumbing._git(
                    "mktree",
                    input_bytes=f"100644 blob {blob}\treader-progress.json\n".encode("ascii"),
                ),
                "write reader progress tree",
            )
            commit = self._plumbing._write_commit(
                tree,
                (parent,) if parent else (),
                saved_at,
                "PeopleBot reader progress",
            )
            self._plumbing._update_ref(
                self.ref_name,
                commit,
                parent or _ZERO_OBJECT_ID,
                reflog_message="peoplebot reader progress v0",
                conflict_code="cycle.progress_conflict",
                conflict_detail="reader progress changed",
                symbolic_code="cycle.progress_ref_symbolic",
                symbolic_detail="reader progress ref is symbolic",
                inspection_code="cycle.progress_inspection_failed",
                inspection_detail="reader progress identity is unknown",
                persistence_code="cycle.progress_persistence_failed",
                persistence_detail="reader progress could not be attached",
            )
        except ProvenanceError as error:
            raise CycleError(error.code, error.detail) from error
        return ProgressState(
            progress,
            StateRef(self.repository, commit, "reader-progress.json"),
            self.ref_name,
        )


@dataclass(frozen=True, slots=True)
class TaskRoute:
    purpose: str
    handler: str

    def __post_init__(self) -> None:
        _identifier(self.purpose, "route purpose")
        _identifier(self.handler, "route handler")


@dataclass(frozen=True, slots=True)
class TaskPolicy:
    routes: tuple[TaskRoute, ...]
    maximum_completed_tasks: int
    allow_stop_messages: bool = True

    def __post_init__(self) -> None:
        if not self.routes or len(self.routes) > 16:
            raise ValueError("task policy requires one to sixteen routes")
        if len({item.purpose for item in self.routes}) != len(self.routes):
            raise ValueError("task policy purposes must be unique")
        if isinstance(self.maximum_completed_tasks, bool) or not (
            1 <= self.maximum_completed_tasks <= 256
        ):
            raise ValueError("maximum_completed_tasks must be an integer from 1 to 256")
        if not isinstance(self.allow_stop_messages, bool):
            raise ValueError("allow_stop_messages must be a boolean")

    def handler_for(self, purpose: str) -> str | None:
        return next((item.handler for item in self.routes if item.purpose == purpose), None)


@dataclass(frozen=True, slots=True)
class TaskHandlerResult:
    disposition: TaskDisposition
    reply_content: str
    states: tuple[StateRef, ...] = ()
    memory_items: tuple[tuple[str, str], ...] = ()
    provider_invoked: bool = False

    def __post_init__(self) -> None:
        if self.disposition not in {
            TaskDisposition.COMPLETED,
            TaskDisposition.FAILED,
            TaskDisposition.UNRESOLVED,
            TaskDisposition.STOPPED,
        }:
            raise ValueError("handler disposition is invalid")
        _require_text(self.reply_content, "reply_content")
        if len(self.reply_content.encode("utf-8")) > 4096:
            raise ValueError("reply_content exceeds 4096 bytes")
        if len(self.states) > 8 or not all(isinstance(item, StateRef) for item in self.states):
            raise ValueError("handler states are invalid")
        if len(self.memory_items) > 16:
            raise ValueError("handler memory items exceed 16")
        for path, content in self.memory_items:
            _require_text(path, "memory item path")
            if not isinstance(content, str) or not content or "\0" in content:
                raise ValueError("memory item content must be non-empty UTF-8 text without NUL")
            if len(content.encode("utf-8")) > 65_536:
                raise ValueError("memory item content exceeds 65536 bytes")
        if not isinstance(self.provider_invoked, bool):
            raise ValueError("provider_invoked must be a boolean")


TaskHandler = Callable[[Message], TaskHandlerResult]
MemoryCheckpoint = Callable[[PublishedMessage, TaskHandlerResult], StateRef | None]


@dataclass(frozen=True, slots=True)
class CycleBindings:
    environment_id: str
    instance_id: str
    runtime_root: Path
    local_checkout: Path
    local_repository: str
    outbound_ref: str
    destination: OutboundDestination
    sources: tuple[PeerSource, ...]
    progress_ref: str
    status_path: Path
    stop_path: Path

    def __post_init__(self) -> None:
        _identifier(self.environment_id, "environment_id")
        _identifier(self.instance_id, "instance_id")
        for field in ("runtime_root", "local_checkout", "status_path", "stop_path"):
            if not getattr(self, field).is_absolute():
                raise ValueError(f"{field} must be absolute")
        _require_text(self.local_repository, "local_repository")
        if self.destination.repository != self.local_repository:
            raise ValueError("outbound destination must use the local repository identity")
        if self.destination.ref_name != self.outbound_ref:
            raise ValueError("destination ref must equal the local outbound ref")
        if not self.sources or len(self.sources) > _MAX_SOURCES:
            raise ValueError("cycle requires one to eight peer sources")
        if self.progress_ref != reader_progress_ref(self.environment_id, self.instance_id):
            raise ValueError("progress_ref does not match environment and Instance")


@dataclass(frozen=True, slots=True)
class CycleStatus:
    execution_id: str
    code: str
    disposition: str
    work_invoked: bool
    provider_invoked: bool
    message_state: StateRef | None = None
    reply_state: StateRef | None = None
    memory_state: StateRef | None = None
    progress_state: StateRef | None = None
    handler: str | None = None
    original_code: str | None = None
    original_disposition: str | None = None
    terminal_progress_persisted: bool | None = None
    status_persisted: bool = True

    def __post_init__(self) -> None:
        _identifier(self.execution_id, "status execution_id")
        if self.original_code is not None:
            _identifier(self.original_code, "status original_code")
        if self.original_disposition is not None:
            _identifier(self.original_disposition, "status original_disposition")
        if self.terminal_progress_persisted is not None and not isinstance(
            self.terminal_progress_persisted, bool
        ):
            raise ValueError("terminal_progress_persisted must be a boolean or null")
        if not isinstance(self.status_persisted, bool):
            raise ValueError("status_persisted must be a boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "disposition": self.disposition,
            "execution_id": self.execution_id,
            "format": "peoplebot.work-cycle-status.v0",
            "handler": self.handler,
            "memory_state": self.memory_state.to_dict() if self.memory_state else None,
            "message_state": self.message_state.to_dict() if self.message_state else None,
            "original_code": self.original_code,
            "original_disposition": self.original_disposition,
            "progress_state": self.progress_state.to_dict() if self.progress_state else None,
            "provider_invoked": self.provider_invoked,
            "reply_state": self.reply_state.to_dict() if self.reply_state else None,
            "status_persisted": self.status_persisted,
            "terminal_progress_persisted": self.terminal_progress_persisted,
            "work_invoked": self.work_invoked,
        }


class CycleError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _read_configuration(path: str | Path, label: str) -> object:
    source = Path(path)
    if not source.is_absolute():
        raise ValueError(f"{label} path must be absolute")
    try:
        content = source.read_bytes()
    except OSError as error:
        raise ValueError(f"{label} is unavailable") from error
    if not content or len(content) > _MAX_CONFIGURATION_BYTES:
        raise ValueError(f"{label} must contain 1-65536 bytes")
    try:
        return json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error


def _configured_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty path string")
    return Path(value)


def load_cycle_bindings(path: str | Path) -> CycleBindings:
    """Load strict caller-owned local and remote bindings from JSON."""

    value = _read_configuration(path, "cycle bindings")
    fields = {
        "destination",
        "environment_id",
        "format",
        "instance_id",
        "local_checkout",
        "local_repository",
        "outbound_ref",
        "progress_ref",
        "runtime_root",
        "sources",
        "status_path",
        "stop_path",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("cycle bindings fields are invalid")
    if value.get("format") != "peoplebot.cycle-bindings.v0":
        raise ValueError("cycle bindings format is invalid")

    endpoint_fields = {"expected_url", "ref_name", "remote", "repository"}
    destination = value.get("destination")
    if not isinstance(destination, Mapping) or set(destination) != endpoint_fields:
        raise ValueError("cycle destination fields are invalid")
    sources = value.get("sources")
    if not isinstance(sources, list):
        raise ValueError("cycle sources must be an array")
    parsed_sources: list[PeerSource] = []
    for source in sources:
        if not isinstance(source, Mapping) or set(source) != endpoint_fields | {"allowed_senders"}:
            raise ValueError("cycle source fields are invalid")
        allowed = source.get("allowed_senders")
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            raise ValueError("cycle source allowed_senders must be an array of strings")
        parsed_sources.append(
            PeerSource(
                source.get("repository"),  # type: ignore[arg-type]
                source.get("remote"),  # type: ignore[arg-type]
                source.get("expected_url"),  # type: ignore[arg-type]
                source.get("ref_name"),  # type: ignore[arg-type]
                tuple(allowed),
            )
        )
    return CycleBindings(
        environment_id=value.get("environment_id"),  # type: ignore[arg-type]
        instance_id=value.get("instance_id"),  # type: ignore[arg-type]
        runtime_root=_configured_path(value.get("runtime_root"), "runtime_root"),
        local_checkout=_configured_path(value.get("local_checkout"), "local_checkout"),
        local_repository=value.get("local_repository"),  # type: ignore[arg-type]
        outbound_ref=value.get("outbound_ref"),  # type: ignore[arg-type]
        destination=OutboundDestination(
            destination.get("repository"),  # type: ignore[arg-type]
            destination.get("remote"),  # type: ignore[arg-type]
            destination.get("expected_url"),  # type: ignore[arg-type]
            destination.get("ref_name"),  # type: ignore[arg-type]
        ),
        sources=tuple(parsed_sources),
        progress_ref=value.get("progress_ref"),  # type: ignore[arg-type]
        status_path=_configured_path(value.get("status_path"), "status_path"),
        stop_path=_configured_path(value.get("stop_path"), "stop_path"),
    )


def load_task_policy(path: str | Path) -> TaskPolicy:
    """Load a strict finite purpose-to-local-handler allowlist from JSON."""

    value = _read_configuration(path, "task policy")
    if not isinstance(value, Mapping) or set(value) != {
        "allow_stop_messages",
        "format",
        "maximum_completed_tasks",
        "routes",
    }:
        raise ValueError("task policy fields are invalid")
    if value.get("format") != "peoplebot.task-policy.v0":
        raise ValueError("task policy format is invalid")
    routes = value.get("routes")
    if not isinstance(routes, list):
        raise ValueError("task policy routes must be an array")
    parsed_routes: list[TaskRoute] = []
    for route in routes:
        if not isinstance(route, Mapping) or set(route) != {"handler", "purpose"}:
            raise ValueError("task policy route fields are invalid")
        parsed_routes.append(
            TaskRoute(
                route.get("purpose"),  # type: ignore[arg-type]
                route.get("handler"),  # type: ignore[arg-type]
            )
        )
    maximum = value.get("maximum_completed_tasks")
    allow_stop = value.get("allow_stop_messages")
    if isinstance(maximum, bool) or not isinstance(maximum, int):
        raise ValueError("maximum_completed_tasks must be an integer")
    if not isinstance(allow_stop, bool):
        raise ValueError("allow_stop_messages must be a boolean")
    return TaskPolicy(
        tuple(parsed_routes),
        maximum,
        allow_stop,
    )


def _replace_progress(
    progress: ReaderProgress,
    *,
    cursors: tuple[SourceCursor, ...] | None = None,
    task: TaskProgress | None = None,
    reconciliation: ReaderReconciliation | None = None,
) -> ReaderProgress:
    tasks = list(progress.tasks)
    if task is not None:
        tasks = [item for item in tasks if item.message_id != task.message_id]
        tasks.append(task)
    reconciliations = progress.reconciliations
    if reconciliation is not None:
        reconciliations = (*reconciliations, reconciliation)
    return ReaderProgress(
        progress.environment_id,
        progress.instance_id,
        progress.cursors if cursors is None else cursors,
        tuple(tasks),
        reconciliations,
    )


@dataclass(frozen=True, slots=True)
class ReaderReconciliationResult:
    code: str
    reconciled: bool
    prior_progress_state: StateRef | None
    progress_state: StateRef | None
    reconciliation: ReaderReconciliation | None
    provider_invoked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "format": "peoplebot.reader-reconciliation-result.v0",
            "prior_progress_state": (
                self.prior_progress_state.to_dict() if self.prior_progress_state else None
            ),
            "progress_state": self.progress_state.to_dict() if self.progress_state else None,
            "provider_invoked": self.provider_invoked,
            "reconciled": self.reconciled,
            "reconciliation": (
                self.reconciliation.to_dict() if self.reconciliation else None
            ),
        }


def reconcile_reader_task(
    bindings: CycleBindings,
    reconciliation: ReaderReconciliation,
    execution_id: str,
    evidence_check: Callable[[], None],
) -> ReaderReconciliationResult:
    """Close one exact known terminal failure without erasing its reader history."""

    _identifier(execution_id, "execution_id")
    if (
        reconciliation.prior_progress_state.repository != bindings.local_repository
        or reconciliation.prior_task.message_state.repository != bindings.local_repository
    ):
        raise ValueError("reconciliation States do not match cycle repository")
    logical_instance = f"work-cycle-owner:{bindings.instance_id}"
    owner = try_acquire_execution(
        bindings.runtime_root, bindings.environment_id, logical_instance, execution_id
    )
    if not owner.acquired:
        return ReaderReconciliationResult(
            "cycle.reconciliation_busy", False, None, None, None
        )
    assert owner.admission is not None
    task_owner = None
    try:
        attempt = try_acquire_execution(
            bindings.runtime_root,
            bindings.environment_id,
            bindings.instance_id,
            execution_id,
        )
        if not attempt.acquired:
            return ReaderReconciliationResult(
                "cycle.reconciliation_busy", False, None, None, None
            )
        assert attempt.admission is not None
        task_owner = attempt.admission
        if bindings.stop_path.exists():
            return ReaderReconciliationResult(
                "cycle.reconciliation_stopped", False, None, None, None
            )
        store = ReaderProgressStore(
            bindings.local_checkout, bindings.local_repository, bindings.progress_ref
        )
        current = store.load()
        if current is not None:
            existing = tuple(
                item for item in current.progress.reconciliations
                if item.reconciliation_id == reconciliation.reconciliation_id
            )
            if existing:
                terminal = next(
                    (
                        item for item in current.progress.tasks
                        if item.message_id == reconciliation.prior_task.message_id
                    ),
                    None,
                )
                if (
                    len(existing) == 1
                    and existing[0] == reconciliation
                    and terminal == replace(
                        reconciliation.prior_task, disposition=TaskDisposition.FAILED
                    )
                ):
                    return ReaderReconciliationResult(
                        "cycle.reconciliation_already_applied",
                        True,
                        reconciliation.prior_progress_state,
                        current.state,
                        reconciliation,
                    )
                raise CycleError(
                    "cycle.reconciliation_conflict",
                    "reconciliation identity already exists with different State",
                )
        if current is None or current.state != reconciliation.prior_progress_state:
            raise CycleError(
                "cycle.reconciliation_progress_changed",
                "reader progress differs from the exact reviewed prior State",
            )
        barriers = tuple(
            item for item in current.progress.tasks
            if item.disposition in {
                TaskDisposition.CLAIMED,
                TaskDisposition.UNRESOLVED,
                TaskDisposition.STOPPED,
            }
        )
        if barriers != (reconciliation.prior_task,):
            raise CycleError(
                "cycle.reconciliation_task_changed",
                "the exact unresolved task is not the sole reader barrier",
            )
        evidence_check()
        terminal = replace(
            reconciliation.prior_task, disposition=TaskDisposition.FAILED
        )
        updated = _replace_progress(
            current.progress, task=terminal, reconciliation=reconciliation
        )
        saved = store.save(updated, current, reconciliation.reconciled_at)
        return ReaderReconciliationResult(
            "cycle.reconciled_terminal_failure",
            True,
            current.state,
            saved.state,
            reconciliation,
        )
    finally:
        if task_owner is not None:
            task_owner.release()
        owner.admission.release()


def persist_cycle_status(path: str | Path, status: CycleStatus) -> None:
    target = Path(path)
    if not target.is_absolute():
        raise ValueError("status path must be absolute")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_bytes(stable_json_bytes(status.to_dict()))
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _persist_terminal_status(status_path: Path, status: CycleStatus) -> CycleStatus:
    """Persist once, or return a bounded non-durable current-run diagnostic."""

    try:
        persist_cycle_status(status_path, status)
        return status
    except OSError:
        progress_failed = status.terminal_progress_persisted is False
        return CycleStatus(
            execution_id=status.execution_id,
            code=(
                "cycle.progress_and_status_persist_failed"
                if progress_failed
                else "cycle.status_persist_failed"
            ),
            disposition="unresolved",
            work_invoked=status.work_invoked,
            provider_invoked=status.provider_invoked,
            message_state=status.message_state,
            reply_state=status.reply_state,
            memory_state=status.memory_state,
            progress_state=status.progress_state,
            handler=status.handler,
            original_code=status.original_code or status.code,
            original_disposition=(
                status.original_disposition or status.disposition
            ),
            terminal_progress_persisted=status.terminal_progress_persisted,
            status_persisted=False,
        )


def _reply_message(
    bindings: CycleBindings,
    published: PublishedMessage,
    result: TaskHandlerResult,
    created_at: str,
    memory_state: StateRef | None,
) -> Message:
    request = published.message
    digest = hashlib.sha256(
        b"peoplebot.reply.v0\0"
        + bindings.environment_id.encode("utf-8")
        + b"\0"
        + request.message_id.encode("utf-8")
        + b"\0"
        + result.disposition.value.encode("ascii")
    ).hexdigest()
    states = list(result.states)
    if memory_state is not None and memory_state not in states:
        states.append(memory_state)
    return Message(
        message_id=f"reply-{digest}",
        kind=MessageKind.REPLY,
        sender=bindings.environment_id,
        recipient=request.sender,
        task_id=request.task_id,
        correlation_id=request.correlation_id,
        purpose=f"{request.purpose}.result",
        content=result.reply_content,
        created_at=created_at,
        reply_to=request.message_id,
        states=tuple(states),
    )


def _run_owned_work_cycle_tick(
    bindings: CycleBindings,
    policy: TaskPolicy,
    handlers: Mapping[str, TaskHandler],
    execution_id: str,
    started_at: str,
    finished_at: str,
    *,
    memory_checkpoint: MemoryCheckpoint | None = None,
) -> CycleStatus:
    """Run at most one locally authorized task, then publish one correlated reply."""

    _identifier(execution_id, "execution_id")
    started = _utc_timestamp(started_at, "started_at")
    finished = _utc_timestamp(finished_at, "finished_at")
    if finished < started:
        raise ValueError("finished_at must not precede started_at")
    if bindings.stop_path.exists():
        status = CycleStatus(execution_id, "cycle.stopped", "stopped", False, False)
        persist_cycle_status(bindings.status_path, status)
        return status

    attempt = try_acquire_execution(
        bindings.runtime_root,
        bindings.environment_id,
        bindings.instance_id,
        execution_id,
    )
    if not attempt.acquired:
        status = CycleStatus(execution_id, "cycle.busy", "busy", False, False)
        persist_cycle_status(bindings.status_path, status)
        return status
    assert attempt.admission is not None
    progress_store = ReaderProgressStore(
        bindings.local_checkout, bindings.local_repository, bindings.progress_ref
    )
    claimed_state: ProgressState | None = None
    selected: PublishedMessage | None = None
    handler_name: str | None = None
    handler_result: TaskHandlerResult | None = None
    try:
        current = progress_store.load()
        progress = current.progress if current else ReaderProgress(
            bindings.environment_id, bindings.instance_id
        )
        if current is not None and (
            progress.environment_id != bindings.environment_id
            or progress.instance_id != bindings.instance_id
        ):
            raise CycleError(
                "cycle.progress_binding_mismatch",
                "reader progress belongs to another environment or Instance",
            )
        barriers = [
            item
            for item in progress.tasks
            if item.disposition
            in {
                TaskDisposition.CLAIMED,
                TaskDisposition.UNRESOLVED,
                TaskDisposition.STOPPED,
            }
        ]
        if barriers:
            barrier = barriers[0]
            if barrier.disposition is TaskDisposition.STOPPED:
                barrier_code = "cycle.stopped_halt"
                barrier_disposition = "stopped"
            elif barrier.disposition is TaskDisposition.UNRESOLVED:
                barrier_code = "cycle.unresolved_halt"
                barrier_disposition = "unresolved"
            else:
                barrier_code = "cycle.claimed_unresolved"
                barrier_disposition = "unresolved"
            status = CycleStatus(
                execution_id,
                barrier_code,
                barrier_disposition,
                False,
                False,
                message_state=barrier.message_state,
                reply_state=barrier.reply_state,
                memory_state=barrier.memory_state,
                progress_state=current.state if current else None,
            )
            persist_cycle_status(bindings.status_path, status)
            return status
        handled = {item.message_id for item in progress.tasks}
        completed = sum(
            item.disposition in {TaskDisposition.COMPLETED, TaskDisposition.FAILED}
            for item in progress.tasks
        )
        if completed >= policy.maximum_completed_tasks:
            status = CycleStatus(
                execution_id,
                "cycle.exhausted",
                "exhausted",
                False,
                False,
                progress_state=current.state if current else None,
            )
            persist_cycle_status(bindings.status_path, status)
            return status

        cursors: list[SourceCursor] = []
        candidates: list[PublishedMessage] = []
        for source in bindings.sources:
            tip, messages = read_peer_messages(bindings.local_checkout, source)
            if tip is not None:
                cursors.append(SourceCursor(source.repository, source.ref_name, tip))
            for item in messages:
                message = item.message
                if (
                    message.recipient == bindings.environment_id
                    and message.kind in {MessageKind.TASK, MessageKind.STOP}
                    and message.message_id not in handled
                ):
                    candidates.append(item)
        progress = _replace_progress(progress, cursors=tuple(cursors))
        if not candidates:
            saved = (
                current
                if current is not None and progress == current.progress
                else progress_store.save(progress, current, started_at)
            )
            status = CycleStatus(
                execution_id,
                "cycle.idle",
                "idle",
                False,
                False,
                progress_state=saved.state,
            )
            persist_cycle_status(bindings.status_path, status)
            return status
        candidates.sort(
            key=lambda item: (
                _utc_timestamp(item.message.created_at, "message created_at"),
                item.message.message_id,
            )
        )
        selected = candidates[0]
        message = selected.message
        if message.kind is MessageKind.STOP:
            if not policy.allow_stop_messages:
                handler_result = TaskHandlerResult(
                    TaskDisposition.FAILED, "Stop request is not permitted by local policy."
                )
            else:
                handler_result = TaskHandlerResult(
                    TaskDisposition.STOPPED, "Local work cycle stopped by permitted request."
                )
        else:
            handler_name = policy.handler_for(message.purpose)
            handler = handlers.get(handler_name) if handler_name is not None else None
            if handler is None:
                handler_result = TaskHandlerResult(
                    TaskDisposition.FAILED,
                    "No locally approved handler is available for this message purpose.",
                )

        claimed = TaskProgress(
            selected.state,
            message.message_id,
            message.task_id,
            TaskDisposition.CLAIMED,
        )
        claimed_state = progress_store.save(
            _replace_progress(progress, task=claimed), current, started_at
        )
        if handler_result is None:
            assert handler_name is not None
            try:
                handler_result = handlers[handler_name](message)
            except Exception as error:
                handler_result = TaskHandlerResult(
                    TaskDisposition.UNRESOLVED,
                    f"Handler stopped with {type(error).__module__}.{type(error).__qualname__}.",
                )
    except (CycleError, MessageError) as error:
        status = CycleStatus(execution_id, error.code, "unresolved", False, False)
        persist_cycle_status(bindings.status_path, status)
        return status
    finally:
        attempt.admission.release()

    assert selected is not None
    assert handler_result is not None
    memory_state: StateRef | None = None
    if memory_checkpoint is not None and handler_result.memory_items:
        try:
            memory_state = memory_checkpoint(selected, handler_result)
        except Exception as error:
            handler_result = TaskHandlerResult(
                TaskDisposition.UNRESOLVED,
                (
                    "Task result exists but its explicit memory checkpoint stopped with "
                    f"{type(error).__module__}.{type(error).__qualname__}."
                ),
                states=handler_result.states,
                provider_invoked=handler_result.provider_invoked,
            )

    reply = _reply_message(bindings, selected, handler_result, finished_at, memory_state)
    reply_state: StateRef | None = None
    final_disposition = handler_result.disposition
    try:
        result = append_and_publish_owned_message(
            bindings.runtime_root,
            bindings.environment_id,
            f"publish:{execution_id}",
            OutboundMessageStore(
                bindings.local_checkout, bindings.local_repository, bindings.outbound_ref
            ),
            reply,
            bindings.destination,
        )
        reply_state = result.local.state
        if result.publication.disposition is not PublicationDisposition.REMOTE_VERIFIED:
            final_disposition = TaskDisposition.UNRESOLVED
    except (MessageError, ValueError):
        final_disposition = TaskDisposition.UNRESOLVED

    assert claimed_state is not None
    final_task = TaskProgress(
        selected.state,
        selected.message.message_id,
        selected.message.task_id,
        final_disposition,
        reply_state,
        memory_state,
    )
    original_code = f"cycle.{final_disposition.value}"
    try:
        final_progress = progress_store.save(
            _replace_progress(claimed_state.progress, task=final_task),
            claimed_state,
            finished_at,
        )
    except (CycleError, OSError, ValueError):
        status = CycleStatus(
            execution_id=execution_id,
            code="cycle.terminal_progress_persist_failed",
            disposition="unresolved",
            work_invoked=handler_name is not None,
            provider_invoked=handler_result.provider_invoked,
            message_state=selected.state,
            reply_state=reply_state,
            memory_state=memory_state,
            progress_state=claimed_state.state,
            handler=handler_name,
            original_code=original_code,
            original_disposition=final_disposition.value,
            terminal_progress_persisted=False,
        )
        return _persist_terminal_status(bindings.status_path, status)
    status = CycleStatus(
        execution_id=execution_id,
        code=original_code,
        disposition=final_disposition.value,
        work_invoked=handler_name is not None,
        provider_invoked=handler_result.provider_invoked,
        message_state=selected.state,
        reply_state=reply_state,
        memory_state=memory_state,
        progress_state=final_progress.state,
        handler=handler_name,
        terminal_progress_persisted=True,
    )
    return _persist_terminal_status(bindings.status_path, status)


def run_work_cycle_tick(
    bindings: CycleBindings,
    policy: TaskPolicy,
    handlers: Mapping[str, TaskHandler],
    execution_id: str,
    started_at: str,
    finished_at: str,
    *,
    memory_checkpoint: MemoryCheckpoint | None = None,
) -> CycleStatus:
    """Retain one logical tick owner through memory, reply, and final progress."""

    _identifier(execution_id, "execution_id")
    logical_instance = f"work-cycle-owner:{bindings.instance_id}"
    owner = try_acquire_execution(
        bindings.runtime_root,
        bindings.environment_id,
        logical_instance,
        execution_id,
    )
    if not owner.acquired:
        status = CycleStatus(execution_id, "cycle.busy", "busy", False, False)
        persist_cycle_status(bindings.status_path, status)
        return status
    assert owner.admission is not None
    try:
        return _run_owned_work_cycle_tick(
            bindings,
            policy,
            handlers,
            execution_id,
            started_at,
            finished_at,
            memory_checkpoint=memory_checkpoint,
        )
    finally:
        owner.admission.release()
