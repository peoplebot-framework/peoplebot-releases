"""Bounded private implementation/review workflow for approved exact tasks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from ._json import stable_json_bytes
from .adapters.codex_read_only import (
    UNKNOWN_USAGE,
    ProcessOwnershipUnresolved,
    ProcessRunner,
    _codex_environment,
    _run_process,
)
from .adapters.project_review import (
    ProjectReviewAdapter,
    ProjectReviewResponse,
    run_project_review_execution,
)
from .execution import (
    ExecutionRecord,
    ExecutionStatus,
    TerminalOutcome,
    UsageConfidence,
    UsageObservation,
    UsageSource,
)
from .memory import (
    GitMemoryStore,
    MemoryCheckpointRequest,
    MemoryItem,
    assemble_instance_memory_context,
    run_instance_memory_execution,
)
from .messaging import (
    Message,
    MessageError,
    MessageKind,
    OutboundMessageStore,
    PublicationDisposition,
    PublishedMessage,
    append_and_publish_owned_message,
    inspect_remote_tip,
    validate_correlated_reply,
)
from .preparation import ContextPolicy, assemble_context
from .provenance import (
    ExecutionStart,
    GitAttemptStore,
    ProvenanceRunResult,
    run_with_execution_provenance,
)
from .state import (
    _GIT_GLOBAL_OPTIONS,
    _git_environment,
    StateRef,
    StateResolutionError,
    resolve_state,
)
from .work_cycle import (
    CycleBindings,
    CycleStatus,
    ReaderReconciliation,
    ReaderReconciliationResult,
    ReaderProgressStore,
    TaskDisposition,
    TaskHandlerResult,
    TaskPolicy,
    TaskProgress,
    reconcile_reader_task,
    run_work_cycle_tick,
)


_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_PROGRESS_REF_PREFIX = "refs/peoplebot/development/v0/"
_ZERO_OBJECT_ID = "0" * 40
_MAX_JSON_BYTES = 65_536
_MAX_FAILURE_EVENT_LINES = 128
_FAILURE_EVENT_TYPES = frozenset({"error", "turn.failed"})
_PROCESSING_EVENT_TYPES = frozenset(
    {"thread.started", "turn.started", "turn.failed", "turn.completed"}
)
_FAILURE_CLASSIFICATIONS = frozenset(
    {"authentication", "configuration", "model", "network", "service_limit", "runtime", "unknown"}
)
_FAILURE_SOURCES = frozenset(
    {"structured_event", "stderr_metadata", "stdout_metadata", "exit_status"}
)


class DevelopmentError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _text(value: object, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty without surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} contains a control character")
    if len(value.encode("utf-8")) > maximum:
        raise ValueError(f"{field} exceeds {maximum} bytes")
    return value


def _identifier(value: object, field: str) -> str:
    result = _text(value, field, 128)
    if not _IDENTIFIER.fullmatch(result):
        raise ValueError(f"{field} is not a bounded identifier")
    return result


def _object_id(value: object, field: str) -> str:
    result = _text(value, field, 40)
    if not _OBJECT_ID.fullmatch(result):
        raise ValueError(f"{field} must be a lowercase full Git object ID")
    return result


def _path(value: object, field: str) -> str:
    result = _text(value, field, 512)
    candidate = PurePosixPath(result)
    if (
        candidate.is_absolute()
        or result != candidate.as_posix()
        or "\\" in result
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError(f"{field} must be a canonical relative POSIX path")
    return result


def _state(value: object, field: str, *, path_required: bool | None = None) -> StateRef:
    if not isinstance(value, Mapping) or set(value) != {"commit", "path", "repository"}:
        raise ValueError(f"{field} State fields are invalid")
    state = StateRef(value["repository"], value["commit"], value["path"])  # type: ignore[arg-type]
    if path_required is True and state.path is None:
        raise ValueError(f"{field} must select a path")
    if path_required is False and state.path is not None:
        raise ValueError(f"{field} must be repository-level")
    return state


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field, 40)
    if not text.endswith("Z"):
        raise ValueError(f"{field} must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{field} must be an RFC 3339 UTC timestamp") from error
    return parsed


def development_progress_ref(environment_id: str, task_id: str) -> str:
    environment = _identifier(environment_id, "environment_id")
    task = _identifier(task_id, "task_id")
    digest = hashlib.sha256(
        b"peoplebot.development-progress.v0\0"
        + environment.encode("utf-8")
        + b"\0"
        + task.encode("utf-8")
    ).hexdigest()
    return f"{_PROGRESS_REF_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class DevelopmentTask:
    task_id: str
    proposal_id: str
    objective: str
    repository: str
    base_commit: str
    allowed_paths: tuple[str, ...]
    candidate_ref: str
    verification_commands: tuple[tuple[str, ...], ...]
    review_context_paths: tuple[str, ...]
    context_policy_path: str
    maximum_invocations: int
    maximum_corrections: int
    per_invocation_timeout_seconds: int
    maximum_elapsed_seconds: int
    commit_message: str

    def __post_init__(self) -> None:
        _identifier(self.task_id, "task_id")
        _identifier(self.proposal_id, "proposal_id")
        _text(self.objective, "objective", 8192)
        _text(self.repository, "repository", 512)
        _object_id(self.base_commit, "base_commit")
        if not self.allowed_paths or len(self.allowed_paths) > 32:
            raise ValueError("allowed_paths must contain one to 32 paths")
        paths = tuple(_path(value, "allowed path") for value in self.allowed_paths)
        if len(set(paths)) != len(paths):
            raise ValueError("allowed_paths must be unique")
        if not self.candidate_ref.startswith("refs/heads/codex/"):
            raise ValueError("candidate_ref must be an explicit codex private branch ref")
        if not self.verification_commands or len(self.verification_commands) > 8:
            raise ValueError("verification_commands must contain one to eight commands")
        for command in self.verification_commands:
            if not command or len(command) > 16:
                raise ValueError("verification command must contain one to 16 arguments")
            for argument in command:
                _text(argument, "verification argument", 1024)
        if not self.review_context_paths or len(self.review_context_paths) > 16:
            raise ValueError("review_context_paths must contain one to 16 paths")
        for value in self.review_context_paths:
            _path(value, "review context path")
        _path(self.context_policy_path, "context_policy_path")
        for value, field, lower, upper in (
            (self.maximum_invocations, "maximum_invocations", 1, 8),
            (self.maximum_corrections, "maximum_corrections", 0, 3),
            (self.per_invocation_timeout_seconds, "per_invocation_timeout_seconds", 1, 900),
            (self.maximum_elapsed_seconds, "maximum_elapsed_seconds", 1, 3600),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{field} must be an integer from {lower} to {upper}")
        if self.maximum_invocations < 2 + (2 * self.maximum_corrections):
            raise ValueError("maximum_invocations cannot cover the configured review/corrections")
        _text(self.commit_message, "commit_message", 200)

    @property
    def digest(self) -> str:
        return hashlib.sha256(stable_json_bytes(self.to_dict())).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_paths": list(self.allowed_paths),
            "base_commit": self.base_commit,
            "candidate_ref": self.candidate_ref,
            "commit_message": self.commit_message,
            "context_policy_path": self.context_policy_path,
            "format": "peoplebot.development-task.v0",
            "maximum_corrections": self.maximum_corrections,
            "maximum_elapsed_seconds": self.maximum_elapsed_seconds,
            "maximum_invocations": self.maximum_invocations,
            "objective": self.objective,
            "per_invocation_timeout_seconds": self.per_invocation_timeout_seconds,
            "proposal_id": self.proposal_id,
            "repository": self.repository,
            "review_context_paths": list(self.review_context_paths),
            "task_id": self.task_id,
            "verification_commands": [list(command) for command in self.verification_commands],
        }


def development_task_from_dict(value: object) -> DevelopmentTask:
    fields = {
        "allowed_paths", "base_commit", "candidate_ref", "commit_message",
        "context_policy_path", "format", "maximum_corrections",
        "maximum_elapsed_seconds", "maximum_invocations", "objective",
        "per_invocation_timeout_seconds", "proposal_id", "repository",
        "review_context_paths", "task_id", "verification_commands",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("development task fields are invalid")
    if value.get("format") != "peoplebot.development-task.v0":
        raise ValueError("development task format is invalid")
    allowed = value.get("allowed_paths")
    review_paths = value.get("review_context_paths")
    commands = value.get("verification_commands")
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise ValueError("allowed_paths must be an array of strings")
    if not isinstance(review_paths, list) or not all(isinstance(item, str) for item in review_paths):
        raise ValueError("review_context_paths must be an array of strings")
    if not isinstance(commands, list) or not all(
        isinstance(command, list) and all(isinstance(item, str) for item in command)
        for command in commands
    ):
        raise ValueError("verification_commands must be arrays of strings")
    return DevelopmentTask(
        task_id=value["task_id"], proposal_id=value["proposal_id"],  # type: ignore[arg-type]
        objective=value["objective"], repository=value["repository"],  # type: ignore[arg-type]
        base_commit=value["base_commit"], allowed_paths=tuple(allowed),  # type: ignore[arg-type]
        candidate_ref=value["candidate_ref"],  # type: ignore[arg-type]
        verification_commands=tuple(tuple(command) for command in commands),
        review_context_paths=tuple(review_paths),
        context_policy_path=value["context_policy_path"],  # type: ignore[arg-type]
        maximum_invocations=value["maximum_invocations"],  # type: ignore[arg-type]
        maximum_corrections=value["maximum_corrections"],  # type: ignore[arg-type]
        per_invocation_timeout_seconds=value["per_invocation_timeout_seconds"],  # type: ignore[arg-type]
        maximum_elapsed_seconds=value["maximum_elapsed_seconds"],  # type: ignore[arg-type]
        commit_message=value["commit_message"],  # type: ignore[arg-type]
    )


def load_development_task(checkout: str | Path, state: StateRef) -> DevelopmentTask:
    if state.path is None:
        raise ValueError("development task State must select one JSON file")
    context = assemble_context(
        checkout,
        StateRef(state.repository, state.commit),
        (state.path,),
        ContextPolicy(state, 1, _MAX_JSON_BYTES, _MAX_JSON_BYTES),
    )
    try:
        value = json.loads(context.documents[0].to_source_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("development task is not valid UTF-8 JSON") from error
    return development_task_from_dict(value)


@dataclass(frozen=True, slots=True)
class DevelopmentAuthority:
    """Local authority which message prose cannot extend."""

    active: bool
    environment_id: str
    coordinator_instance_id: str
    implementer_instance_id: str
    reviewer_instance_id: str
    framework_checkout: Path
    project_checkout: Path
    runtime_root: Path
    worktree_root: Path
    executable: Path
    codex_home: Path
    task_state: StateRef
    request_state: StateRef
    selected_message_id: str
    development_blueprint: StateRef
    development_adapter: StateRef
    review_blueprint: StateRef
    review_adapter: StateRef

    def __post_init__(self) -> None:
        if not isinstance(self.active, bool):
            raise ValueError("active must be a boolean")
        identities = (
            self.environment_id,
            self.coordinator_instance_id,
            self.implementer_instance_id,
            self.reviewer_instance_id,
            self.selected_message_id,
        )
        for value in identities:
            _identifier(value, "authority identity")
        if len(set(identities[1:4])) != 3:
            raise ValueError("coordinator, implementer, and reviewer Instances must be distinct")
        for value, field in (
            (self.framework_checkout, "framework_checkout"),
            (self.project_checkout, "project_checkout"),
            (self.runtime_root, "runtime_root"),
            (self.worktree_root, "worktree_root"),
            (self.executable, "executable"),
            (self.codex_home, "codex_home"),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError(f"{field} must be an absolute path")
        if self.task_state.path is None or self.request_state.path is None:
            raise ValueError("task and request States must select exact paths")
        if self.development_blueprint.path != "peoplebot/blueprints/development/blueprint.json":
            raise ValueError("development Blueprint State selects an unsupported path")
        if self.development_adapter.path != "peoplebot/adapters/development/adapter.json":
            raise ValueError("development Adapter State selects an unsupported path")
        if self.review_blueprint.path != "peoplebot/blueprints/project_review/blueprint.json":
            raise ValueError("review Blueprint State selects an unsupported path")
        if self.review_adapter.path != "peoplebot/adapters":
            raise ValueError("review Adapter State must select the versioned adapters directory")


def _absolute(value: object, field: str) -> Path:
    result = Path(_text(value, field, 2048))
    if not result.is_absolute():
        raise ValueError(f"{field} must be absolute")
    return result


def development_authority_from_dict(value: object) -> DevelopmentAuthority:
    fields = {
        "active", "codex_home", "coordinator_instance_id", "development_adapter",
        "development_blueprint", "environment_id", "executable", "format",
        "framework_checkout", "implementer_instance_id", "project_checkout",
        "request_state", "review_adapter", "review_blueprint", "reviewer_instance_id",
        "runtime_root", "selected_message_id", "task_state", "worktree_root",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("development authority fields are invalid")
    if value.get("format") != "peoplebot.development-authority.v0":
        raise ValueError("development authority format is invalid")
    return DevelopmentAuthority(
        active=value["active"],  # type: ignore[arg-type]
        environment_id=value["environment_id"],  # type: ignore[arg-type]
        coordinator_instance_id=value["coordinator_instance_id"],  # type: ignore[arg-type]
        implementer_instance_id=value["implementer_instance_id"],  # type: ignore[arg-type]
        reviewer_instance_id=value["reviewer_instance_id"],  # type: ignore[arg-type]
        framework_checkout=_absolute(value["framework_checkout"], "framework_checkout"),
        project_checkout=_absolute(value["project_checkout"], "project_checkout"),
        runtime_root=_absolute(value["runtime_root"], "runtime_root"),
        worktree_root=_absolute(value["worktree_root"], "worktree_root"),
        executable=_absolute(value["executable"], "executable"),
        codex_home=_absolute(value["codex_home"], "codex_home"),
        task_state=_state(value["task_state"], "task_state", path_required=True),
        request_state=_state(value["request_state"], "request_state", path_required=True),
        selected_message_id=value["selected_message_id"],  # type: ignore[arg-type]
        development_blueprint=_state(value["development_blueprint"], "development_blueprint", path_required=True),
        development_adapter=_state(value["development_adapter"], "development_adapter", path_required=True),
        review_blueprint=_state(value["review_blueprint"], "review_blueprint", path_required=True),
        review_adapter=_state(value["review_adapter"], "review_adapter", path_required=True),
    )


def load_development_authority(path: str | Path) -> DevelopmentAuthority:
    source = Path(path)
    try:
        content = source.read_bytes()
    except OSError as error:
        raise DevelopmentError("development.authority_unavailable", "authority file is unavailable") from error
    if len(content) > _MAX_JSON_BYTES:
        raise ValueError("development authority exceeds 65536 bytes")
    try:
        return development_authority_from_dict(json.loads(content))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("development authority is not valid UTF-8 JSON") from error


class DevelopmentStage(StrEnum):
    IMPORTED = "imported"
    IMPLEMENTER_RESERVED = "implementer_reserved"
    CANDIDATE_READY = "candidate_ready"
    REVIEWER_RESERVED = "reviewer_reserved"
    CORRECTION_PENDING = "correction_pending"
    FINDINGS = "findings"
    ACCEPTED = "accepted"
    FAILED = "failed"
    UNRESOLVED = "unresolved"
    EXPORTED = "exported"


@dataclass(frozen=True, slots=True)
class ProcessStreamFingerprint:
    captured_bytes: int
    sha256: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.captured_bytes, bool)
            or not isinstance(self.captured_bytes, int)
            or not 0 <= self.captured_bytes <= 262_145
        ):
            raise ValueError("captured_bytes is invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("stream sha256 is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"captured_bytes": self.captured_bytes, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class DevelopmentProcessDiagnostic:
    classification: str
    classification_source: str
    summary: str
    recommended_action: str
    event_lines_observed: int
    event_lines_processed: int
    invalid_event_lines: int
    failure_event_types: tuple[str, ...]
    provider_processing_observed: bool
    provider_response_observed: bool
    stdout: ProcessStreamFingerprint
    stderr: ProcessStreamFingerprint

    def __post_init__(self) -> None:
        if self.classification not in _FAILURE_CLASSIFICATIONS:
            raise ValueError("development failure classification is invalid")
        if self.classification_source not in _FAILURE_SOURCES:
            raise ValueError("development failure source is invalid")
        _text(self.summary, "diagnostic summary", 512)
        _text(self.recommended_action, "diagnostic recommended_action", 512)
        for field, value in (
            ("event_lines_observed", self.event_lines_observed),
            ("event_lines_processed", self.event_lines_processed),
            ("invalid_event_lines", self.invalid_event_lines),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} is invalid")
        if self.event_lines_processed > _MAX_FAILURE_EVENT_LINES:
            raise ValueError("event_lines_processed exceeds the diagnostic limit")
        if self.invalid_event_lines > self.event_lines_processed:
            raise ValueError("invalid_event_lines exceeds processed events")
        if any(item not in _FAILURE_EVENT_TYPES for item in self.failure_event_types):
            raise ValueError("failure_event_types contains an unsupported value")
        if len(set(self.failure_event_types)) != len(self.failure_event_types):
            raise ValueError("failure_event_types must be unique")
        if type(self.provider_processing_observed) is not bool:
            raise ValueError("provider_processing_observed must be Boolean")
        if type(self.provider_response_observed) is not bool:
            raise ValueError("provider_response_observed must be Boolean")

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "classification_source": self.classification_source,
            "event_lines_observed": self.event_lines_observed,
            "event_lines_processed": self.event_lines_processed,
            "failure_event_types": list(self.failure_event_types),
            "format": "peoplebot.development-process-diagnostic.v0",
            "invalid_event_lines": self.invalid_event_lines,
            "provider_processing_observed": self.provider_processing_observed,
            "provider_response_observed": self.provider_response_observed,
            "recommended_action": self.recommended_action,
            "stderr": self.stderr.to_dict(),
            "stdout": self.stdout.to_dict(),
            "summary": self.summary,
        }


def _stream_fingerprint_from_dict(value: object, field: str) -> ProcessStreamFingerprint:
    if not isinstance(value, Mapping) or set(value) != {"captured_bytes", "sha256"}:
        raise ValueError(f"{field} fingerprint fields are invalid")
    return ProcessStreamFingerprint(value["captured_bytes"], value["sha256"])  # type: ignore[arg-type]


def development_process_diagnostic_from_dict(value: object) -> DevelopmentProcessDiagnostic:
    fields = {
        "classification", "classification_source", "event_lines_observed",
        "event_lines_processed", "failure_event_types", "format",
        "invalid_event_lines", "provider_processing_observed",
        "provider_response_observed", "recommended_action", "stderr", "stdout", "summary",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("development process diagnostic fields are invalid")
    if value.get("format") != "peoplebot.development-process-diagnostic.v0":
        raise ValueError("development process diagnostic format is invalid")
    event_types = value.get("failure_event_types")
    if not isinstance(event_types, list) or not all(isinstance(item, str) for item in event_types):
        raise ValueError("failure_event_types must be strings")
    return DevelopmentProcessDiagnostic(
        classification=value["classification"],  # type: ignore[arg-type]
        classification_source=value["classification_source"],  # type: ignore[arg-type]
        summary=value["summary"],  # type: ignore[arg-type]
        recommended_action=value["recommended_action"],  # type: ignore[arg-type]
        event_lines_observed=value["event_lines_observed"],  # type: ignore[arg-type]
        event_lines_processed=value["event_lines_processed"],  # type: ignore[arg-type]
        invalid_event_lines=value["invalid_event_lines"],  # type: ignore[arg-type]
        failure_event_types=tuple(event_types),
        provider_processing_observed=value["provider_processing_observed"],  # type: ignore[arg-type]
        provider_response_observed=value["provider_response_observed"],  # type: ignore[arg-type]
        stdout=_stream_fingerprint_from_dict(value["stdout"], "stdout"),
        stderr=_stream_fingerprint_from_dict(value["stderr"], "stderr"),
    )


@dataclass(frozen=True, slots=True)
class DevelopmentInvocationReport:
    execution_id: str
    role: str
    environment_id: str
    machine_id: str
    instance_id: str
    task_id: str
    started_at: str
    finished_at: str
    elapsed_milliseconds: int
    outcome: str
    child_launched: bool
    process_exit_code: int | None
    provider_response_observed: bool
    runtime: str | None
    model: str | None
    usage: tuple[UsageObservation, ...]
    evidence: StateRef | None = None
    attempt_ref: str | None = None
    attempt_state: StateRef | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.execution_id, "execution_id"),
            (self.role, "role"),
            (self.environment_id, "environment_id"),
            (self.machine_id, "machine_id"),
            (self.instance_id, "instance_id"),
            (self.task_id, "task_id"),
            (self.outcome, "outcome"),
        ):
            _identifier(value, field)
        _timestamp(self.started_at, "started_at")
        _timestamp(self.finished_at, "finished_at")
        if (
            isinstance(self.elapsed_milliseconds, bool)
            or not isinstance(self.elapsed_milliseconds, int)
            or self.elapsed_milliseconds < 0
        ):
            raise ValueError("elapsed_milliseconds is invalid")
        if type(self.child_launched) is not bool or type(self.provider_response_observed) is not bool:
            raise ValueError("invocation observation flags must be Boolean")
        if self.process_exit_code is not None and (
            isinstance(self.process_exit_code, bool) or not isinstance(self.process_exit_code, int)
        ):
            raise ValueError("process_exit_code is invalid")
        if self.runtime is not None:
            _text(self.runtime, "runtime", 128)
        if self.model is not None:
            _text(self.model, "model", 128)
        if not self.usage or not all(isinstance(item, UsageObservation) for item in self.usage):
            raise ValueError("usage observations are invalid")
        if self.attempt_ref is not None and not self.attempt_ref.startswith(
            "refs/heads/codex/peoplebot-attempts/"
        ):
            raise ValueError("attempt_ref is outside the ordinary attempt namespace")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt_ref": self.attempt_ref,
            "attempt_state": self.attempt_state.to_dict() if self.attempt_state else None,
            "child_launched": self.child_launched,
            "elapsed_milliseconds": self.elapsed_milliseconds,
            "environment_id": self.environment_id,
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "execution_id": self.execution_id,
            "finished_at": self.finished_at,
            "format": "peoplebot.development-invocation-report.v0",
            "instance_id": self.instance_id,
            "machine_id": self.machine_id,
            "model": self.model,
            "outcome": self.outcome,
            "process_exit_code": self.process_exit_code,
            "provider_response_observed": self.provider_response_observed,
            "role": self.role,
            "runtime": self.runtime,
            "started_at": self.started_at,
            "task_id": self.task_id,
            "usage": [item.to_dict() for item in self.usage],
        }


def development_invocation_report_from_dict(value: object) -> DevelopmentInvocationReport:
    fields = {
        "attempt_ref", "attempt_state", "child_launched", "elapsed_milliseconds",
        "environment_id", "evidence", "execution_id", "finished_at", "format",
        "instance_id", "machine_id", "model", "outcome", "process_exit_code",
        "provider_response_observed", "role", "runtime", "started_at", "task_id", "usage",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("development invocation report fields are invalid")
    if value.get("format") != "peoplebot.development-invocation-report.v0":
        raise ValueError("development invocation report format is invalid")
    usage = value.get("usage")
    if not isinstance(usage, list):
        raise ValueError("development invocation usage must be an array")
    parsed_usage: list[UsageObservation] = []
    for item in usage:
        if not isinstance(item, Mapping) or set(item) != {
            "confidence", "metric", "source", "unit", "value"
        }:
            raise ValueError("development invocation usage fields are invalid")
        try:
            parsed_usage.append(UsageObservation(
                item["metric"], item["value"], item["unit"],  # type: ignore[arg-type]
                UsageSource(item["source"]), UsageConfidence(item["confidence"]),
            ))
        except (TypeError, ValueError) as error:
            raise ValueError("development invocation usage is invalid") from error
    return DevelopmentInvocationReport(
        execution_id=value["execution_id"],  # type: ignore[arg-type]
        role=value["role"],  # type: ignore[arg-type]
        environment_id=value["environment_id"],  # type: ignore[arg-type]
        machine_id=value["machine_id"],  # type: ignore[arg-type]
        instance_id=value["instance_id"],  # type: ignore[arg-type]
        task_id=value["task_id"],  # type: ignore[arg-type]
        started_at=value["started_at"],  # type: ignore[arg-type]
        finished_at=value["finished_at"],  # type: ignore[arg-type]
        elapsed_milliseconds=value["elapsed_milliseconds"],  # type: ignore[arg-type]
        outcome=value["outcome"],  # type: ignore[arg-type]
        child_launched=value["child_launched"],  # type: ignore[arg-type]
        process_exit_code=value["process_exit_code"],  # type: ignore[arg-type]
        provider_response_observed=value["provider_response_observed"],  # type: ignore[arg-type]
        runtime=value["runtime"],  # type: ignore[arg-type]
        model=value["model"],  # type: ignore[arg-type]
        usage=tuple(parsed_usage),
        evidence=_optional_state(value["evidence"], "evidence"),
        attempt_ref=value["attempt_ref"],  # type: ignore[arg-type]
        attempt_state=_optional_state(value["attempt_state"], "attempt_state"),
    )


@dataclass(frozen=True, slots=True)
class DevelopmentProgress:
    task_id: str
    task_digest: str
    request_state: StateRef
    task_state: StateRef
    stage: DevelopmentStage
    started_at: str
    invocation_reservations: tuple[str, ...] = ()
    candidate_state: StateRef | None = None
    implementation_evidence: StateRef | None = None
    implementation_diagnostic: DevelopmentProcessDiagnostic | None = None
    review_evidence: StateRef | None = None
    review_outcome: str | None = None
    review_findings: str | None = None
    corrections_used: int = 0
    reply_state: StateRef | None = None
    invocation_reports: tuple["DevelopmentInvocationReport", ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.task_id, "task_id")
        if not re.fullmatch(r"[0-9a-f]{64}", self.task_digest):
            raise ValueError("task_digest must be a SHA-256 digest")
        _timestamp(self.started_at, "started_at")
        if not isinstance(self.stage, DevelopmentStage):
            raise ValueError("stage is invalid")
        for reservation in self.invocation_reservations:
            _identifier(reservation, "invocation reservation")
        if len(set(self.invocation_reservations)) != len(self.invocation_reservations):
            raise ValueError("invocation reservations must be unique")
        if isinstance(self.corrections_used, bool) or not 0 <= self.corrections_used <= 3:
            raise ValueError("corrections_used is invalid")
        if self.review_findings is not None:
            _text(self.review_findings, "review_findings", 16_384)
            try:
                decoded = json.loads(self.review_findings)
            except json.JSONDecodeError as error:
                raise ValueError("review_findings must be JSON") from error
            if not isinstance(decoded, list):
                raise ValueError("review_findings must encode an array")
        if self.implementation_diagnostic is not None and not isinstance(
            self.implementation_diagnostic, DevelopmentProcessDiagnostic
        ):
            raise ValueError("implementation_diagnostic is invalid")
        if len(self.invocation_reports) > 8 or not all(
            isinstance(item, DevelopmentInvocationReport) for item in self.invocation_reports
        ):
            raise ValueError("invocation_reports are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_state": self.candidate_state.to_dict() if self.candidate_state else None,
            "corrections_used": self.corrections_used,
            "format": "peoplebot.development-progress.v0",
            "implementation_evidence": self.implementation_evidence.to_dict() if self.implementation_evidence else None,
            "implementation_diagnostic": (
                self.implementation_diagnostic.to_dict() if self.implementation_diagnostic else None
            ),
            "invocation_reservations": list(self.invocation_reservations),
            "invocation_reports": [item.to_dict() for item in self.invocation_reports],
            "reply_state": self.reply_state.to_dict() if self.reply_state else None,
            "request_state": self.request_state.to_dict(),
            "review_evidence": self.review_evidence.to_dict() if self.review_evidence else None,
            "review_outcome": self.review_outcome,
            "review_findings": self.review_findings,
            "stage": self.stage.value,
            "started_at": self.started_at,
            "task_digest": self.task_digest,
            "task_id": self.task_id,
            "task_state": self.task_state.to_dict(),
        }


def _optional_state(value: object, field: str) -> StateRef | None:
    return None if value is None else _state(value, field)


def development_progress_from_dict(value: object) -> DevelopmentProgress:
    legacy_fields = {
        "candidate_state", "corrections_used", "format", "implementation_evidence",
        "invocation_reservations", "reply_state", "request_state", "review_evidence",
        "review_findings", "review_outcome", "stage", "started_at", "task_digest", "task_id", "task_state",
    }
    optional_fields = {"implementation_diagnostic", "invocation_reports"}
    if (
        not isinstance(value, Mapping)
        or not legacy_fields.issubset(value)
        or not set(value).issubset(legacy_fields | optional_fields)
    ):
        raise ValueError("development progress fields are invalid")
    if value.get("format") != "peoplebot.development-progress.v0":
        raise ValueError("development progress format is invalid")
    reservations = value.get("invocation_reservations")
    if not isinstance(reservations, list) or not all(isinstance(item, str) for item in reservations):
        raise ValueError("invocation_reservations must be strings")
    reports = value.get("invocation_reports", [])
    if not isinstance(reports, list):
        raise ValueError("invocation_reports must be an array")
    try:
        stage = DevelopmentStage(value.get("stage"))
    except (TypeError, ValueError) as error:
        raise ValueError("development progress stage is invalid") from error
    return DevelopmentProgress(
        task_id=value["task_id"], task_digest=value["task_digest"],  # type: ignore[arg-type]
        request_state=_state(value["request_state"], "request_state"),
        task_state=_state(value["task_state"], "task_state"), stage=stage,
        started_at=value["started_at"], invocation_reservations=tuple(reservations),  # type: ignore[arg-type]
        candidate_state=_optional_state(value["candidate_state"], "candidate_state"),
        implementation_evidence=_optional_state(value["implementation_evidence"], "implementation_evidence"),
        implementation_diagnostic=(
            None if value.get("implementation_diagnostic") is None
            else development_process_diagnostic_from_dict(value["implementation_diagnostic"])
        ),
        review_evidence=_optional_state(value["review_evidence"], "review_evidence"),
        review_outcome=value["review_outcome"],  # type: ignore[arg-type]
        review_findings=value["review_findings"],  # type: ignore[arg-type]
        corrections_used=value["corrections_used"],  # type: ignore[arg-type]
        reply_state=_optional_state(value["reply_state"], "reply_state"),
        invocation_reports=tuple(
            development_invocation_report_from_dict(item) for item in reports
        ),
    )


class DevelopmentProgressStore:
    """Append durable workflow snapshots on one deterministic direct ref."""

    def __init__(self, checkout: str | Path, repository: str, ref_name: str) -> None:
        if not ref_name.startswith(_PROGRESS_REF_PREFIX):
            raise ValueError("progress ref is outside the development namespace")
        self._store = GitAttemptStore(checkout, repository)
        self.repository = repository
        self.ref_name = ref_name

    def _tip(self) -> str | None:
        result = self._store._git("rev-parse", "--verify", "--quiet", self.ref_name)
        if result.returncode == 1:
            return None
        value = result.stdout.decode("ascii", "replace").strip()
        if result.returncode != 0 or not _OBJECT_ID.fullmatch(value):
            raise DevelopmentError("development.progress_unavailable", "progress ref is ambiguous")
        return value

    def load(self) -> tuple[DevelopmentProgress | None, StateRef | None]:
        tip = self._tip()
        if tip is None:
            return None, None
        result = self._store._git("show", f"{tip}:progress.json")
        if result.returncode != 0 or len(result.stdout) > _MAX_JSON_BYTES:
            raise DevelopmentError("development.progress_unavailable", "progress blob is unavailable")
        try:
            progress = development_progress_from_dict(json.loads(result.stdout))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise DevelopmentError("development.progress_invalid", "progress blob is malformed") from error
        return progress, StateRef(self.repository, tip, "progress.json")

    def persist(self, progress: DevelopmentProgress, expected: StateRef | None) -> StateRef:
        current = self._tip()
        expected_commit = expected.commit if expected else None
        if current != expected_commit:
            raise DevelopmentError("development.progress_conflict", "progress changed concurrently")
        blob = self._store._write_blob(stable_json_bytes(progress.to_dict()))
        tree = self._store._object_id(
            self._store._git("mktree", input_bytes=f"100644 blob {blob}\tprogress.json\n".encode("ascii")),
            "write the development progress tree",
        )
        commit = self._store._write_commit(
            tree, (current,) if current else (), progress.started_at,
            f"PeopleBot development progress {progress.task_id}",
        )
        self._store._update_ref(
            self.ref_name, commit, current or _ZERO_OBJECT_ID,
            reflog_message="peoplebot development progress v0",
            conflict_code="development.progress_conflict",
            conflict_detail="development progress changed concurrently",
            symbolic_code="development.progress_invalid",
            symbolic_detail="development progress ref is symbolic",
            inspection_code="development.progress_unavailable",
            inspection_detail="development progress ref cannot be inspected",
            persistence_code="development.progress_persist_failed",
            persistence_detail="development progress could not be attached",
        )
        return StateRef(self.repository, commit, "progress.json")


@dataclass(frozen=True, slots=True)
class DevelopmentAdapterConfiguration:
    runtime: str
    runtime_version: str
    model: str
    timeout_seconds: int
    max_prompt_bytes: int

    def __post_init__(self) -> None:
        if self.runtime != "codex-cli":
            raise ValueError("development Adapter requires codex-cli")
        _text(self.runtime_version, "runtime_version", 64)
        _text(self.model, "model", 128)
        if not 1 <= self.timeout_seconds <= 900:
            raise ValueError("timeout_seconds must be between 1 and 900")
        if not 1024 <= self.max_prompt_bytes <= 65_536:
            raise ValueError("max_prompt_bytes must be between 1024 and 65536")


def _load_exact_json(checkout: Path, state: StateRef, maximum: int = _MAX_JSON_BYTES) -> Mapping[str, Any]:
    if state.path is None:
        raise ValueError("exact JSON State must select a path")
    context = assemble_context(
        checkout, StateRef(state.repository, state.commit), (state.path,),
        ContextPolicy(state, 1, maximum, maximum),
    )
    try:
        value = json.loads(context.documents[0].to_source_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("exact State is not valid UTF-8 JSON") from error
    if not isinstance(value, Mapping):
        raise ValueError("exact JSON State must contain an object")
    return value


def load_development_adapter_configuration(
    checkout: str | Path, state: StateRef
) -> DevelopmentAdapterConfiguration:
    value = _load_exact_json(Path(checkout), state)
    if set(value) != {
        "format", "max_prompt_bytes", "model", "runtime", "runtime_version",
        "sandbox", "timeout_seconds",
    } or value.get("format") != "peoplebot.codex-development-adapter.v0":
        raise ValueError("development Adapter configuration is invalid")
    if value.get("sandbox") != "workspace-write":
        raise ValueError("development Adapter requires workspace-write sandboxing")
    return DevelopmentAdapterConfiguration(
        value["runtime"], value["runtime_version"], value["model"],  # type: ignore[arg-type]
        value["timeout_seconds"], value["max_prompt_bytes"],  # type: ignore[arg-type]
    )


def validate_development_blueprint(checkout: str | Path, state: StateRef) -> None:
    value = _load_exact_json(Path(checkout), state, 16_384)
    expected = {
        "agent_type": "peoplebot.development-implementer",
        "authority": {
            "allowed_paths_only": True,
            "candidate_branch_only": True,
            "deployment": False,
            "external_publication": False,
            "main_mutation": False,
        },
        "format": "peoplebot.development-blueprint.v0",
        "purpose": "Produce one bounded candidate commit for an explicitly approved exact task.",
        "stopping": {
            "self_retry": False,
            "verification_required": True,
        },
        "version": "0",
    }
    if value != expected:
        raise ValueError("development Blueprint content is invalid")


@dataclass(slots=True)
class DevelopmentObservation:
    code: str
    detail: str
    process_started: bool
    process_exit_code: int | None
    candidate_state: StateRef | None
    changed_paths: tuple[str, ...]
    verification_commands: tuple[tuple[str, ...], ...]
    usage: tuple[UsageObservation, ...] = (UNKNOWN_USAGE,)
    process_diagnostic: DevelopmentProcessDiagnostic | None = None
    workspace_cleanup_disposition: str = "not_created"
    workspace_remnant: str | None = None
    attempt_ref: str | None = None
    attempt_state: StateRef | None = None
    attempt_preservation: str = "not_created"
    invocation_started_at: str | None = None
    invocation_finished_at: str | None = None
    elapsed_milliseconds: int | None = None
    runtime: str | None = None
    model: str | None = None
    provider_response_observed: bool = False

    @property
    def succeeded(self) -> bool:
        return self.code == "development.completed" and self.candidate_state is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_state": self.candidate_state.to_dict() if self.candidate_state else None,
            "changed_paths": list(self.changed_paths),
            "code": self.code,
            "detail": self.detail,
            "format": "peoplebot.development-observation.v0",
            "process_exit_code": self.process_exit_code,
            "process_started": self.process_started,
            "process_diagnostic": (
                self.process_diagnostic.to_dict() if self.process_diagnostic else None
            ),
            "attempt_ref": self.attempt_ref,
            "attempt_state": self.attempt_state.to_dict() if self.attempt_state else None,
            "attempt_preservation": self.attempt_preservation,
            "elapsed_milliseconds": self.elapsed_milliseconds,
            "invocation_finished_at": self.invocation_finished_at,
            "invocation_started_at": self.invocation_started_at,
            "model": self.model,
            "provider_response_observed": self.provider_response_observed,
            "runtime": self.runtime,
            "usage": [item.to_dict() for item in self.usage],
            "verification_commands": [list(item) for item in self.verification_commands],
            "workspace_cleanup_disposition": self.workspace_cleanup_disposition,
            "workspace_remnant": self.workspace_remnant,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentExecutionResult:
    observation: DevelopmentObservation | None
    provenance: ProvenanceRunResult

    @property
    def observation_evidence(self) -> StateRef | None:
        terminal = self.provenance.terminal_evidence
        if terminal is None or self.observation is None:
            return None
        return StateRef(terminal.state.repository, terminal.state.commit, "adapter-observation.json")


class CommandRunner(Protocol):
    def __call__(
        self, command: tuple[str, ...], cwd: Path,
        environment: Mapping[str, str], timeout_seconds: int,
    ) -> subprocess.CompletedProcess[bytes]: ...


def _command_runner(
    command: tuple[str, ...], cwd: Path,
    environment: Mapping[str, str], timeout_seconds: int,
) -> subprocess.CompletedProcess[bytes]:
    return _run_process(
        command, b"", environment, timeout_seconds, _cwd=cwd
    )


def _stream_fingerprint(content: bytes) -> ProcessStreamFingerprint:
    return ProcessStreamFingerprint(len(content), hashlib.sha256(content).hexdigest())


def _failure_texts(value: object, *, depth: int = 0) -> tuple[str, ...]:
    """Select diagnostic-only text for classification; callers never persist it."""

    if depth > 2:
        return ()
    if isinstance(value, str):
        return (value[:8192],)
    if not isinstance(value, Mapping):
        return ()
    selected: list[str] = []
    for key in ("code", "type", "message", "detail", "reason", "error"):
        if key in value:
            selected.extend(_failure_texts(value[key], depth=depth + 1))
    return tuple(selected)


def _failure_classification(texts: Sequence[tuple[str, str]]) -> tuple[str, str]:
    patterns = (
        ("service_limit", ("rate limit", "usage limit", "quota", "too many requests", "capacity")),
        ("authentication", ("authentication", "unauthorized", "forbidden", "api key", "login", "sign in")),
        ("model", ("model unavailable", "model not found", "unsupported model", "model access", "unknown model")),
        ("network", ("network", "connection", "connectivity", "dns", "timed out", "timeout", "service unavailable")),
        ("configuration", ("configuration", "config file", "invalid argument", "unknown option", "missing setting")),
        ("runtime", ("runtime", "panic", "internal error", "failed to spawn", "process failure")),
    )
    for classification, needles in patterns:
        for source, text in texts:
            folded = text.casefold()
            if any(needle in folded for needle in needles):
                return classification, source
    if any(source == "structured_event" for source, _ in texts):
        return "unknown", "structured_event"
    if any(source == "stderr_metadata" for source, _ in texts):
        return "unknown", "stderr_metadata"
    if any(source == "stdout_metadata" for source, _ in texts):
        return "unknown", "stdout_metadata"
    return "unknown", "exit_status"


def _failure_wording(classification: str) -> tuple[str, str]:
    wording = {
        "authentication": (
            "Codex reported an authentication or authorization failure.",
            "Verify the configured provider login and authorization without exposing credentials before a newly authorized run.",
        ),
        "configuration": (
            "Codex reported a configuration or invocation-argument failure.",
            "Verify the pinned runtime configuration and supported command arguments before a newly authorized run.",
        ),
        "model": (
            "Codex reported a model selection or model-access failure.",
            "Verify that the pinned model remains available to the configured account before a newly authorized run.",
        ),
        "network": (
            "Codex reported a network or service-connectivity failure.",
            "Verify provider connectivity and reconcile external effects before a newly authorized run.",
        ),
        "service_limit": (
            "Codex reported a provider capacity, quota, or usage-limit failure.",
            "Inspect supported account usage or reset evidence before considering a newly authorized run.",
        ),
        "runtime": (
            "Codex reported a local runtime or internal process failure.",
            "Inspect the pinned runtime installation and bounded local process evidence before a newly authorized run.",
        ),
        "unknown": (
            "The Codex child exited nonzero without a recognized safe failure classification.",
            "Inspect only bounded sanitized process evidence; do not infer a provider cause or retry automatically.",
        ),
    }
    return wording[classification]


def _usage_from_completion_events(events: Sequence[Mapping[str, Any]]) -> tuple[UsageObservation, ...]:
    completed = [event for event in events if event.get("type") == "turn.completed"]
    if len(completed) != 1 or not isinstance(completed[0].get("usage"), Mapping):
        return (UNKNOWN_USAGE,)
    observations: list[UsageObservation] = []
    usage = completed[0]["usage"]
    for metric in (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens",
    ):
        amount = usage.get(metric)
        if amount is None:
            continue
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            return (UNKNOWN_USAGE,)
        observations.append(
            UsageObservation(
                f"codex.{metric}", amount, "tokens",
                UsageSource.PROVIDER_REPORTED, UsageConfidence.EXACT,
            )
        )
    return tuple(observations) or (UNKNOWN_USAGE,)


def _diagnose_development_process_failure(
    completed: subprocess.CompletedProcess[bytes],
) -> tuple[DevelopmentProcessDiagnostic, tuple[UsageObservation, ...]]:
    stdout = completed.stdout if isinstance(completed.stdout, bytes) else b""
    stderr = completed.stderr if isinstance(completed.stderr, bytes) else b""
    lines = [line for line in stdout.splitlines() if line]
    processed = lines[:_MAX_FAILURE_EVENT_LINES]
    events: list[Mapping[str, Any]] = []
    invalid = 0
    failure_event_types: list[str] = []
    classification_inputs: list[tuple[str, str]] = []
    provider_processing_observed = False
    completed_turn_observed = False
    completed_agent_message_observed = False
    for line in processed:
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            invalid += 1
            continue
        if not isinstance(event, Mapping):
            invalid += 1
            continue
        events.append(event)
        event_type = event.get("type")
        if not isinstance(event_type, str):
            continue
        if event_type in _PROCESSING_EVENT_TYPES:
            provider_processing_observed = True
        if event_type in _FAILURE_EVENT_TYPES:
            if event_type not in failure_event_types:
                failure_event_types.append(event_type)
            classification_inputs.extend(
                ("structured_event", text) for text in _failure_texts(event)
            )
        if event_type == "turn.completed":
            completed_turn_observed = True
        if event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "agent_message" and isinstance(
                item.get("text"), str
            ):
                completed_agent_message_observed = True
            if isinstance(item, Mapping) and item.get("type") == "error":
                classification_inputs.extend(
                    ("structured_event", text) for text in _failure_texts(item)
                )
    if stderr:
        classification_inputs.append(("stderr_metadata", stderr.decode("utf-8", "replace")[:65_536]))
    if stdout and not classification_inputs:
        classification_inputs.append(("stdout_metadata", "unclassified structured output"))
    classification, source = _failure_classification(classification_inputs)
    summary, action = _failure_wording(classification)
    diagnostic = DevelopmentProcessDiagnostic(
        classification=classification,
        classification_source=source,
        summary=summary,
        recommended_action=action,
        event_lines_observed=len(lines),
        event_lines_processed=len(processed),
        invalid_event_lines=invalid,
        failure_event_types=tuple(failure_event_types),
        provider_processing_observed=provider_processing_observed,
        provider_response_observed=(
            completed_turn_observed and completed_agent_message_observed
        ),
        stdout=_stream_fingerprint(stdout),
        stderr=_stream_fingerprint(stderr),
    )
    return diagnostic, _usage_from_completion_events(events)


def _git_bytes(checkout: Path, *arguments: str, timeout: int = 30) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (
            "git", *_GIT_GLOBAL_OPTIONS, "-C", os.fspath(checkout),
            "-c", f"core.hooksPath={os.devnull}", *arguments,
        ),
        capture_output=True, check=False, shell=False, timeout=timeout,
        env=_git_environment(),
    )


def _git_ok(checkout: Path, *arguments: str, timeout: int = 30) -> bytes:
    result = _git_bytes(checkout, *arguments, timeout=timeout)
    if result.returncode != 0:
        raise DevelopmentError("development.git_failed", f"Git operation {arguments[0]} failed")
    return result.stdout


def _changed_paths(worktree: Path, timeout_seconds: int = 30) -> tuple[str, ...]:
    content = _git_ok(
        worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all",
        timeout=timeout_seconds,
    )
    entries = content.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(entries) and entries[index]:
        entry = entries[index]
        if len(entry) < 4:
            raise DevelopmentError("development.status_invalid", "Git status output is malformed")
        status = entry[:2]
        path = entry[3:].decode("utf-8", "strict").replace("\\", "/")
        if status[:1] in {b"R", b"C"}:
            index += 1
            if index >= len(entries) or not entries[index]:
                raise DevelopmentError("development.status_invalid", "Git rename status is malformed")
            paths.append(entries[index].decode("utf-8", "strict").replace("\\", "/"))
        paths.append(path)
        index += 1
    return tuple(sorted(set(paths)))


def _path_allowed(path: str, allowed: Sequence[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in allowed)


def _attempt_branch_ref(task: DevelopmentTask, starting_state: StateRef, execution_id: str) -> str:
    digest = hashlib.sha256(
        b"peoplebot.development-attempt.v0\0"
        + task.repository.encode("utf-8")
        + b"\0"
        + task.task_id.encode("utf-8")
        + b"\0"
        + execution_id.encode("utf-8")
        + b"\0"
        + starting_state.commit.encode("ascii")
    ).hexdigest()
    return f"refs/heads/codex/peoplebot-attempts/{digest}"


class CodexDevelopmentAdapter:
    """One admitted editing invocation with host-verified diff, tests, and commit."""

    def __init__(
        self,
        framework_checkout: str | Path,
        state: StateRef,
        executable: str | Path,
        codex_home: str | Path,
        worktree_root: str | Path,
        *,
        runner: ProcessRunner = _run_process,
        command_runner: CommandRunner = _command_runner,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.state = state
        self.configuration = load_development_adapter_configuration(framework_checkout, state)
        self.executable = Path(executable).resolve(strict=True)
        self.codex_home = Path(codex_home).resolve(strict=True)
        self.worktree_root = Path(worktree_root)
        self.runner = runner
        self.command_runner = command_runner
        self.monotonic = monotonic
        self.now = now or (lambda: datetime.now(UTC))

    def invoke(
        self, task: DevelopmentTask, starting_state: StateRef, execution_id: str,
        objective: str,
    ) -> DevelopmentObservation:
        observation: DevelopmentObservation | None = None
        if starting_state.repository != task.repository or starting_state.path is not None:
            raise ValueError("implementation starting State is invalid")
        project_checkout = getattr(self, "project_checkout", None)
        if project_checkout is None:
            raise ValueError("project_checkout must be bound before invocation")
        project_path = Path(project_checkout)
        worktree = self.worktree_root / execution_id
        attempt_ref = _attempt_branch_ref(task, starting_state, execution_id)
        attempt_state: StateRef | None = None
        attempt_preservation = "not_created"
        worktree_registered = False
        invocation_started_at: str | None = None
        invocation_finished_at: str | None = None
        invocation_started_monotonic: float | None = None
        elapsed_milliseconds: int | None = None
        observed_usage: tuple[UsageObservation, ...] = (UNKNOWN_USAGE,)
        provider_response_observed = False

        def utc_now() -> str:
            return self.now().isoformat().replace("+00:00", "Z")

        def preserve_failed_attempt() -> None:
            nonlocal attempt_state, attempt_preservation
            if not worktree_registered or not worktree.is_dir():
                return
            attempt_preservation = "worktree_retained_uncheckpointed"
            try:
                current = _git_ok(
                    project_path, "rev-parse", "--verify", attempt_ref, timeout=15
                ).decode("ascii").strip()
                if current != starting_state.commit:
                    attempt_state = StateRef(task.repository, current)
                    attempt_preservation = "checkpointed"
                    return
                head = _git_ok(worktree, "rev-parse", "HEAD", timeout=15).decode("ascii").strip()
                changed_now = _changed_paths(worktree, 15)
                if head != starting_state.commit or any(
                    not _path_allowed(path, task.allowed_paths) for path in changed_now
                ):
                    attempt_state = StateRef(task.repository, current)
                    return
                if not changed_now:
                    attempt_state = StateRef(task.repository, current)
                    attempt_preservation = "branch_at_base"
                    return
                _git_ok(worktree, "add", "-A", "--", *task.allowed_paths, timeout=15)
                staged = tuple(
                    sorted(
                        item.decode("utf-8", "strict").replace("\\", "/")
                        for item in _git_ok(
                            worktree, "diff", "--cached", "--name-only", "-z",
                            starting_state.commit, "--", timeout=15,
                        ).split(b"\0")
                        if item
                    )
                )
                if staged != changed_now:
                    attempt_state = StateRef(task.repository, current)
                    return
                tree = _git_ok(worktree, "write-tree", timeout=15).decode("ascii").strip()
                plumbing = GitAttemptStore(project_path, task.repository)
                checkpoint = plumbing._write_commit(
                    tree, (starting_state.commit,), utc_now(),
                    f"{task.commit_message} (incomplete attempt)",
                )
                plumbing._update_ref(
                    attempt_ref, checkpoint, starting_state.commit,
                    reflog_message="peoplebot development failed attempt checkpoint v0",
                    conflict_code="development.attempt_ref_conflict",
                    conflict_detail="attempt branch changed before partial checkpoint",
                    symbolic_code="development.attempt_ref_symbolic",
                    symbolic_detail="attempt branch became symbolic",
                    inspection_code="development.attempt_ref_unavailable",
                    inspection_detail="attempt branch cannot be inspected",
                    persistence_code="development.attempt_ref_failed",
                    persistence_detail="partial attempt could not be attached",
                )
                attempt_state = StateRef(task.repository, checkpoint)
                attempt_preservation = "checkpointed"
            except Exception:
                # The worktree and current ordinary branch remain the recovery surface.
                return

        def finish(*args: Any) -> DevelopmentObservation:
            nonlocal observation
            code = args[0] if args else ""
            if code != "development.completed":
                preserve_failed_attempt()
            observation = DevelopmentObservation(*args)
            observation.attempt_ref = attempt_ref if attempt_state is not None else None
            observation.attempt_state = attempt_state
            observation.attempt_preservation = attempt_preservation
            observation.invocation_started_at = invocation_started_at
            observation.invocation_finished_at = invocation_finished_at
            observation.elapsed_milliseconds = elapsed_milliseconds
            observation.runtime = self.configuration.runtime
            observation.model = self.configuration.model
            observation.provider_response_observed = provider_response_observed
            observation.usage = observed_usage
            if worktree_registered and code != "development.completed":
                observation.workspace_cleanup_disposition = "retained"
                observation.workspace_remnant = os.fspath(worktree)
                if not observation.changed_paths:
                    try:
                        observation.changed_paths = _changed_paths(worktree, 15)
                    except Exception:
                        pass
            return observation

        if worktree.exists():
            return finish(
                "development.worktree_conflict", "owned worktree path already exists", False,
                None, None, (), (),
            )
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        deadline = self.monotonic() + task.per_invocation_timeout_seconds

        def remaining() -> int:
            seconds = deadline - self.monotonic()
            if seconds < 1:
                raise DevelopmentError(
                    "development.timeout", "implementation authority deadline expired"
                )
            return int(seconds)

        plumbing = GitAttemptStore(project_path, task.repository)
        try:
            plumbing._update_ref(
                attempt_ref, starting_state.commit, _ZERO_OBJECT_ID,
                reflog_message="peoplebot development attempt branch v0",
                conflict_code="development.attempt_ref_conflict",
                conflict_detail="ordinary attempt branch already exists",
                symbolic_code="development.attempt_ref_symbolic",
                symbolic_detail="ordinary attempt branch is symbolic",
                inspection_code="development.attempt_ref_unavailable",
                inspection_detail="ordinary attempt branch cannot be inspected",
                persistence_code="development.attempt_ref_failed",
                persistence_detail="ordinary attempt branch could not be created",
            )
        except DevelopmentError as error:
            return finish(error.code, error.detail, False, None, None, (), ())
        attempt_state = StateRef(task.repository, starting_state.commit)
        attempt_preservation = "branch_at_base"
        result = _git_bytes(
            project_path, "worktree", "add", os.fspath(worktree),
            attempt_ref.removeprefix("refs/heads/"),
            timeout=min(30, remaining()),
        )
        if result.returncode != 0:
            failed = finish(
                "development.worktree_failed", "isolated worktree could not be created", False,
                None, None, (), (),
            )
            if worktree.exists():
                failed.workspace_cleanup_disposition = "manual_recovery_required"
                failed.workspace_remnant = os.fspath(worktree)
            return failed
        worktree_registered = True
        started = False
        exit_code: int | None = None
        ownership_unresolved = False
        changed: tuple[str, ...] = ()
        verified: list[tuple[str, ...]] = []
        try:
            head = _git_ok(
                worktree, "rev-parse", "HEAD", timeout=min(15, remaining())
            ).decode("ascii").strip()
            if head != starting_state.commit:
                raise DevelopmentError(
                    "development.starting_head_mismatch",
                    "isolated worktree HEAD does not equal the approved starting State",
                )
            prompt = stable_json_bytes({
                "allowed_paths": list(task.allowed_paths),
                "base_state": starting_state.to_dict(),
                "format": "peoplebot.development-request.v0",
                "instructions": (
                    "Implement only the supplied objective in the isolated worktree. Change only "
                    "allowed paths. Do not commit, push, publish, alter credentials, or invoke another model."
                ),
                "objective": objective,
                "task_id": task.task_id,
                "verification_commands": [list(item) for item in task.verification_commands],
            })
            if len(prompt) > self.configuration.max_prompt_bytes:
                return finish(
                    "development.input_limit_exceeded", "implementation prompt exceeds configured limit",
                    False, None, None, (), (),
                )
            environment = _codex_environment(self.codex_home)
            version = self.runner(
                (os.fspath(self.executable), "--version"), b"", environment,
                min(15, remaining()),
            )
            if version.returncode != 0 or version.stdout.decode("utf-8", "replace").strip() != (
                f"{self.configuration.runtime} {self.configuration.runtime_version}"
            ):
                return finish(
                    "development.runtime_mismatch", "Codex CLI version does not match exact Adapter State",
                    False, None, None, (), (),
                )
            command = (
                os.fspath(self.executable), "exec", "--json", "--skip-git-repo-check",
                "-C", os.fspath(worktree), "--sandbox", "workspace-write", "--model",
                self.configuration.model, "-",
            )
            started = True
            invocation_started_at = utc_now()
            invocation_started_monotonic = self.monotonic()
            try:
                completed = self.runner(
                    command, prompt, environment,
                    min(remaining(), self.configuration.timeout_seconds),
                )
            except ProcessOwnershipUnresolved:
                ownership_unresolved = True
                raise
            except subprocess.TimeoutExpired:
                invocation_finished_at = utc_now()
                elapsed_milliseconds = max(
                    0, int((self.monotonic() - invocation_started_monotonic) * 1000)
                )
                return finish(
                    "development.timeout", "implementation invocation exceeded its deadline",
                    True, None, None, (), (),
                )
            except OSError:
                invocation_finished_at = utc_now()
                elapsed_milliseconds = max(
                    0, int((self.monotonic() - invocation_started_monotonic) * 1000)
                )
                return finish(
                    "development.process_failed", "implementation process failed after start",
                    True, None, None, (), (),
                )
            invocation_finished_at = utc_now()
            elapsed_milliseconds = max(
                0, int((self.monotonic() - invocation_started_monotonic) * 1000)
            )
            exit_code = completed.returncode
            process_metadata, observed_usage = _diagnose_development_process_failure(completed)
            provider_response_observed = process_metadata.provider_response_observed
            if exit_code != 0:
                return finish(
                    "development.process_failed",
                    f"implementation process returned nonzero; {process_metadata.summary}",
                    True, exit_code, None, (), (), observed_usage, process_metadata,
                )
            if _git_ok(
                worktree, "rev-parse", "HEAD", timeout=min(15, remaining())
            ).decode("ascii").strip() != starting_state.commit:
                return finish(
                    "development.head_moved", "editing process moved or committed worktree HEAD",
                    True, exit_code, None, (), (),
                )
            changed = _changed_paths(worktree, min(15, remaining()))
            if not changed:
                return finish(
                    "development.no_change", "implementation produced no candidate diff",
                    True, exit_code, None, (), (),
                )
            if any(not _path_allowed(path, task.allowed_paths) for path in changed):
                return finish(
                    "development.path_violation", "candidate changed a path outside local authority",
                    True, exit_code, None, changed, (),
                )
            _git_ok(
                worktree, "add", "-A", "--", *task.allowed_paths,
                timeout=min(15, remaining()),
            )
            staged_paths = tuple(
                sorted(
                    item.decode("utf-8", "strict").replace("\\", "/")
                    for item in _git_ok(
                        worktree, "diff", "--cached", "--name-only", "-z",
                        starting_state.commit, "--",
                        timeout=min(15, remaining()),
                    ).split(b"\0")
                    if item
                )
            )
            if staged_paths != changed or any(
                not _path_allowed(path, task.allowed_paths) for path in staged_paths
            ):
                return finish(
                    "development.diff_mismatch",
                    "actual base-to-candidate diff does not match the approved working change set",
                    True, exit_code, None, staged_paths, (),
                )
            verified_tree = _git_ok(
                worktree, "write-tree", timeout=min(15, remaining())
            ).decode("ascii").strip()
            for verification in task.verification_commands:
                timeout = remaining()
                try:
                    check = self.command_runner(
                        verification, worktree, environment, timeout
                    )
                except ProcessOwnershipUnresolved:
                    ownership_unresolved = True
                    raise
                if check.returncode != 0:
                    return finish(
                        "development.verification_failed", "a required verification command failed",
                        True, exit_code, None, changed, tuple(verified),
                    )
                verified.append(verification)
            after_verification = _changed_paths(worktree, min(15, remaining()))
            if any(not _path_allowed(path, task.allowed_paths) for path in after_verification):
                return finish(
                    "development.path_violation",
                    "verification changed a path outside local authority",
                    True, exit_code, None, after_verification, tuple(verified),
                )
            _git_ok(
                worktree, "add", "-A", "--", *task.allowed_paths,
                timeout=min(15, remaining()),
            )
            final_tree = _git_ok(
                worktree, "write-tree", timeout=min(15, remaining())
            ).decode("ascii").strip()
            if final_tree != verified_tree:
                return finish(
                    "development.verified_content_changed",
                    "candidate bytes changed during or after required verification",
                    True, exit_code, None, after_verification, tuple(verified),
                )
            if _git_ok(
                worktree, "rev-parse", "HEAD", timeout=min(15, remaining())
            ).decode("ascii").strip() != starting_state.commit:
                return finish(
                    "development.head_moved", "worktree HEAD changed during verification",
                    True, exit_code, None, after_verification, tuple(verified),
                )
            plumbing = GitAttemptStore(
                project_path, task.repository, timeout_seconds=min(15, remaining())
            )
            candidate = plumbing._write_commit(
                final_tree, (starting_state.commit,),
                datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                task.commit_message,
            )
            _object_id(candidate, "candidate commit")
            candidate_tree = _git_ok(
                project_path, "rev-parse", f"{candidate}^{{tree}}",
                timeout=min(15, remaining()),
            ).decode("ascii").strip()
            if candidate_tree != verified_tree:
                raise DevelopmentError(
                    "development.candidate_tree_mismatch",
                    "candidate commit tree differs from the verified tree",
                )
            final_paths = tuple(
                sorted(
                    item.decode("utf-8", "strict").replace("\\", "/")
                    for item in _git_ok(
                        project_path, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                        starting_state.commit, candidate,
                        timeout=min(15, remaining()),
                    ).split(b"\0")
                    if item
                )
            )
            if final_paths != staged_paths or any(
                not _path_allowed(path, task.allowed_paths) for path in final_paths
            ):
                raise DevelopmentError(
                    "development.candidate_diff_mismatch",
                    "final committed diff is outside or differs from verified content",
                )
            plumbing = GitAttemptStore(
                project_path, task.repository, timeout_seconds=min(15, remaining())
            )
            plumbing._update_ref(
                attempt_ref, candidate, starting_state.commit,
                reflog_message="peoplebot development successful attempt checkpoint v0",
                conflict_code="development.attempt_ref_conflict",
                conflict_detail="attempt branch changed before candidate checkpoint",
                symbolic_code="development.attempt_ref_symbolic",
                symbolic_detail="attempt branch became symbolic",
                inspection_code="development.attempt_ref_unavailable",
                inspection_detail="attempt branch cannot be inspected",
                persistence_code="development.attempt_ref_failed",
                persistence_detail="candidate could not be attached to its attempt branch",
            )
            attempt_state = StateRef(task.repository, candidate)
            attempt_preservation = "candidate"
            symbolic = plumbing._git(
                "symbolic-ref", "--quiet", "--no-recurse", task.candidate_ref
            )
            if symbolic.returncode == 0:
                raise DevelopmentError(
                    "development.candidate_ref_symbolic",
                    "candidate ref is symbolic and was preserved",
                )
            if symbolic.returncode != 1:
                raise DevelopmentError(
                    "development.candidate_ref_unavailable",
                    "candidate ref directness is ambiguous",
                )
            current = _git_bytes(
                project_path, "rev-parse", "--verify", "--quiet", task.candidate_ref,
                timeout=min(15, remaining()),
            )
            if current.returncode == 1:
                expected = _ZERO_OBJECT_ID
            elif current.returncode == 0:
                expected = current.stdout.decode("ascii").strip()
                if expected != starting_state.commit:
                    raise DevelopmentError("development.candidate_ref_conflict", "candidate branch changed")
            else:
                raise DevelopmentError("development.candidate_ref_unavailable", "candidate branch is ambiguous")
            plumbing = GitAttemptStore(
                project_path, task.repository, timeout_seconds=min(15, remaining())
            )
            plumbing._update_ref(
                task.candidate_ref, candidate, expected,
                reflog_message="peoplebot development candidate v0",
                conflict_code="development.candidate_ref_conflict",
                conflict_detail="candidate branch changed",
                symbolic_code="development.candidate_ref_symbolic",
                symbolic_detail="candidate ref is symbolic and was preserved",
                inspection_code="development.candidate_ref_unavailable",
                inspection_detail="candidate ref directness is ambiguous",
                persistence_code="development.candidate_ref_failed",
                persistence_detail="candidate ref could not be advanced",
            )
            return finish(
                "development.completed", "host verified diff, commands, commit, and candidate ref",
                True, exit_code, StateRef(task.repository, candidate), final_paths, tuple(verified),
            )
        except DevelopmentError as error:
            return finish(
                error.code, error.detail, started, exit_code, None, changed, tuple(verified)
            )
        except Exception as error:
            return finish(
                "development.unexpected_failure",
                f"implementation raised {type(error).__module__}.{type(error).__qualname__}",
                started, exit_code, None, changed, tuple(verified),
            )
        finally:
            if not ownership_unresolved and observation is not None and observation.succeeded:
                removed = _git_bytes(
                    project_path, "worktree", "remove", "--force", os.fspath(worktree)
                )
                if observation is not None:
                    if removed.returncode == 0:
                        observation.workspace_cleanup_disposition = "removed"
                    else:
                        observation.workspace_cleanup_disposition = "manual_recovery_required"
                        observation.workspace_remnant = os.fspath(worktree)

    def run(
        self, runtime_root: str | Path, store: GitAttemptStore, start: Any,
        task: DevelopmentTask, finished_at: Callable[[], str], project_checkout: str | Path,
    ) -> DevelopmentExecutionResult:
        from .provenance import ExecutionStart

        if not isinstance(start, ExecutionStart):
            raise ValueError("start must be an ExecutionStart")
        if start.adapter != self.state or start.starting_state.repository != task.repository:
            raise ValueError("implementation Execution does not match adopted task/Adapter")
        self.project_checkout = Path(project_checkout)  # type: ignore[attr-defined]
        observation: DevelopmentObservation | None = None

        def record(status: ExecutionStatus, outcome: TerminalOutcome | None) -> ExecutionRecord:
            assert observation is not None
            return ExecutionRecord(
                execution_id=start.execution_id, environment_id=start.environment_id,
                instance_id=start.instance_id, objective=start.objective,
                started_at=start.started_at, finished_at=finished_at(),
                starting_state=start.starting_state, blueprint=start.blueprint,
                adapter=start.adapter, status=status, procedures=start.procedures,
                input_states=start.input_states, input_messages=start.input_messages,
                resulting_state=observation.candidate_state if status is ExecutionStatus.COMPLETED else None,
                terminal_outcome=outcome, usage=observation.usage,
                artifacts=(observation.candidate_state,) if observation.candidate_state and status is not ExecutionStatus.COMPLETED else (),
            )

        def invoke() -> ExecutionRecord:
            nonlocal observation
            observation = self.invoke(task, start.starting_state, start.execution_id, start.objective)
            if observation.succeeded:
                return record(ExecutionStatus.COMPLETED, None)
            return record(ExecutionStatus.FAILED, TerminalOutcome(observation.code, observation.detail))

        def failed(error: Exception) -> ExecutionRecord:
            nonlocal observation
            observation = DevelopmentObservation(
                "development.unexpected_failure",
                f"implementation raised {type(error).__module__}.{type(error).__qualname__}",
                False, None, None, (), (),
            )
            return record(ExecutionStatus.FAILED, TerminalOutcome(observation.code, observation.detail))

        def artifacts(_record: ExecutionRecord) -> Mapping[str, bytes]:
            assert observation is not None
            return {"adapter-observation.json": stable_json_bytes(observation.to_dict())}

        provenance = run_with_execution_provenance(
            runtime_root, store, start, invoke, failed, artifacts
        )
        return DevelopmentExecutionResult(observation, provenance)


@dataclass(frozen=True, slots=True)
class ReviewResult:
    candidate_state: StateRef
    response: ProjectReviewResponse | None
    evidence: StateRef | None
    process_started: bool
    code: str
    process_exit_code: int | None = None
    provider_response_observed: bool = False
    usage: tuple[UsageObservation, ...] = (UNKNOWN_USAGE,)
    runtime: str | None = None
    model: str | None = None
    execution_record: ExecutionRecord | None = None
    ordinary_terminal: bool = False


class ImplementationRunner(Protocol):
    def run(
        self, runtime_root: str | Path, store: GitAttemptStore, start: Any,
        task: DevelopmentTask, finished_at: Callable[[], str], project_checkout: str | Path,
    ) -> DevelopmentExecutionResult: ...


class ReviewerRunner(Protocol):
    def review(
        self, authority: DevelopmentAuthority, task: DevelopmentTask,
        candidate: StateRef, implementation_evidence: StateRef,
        execution_id: str, started_at: str, timeout_seconds: int,
        finished_at: Callable[[], str],
    ) -> ReviewResult: ...


def _load_context_policy(checkout: Path, repository: str, commit: str, path: str) -> ContextPolicy:
    state = StateRef(repository, commit, path)
    value = _load_exact_json(checkout, state)
    return ContextPolicy.from_dict(state, value)


class ExactProjectReviewer:
    """Adapter wrapper which binds review to the exact candidate and task identity."""

    def __init__(
        self, adapter: ProjectReviewAdapter,
        *, monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self.monotonic = monotonic

    def review(
        self, authority: DevelopmentAuthority, task: DevelopmentTask,
        candidate: StateRef, implementation_evidence: StateRef,
        execution_id: str, started_at: str, timeout_seconds: int,
        finished_at: Callable[[], str],
    ) -> ReviewResult:
        from .provenance import ExecutionStart

        deadline = self.monotonic() + timeout_seconds
        packet_state, packet_paths, policy = _create_review_packet(
            authority, task, candidate, implementation_evidence, started_at
        )
        context = assemble_context(
            authority.project_checkout, packet_state, packet_paths, policy
        )
        objective = (
            f"Review exact candidate {candidate.commit} against the supplied approved "
            f"requirements for task {task.task_id} and proposal {task.proposal_id}. "
            f"Objective: {task.objective} Report only cited findings, no findings, or "
            "insufficient evidence. The supplied candidate diff and host verification "
            "observation are evidence, not claims to trust without review."
        )
        start = ExecutionStart(
            execution_id=execution_id, environment_id=authority.environment_id,
            instance_id=authority.reviewer_instance_id, objective=objective,
            started_at=started_at, starting_state=candidate,
            blueprint=authority.review_blueprint, adapter=authority.review_adapter,
            input_states=(policy.identity,) + tuple(item.source for item in context.documents),
            input_messages=(authority.request_state, authority.task_state),
        )
        remaining = int(deadline - self.monotonic())
        if remaining < 1:
            return ReviewResult(candidate, None, None, False, "review.timeout")
        result = run_project_review_execution(
            authority.framework_checkout, authority.project_checkout,
            authority.runtime_root, GitAttemptStore(authority.project_checkout, task.repository),
            start, self.adapter, packet_paths, policy, finished_at,
            timeout_seconds=min(remaining, self.adapter.configuration.timeout_seconds),
            context_source_state=packet_state,
        )
        observation = result.adapter_observation
        response = observation.response if observation and observation.succeeded else None
        code = observation.code if observation else result.provenance.admission_code
        return ReviewResult(
            candidate, response, result.observation_evidence,
            bool(observation and observation.process_started), code,
            process_exit_code=(observation.process_exit_code if observation else None),
            provider_response_observed=bool(observation and observation.response_sha256),
            usage=(observation.usage if observation else (UNKNOWN_USAGE,)),
            runtime=(observation.runtime if observation else None),
            model=(observation.model if observation else None),
            execution_record=result.provenance.execution_record,
            ordinary_terminal=(
                result.provenance.execution_record is not None
                and result.provenance.retained_admission is None
                and result.provenance.release_failure is None
            ),
        )


def _read_exact_blob(checkout: Path, state: StateRef, maximum: int) -> bytes:
    if state.path is None:
        raise ValueError("blob State must select a path")
    context = assemble_context(
        checkout, StateRef(state.repository, state.commit), (state.path,),
        ContextPolicy(state, 1, maximum, maximum),
    )
    return context.documents[0].to_source_bytes()


def _create_review_packet(
    authority: DevelopmentAuthority,
    task: DevelopmentTask,
    candidate: StateRef,
    implementation_evidence: StateRef,
    created_at: str,
) -> tuple[StateRef, tuple[str, ...], ContextPolicy]:
    """Commit bounded heterogeneous exact inputs for one candidate review."""

    if candidate.repository != task.repository or implementation_evidence.repository != task.repository:
        raise ValueError("review packet States must belong to the task repository")
    request = _read_exact_blob(authority.project_checkout, authority.request_state, 8_192)
    implementation = _read_exact_blob(
        authority.project_checkout, implementation_evidence, 16_384
    )
    diff = _git_bytes(
        authority.project_checkout, "diff", "--no-ext-diff", "--no-color", "--unified=20",
        task.base_commit, candidate.commit, "--", *task.allowed_paths,
    )
    if diff.returncode != 0:
        raise DevelopmentError("review.diff_unavailable", "candidate diff is unavailable")
    if not diff.stdout or len(diff.stdout) > 24_576:
        raise DevelopmentError(
            "review.diff_limit_exceeded", "candidate diff is empty or exceeds 24576 bytes"
        )
    review_sources: list[dict[str, Any]] = []
    source_documents: dict[str, bytes] = {}
    for index, path in enumerate(task.review_context_paths, 1):
        source = StateRef(task.repository, candidate.commit, path)
        content = _read_exact_blob(authority.project_checkout, source, 32_768)
        packet_path = f"review-source-{index:02d}.txt"
        source_documents[packet_path] = content
        review_sources.append({
            "bytes": len(content),
            "packet_path": packet_path,
            "sha256": hashlib.sha256(content).hexdigest(),
            "source": source.to_dict(),
        })
    requirements = stable_json_bytes({
        "approved_task_state": authority.task_state.to_dict(),
        "candidate_state": candidate.to_dict(),
        "format": "peoplebot.development-review-requirements.v0",
        "implementation_evidence": implementation_evidence.to_dict(),
        "request_state": authority.request_state.to_dict(),
        "review_sources": review_sources,
        "task": task.to_dict(),
    })
    documents = {
        "requirements.json": requirements,
        "coordination-request.md": request,
        "candidate.diff": diff.stdout,
        "implementation-observation.json": implementation,
        **source_documents,
    }
    if sum(len(value) for value in documents.values()) > 49_152:
        raise DevelopmentError(
            "review.context_limit_exceeded", "review packet exceeds the adopted total context bound"
        )
    plumbing = GitAttemptStore(authority.project_checkout, task.repository)
    blobs = {name: plumbing._write_blob(content) for name, content in documents.items()}
    policy_content = stable_json_bytes({
        "exclusions": [], "format": "peoplebot.context-policy.v0",
        "max_blob_bytes": 32_768, "max_entries": len(documents),
        "max_total_blob_bytes": 49_152,
    })
    blobs["context-policy.json"] = plumbing._write_blob(policy_content)
    serialized = b"".join(
        f"100644 blob {blob}\t{name}\n".encode("ascii")
        for name, blob in sorted(blobs.items())
    )
    tree = plumbing._object_id(
        plumbing._git("mktree", input_bytes=serialized), "write the review packet tree"
    )
    commit = plumbing._write_commit(
        tree, (candidate.commit,), created_at,
        f"PeopleBot development review packet {task.task_id}",
    )
    digest = hashlib.sha256(
        f"{task.task_id}\0{candidate.commit}".encode("utf-8")
    ).hexdigest()
    ref_name = f"refs/peoplebot/development-review/v0/{digest}"
    current = plumbing._git("rev-parse", "--verify", "--quiet", ref_name)
    if current.returncode == 1:
        expected = _ZERO_OBJECT_ID
    elif current.returncode == 0:
        expected = current.stdout.decode("ascii", "replace").strip()
        if expected != commit:
            raise DevelopmentError("review.packet_conflict", "review packet ref conflicts")
        commit = expected
    else:
        raise DevelopmentError("review.packet_unavailable", "review packet ref is ambiguous")
    if expected == _ZERO_OBJECT_ID:
        plumbing._update_ref(
            ref_name, commit, expected,
            reflog_message="peoplebot development review packet v0",
            conflict_code="review.packet_conflict", conflict_detail="review packet changed",
            symbolic_code="review.packet_symbolic", symbolic_detail="review packet ref is symbolic",
            inspection_code="review.packet_unavailable", inspection_detail="review packet ref is ambiguous",
            persistence_code="review.packet_persist_failed", persistence_detail="review packet ref update failed",
        )
    state = StateRef(task.repository, commit)
    policy_state = StateRef(task.repository, commit, "context-policy.json")
    policy = ContextPolicy.from_dict(policy_state, json.loads(policy_content))
    return state, tuple(documents), policy


_TERMINAL_STAGES = frozenset({
    DevelopmentStage.ACCEPTED,
    DevelopmentStage.FINDINGS,
    DevelopmentStage.FAILED,
    DevelopmentStage.UNRESOLVED,
    DevelopmentStage.EXPORTED,
})


def _elapsed_milliseconds(started_at: str, finished_at: str) -> int:
    return max(0, int((_timestamp(finished_at, "finished_at") - _timestamp(
        started_at, "started_at"
    )).total_seconds() * 1000))


def _implementation_invocation_report(
    authority: DevelopmentAuthority,
    task: DevelopmentTask,
    execution: ExecutionStart,
    result: DevelopmentExecutionResult,
) -> DevelopmentInvocationReport:
    observation = result.observation
    provenance = getattr(result, "provenance", None)
    record = getattr(provenance, "execution_record", None)
    started_at = (
        getattr(observation, "invocation_started_at", None)
        if observation and getattr(observation, "invocation_started_at", None)
        else execution.started_at
    )
    finished_at = (
        getattr(observation, "invocation_finished_at", None)
        if observation and getattr(observation, "invocation_finished_at", None)
        else record.finished_at if record else execution.started_at
    )
    elapsed = (
        getattr(observation, "elapsed_milliseconds", None)
        if observation and getattr(observation, "elapsed_milliseconds", None) is not None
        else _elapsed_milliseconds(started_at, finished_at)
    )
    outcome = (
        observation.code if observation else
        provenance.task_failure.code if provenance and provenance.task_failure else
        provenance.persistence_failure.code if provenance and provenance.persistence_failure else
        provenance.admission_code if provenance else "development.result_unavailable"
    )
    usage = (
        getattr(observation, "usage", (UNKNOWN_USAGE,)) if observation else
        record.usage if record and record.usage else
        (UNKNOWN_USAGE,)
    )
    return DevelopmentInvocationReport(
        execution.execution_id, "implementer", authority.environment_id,
        authority.environment_id, authority.implementer_instance_id, task.task_id,
        started_at, finished_at, elapsed, outcome,
        bool(observation and observation.process_started),
        getattr(observation, "process_exit_code", None) if observation else None,
        bool(observation and getattr(observation, "provider_response_observed", False)),
        getattr(observation, "runtime", None) if observation else None,
        getattr(observation, "model", None) if observation else None,
        usage, result.observation_evidence,
        getattr(observation, "attempt_ref", None) if observation else None,
        getattr(observation, "attempt_state", None) if observation else None,
    )


def _review_invocation_report(
    authority: DevelopmentAuthority,
    task: DevelopmentTask,
    execution_id: str,
    started_at: str,
    fallback_finished_at: str,
    review: ReviewResult,
) -> DevelopmentInvocationReport:
    record = review.execution_record
    report_started = record.started_at if record else started_at
    report_finished = record.finished_at if record else fallback_finished_at
    return DevelopmentInvocationReport(
        execution_id, "reviewer", authority.environment_id, authority.environment_id,
        authority.reviewer_instance_id, task.task_id, report_started, report_finished,
        _elapsed_milliseconds(report_started, report_finished), review.code,
        review.process_started, review.process_exit_code,
        review.provider_response_observed, review.runtime, review.model,
        review.usage, review.evidence,
    )


def _ordinary_stopped_implementation_failure(result: DevelopmentExecutionResult) -> bool:
    observation = result.observation
    if (
        observation is None
        or getattr(observation, "succeeded", False)
        or getattr(observation, "candidate_state", None) is not None
    ):
        return False
    provenance = getattr(result, "provenance", None)
    if (
        provenance is None
        or provenance.execution_record is None
        or provenance.retained_admission is not None
        or provenance.release_failure is not None
    ):
        return False
    return not observation.code.startswith((
        "development.attempt_ref_",
        "development.candidate_ref_",
        "development.worktree_",
    ))


def _usage_summary(report: DevelopmentInvocationReport) -> str:
    measured = [
        f"{item.metric}={item.value} {item.unit}"
        for item in report.usage if item.value is not None
    ]
    return ", ".join(measured) if measured else "usage unknown"


def _result_for_progress(progress: DevelopmentProgress, *, provider_invoked: bool) -> TaskHandlerResult:
    states = tuple(
        item for item in (
            progress.candidate_state,
            progress.implementation_evidence,
            progress.review_evidence,
        ) if item is not None
    )
    if progress.stage in {DevelopmentStage.ACCEPTED, DevelopmentStage.EXPORTED}:
        disposition = TaskDisposition.COMPLETED
        text = (
            f"Accepted exact candidate {progress.candidate_state.commit}; "
            f"review outcome {progress.review_outcome}."
            if progress.candidate_state else "Accepted task."
        )
    elif progress.stage in {DevelopmentStage.FINDINGS, DevelopmentStage.FAILED}:
        disposition = TaskDisposition.FAILED
        text = (
            f"Stopped at {progress.stage.value}; review outcome "
            f"{progress.review_outcome or 'unavailable'}."
        )
    else:
        disposition = TaskDisposition.UNRESOLVED
        text = (
            f"Stopped unresolved at durable stage {progress.stage.value}; "
            "operator reconciliation is required before any new invocation."
        )
    diagnostic = progress.implementation_diagnostic
    if diagnostic is not None and progress.review_outcome == "development.process_failed":
        evidence = (
            progress.implementation_evidence.commit
            if progress.implementation_evidence is not None else "unavailable"
        )
        terminal_word = "failed" if progress.stage is DevelopmentStage.FAILED else "unresolved"
        text = (
            f"Stopped {terminal_word}: terminal code development.process_failed at implementer_process; "
            f"reservations {len(progress.invocation_reservations)}; child process started true; "
            f"provider processing observed {str(diagnostic.provider_processing_observed).lower()}; "
            f"verified provider response observed {str(diagnostic.provider_response_observed).lower()}; "
            f"diagnostic {diagnostic.classification}: {diagnostic.summary} "
            f"Local-only implementation evidence commit {evidence}."
        )
        if progress.stage is DevelopmentStage.UNRESOLVED:
            text += " Operator reconciliation is required before any new invocation."
    if progress.invocation_reports:
        text += " Calls: " + "; ".join(
            f"{item.role} {item.execution_id} {item.outcome}, {_usage_summary(item)}"
            for item in progress.invocation_reports
        ) + "."
    memory = stable_json_bytes({
        "candidate_state": progress.candidate_state.to_dict() if progress.candidate_state else None,
        "implementation_diagnostic": diagnostic.to_dict() if diagnostic else None,
        "invocation_reservations": list(progress.invocation_reservations),
        "invocation_reports": [item.to_dict() for item in progress.invocation_reports],
        "next_step": text,
        "review_outcome": progress.review_outcome,
        "task_id": progress.task_id,
    }).decode("utf-8")
    return TaskHandlerResult(
        disposition, text, states=states,
        memory_items=(("development/latest.json", memory),),
        provider_invoked=provider_invoked,
    )


class DevelopmentWorkflowHandler:
    """Production work-cycle handler for one exact, locally authorized development task."""

    def __init__(
        self, authority: DevelopmentAuthority, implementer: ImplementationRunner,
        reviewer: ReviewerRunner, *, now: Callable[[], datetime] | None = None,
    ) -> None:
        self.authority = authority
        self.implementer = implementer
        self.reviewer = reviewer
        self.now = now or (lambda: datetime.now(UTC))

    def __call__(self, message: Message) -> TaskHandlerResult:
        authority = self.authority
        if not authority.active:
            return TaskHandlerResult(
                TaskDisposition.UNRESOLVED,
                "Development authority is inactive; configure reviewed live bindings before launch.",
            )
        if (
            message.kind is not MessageKind.TASK
            or message.message_id != authority.selected_message_id
            or message.task_id == ""
            or authority.task_state not in message.states
            or authority.request_state not in message.states
        ):
            return TaskHandlerResult(
                TaskDisposition.FAILED,
                "Message is not the exact selected structured development task.",
            )
        task = load_development_task(authority.project_checkout, authority.task_state)
        if task.task_id != message.task_id or task.proposal_id != message.correlation_id:
            return TaskHandlerResult(
                TaskDisposition.FAILED,
                "Message task/proposal identity does not match the exact task State.",
            )
        if task.repository != authority.task_state.repository:
            return TaskHandlerResult(
                TaskDisposition.FAILED, "Task repository is outside the local authority binding."
            )
        validate_development_blueprint(authority.framework_checkout, authority.development_blueprint)
        resolve_state(authority.project_checkout, StateRef(task.repository, task.base_commit))
        store = DevelopmentProgressStore(
            authority.project_checkout, task.repository,
            development_progress_ref(authority.environment_id, task.task_id),
        )
        progress, progress_state = store.load()
        if progress is None:
            started_at = self.now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
            progress = DevelopmentProgress(
                task.task_id, task.digest, authority.request_state, authority.task_state,
                DevelopmentStage.IMPORTED, started_at,
            )
            try:
                progress_state = store.persist(progress, None)
            except Exception:
                return _result_for_progress(
                    replace(progress, stage=DevelopmentStage.UNRESOLVED,
                            review_outcome="progress.import_persist_failed"),
                    provider_invoked=False,
                )
        elif (
            progress.task_digest != task.digest
            or progress.task_state != authority.task_state
            or progress.request_state != authority.request_state
        ):
            return TaskHandlerResult(
                TaskDisposition.UNRESOLVED,
                "Durable progress belongs to conflicting task authority; no invocation occurred.",
            )
        if progress.stage in _TERMINAL_STAGES:
            return _result_for_progress(progress, provider_invoked=False)
        if progress.stage in {DevelopmentStage.IMPLEMENTER_RESERVED, DevelopmentStage.REVIEWER_RESERVED}:
            return _result_for_progress(progress, provider_invoked=False)
        started = _timestamp(progress.started_at, "started_at")

        def remaining_seconds() -> int:
            seconds = task.maximum_elapsed_seconds - int(
                (self.now() - started).total_seconds()
            )
            return max(0, seconds)

        if remaining_seconds() < 1:
            progress = replace(progress, stage=DevelopmentStage.FAILED, review_outcome="elapsed_budget_exhausted")
            try:
                store.persist(progress, progress_state)
            except Exception:
                progress = replace(progress, stage=DevelopmentStage.UNRESOLVED,
                                   review_outcome="elapsed_budget_and_progress_persist_failed")
            return _result_for_progress(progress, provider_invoked=False)

        provider_invoked = False
        while True:
            if progress.stage in {DevelopmentStage.IMPORTED, DevelopmentStage.CORRECTION_PENDING}:
                if remaining_seconds() < 1:
                    progress = replace(progress, stage=DevelopmentStage.FAILED,
                                       review_outcome="elapsed_budget_exhausted")
                    try:
                        store.persist(progress, progress_state)
                    except Exception:
                        progress = replace(progress, stage=DevelopmentStage.UNRESOLVED,
                                           review_outcome="elapsed_budget_and_progress_persist_failed")
                    return _result_for_progress(progress, provider_invoked=provider_invoked)
                if len(progress.invocation_reservations) >= task.maximum_invocations:
                    progress = replace(progress, stage=DevelopmentStage.FAILED, review_outcome="invocation_budget_exhausted")
                    try:
                        store.persist(progress, progress_state)
                    except Exception:
                        progress = replace(
                            progress, stage=DevelopmentStage.UNRESOLVED,
                            review_outcome="invocation_budget_and_progress_persist_failed",
                        )
                    return _result_for_progress(progress, provider_invoked=provider_invoked)
                ordinal = len(progress.invocation_reservations) + 1
                execution_id = f"{task.task_id}-implementer-{ordinal}"
                correction = progress.stage is DevelopmentStage.CORRECTION_PENDING
                progress = replace(
                    progress, stage=DevelopmentStage.IMPLEMENTER_RESERVED,
                    invocation_reservations=progress.invocation_reservations + (execution_id,),
                    corrections_used=progress.corrections_used + (1 if correction else 0),
                )
                try:
                    progress_state = store.persist(progress, progress_state)
                except Exception:
                    return _result_for_progress(
                        replace(progress, stage=DevelopmentStage.UNRESOLVED,
                                review_outcome="implementer_reservation_persist_failed"),
                        provider_invoked=provider_invoked,
                    )
                from .provenance import ExecutionStart
                starting = progress.candidate_state or StateRef(task.repository, task.base_commit)
                objective = task.objective
                if correction:
                    if not progress.review_findings or progress.review_evidence is None:
                        return _result_for_progress(
                            replace(progress, stage=DevelopmentStage.UNRESOLVED,
                                    review_outcome="correction.findings_unavailable"),
                            provider_invoked=provider_invoked,
                        )
                    objective += (
                        " Correct only these findings from the immediately preceding exact review "
                        f"at {json.dumps(progress.review_evidence.to_dict(), sort_keys=True, separators=(',', ':'))}: "
                        f"{progress.review_findings}"
                    )
                bounded_task = replace(
                    task,
                    per_invocation_timeout_seconds=min(
                        task.per_invocation_timeout_seconds, remaining_seconds()
                    ),
                )
                execution = ExecutionStart(
                    execution_id=execution_id, environment_id=authority.environment_id,
                    instance_id=authority.implementer_instance_id, objective=objective,
                    started_at=self.now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    starting_state=starting, blueprint=authority.development_blueprint,
                    adapter=authority.development_adapter,
                    input_states=(authority.task_state,), input_messages=(authority.request_state,),
                )
                result = self.implementer.run(
                    authority.runtime_root,
                    GitAttemptStore(authority.project_checkout, task.repository),
                    execution, bounded_task,
                    lambda: self.now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    authority.project_checkout,
                )
                invocation_report = _implementation_invocation_report(
                    authority, task, execution, result
                )
                provider_invoked = provider_invoked or bool(
                    result.observation and result.observation.process_started
                )
                candidate = result.observation.candidate_state if result.observation else None
                if candidate is None or result.observation_evidence is None:
                    ordinary_failure = _ordinary_stopped_implementation_failure(result)
                    progress = replace(
                        progress, stage=(
                            DevelopmentStage.FAILED
                            if ordinary_failure else DevelopmentStage.UNRESOLVED
                        ),
                        candidate_state=candidate or progress.candidate_state,
                        implementation_evidence=(
                            result.observation_evidence or progress.implementation_evidence
                        ),
                        implementation_diagnostic=(
                            getattr(result.observation, "process_diagnostic", None)
                            if result.observation else progress.implementation_diagnostic
                        ),
                        review_outcome=(
                            result.observation.code if result.observation
                            else "implementation.terminal_evidence_unavailable"
                        ),
                        invocation_reports=progress.invocation_reports + (invocation_report,),
                    )
                    try:
                        store.persist(progress, progress_state)
                    except Exception:
                        pass
                    return _result_for_progress(progress, provider_invoked=provider_invoked)
                progress = replace(
                    progress, stage=DevelopmentStage.CANDIDATE_READY,
                    candidate_state=candidate,
                    implementation_evidence=result.observation_evidence,
                    invocation_reports=progress.invocation_reports + (invocation_report,),
                )
                try:
                    progress_state = store.persist(progress, progress_state)
                except Exception:
                    return _result_for_progress(
                        replace(progress, stage=DevelopmentStage.UNRESOLVED,
                                review_outcome="candidate_ready_progress_persist_failed"),
                        provider_invoked=provider_invoked,
                    )

            if progress.stage is DevelopmentStage.CANDIDATE_READY:
                if remaining_seconds() < 1:
                    progress = replace(progress, stage=DevelopmentStage.FAILED,
                                       review_outcome="elapsed_before_review")
                    try:
                        store.persist(progress, progress_state)
                    except Exception:
                        progress = replace(progress, stage=DevelopmentStage.UNRESOLVED,
                                           review_outcome="elapsed_and_progress_persist_failed")
                    return _result_for_progress(progress, provider_invoked=provider_invoked)
                if len(progress.invocation_reservations) >= task.maximum_invocations:
                    progress = replace(progress, stage=DevelopmentStage.FAILED, review_outcome="invocation_budget_exhausted")
                    try:
                        store.persist(progress, progress_state)
                    except Exception:
                        progress = replace(
                            progress, stage=DevelopmentStage.UNRESOLVED,
                            review_outcome="invocation_budget_and_progress_persist_failed",
                        )
                    return _result_for_progress(progress, provider_invoked=provider_invoked)
                assert progress.candidate_state is not None
                ordinal = len(progress.invocation_reservations) + 1
                execution_id = f"{task.task_id}-reviewer-{ordinal}"
                progress = replace(
                    progress, stage=DevelopmentStage.REVIEWER_RESERVED,
                    invocation_reservations=progress.invocation_reservations + (execution_id,),
                )
                try:
                    progress_state = store.persist(progress, progress_state)
                except Exception:
                    return _result_for_progress(
                        replace(progress, stage=DevelopmentStage.UNRESOLVED,
                                review_outcome="reviewer_reservation_persist_failed"),
                        provider_invoked=provider_invoked,
                    )
                review_started_at = self.now().replace(microsecond=0).isoformat().replace(
                    "+00:00", "Z"
                )
                review = self.reviewer.review(
                    authority, task, progress.candidate_state,
                    progress.implementation_evidence, execution_id,
                    review_started_at,
                    min(task.per_invocation_timeout_seconds, remaining_seconds()),
                    lambda: self.now().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                )
                review_finished_at = self.now().replace(microsecond=0).isoformat().replace(
                    "+00:00", "Z"
                )
                review_report = _review_invocation_report(
                    authority, task, execution_id, review_started_at,
                    review_finished_at, review,
                )
                provider_invoked = provider_invoked or review.process_started
                if review.candidate_state != progress.candidate_state:
                    progress = replace(
                        progress, stage=DevelopmentStage.UNRESOLVED,
                        review_outcome="review.candidate_mismatch",
                        invocation_reports=progress.invocation_reports + (review_report,),
                    )
                    try:
                        store.persist(progress, progress_state)
                    except Exception:
                        progress = replace(progress, review_outcome="review_mismatch_and_progress_persist_failed")
                    return _result_for_progress(progress, provider_invoked=provider_invoked)
                if review.evidence is None or review.response is None:
                    progress = replace(
                        progress, stage=(
                            DevelopmentStage.FAILED
                            if review.ordinary_terminal else DevelopmentStage.UNRESOLVED
                        ),
                        review_evidence=review.evidence or progress.review_evidence,
                        review_outcome=review.code,
                        invocation_reports=progress.invocation_reports + (review_report,),
                    )
                    try:
                        store.persist(progress, progress_state)
                    except Exception:
                        pass
                    return _result_for_progress(progress, provider_invoked=provider_invoked)
                outcome = review.response.outcome
                findings_json = json.dumps(
                    [item.to_dict() for item in review.response.findings],
                    sort_keys=True, separators=(",", ":"),
                )
                if outcome == "no_findings":
                    progress = replace(
                        progress, stage=DevelopmentStage.ACCEPTED,
                        review_evidence=review.evidence, review_outcome=outcome,
                        review_findings=findings_json,
                        invocation_reports=progress.invocation_reports + (review_report,),
                    )
                elif outcome == "findings" and progress.corrections_used < task.maximum_corrections:
                    progress = replace(
                        progress, stage=DevelopmentStage.CORRECTION_PENDING,
                        review_evidence=review.evidence, review_outcome=outcome,
                        review_findings=findings_json,
                        invocation_reports=progress.invocation_reports + (review_report,),
                    )
                elif outcome == "findings":
                    progress = replace(
                        progress, stage=DevelopmentStage.FINDINGS,
                        review_evidence=review.evidence, review_outcome=outcome,
                        review_findings=findings_json,
                        invocation_reports=progress.invocation_reports + (review_report,),
                    )
                else:
                    progress = replace(
                        progress, stage=DevelopmentStage.UNRESOLVED,
                        review_evidence=review.evidence, review_outcome=outcome,
                        review_findings=findings_json,
                        invocation_reports=progress.invocation_reports + (review_report,),
                    )
                try:
                    progress_state = store.persist(progress, progress_state)
                except Exception:
                    return _result_for_progress(
                        replace(
                            progress, stage=DevelopmentStage.UNRESOLVED,
                            review_outcome=f"{outcome};review_progress_persist_failed",
                        ),
                        provider_invoked=provider_invoked,
                    )
                if remaining_seconds() < 1 and progress.stage is DevelopmentStage.CORRECTION_PENDING:
                    progress = replace(progress, stage=DevelopmentStage.FAILED,
                                       review_outcome="elapsed_before_correction")
                    try:
                        store.persist(progress, progress_state)
                    except Exception:
                        progress = replace(progress, stage=DevelopmentStage.UNRESOLVED,
                                           review_outcome="elapsed_and_progress_persist_failed")
                    return _result_for_progress(progress, provider_invoked=provider_invoked)
                if progress.stage is DevelopmentStage.CORRECTION_PENDING:
                    continue
                return _result_for_progress(progress, provider_invoked=provider_invoked)


def development_handler_registry(
    authority: DevelopmentAuthority,
    implementer: ImplementationRunner,
    reviewer: ReviewerRunner,
) -> Mapping[str, Callable[[Message], TaskHandlerResult]]:
    return {"development.execute": DevelopmentWorkflowHandler(authority, implementer, reviewer)}


def import_selected_coordination_task(
    authority: DevelopmentAuthority, task: DevelopmentTask, *, created_at: str,
    sender: str, recipient: str,
) -> Message:
    """Import only the configured exact structured task into sovereign messaging."""

    exact = load_development_task(authority.project_checkout, authority.task_state)
    if authority.task_state.repository != task.repository or exact.digest != task.digest:
        raise DevelopmentError("bridge.task_mismatch", "task repository is not locally authorized")
    return Message(
        message_id=authority.selected_message_id, kind=MessageKind.TASK,
        sender=sender, recipient=recipient, task_id=task.task_id,
        correlation_id=task.proposal_id, purpose="development.execute",
        content="Execute only the separately bound exact development task State.",
        created_at=created_at, states=(authority.request_state, authority.task_state),
    )


def render_coordination_reply(reply: Message, request: Message) -> bytes:
    """Render a bounded bootstrap Markdown reply after exact correlation validation."""

    validate_correlated_reply(reply, request)
    lines = [
        f"# PeopleBot coordination reply — {reply.message_id}", "",
        f"- Request: `{request.message_id}`", f"- Task: `{reply.task_id}`",
        f"- Correlation: `{reply.correlation_id}`", f"- Created: `{reply.created_at}`",
        "", reply.content, "", "## Exact States", "",
    ]
    lines.extend(
        f"- `{item.repository}@{item.commit}:{item.path or '.'}`" for item in reply.states
    )
    content = ("\n".join(lines) + "\n").encode("utf-8")
    if len(content) > 16_384:
        raise DevelopmentError("bridge.reply_limit_exceeded", "rendered reply is too large")
    return content


def create_coordination_reply_file(root: str | Path, relative_path: str, content: bytes) -> Path:
    """Create one append-only reply file; matching repeats reconcile, conflicts stop."""

    path_text = _path(relative_path, "coordination reply path")
    if not path_text.startswith("coordination/replies/") or not path_text.endswith(".md"):
        raise ValueError("coordination reply path is outside the bootstrap reply namespace")
    root_path = Path(root).resolve()
    destination = (root_path / Path(*PurePosixPath(path_text).parts)).resolve()
    if root_path not in destination.parents:
        raise ValueError("coordination reply escapes its checkout")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("xb") as stream:
            stream.write(content)
    except FileExistsError:
        if destination.read_bytes() != content:
            raise DevelopmentError("bridge.reply_conflict", "reply path has conflicting content")
    return destination


@dataclass(frozen=True, slots=True)
class DevelopmentCycleOperations:
    active: bool
    import_sender: str
    import_created_at: str
    memory_checkout: Path
    memory_repository: str
    memory_expected_state: StateRef
    memory_initial: bool
    coordination_checkout: Path
    coordination_repository: str
    coordination_remote: str
    coordination_expected_url: str
    coordination_ref: str
    coordination_reply_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.active, bool) or not isinstance(self.memory_initial, bool):
            raise ValueError("operations active/initial fields must be booleans")
        _identifier(self.import_sender, "import_sender")
        _timestamp(self.import_created_at, "import_created_at")
        for value, field in (
            (self.memory_checkout, "memory_checkout"),
            (self.coordination_checkout, "coordination_checkout"),
        ):
            if not value.is_absolute():
                raise ValueError(f"{field} must be absolute")
        _text(self.memory_repository, "memory_repository", 512)
        _text(self.coordination_repository, "coordination_repository", 512)
        _identifier(self.coordination_remote, "coordination_remote")
        _text(self.coordination_expected_url, "coordination_expected_url", 2048)
        if not self.coordination_ref.startswith("refs/heads/"):
            raise ValueError("coordination_ref must be a branch ref")
        _path(self.coordination_reply_path, "coordination_reply_path")
        if not self.coordination_reply_path.startswith("coordination/replies/"):
            raise ValueError("coordination reply path is outside its namespace")


def development_cycle_operations_from_dict(value: object) -> DevelopmentCycleOperations:
    fields = {
        "active", "coordination_checkout", "coordination_expected_url",
        "coordination_ref", "coordination_remote", "coordination_reply_path",
        "coordination_repository", "format", "import_created_at", "import_sender", "memory_checkout",
        "memory_expected_state", "memory_initial", "memory_repository",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("development cycle operations fields are invalid")
    if value.get("format") != "peoplebot.development-cycle-operations.v0":
        raise ValueError("development cycle operations format is invalid")
    return DevelopmentCycleOperations(
        value["active"], value["import_sender"], value["import_created_at"],  # type: ignore[arg-type]
        _absolute(value["memory_checkout"], "memory_checkout"),
        value["memory_repository"],  # type: ignore[arg-type]
        _state(value["memory_expected_state"], "memory_expected_state", path_required=False),
        value["memory_initial"],  # type: ignore[arg-type]
        _absolute(value["coordination_checkout"], "coordination_checkout"),
        value["coordination_repository"], value["coordination_remote"],  # type: ignore[arg-type]
        value["coordination_expected_url"], value["coordination_ref"],  # type: ignore[arg-type]
        value["coordination_reply_path"],  # type: ignore[arg-type]
    )


def load_development_cycle_operations(path: str | Path) -> DevelopmentCycleOperations:
    source = Path(path)
    try:
        content = source.read_bytes()
    except OSError as error:
        raise DevelopmentError("development.operations_unavailable", "operations file is unavailable") from error
    if not content or len(content) > _MAX_JSON_BYTES:
        raise ValueError("development operations must contain 1-65536 bytes")
    try:
        return development_cycle_operations_from_dict(json.loads(content))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("development operations is not valid UTF-8 JSON") from error


@dataclass(frozen=True, slots=True)
class DevelopmentReaderRecovery:
    active: bool
    reconciliation_id: str
    expected_reader_progress: StateRef
    expected_reader_task: TaskProgress
    expected_cycle_status_sha256: str
    expected_development_progress: StateRef
    expected_implementation_evidence: StateRef
    terminal_code: str
    evidence_states: tuple[StateRef, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.active, bool):
            raise ValueError("recovery active must be a boolean")
        _identifier(self.reconciliation_id, "reconciliation_id")
        if self.expected_reader_progress.path != "reader-progress.json":
            raise ValueError("expected_reader_progress must select reader-progress.json")
        if self.expected_reader_task.disposition is not TaskDisposition.UNRESOLVED:
            raise ValueError("expected reader task must be unresolved")
        if not re.fullmatch(r"[0-9a-f]{64}", self.expected_cycle_status_sha256):
            raise ValueError("expected_cycle_status_sha256 must be a SHA-256 digest")
        if self.expected_development_progress.path != "progress.json":
            raise ValueError("expected_development_progress must select progress.json")
        if self.expected_implementation_evidence.path != "adapter-observation.json":
            raise ValueError(
                "expected_implementation_evidence must select adapter-observation.json"
            )
        _identifier(self.terminal_code, "terminal_code")
        if not self.evidence_states or len(self.evidence_states) > 8:
            raise ValueError("recovery requires one to eight evidence States")
        required = {
            self.expected_development_progress,
            self.expected_implementation_evidence,
        }
        if not required.issubset(set(self.evidence_states)):
            raise ValueError("recovery evidence omits required development States")


def _task_progress_from_dict(value: object) -> TaskProgress:
    if not isinstance(value, Mapping) or set(value) != {
        "disposition", "memory_state", "message_id", "message_state", "reply_state", "task_id"
    }:
        raise ValueError("expected reader task fields are invalid")
    try:
        disposition = TaskDisposition(value["disposition"])
    except (TypeError, ValueError) as error:
        raise ValueError("expected reader task disposition is invalid") from error
    return TaskProgress(
        _state(value["message_state"], "message_state", path_required=True),
        value["message_id"],  # type: ignore[arg-type]
        value["task_id"],  # type: ignore[arg-type]
        disposition,
        None if value["reply_state"] is None else _state(value["reply_state"], "reply_state"),
        None if value["memory_state"] is None else _state(value["memory_state"], "memory_state"),
    )


def development_reader_recovery_from_dict(value: object) -> DevelopmentReaderRecovery:
    fields = {
        "active", "evidence_states", "expected_cycle_status_sha256",
        "expected_development_progress", "expected_implementation_evidence",
        "expected_reader_progress", "expected_reader_task", "format",
        "reconciliation_id", "terminal_code",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("development reader recovery fields are invalid")
    if value.get("format") != "peoplebot.development-reader-recovery.v0":
        raise ValueError("development reader recovery format is invalid")
    evidence = value.get("evidence_states")
    if not isinstance(evidence, list):
        raise ValueError("recovery evidence_states must be an array")
    return DevelopmentReaderRecovery(
        value["active"],  # type: ignore[arg-type]
        value["reconciliation_id"],  # type: ignore[arg-type]
        _state(value["expected_reader_progress"], "expected_reader_progress"),
        _task_progress_from_dict(value["expected_reader_task"]),
        value["expected_cycle_status_sha256"],  # type: ignore[arg-type]
        _state(value["expected_development_progress"], "expected_development_progress"),
        _state(value["expected_implementation_evidence"], "expected_implementation_evidence"),
        value["terminal_code"],  # type: ignore[arg-type]
        tuple(_state(item, "evidence_state") for item in evidence),
    )


def load_development_reader_recovery(path: str | Path) -> DevelopmentReaderRecovery:
    source = Path(path)
    try:
        content = source.read_bytes()
    except OSError as error:
        raise DevelopmentError(
            "development.recovery_unavailable", "reader recovery file is unavailable"
        ) from error
    if not content or len(content) > _MAX_JSON_BYTES:
        raise ValueError("development reader recovery must contain 1-65536 bytes")
    try:
        return development_reader_recovery_from_dict(json.loads(content))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("development reader recovery is not valid UTF-8 JSON") from error


def reconcile_development_cycle_reader(
    bindings: CycleBindings,
    recovery: DevelopmentReaderRecovery,
    execution_id: str,
    reconciled_at: str,
) -> ReaderReconciliationResult:
    """Evidence-check and close one exact stopped development failure."""

    if not recovery.active:
        return ReaderReconciliationResult(
            "development.recovery_inactive", False, None, None, None
        )
    _timestamp(reconciled_at, "reconciled_at")
    if (
        recovery.expected_reader_progress.repository != bindings.local_repository
        or recovery.expected_reader_task.message_state.repository != bindings.local_repository
        or recovery.expected_development_progress.repository != bindings.local_repository
        or recovery.expected_implementation_evidence.repository != bindings.local_repository
    ):
        raise ValueError("recovery repository does not match cycle bindings")

    reconciliation = ReaderReconciliation(
        recovery.reconciliation_id,
        recovery.expected_reader_progress,
        recovery.expected_reader_task,
        recovery.terminal_code,
        recovery.evidence_states,
        reconciled_at,
    )

    def evidence_check() -> None:
        for state in recovery.evidence_states:
            try:
                resolve_state(bindings.local_checkout, state)
            except StateResolutionError as error:
                raise DevelopmentError(
                    "development.recovery_evidence_unavailable",
                    "a pinned recovery evidence State is unavailable",
                ) from error
        try:
            status_bytes = bindings.status_path.read_bytes()
        except OSError as error:
            raise DevelopmentError(
                "development.recovery_status_unavailable",
                "the exact prior cycle status is unavailable",
            ) from error
        if not status_bytes or len(status_bytes) > _MAX_JSON_BYTES:
            raise DevelopmentError(
                "development.recovery_status_invalid",
                "cycle status is empty or exceeds the bounded JSON limit",
            )
        if hashlib.sha256(status_bytes).hexdigest() != recovery.expected_cycle_status_sha256:
            raise DevelopmentError(
                "development.recovery_status_changed",
                "cycle status differs from the exact reviewed prior bytes",
            )
        try:
            status = json.loads(status_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DevelopmentError(
                "development.recovery_status_invalid", "cycle status is malformed"
            ) from error
        expected_task = recovery.expected_reader_task
        if (
            not isinstance(status, Mapping)
            or status.get("format") != "peoplebot.work-cycle-status.v0"
            or status.get("code") not in {"cycle.unresolved", "cycle.unresolved_halt"}
            or status.get("disposition") != "unresolved"
            or status.get("message_state") != expected_task.message_state.to_dict()
            or status.get("reply_state")
            != (expected_task.reply_state.to_dict() if expected_task.reply_state else None)
            or status.get("memory_state")
            != (expected_task.memory_state.to_dict() if expected_task.memory_state else None)
            or status.get("progress_state") != recovery.expected_reader_progress.to_dict()
            or status.get("status_persisted") is not True
        ):
            raise DevelopmentError(
                "development.recovery_status_mismatch",
                "cycle status does not describe the exact reviewed unresolved task",
            )
        progress_ref = development_progress_ref(
            bindings.environment_id, expected_task.task_id
        )
        progress, state = DevelopmentProgressStore(
            bindings.local_checkout, bindings.local_repository, progress_ref
        ).load()
        if state != recovery.expected_development_progress or progress is None:
            raise DevelopmentError(
                "development.recovery_progress_changed",
                "development progress differs from the exact reviewed prior State",
            )
        if (
            progress.task_id != expected_task.task_id
            or progress.review_outcome != recovery.terminal_code
            or progress.candidate_state is not None
            or progress.review_evidence is not None
            or progress.implementation_evidence != recovery.expected_implementation_evidence
            or len(progress.invocation_reservations) != 1
        ):
            raise DevelopmentError(
                "development.recovery_progress_mismatch",
                "development progress does not prove the reviewed terminal failure",
            )
        observation = _load_exact_json(
            bindings.local_checkout, recovery.expected_implementation_evidence
        )
        execution = _load_exact_json(
            bindings.local_checkout,
            StateRef(
                bindings.local_repository,
                recovery.expected_implementation_evidence.commit,
                "execution.json",
            ),
        )
        terminal = execution.get("terminal_outcome")
        if (
            observation.get("code") != recovery.terminal_code
            or observation.get("process_started") is not True
            or observation.get("process_exit_code") != 1
            or observation.get("candidate_state") is not None
            or execution.get("status") != "failed"
            or not isinstance(terminal, Mapping)
            or terminal.get("code") != recovery.terminal_code
            or execution.get("resulting_state") is not None
        ):
            raise DevelopmentError(
                "development.recovery_evidence_mismatch",
                "terminal evidence does not prove the reviewed stopped failure",
            )

    return reconcile_reader_task(
        bindings, reconciliation, execution_id, evidence_check
    )


@dataclass(frozen=True, slots=True)
class CoordinationFilePublication:
    code: str
    disposition: PublicationDisposition
    state: StateRef | None
    observed_remote: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "disposition": self.disposition.value,
            "observed_remote": self.observed_remote,
            "state": self.state.to_dict() if self.state else None,
        }


def _remote_branch_tip(
    checkout: Path, remote: str, ref_name: str, timeout_seconds: int = 30
) -> str | None:
    result = _git_bytes(checkout, "ls-remote", "--refs", remote, ref_name, timeout=timeout_seconds)
    if result.returncode != 0:
        raise DevelopmentError("bridge.remote_unavailable", "coordination remote is unavailable")
    if not result.stdout:
        return None
    lines = result.stdout.decode("ascii", "replace").splitlines()
    if len(lines) != 1:
        raise DevelopmentError("bridge.remote_ambiguous", "coordination remote ref is ambiguous")
    fields = lines[0].split("\t")
    if len(fields) != 2 or fields[1] != ref_name or not _OBJECT_ID.fullmatch(fields[0]):
        raise DevelopmentError("bridge.remote_invalid", "coordination remote ref is invalid")
    return fields[0]


def publish_coordination_reply_file(
    operations: DevelopmentCycleOperations,
    content: bytes,
    created_at: str,
) -> CoordinationFilePublication:
    """Create, commit, normally push, and verify one configured append-only reply."""

    checkout = operations.coordination_checkout
    remote_url = _git_ok(checkout, "remote", "get-url", "--push", operations.coordination_remote).decode(
        "utf-8", "replace"
    ).strip()
    if remote_url != operations.coordination_expected_url:
        raise DevelopmentError("bridge.remote_mismatch", "coordination push URL is not the approved endpoint")
    branch = _git_ok(checkout, "symbolic-ref", "--quiet", "HEAD").decode("ascii").strip()
    if branch != operations.coordination_ref:
        raise DevelopmentError("bridge.branch_mismatch", "coordination checkout is on another branch")
    status = _git_ok(checkout, "status", "--porcelain=v1", "-z")
    if status:
        raise DevelopmentError("bridge.checkout_dirty", "coordination checkout has unrelated work")
    local = _git_ok(checkout, "rev-parse", "HEAD").decode("ascii").strip()
    remote = _remote_branch_tip(checkout, operations.coordination_remote, operations.coordination_ref)
    relative = operations.coordination_reply_path
    destination = checkout / Path(*PurePosixPath(relative).parts)
    if destination.exists():
        if destination.read_bytes() != content:
            raise DevelopmentError("bridge.reply_conflict", "coordination reply has conflicting content")
        state = StateRef(operations.coordination_repository, local, relative)
        if remote == local:
            return CoordinationFilePublication(
                "bridge.remote_verified", PublicationDisposition.REMOTE_VERIFIED, state, remote
            )
        return CoordinationFilePublication(
            "bridge.prior_publication_unresolved", PublicationDisposition.UNCERTAIN, state, remote
        )
    if remote != local:
        raise DevelopmentError(
            "bridge.local_remote_mismatch", "coordination checkout is not at the verified remote tip"
        )
    create_coordination_reply_file(checkout, relative, content)
    added = _git_bytes(checkout, "add", "--", relative)
    if added.returncode != 0:
        raise DevelopmentError("bridge.stage_failed", "coordination reply could not be staged")
    committed = _git_bytes(
        checkout, "-c", "user.name=PeopleBot", "-c", "user.email=peoplebot@invalid",
        "-c", "commit.gpgsign=false", "commit", "-m",
        f"Post PeopleBot development result {created_at}",
    )
    if committed.returncode != 0:
        raise DevelopmentError("bridge.commit_failed", "coordination reply could not be committed")
    commit = _git_ok(checkout, "rev-parse", "HEAD").decode("ascii").strip()
    state = StateRef(operations.coordination_repository, commit, relative)
    try:
        pushed = _git_bytes(
            checkout, "push", "--porcelain", "--no-tags", "--no-recurse-submodules",
            operations.coordination_remote, f"{commit}:{operations.coordination_ref}",
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pushed = None
    try:
        observed = _remote_branch_tip(
            checkout, operations.coordination_remote, operations.coordination_ref
        )
    except DevelopmentError:
        observed = None
    if observed == commit:
        return CoordinationFilePublication(
            "bridge.remote_verified", PublicationDisposition.REMOTE_VERIFIED, state, observed
        )
    if pushed is not None and pushed.returncode != 0 and observed == remote:
        return CoordinationFilePublication(
            "bridge.publication_failed", PublicationDisposition.FAILED, state, observed
        )
    return CoordinationFilePublication(
        "bridge.publication_uncertain", PublicationDisposition.UNCERTAIN, state, observed
    )


@dataclass(frozen=True, slots=True)
class DevelopmentCycleCommandResult:
    cycle: CycleStatus | None
    import_state: StateRef | None
    coordination: CoordinationFilePublication | None
    code: str
    provider_invoked: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "coordination": self.coordination.to_dict() if self.coordination else None,
            "cycle": self.cycle.to_dict() if self.cycle else None,
            "format": "peoplebot.development-cycle-command-result.v0",
            "import_state": self.import_state.to_dict() if self.import_state else None,
            "provider_invoked": self.provider_invoked,
        }


def run_development_cycle_command(
    authority: DevelopmentAuthority,
    operations: DevelopmentCycleOperations,
    bindings: CycleBindings,
    policy: TaskPolicy,
    implementer: ImplementationRunner,
    reviewer: ReviewerRunner,
    execution_id: str,
    timestamp: str,
) -> DevelopmentCycleCommandResult:
    """Supported import -> cycle -> memory -> reply-export production composition."""

    if not authority.active or not operations.active:
        return DevelopmentCycleCommandResult(
            None, None, None, "development.inactive", False
        )
    if (
        bindings.environment_id != authority.environment_id
        or bindings.instance_id != authority.coordinator_instance_id
        or operations.memory_repository != bindings.local_repository
        or operations.memory_checkout != bindings.local_checkout
    ):
        raise ValueError("operations/cycle bindings do not match development authority")
    matching_sources = tuple(
        source for source in bindings.sources
        if source.repository == bindings.destination.repository
        and source.remote == bindings.destination.remote
        and source.expected_url == bindings.destination.expected_url
        and source.ref_name == bindings.destination.ref_name
        and operations.import_sender in source.allowed_senders
        and authority.environment_id in source.allowed_senders
    )
    if len(matching_sources) != 1:
        raise ValueError("cycle must read exactly one approved owner-published import source")
    task = load_development_task(authority.project_checkout, authority.task_state)
    request = import_selected_coordination_task(
        authority, task, created_at=operations.import_created_at,
        sender=operations.import_sender, recipient=authority.environment_id,
    )
    message_store = OutboundMessageStore(
        bindings.local_checkout, bindings.local_repository, bindings.outbound_ref
    )
    remote_tip = inspect_remote_tip(
        bindings.local_checkout, bindings.destination, push=True
    )
    local_tip = message_store._current()
    if local_tip != remote_tip:
        raise DevelopmentError(
            "bridge.message_local_remote_mismatch",
            "owner message history is not at its verified remote tip",
        )
    existing = tuple(
        item for item in message_store.messages(local_tip)
        if item.message.message_id == request.message_id
    )
    if existing:
        if len(existing) != 1 or existing[0].message.to_json_bytes() != request.to_json_bytes():
            raise DevelopmentError("bridge.message_conflict", "selected import ID conflicts")
        imported = existing[0]
    else:
        publication = append_and_publish_owned_message(
            bindings.runtime_root, bindings.environment_id,
            f"import:{execution_id}", message_store, request, bindings.destination,
        )
        if publication.publication.disposition is not PublicationDisposition.REMOTE_VERIFIED:
            return DevelopmentCycleCommandResult(
                None, publication.local.state, None,
                publication.publication.code, False,
            )
        imported = publication.local

    def memory_checkpoint(selected: PublishedMessage, result: TaskHandlerResult) -> StateRef | None:
        items = tuple(MemoryItem(path, content) for path, content in result.memory_items)
        if not items:
            return None
        start = ExecutionStart(
            execution_id=f"memory:{execution_id}",
            environment_id=authority.environment_id,
            instance_id=authority.coordinator_instance_id,
            objective="Checkpoint the exact development-cycle decision and next step.",
            started_at=timestamp,
            starting_state=operations.memory_expected_state,
            blueprint=authority.development_blueprint,
            adapter=authority.development_adapter,
            input_states=result.states,
            input_messages=(selected.state,),
        )
        checkpoint = run_instance_memory_execution(
            bindings.runtime_root,
            GitAttemptStore(operations.memory_checkout, operations.memory_repository),
            GitMemoryStore(operations.memory_checkout, operations.memory_repository),
            start,
            MemoryCheckpointRequest(
                operations.memory_repository, authority.environment_id,
                authority.coordinator_instance_id, authority.development_blueprint,
                operations.memory_expected_state, items, timestamp,
                initial=operations.memory_initial,
            ),
            lambda: timestamp,
        )
        if checkpoint.checkpoint is None:
            raise DevelopmentError(
                "development.memory_checkpoint_failed", "memory checkpoint is unavailable"
            )
        return checkpoint.checkpoint.state

    cycle = run_work_cycle_tick(
        bindings, policy, development_handler_registry(authority, implementer, reviewer),
        execution_id, timestamp, timestamp, memory_checkpoint=memory_checkpoint,
    )
    if cycle.message_state is not None and cycle.message_state != imported.state:
        return DevelopmentCycleCommandResult(
            cycle,
            imported.state,
            None,
            "bridge.cycle_message_mismatch",
            cycle.provider_invoked,
        )
    if cycle.reply_state is None:
        return DevelopmentCycleCommandResult(
            cycle, imported.state, None, cycle.code, cycle.provider_invoked
        )
    refreshed_tip = inspect_remote_tip(
        bindings.local_checkout, bindings.destination, push=True
    )
    replies = tuple(
        item for item in message_store.messages(refreshed_tip)
        if item.state == cycle.reply_state
    )
    if len(replies) != 1:
        return DevelopmentCycleCommandResult(
            cycle, imported.state, None, "bridge.reply_state_unavailable",
            cycle.provider_invoked,
        )
    rendered = render_coordination_reply(replies[0].message, request)
    coordination = publish_coordination_reply_file(operations, rendered, timestamp)
    export_progress_ok = True
    if coordination.disposition is PublicationDisposition.REMOTE_VERIFIED:
        progress_store = DevelopmentProgressStore(
            authority.project_checkout, task.repository,
            development_progress_ref(authority.environment_id, task.task_id),
        )
        progress, progress_state = progress_store.load()
        if progress is None or progress_state is None:
            export_progress_ok = False
        elif progress.stage is not DevelopmentStage.EXPORTED or progress.reply_state != coordination.state:
            try:
                progress_store.persist(
                    replace(
                        progress,
                        stage=(
                            DevelopmentStage.EXPORTED
                            if progress.stage is DevelopmentStage.ACCEPTED
                            else progress.stage
                        ),
                        reply_state=coordination.state,
                    ),
                    progress_state,
                )
            except Exception:
                export_progress_ok = False
    complete_export = (
        cycle.memory_state is not None
        and coordination.disposition is PublicationDisposition.REMOTE_VERIFIED
        and export_progress_ok
    )
    if complete_export and cycle.disposition == "completed":
        code = "development.completed"
    elif complete_export and cycle.disposition == "failed":
        code = "development.failed"
    else:
        code = "development.unresolved"
    return DevelopmentCycleCommandResult(
        cycle, imported.state, coordination, code, cycle.provider_invoked
    )
