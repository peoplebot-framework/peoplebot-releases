"""In-progress bounded validation for exact Instance-memory synchronization."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from ._json import stable_json_bytes
from .adapters.codex_read_only import (
    DirectProcessFailure,
    DirectProcessSetupFailure,
    DirectProcessTimeout,
    _run_process,
)
from .memory import (
    GitMemoryStore,
    MemoryError,
    _CONTENT_DIRECTORY,
    _MAX_ITEM_BYTES,
    _MAX_ITEMS,
    _MAX_METADATA_BYTES,
    _MAX_TOTAL_BYTES,
    _METADATA_PATH,
    _memory_path,
    _validate_metadata_binding,
    instance_memory_ref,
    read_instance_memory_metadata,
)
from .execution import ExecutionRecord, ExecutionStatus, TerminalOutcome
from .provenance import (
    AttemptEvidenceStore,
    ExecutionStart,
    GitAttemptStore,
    ProvenanceError,
    ProvenanceRunResult,
    _ZERO_OBJECT_ID,
    run_with_execution_provenance,
)
from .state import (
    _GIT_GLOBAL_OPTIONS,
    _git_environment,
    StateRef,
    StateResolutionError,
    resolve_state,
)


_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FULL_REF = re.compile(r"^refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]*$")


class SynchronizationDisposition(StrEnum):
    LOCAL_ONLY = "local_only"
    REMOTE_VERIFIED = "remote_verified"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class SynchronizationError(RuntimeError):
    """A classified refusal before any remote mutation."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class SynchronizationLimits:
    """Versioned practical bounds for one Git transport operation."""

    timeout_seconds: int = 30
    max_lineage_commits: int = 256

    def __post_init__(self) -> None:
        if isinstance(self.timeout_seconds, bool) or not 1 <= self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be an integer from 1 through 120")
        if isinstance(self.max_lineage_commits, bool) or not 1 <= self.max_lineage_commits <= 4096:
            raise ValueError("max_lineage_commits must be an integer from 1 through 4096")

    def to_dict(self) -> dict[str, object]:
        return {
            "format": "peoplebot.memory-synchronization-limits.v0",
            "max_lineage_commits": self.max_lineage_commits,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class MemorySynchronizationRequest:
    repository: str
    environment_id: str
    instance_id: str
    blueprint: StateRef
    memory_state: StateRef
    authorized_baseline: StateRef
    destination_ref: str
    remote_name: str
    expected_destination_url: str
    expected_remote_state: StateRef | None
    limits: SynchronizationLimits = SynchronizationLimits()

    def __post_init__(self) -> None:
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise ValueError("repository must be non-empty")
        for field, value in (
            ("environment_id", self.environment_id),
            ("instance_id", self.instance_id),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field} must be non-empty with no surrounding whitespace")
        for field, value in (
            ("blueprint", self.blueprint),
            ("memory_state", self.memory_state),
            ("authorized_baseline", self.authorized_baseline),
        ):
            if not isinstance(value, StateRef):
                raise ValueError(f"{field} must be a StateRef")
        if self.memory_state.path is not None or self.authorized_baseline.path is not None:
            raise ValueError("memory_state and authorized_baseline must be repository-level")
        if any(
            state.repository != self.repository
            for state in (self.memory_state, self.authorized_baseline)
        ):
            raise ValueError("memory and baseline repositories must match repository")
        if self.expected_remote_state is not None:
            if (
                not isinstance(self.expected_remote_state, StateRef)
                or self.expected_remote_state.path is not None
                or self.expected_remote_state.repository != self.repository
            ):
                raise ValueError("expected_remote_state must be a matching repository-level StateRef")
        expected_ref = instance_memory_ref(self.environment_id, self.instance_id)
        if self.destination_ref != expected_ref or not _FULL_REF.fullmatch(self.destination_ref):
            raise ValueError("destination_ref must be the full canonical Instance-memory ref")
        if not isinstance(self.remote_name, str) or not _REMOTE_NAME.fullmatch(self.remote_name):
            raise ValueError("remote_name uses unsupported syntax")
        _require_safe_destination(self.expected_destination_url)
        if not isinstance(self.limits, SynchronizationLimits):
            raise ValueError("limits must be SynchronizationLimits")


@dataclass(frozen=True, slots=True)
class MemoryRecoveryRequest:
    synchronization: MemorySynchronizationRequest
    operation_id: str
    expected_local_state: StateRef | None
    original_execution_stopped: bool
    resume_existing: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.synchronization, MemorySynchronizationRequest):
            raise ValueError("synchronization must be MemorySynchronizationRequest")
        if (
            not isinstance(self.operation_id, str)
            or not self.operation_id
            or self.operation_id != self.operation_id.strip()
            or len(self.operation_id) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in self.operation_id)
        ):
            raise ValueError("operation_id must be bounded non-empty text")
        target = self.synchronization.memory_state
        if self.synchronization.expected_remote_state != target:
            raise ValueError("recovery requires expected_remote_state to equal memory_state")
        if self.expected_local_state is not None and (
            not isinstance(self.expected_local_state, StateRef)
            or self.expected_local_state.path is not None
            or self.expected_local_state.repository != target.repository
        ):
            raise ValueError("expected_local_state must be a matching repository-level StateRef")
        if self.original_execution_stopped is not True:
            raise ValueError("recovery requires explicit confirmation that the original Execution stopped")
        if not isinstance(self.resume_existing, bool):
            raise ValueError("resume_existing must be a boolean")


@dataclass(frozen=True, slots=True)
class SynchronizationPreflight:
    destination_url: str
    destination_digest: str
    lineage: tuple[str, ...]
    observed_remote_commit: str | None


@dataclass(frozen=True, slots=True)
class SynchronizationObservation:
    operation: str
    disposition: SynchronizationDisposition
    code: str
    repository: str
    environment_id: str
    instance_id: str
    blueprint: StateRef
    authorized_baseline: StateRef
    limits: SynchronizationLimits
    attempted_state: StateRef
    remote_name: str
    destination_ref: str
    destination_digest: str
    expected_remote_commit: str | None
    observed_remote_commit: str | None
    quarantine_ref: str | None = None
    recovery_refs_retained: bool | None = False
    operation_id: str | None = None
    expected_local_commit: str | None = None
    owner_ref: str | None = None
    recovery_resume: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "attempted_state": self.attempted_state.to_dict(),
            "authorized_baseline": self.authorized_baseline.to_dict(),
            "blueprint": self.blueprint.to_dict(),
            "code": self.code,
            "destination_digest": self.destination_digest,
            "destination_ref": self.destination_ref,
            "disposition": self.disposition.value,
            "environment_id": self.environment_id,
            "expected_remote_commit": self.expected_remote_commit,
            "format": "peoplebot.memory-synchronization-observation.v0",
            "instance_id": self.instance_id,
            "limits": self.limits.to_dict(),
            "observed_remote_commit": self.observed_remote_commit,
            "operation": self.operation,
            "operation_id": self.operation_id,
            "owner_ref": self.owner_ref,
            "quarantine_ref": self.quarantine_ref,
            "recovery_refs_retained": self.recovery_refs_retained,
            "recovery_resume": self.recovery_resume,
            "remote_name": self.remote_name,
            "repository": self.repository,
            "expected_local_commit": self.expected_local_commit,
        }

    def to_json_bytes(self) -> bytes:
        return stable_json_bytes(self.to_dict())


@dataclass(frozen=True, slots=True)
class MemorySynchronizationExecutionResult:
    observation: SynchronizationObservation | None
    provenance: ProvenanceRunResult

    @property
    def observation_evidence(self) -> StateRef | None:
        terminal = self.provenance.terminal_evidence
        if terminal is None or self.observation is None:
            return None
        return StateRef(
            terminal.state.repository,
            terminal.state.commit,
            "synchronization-observation.json",
        )


@dataclass(frozen=True, slots=True)
class MemoryRecoveryResult:
    observation: SynchronizationObservation
    recovered_state: StateRef | None
    quarantine_ref: str
    owner_ref: str


@dataclass(frozen=True, slots=True)
class MemoryRecoveryExecutionResult:
    recovery: MemoryRecoveryResult | None
    provenance: ProvenanceRunResult

    @property
    def observation(self) -> SynchronizationObservation | None:
        return self.recovery.observation if self.recovery is not None else None

    @property
    def observation_evidence(self) -> StateRef | None:
        terminal = self.provenance.terminal_evidence
        if terminal is None or self.recovery is None:
            return None
        return StateRef(
            terminal.state.repository,
            terminal.state.commit,
            "synchronization-observation.json",
        )


def _require_safe_destination(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("expected_destination_url must be non-empty printable text")
    parsed = urlsplit(value)
    if parsed.password is not None or (parsed.scheme and "@" in parsed.netloc):
        raise ValueError("credential-bearing destination URLs are unsupported")


def _destination_digest(value: str) -> str:
    return hashlib.sha256(
        b"peoplebot.memory-synchronization.destination.v0\0" + value.encode("utf-8")
    ).hexdigest()


def _git(checkout: Path, timeout: int, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = _git_environment()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    try:
        return subprocess.run(
            [
                "git",
                *_GIT_GLOBAL_OPTIONS,
                "-C",
                os.fspath(checkout),
                "-c",
                f"core.hooksPath={os.devnull}",
                *arguments,
            ],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=environment,
            shell=False,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise SynchronizationError("git.unavailable", "Git executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise SynchronizationError("git.timeout", "local Git validation exceeded its bound") from error


def _lines(result: subprocess.CompletedProcess[str], code: str, detail: str) -> tuple[str, ...]:
    if result.returncode != 0:
        raise SynchronizationError(code, detail)
    return tuple(line for line in result.stdout.splitlines() if line)


def _resolve_destination(checkout: Path, request: MemorySynchronizationRequest) -> str:
    rewrites = _git(
        checkout,
        request.limits.timeout_seconds,
        "config",
        "--null",
        "--get-regexp",
        r"^[uU][rR][lL]\.",
    )
    rewrite_keys = tuple(
        record.split("\n", 1)[0].lower()
        for record in rewrites.stdout.split("\0")
        if record
    )
    if any(key.endswith((".insteadof", ".pushinsteadof")) for key in rewrite_keys):
        raise SynchronizationError(
            "synchronization.url_rewrite_unsupported",
            "Git URL rewrite configuration is unsupported for this destination",
        )
    if rewrites.returncode not in {0, 1}:
        raise SynchronizationError(
            "synchronization.configuration_unavailable",
            "Git could not inspect effective URL rewrite configuration",
        )
    result = _git(
        checkout,
        request.limits.timeout_seconds,
        "remote",
        "get-url",
        "--push",
        "--all",
        request.remote_name,
    )
    urls = _lines(
        result,
        "synchronization.destination_unavailable",
        "Git could not resolve the configured push destination",
    )
    if len(urls) != 1:
        raise SynchronizationError(
            "synchronization.destination_ambiguous",
            "the configured remote must resolve to exactly one push destination",
        )
    _require_safe_destination(urls[0])
    if urls[0] != request.expected_destination_url:
        raise SynchronizationError(
            "synchronization.destination_mismatch",
            "the resolved push destination does not match the authorized destination",
        )
    receive_pack = _git(
        checkout,
        request.limits.timeout_seconds,
        "config",
        "--get-all",
        f"remote.{request.remote_name}.receivepack",
    )
    if receive_pack.returncode == 0 and receive_pack.stdout.strip():
        raise SynchronizationError(
            "synchronization.transport_override_unsupported",
            "the configured remote has an unsupported receive-pack override",
        )
    if receive_pack.returncode not in {0, 1}:
        raise SynchronizationError(
            "synchronization.configuration_unavailable",
            "Git could not inspect remote transport configuration",
        )
    mirror = _git(
        checkout,
        request.limits.timeout_seconds,
        "config",
        "--bool",
        "--get",
        f"remote.{request.remote_name}.mirror",
    )
    if mirror.returncode == 0 and mirror.stdout.strip() == "true":
        raise SynchronizationError(
            "synchronization.transport_override_unsupported",
            "the configured remote is a mirror and is unsupported",
        )
    if mirror.returncode not in {0, 1}:
        raise SynchronizationError(
            "synchronization.configuration_unavailable",
            "Git could not inspect remote mirror configuration",
        )
    return urls[0]


def _validate_memory_binding(checkout: Path, request: MemorySynchronizationRequest, commit: str) -> None:
    state = StateRef(request.repository, commit)
    metadata = read_instance_memory_metadata(checkout, state)
    _validate_metadata_binding(
        metadata,
        request.repository,
        request.environment_id,
        request.instance_id,
        request.blueprint,
    )
    _validate_memory_tree(checkout, request, state, metadata)


def _metadata_item(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != {
        "bytes",
        "git_path",
        "object_id",
        "path",
        "sha256",
    }:
        raise SynchronizationError(
            "synchronization.memory_tree_invalid",
            "memory metadata contains an unsupported item shape",
        )
    return value


def _validate_memory_tree(
    checkout: Path,
    request: MemorySynchronizationRequest,
    state: StateRef,
    metadata: Mapping[str, object],
) -> None:
    if set(metadata) != {
        "blueprint",
        "environment_id",
        "format",
        "instance_id",
        "items",
        "ref_name",
        "repository",
    }:
        raise SynchronizationError(
            "synchronization.memory_tree_invalid",
            "memory metadata contains unsupported or missing fields",
        )
    items_value = metadata.get("items")
    if not isinstance(items_value, list) or len(items_value) > _MAX_ITEMS:
        raise SynchronizationError(
            "synchronization.memory_tree_invalid",
            "memory metadata item count is invalid",
        )
    expected_entries: dict[str, str] = {}
    paths: list[str] = []
    total = 0
    for raw_item in items_value:
        item = _metadata_item(raw_item)
        path = item.get("path")
        git_path = item.get("git_path")
        object_id = item.get("object_id")
        byte_count = item.get("bytes")
        digest = item.get("sha256")
        try:
            canonical = _memory_path(path)  # type: ignore[arg-type]
        except (TypeError, ValueError) as error:
            raise SynchronizationError(
                "synchronization.memory_tree_invalid",
                "memory metadata contains a noncanonical item path",
            ) from error
        if (
            git_path != f"{_CONTENT_DIRECTORY}/{canonical}"
            or not isinstance(object_id, str)
            or not _OBJECT_ID.fullmatch(object_id)
            or isinstance(byte_count, bool)
            or not isinstance(byte_count, int)
            or byte_count < 0
            or byte_count > _MAX_ITEM_BYTES
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise SynchronizationError(
                "synchronization.memory_tree_invalid",
                "memory metadata item fields are invalid",
            )
        paths.append(canonical)
        total += byte_count
        if canonical in expected_entries:
            raise SynchronizationError(
                "synchronization.memory_tree_invalid",
                "memory metadata item paths are not unique",
            )
        size = _git(checkout, request.limits.timeout_seconds, "cat-file", "-s", object_id)
        if size.returncode != 0 or size.stdout.strip() != str(byte_count):
            raise SynchronizationError(
                "synchronization.memory_tree_invalid",
                "memory item size or object availability does not match metadata",
            )
        content = _git_bytes(
            checkout,
            request.limits.timeout_seconds,
            "cat-file",
            "blob",
            object_id,
        )
        try:
            content.stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SynchronizationError(
                "synchronization.memory_tree_invalid",
                "memory item content is not valid UTF-8",
            ) from error
        if (
            content.returncode != 0
            or b"\0" in content.stdout
            or hashlib.sha256(content.stdout).hexdigest() != digest
        ):
            raise SynchronizationError(
                "synchronization.memory_tree_invalid",
                "memory item content digest does not match metadata",
            )
        expected_entries[git_path] = object_id
    if paths != sorted(paths) or len(paths) != len(set(paths)) or total > _MAX_TOTAL_BYTES:
        raise SynchronizationError(
            "synchronization.memory_tree_invalid",
            "memory item order, uniqueness, or total size is invalid",
        )
    metadata_object = _git(
        checkout,
        request.limits.timeout_seconds,
        "rev-parse",
        "--verify",
        f"{state.commit}:{_METADATA_PATH}",
    )
    metadata_oid = metadata_object.stdout.strip()
    if metadata_object.returncode != 0 or not _OBJECT_ID.fullmatch(metadata_oid):
        raise SynchronizationError(
            "synchronization.memory_tree_invalid",
            "memory metadata blob is unavailable",
        )
    metadata_size = _git(checkout, request.limits.timeout_seconds, "cat-file", "-s", metadata_oid)
    if (
        metadata_size.returncode != 0
        or not metadata_size.stdout.strip().isdigit()
        or int(metadata_size.stdout.strip()) > _MAX_METADATA_BYTES
    ):
        raise SynchronizationError(
            "synchronization.memory_tree_invalid",
            "memory metadata exceeds its supported bound",
        )
    metadata_content = _git_bytes(
        checkout,
        request.limits.timeout_seconds,
        "cat-file",
        "blob",
        metadata_oid,
    )
    if (
        metadata_content.returncode != 0
        or metadata_content.stdout != stable_json_bytes(dict(metadata))
    ):
        raise SynchronizationError(
            "synchronization.memory_tree_invalid",
            "memory metadata is not the canonical supported JSON record",
        )
    expected_entries[_METADATA_PATH] = metadata_oid
    try:
        expected_tree = GitMemoryStore(checkout, request.repository)._write_tree(expected_entries)
        actual_tree = resolve_state(checkout, state).root_tree
    except (MemoryError, StateResolutionError, ValueError) as error:
        raise SynchronizationError(
            "synchronization.memory_tree_invalid",
            "memory tree could not be reconstructed from its manifest",
        ) from error
    if expected_tree != actual_tree:
        raise SynchronizationError(
            "synchronization.memory_tree_invalid",
            "memory commit contains undeclared paths, modes, types, or objects",
        )


def _git_bytes(checkout: Path, timeout: int, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = _git_environment()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    try:
        return subprocess.run(
            [
                "git",
                *_GIT_GLOBAL_OPTIONS,
                "-C",
                os.fspath(checkout),
                "-c",
                f"core.hooksPath={os.devnull}",
                *arguments,
            ],
            capture_output=True,
            check=False,
            env=environment,
            shell=False,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise SynchronizationError("git.unavailable", "Git executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise SynchronizationError("git.timeout", "local Git validation exceeded its bound") from error


def _validate_lineage(checkout: Path, request: MemorySynchronizationRequest) -> tuple[str, ...]:
    try:
        resolve_state(checkout, request.authorized_baseline)
        resolve_state(checkout, request.memory_state)
    except StateResolutionError as error:
        raise SynchronizationError(error.code, error.detail) from error
    ancestry = _git(
        checkout,
        request.limits.timeout_seconds,
        "merge-base",
        "--is-ancestor",
        request.authorized_baseline.commit,
        request.memory_state.commit,
    )
    if ancestry.returncode == 1:
        raise SynchronizationError(
            "synchronization.unauthorized_ancestry",
            "the selected memory State is not descended from the authorized baseline",
        )
    if ancestry.returncode != 0:
        raise SynchronizationError(
            "synchronization.lineage_unavailable",
            "Git could not establish the authorized ancestry",
        )
    listed = _lines(
        _git(
            checkout,
            request.limits.timeout_seconds,
            "rev-list",
            "--reverse",
            f"{request.authorized_baseline.commit}..{request.memory_state.commit}",
        ),
        "synchronization.lineage_unavailable",
        "Git could not enumerate the authorized memory lineage",
    )
    if not listed or len(listed) > request.limits.max_lineage_commits:
        raise SynchronizationError(
            "synchronization.lineage_limit_exceeded",
            "the memory lineage is empty or exceeds the configured bound",
        )
    previous = request.authorized_baseline.commit
    for commit in listed:
        parents = _lines(
            _git(checkout, request.limits.timeout_seconds, "show", "-s", "--format=%P", commit),
            "synchronization.lineage_unavailable",
            "Git could not inspect a memory commit parent",
        )
        if len(parents) != 1 or parents[0] != previous:
            raise SynchronizationError(
                "synchronization.unsupported_lineage",
                "memory publication requires one direct non-merge chain above the baseline",
            )
        try:
            _validate_memory_binding(checkout, request, commit)
        except Exception as error:
            if isinstance(error, SynchronizationError):
                raise
            code = getattr(error, "code", "synchronization.memory_invalid")
            detail = getattr(error, "detail", "a memory lineage commit is invalid")
            raise SynchronizationError(code, detail) from error
        previous = commit
    if request.expected_remote_state is not None:
        allowed = {request.authorized_baseline.commit, *listed}
        if request.expected_remote_state.commit not in allowed:
            raise SynchronizationError(
                "synchronization.remote_boundary_unauthorized",
                "the expected remote State is outside the authorized memory lineage",
            )
        _validate_memory_binding(checkout, request, request.expected_remote_state.commit)
    return listed


def _inspect_remote(
    checkout: Path,
    request: MemorySynchronizationRequest,
    destination_url: str,
) -> str | None:
    result = _transport_git(
        checkout,
        request.limits.timeout_seconds,
        "ls-remote",
        "--refs",
        destination_url,
        request.destination_ref,
    )
    if result.returncode != 0:
        raise SynchronizationError(
            "synchronization.remote_inspection_failed",
            "the authorized destination ref could not be inspected",
        )
    output = result.stdout.decode("utf-8", "replace")
    lines = tuple(line for line in output.splitlines() if line)
    if not lines:
        return None
    if len(lines) != 1:
        raise SynchronizationError(
            "synchronization.remote_inspection_ambiguous",
            "remote inspection returned more than one matching ref",
        )
    fields = lines[0].split("\t")
    if len(fields) != 2 or not _OBJECT_ID.fullmatch(fields[0]) or fields[1] != request.destination_ref:
        raise SynchronizationError(
            "synchronization.remote_inspection_invalid",
            "remote inspection returned an invalid exact-ref observation",
        )
    return fields[0]


def _transport_git(
    checkout: Path,
    timeout: int,
    *arguments: str,
) -> subprocess.CompletedProcess[bytes]:
    environment = _git_environment()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GCM_INTERACTIVE"] = "Never"
    return _run_process(
        (
            "git",
            *_GIT_GLOBAL_OPTIONS,
            "-C",
            os.fspath(checkout),
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "push.followTags=false",
            "-c",
            "submodule.recurse=false",
            *arguments,
        ),
        b"",
        environment,
        timeout,
    )


def _observation(
    request: MemorySynchronizationRequest,
    destination_digest: str,
    disposition: SynchronizationDisposition,
    code: str,
    observed: str | None,
    *,
    operation: str,
    quarantine_ref: str | None = None,
    recovery_refs_retained: bool = False,
    operation_id: str | None = None,
    expected_local_commit: str | None = None,
    owner_ref: str | None = None,
    recovery_resume: bool = False,
) -> SynchronizationObservation:
    return SynchronizationObservation(
        operation=operation,
        disposition=disposition,
        code=code,
        repository=request.repository,
        environment_id=request.environment_id,
        instance_id=request.instance_id,
        blueprint=request.blueprint,
        authorized_baseline=request.authorized_baseline,
        limits=request.limits,
        attempted_state=request.memory_state,
        remote_name=request.remote_name,
        destination_ref=request.destination_ref,
        destination_digest=destination_digest,
        expected_remote_commit=(
            request.expected_remote_state.commit
            if request.expected_remote_state is not None
            else None
        ),
        observed_remote_commit=observed,
        quarantine_ref=quarantine_ref,
        recovery_refs_retained=recovery_refs_retained,
        operation_id=operation_id,
        expected_local_commit=expected_local_commit,
        owner_ref=owner_ref,
        recovery_resume=recovery_resume,
    )


def validate_memory_synchronization(
    checkout: str | Path,
    request: MemorySynchronizationRequest,
) -> SynchronizationPreflight:
    """Validate exact source, authorized lineage, destination, and remote preflight."""

    if not isinstance(request, MemorySynchronizationRequest):
        raise ValueError("request must be MemorySynchronizationRequest")
    checkout_path = Path(checkout)
    if not checkout_path.is_dir():
        raise SynchronizationError("repository.unavailable", "checkout directory does not exist")
    lineage = _validate_lineage(checkout_path, request)
    source = _git(
        checkout_path,
        request.limits.timeout_seconds,
        "show-ref",
        "--verify",
        "--hash",
        request.destination_ref,
    )
    source_lines = _lines(
        source,
        "synchronization.source_ref_unavailable",
        "the canonical local Instance-memory ref is unavailable",
    )
    if source_lines != (request.memory_state.commit,):
        raise SynchronizationError(
            "synchronization.source_ref_mismatch",
            "the canonical local Instance-memory ref does not equal the pinned State",
        )
    destination_url = _resolve_destination(checkout_path, request)
    observed = _inspect_remote(checkout_path, request, destination_url)
    expected = (
        request.expected_remote_state.commit
        if request.expected_remote_state is not None
        else None
    )
    if observed != expected:
        raise SynchronizationError(
            "synchronization.remote_state_mismatch",
            "the observed remote ref does not equal the expected State or absence",
        )
    return SynchronizationPreflight(
        destination_url,
        _destination_digest(destination_url),
        lineage,
        observed,
    )


def synchronize_instance_memory(
    checkout: str | Path,
    request: MemorySynchronizationRequest,
) -> SynchronizationObservation:
    """Publish one exact memory commit with an explicit normal refspec.

    This transport collaborator does not itself acquire admission or persist
    provenance; the review-ready public wrapper remains pending.
    """

    checkout_path = Path(checkout)
    preflight = validate_memory_synchronization(checkout_path, request)
    try:
        pushed = _transport_git(
            checkout_path,
            request.limits.timeout_seconds,
            "push",
            "--porcelain",
            "--no-follow-tags",
            "--no-recurse-submodules",
            preflight.destination_url,
            f"{request.memory_state.commit}:{request.destination_ref}",
        )
    except (DirectProcessTimeout, DirectProcessFailure, DirectProcessSetupFailure):
        return _observation(
            request,
            preflight.destination_digest,
            SynchronizationDisposition.UNCERTAIN,
            "synchronization.transport_uncertain",
            preflight.observed_remote_commit,
            operation="synchronize",
        )
    if pushed.returncode != 0:
        return _observation(
            request,
            preflight.destination_digest,
            SynchronizationDisposition.UNCERTAIN,
            "synchronization.transport_result_uncertain",
            preflight.observed_remote_commit,
            operation="synchronize",
        )
    try:
        observed = _inspect_remote(checkout_path, request, preflight.destination_url)
    except (
        SynchronizationError,
        DirectProcessTimeout,
        DirectProcessFailure,
        DirectProcessSetupFailure,
    ):
        return _observation(
            request,
            preflight.destination_digest,
            SynchronizationDisposition.UNCERTAIN,
            "synchronization.verification_unavailable",
            None,
            operation="synchronize",
        )
    if observed != request.memory_state.commit:
        return _observation(
            request,
            preflight.destination_digest,
            SynchronizationDisposition.UNCERTAIN,
            "synchronization.verification_mismatch",
            observed,
            operation="synchronize",
        )
    return _observation(
        request,
        preflight.destination_digest,
        SynchronizationDisposition.REMOTE_VERIFIED,
        "synchronization.remote_verified",
        observed,
        operation="synchronize",
    )


def reconcile_instance_memory(
    checkout: str | Path,
    request: MemorySynchronizationRequest,
) -> SynchronizationObservation:
    """Inspect an earlier uncertain attempt without retrying publication."""

    checkout_path = Path(checkout)
    _validate_lineage(checkout_path, request)
    destination = _resolve_destination(checkout_path, request)
    digest = _destination_digest(destination)
    try:
        observed = _inspect_remote(checkout_path, request, destination)
    except (SynchronizationError, DirectProcessTimeout, DirectProcessFailure, DirectProcessSetupFailure):
        return _observation(
            request,
            digest,
            SynchronizationDisposition.UNCERTAIN,
            "synchronization.reconciliation_unavailable",
            None,
            operation="reconcile",
        )
    if observed == request.memory_state.commit:
        return _observation(
            request,
            digest,
            SynchronizationDisposition.REMOTE_VERIFIED,
            "synchronization.remote_verified",
            observed,
            operation="reconcile",
        )
    expected = (
        request.expected_remote_state.commit
        if request.expected_remote_state is not None
        else None
    )
    if observed == expected:
        return _observation(
            request,
            digest,
            SynchronizationDisposition.UNCERTAIN,
            "synchronization.prior_state_observed",
            observed,
            operation="reconcile",
        )
    return _observation(
        request,
        digest,
        SynchronizationDisposition.FAILED,
        "synchronization.intervening_state",
        observed,
        operation="reconcile",
    )


def _recovery_refs(request: MemoryRecoveryRequest, destination_digest: str) -> tuple[str, str]:
    sync = request.synchronization
    digest = hashlib.sha256(b"peoplebot.memory-recovery.operation.v0\0")
    for value in (
        sync.repository,
        sync.environment_id,
        sync.instance_id,
        request.operation_id,
        sync.memory_state.commit,
        sync.destination_ref,
        destination_digest,
    ):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    identity = digest.hexdigest()
    return (
        f"refs/peoplebot/quarantine/v0/{identity}",
        f"refs/peoplebot/quarantine-owners/v0/{identity}",
    )


def _ref_value(checkout: Path, timeout: int, ref_name: str) -> str | None:
    symbolic = _git(
        checkout,
        timeout,
        "symbolic-ref",
        "--quiet",
        "--no-recurse",
        ref_name,
    )
    if symbolic.returncode == 0:
        raise SynchronizationError(
            "recovery.ref_symbolic",
            "a recovery ref is symbolic and was preserved",
        )
    if symbolic.returncode != 1:
        raise SynchronizationError(
            "recovery.ref_inspection_failed",
            "Git could not establish direct recovery-ref identity",
        )
    inspected = _git(
        checkout,
        timeout,
        "for-each-ref",
        "--count=2",
        "--format=%(refname)%00%(objectname)%00%(symref)",
        ref_name,
    )
    if inspected.returncode != 0:
        raise SynchronizationError(
            "recovery.ref_inspection_failed",
            "Git could not inspect an exact recovery ref",
        )
    records = tuple(line for line in inspected.stdout.splitlines() if line)
    if not records:
        return None
    if len(records) != 1:
        raise SynchronizationError(
            "recovery.ref_inspection_failed",
            "Git returned ambiguous recovery-ref identity",
        )
    fields = records[0].split("\0")
    if len(fields) != 3 or fields[0] != ref_name or not _OBJECT_ID.fullmatch(fields[1]):
        raise SynchronizationError(
            "recovery.ref_inspection_failed",
            "Git returned invalid recovery-ref identity",
        )
    if fields[2]:
        raise SynchronizationError(
            "recovery.ref_symbolic",
            "a recovery ref is symbolic and was preserved",
        )
    return fields[1]


def _recovery_refs_retained(
    checkout: Path,
    timeout: int,
    quarantine_ref: str,
    owner_ref: str,
) -> bool | None:
    try:
        return any(
            _ref_value(checkout, timeout, ref_name) is not None
            for ref_name in (quarantine_ref, owner_ref)
        )
    except SynchronizationError as error:
        if error.code == "recovery.ref_symbolic":
            return True
        return None


def _recovery_observation(
    request: MemoryRecoveryRequest,
    destination_digest: str,
    disposition: SynchronizationDisposition,
    code: str,
    observed: str | None,
    quarantine_ref: str,
    retained: bool | None,
) -> SynchronizationObservation:
    return _observation(
        request.synchronization,
        destination_digest,
        disposition,
        code,
        observed,
        operation="recover",
        quarantine_ref=quarantine_ref,
        recovery_refs_retained=retained,
        operation_id=request.operation_id,
        expected_local_commit=(
            request.expected_local_state.commit
            if request.expected_local_state is not None
            else None
        ),
        owner_ref=_recovery_refs(request, destination_digest)[1],
        recovery_resume=request.resume_existing,
    )


def recover_instance_memory(
    checkout: str | Path,
    request: MemoryRecoveryRequest,
) -> MemoryRecoveryResult:
    """Fetch, validate, and guardedly attach one exact remote memory State."""

    if not isinstance(request, MemoryRecoveryRequest):
        raise ValueError("request must be MemoryRecoveryRequest")
    sync = request.synchronization
    checkout_path = Path(checkout)
    if not checkout_path.is_dir():
        raise SynchronizationError("repository.unavailable", "checkout directory does not exist")
    destination = _resolve_destination(checkout_path, sync)
    destination_digest = _destination_digest(destination)
    observed = _inspect_remote(checkout_path, sync, destination)
    if observed != sync.memory_state.commit:
        raise SynchronizationError(
            "recovery.remote_state_mismatch",
            "the authorized remote ref does not equal the expected recovery State",
        )
    quarantine_ref, owner_ref = _recovery_refs(request, destination_digest)
    plumbing = GitAttemptStore(checkout_path, sync.repository)
    marker = plumbing._write_blob(
        stable_json_bytes(
            {
                "destination_digest": destination_digest,
                "destination_ref": sync.destination_ref,
                "environment_id": sync.environment_id,
                "expected_commit": sync.memory_state.commit,
                "format": "peoplebot.memory-recovery-owner.v0",
                "instance_id": sync.instance_id,
                "operation_id": request.operation_id,
                "repository": sync.repository,
            }
        )
    )
    current_quarantine = _ref_value(
        checkout_path, sync.limits.timeout_seconds, quarantine_ref
    )
    current_owner = _ref_value(checkout_path, sync.limits.timeout_seconds, owner_ref)
    if request.resume_existing:
        if current_owner != marker:
            raise SynchronizationError(
                "recovery.owner_mismatch",
                "explicit recovery continuation did not find its exact owner marker",
            )
        if current_quarantine not in (None, sync.memory_state.commit):
            raise SynchronizationError(
                "recovery.quarantine_conflict",
                "the retained quarantine ref does not equal the expected recovery State",
            )
    else:
        if current_quarantine is not None:
            raise SynchronizationError(
                "recovery.quarantine_conflict",
                "the operation quarantine ref already exists and was preserved",
            )
        try:
            plumbing._update_refs(
                {
                    owner_ref: (marker, _ZERO_OBJECT_ID),
                    quarantine_ref: (None, _ZERO_OBJECT_ID),
                },
                reflog_message="peoplebot memory recovery owner v0",
                conflict_code="recovery.quarantine_owned",
                conflict_detail="the recovery owner or quarantine identity is already claimed",
                symbolic_code="recovery.ref_symbolic",
                symbolic_detail="a recovery owner or quarantine ref is symbolic and was preserved",
                inspection_code="recovery.ref_inspection_failed",
                inspection_detail="Git could not establish recovery claim identities",
                persistence_code="recovery.owner_persistence_failed",
                persistence_detail="Git could not atomically establish recovery ownership",
            )
        except ProvenanceError as error:
            raise SynchronizationError(error.code, error.detail) from error
        current_quarantine = _ref_value(
            checkout_path, sync.limits.timeout_seconds, quarantine_ref
        )
        if current_quarantine is not None:
            return MemoryRecoveryResult(
                _recovery_observation(
                    request,
                    destination_digest,
                    SynchronizationDisposition.FAILED,
                    "recovery.quarantine_conflict",
                    observed,
                    quarantine_ref,
                    True,
                ),
                None,
                quarantine_ref,
                owner_ref,
            )
    if current_quarantine is None:
        try:
            fetched = _transport_git(
                checkout_path,
                sync.limits.timeout_seconds,
                "fetch",
                "--no-write-fetch-head",
                "--no-tags",
                "--no-recurse-submodules",
                destination,
                sync.destination_ref,
            )
        except (
            DirectProcessTimeout,
            DirectProcessFailure,
            DirectProcessSetupFailure,
            OSError,
        ):
            return MemoryRecoveryResult(
                _recovery_observation(
                    request,
                    destination_digest,
                    SynchronizationDisposition.UNCERTAIN,
                    "recovery.transport_uncertain",
                    observed,
                    quarantine_ref,
                    _recovery_refs_retained(
                        checkout_path,
                        sync.limits.timeout_seconds,
                        quarantine_ref,
                        owner_ref,
                    ),
                ),
                None,
                quarantine_ref,
                owner_ref,
            )
        if fetched.returncode != 0:
            return MemoryRecoveryResult(
                _recovery_observation(
                    request,
                    destination_digest,
                    SynchronizationDisposition.UNCERTAIN,
                    "recovery.transport_result_uncertain",
                    observed,
                    quarantine_ref,
                    _recovery_refs_retained(
                        checkout_path,
                        sync.limits.timeout_seconds,
                        quarantine_ref,
                        owner_ref,
                    ),
                ),
                None,
                quarantine_ref,
                owner_ref,
            )
        try:
            resolve_state(checkout_path, sync.memory_state)
            plumbing._update_refs(
                {
                    owner_ref: (None, marker),
                    quarantine_ref: (sync.memory_state.commit, _ZERO_OBJECT_ID),
                },
                reflog_message="peoplebot memory recovery quarantine v0",
                conflict_code="recovery.quarantine_transition_conflict",
                conflict_detail="recovery ownership or quarantine state changed after object retrieval",
                symbolic_code="recovery.ref_symbolic",
                symbolic_detail="a recovery owner or quarantine ref is symbolic and was preserved",
                inspection_code="recovery.ref_inspection_failed",
                inspection_detail="Git could not establish recovery transition identities",
                persistence_code="recovery.quarantine_persistence_failed",
                persistence_detail="Git could not atomically establish the quarantine ref",
            )
        except (ProvenanceError, StateResolutionError, SynchronizationError) as error:
            return MemoryRecoveryResult(
                _recovery_observation(
                    request,
                    destination_digest,
                    SynchronizationDisposition.FAILED,
                    getattr(error, "code", "recovery.fetched_state_unavailable"),
                    observed,
                    quarantine_ref,
                    _recovery_refs_retained(
                        checkout_path,
                        sync.limits.timeout_seconds,
                        quarantine_ref,
                        owner_ref,
                    ),
                ),
                None,
                quarantine_ref,
                owner_ref,
            )
    try:
        quarantined = _ref_value(checkout_path, sync.limits.timeout_seconds, quarantine_ref)
        if quarantined != sync.memory_state.commit:
            raise SynchronizationError(
                "recovery.quarantine_mismatch",
                "the fetched quarantine ref does not equal the expected memory State",
            )
        _validate_lineage(checkout_path, sync)
        if request.expected_local_state is not None:
            _validate_memory_binding(
                checkout_path,
                sync,
                request.expected_local_state.commit,
            )
            local_ancestry = _git(
                checkout_path,
                sync.limits.timeout_seconds,
                "merge-base",
                "--is-ancestor",
                request.expected_local_state.commit,
                sync.memory_state.commit,
            )
            if local_ancestry.returncode != 0:
                raise SynchronizationError(
                    "recovery.local_state_conflict",
                    "expected local memory is not an ancestor of the recovery State",
                )
        GitMemoryStore(checkout_path, sync.repository)._require_unused_branch(
            sync.destination_ref
        )
        plumbing._update_refs(
            {
                owner_ref: (None, marker),
                quarantine_ref: (None, sync.memory_state.commit),
                sync.destination_ref: (
                    sync.memory_state.commit,
                    (
                        request.expected_local_state.commit
                        if request.expected_local_state is not None
                        else _ZERO_OBJECT_ID
                    ),
                ),
            },
            reflog_message="peoplebot memory recovery v0",
            conflict_code="recovery.publication_conflict",
            conflict_detail="recovery ownership, quarantine, or canonical local State changed",
            symbolic_code="recovery.local_ref_symbolic",
            symbolic_detail="a recovery or canonical local ref is symbolic and was preserved",
            inspection_code="recovery.ref_inspection_failed",
            inspection_detail="Git could not establish recovery publication identities",
            persistence_code="recovery.local_persistence_failed",
            persistence_detail="Git could not atomically attach the recovered memory State",
        )
    except (MemoryError, ProvenanceError, SynchronizationError) as error:
        code = getattr(error, "code", "recovery.validation_failed")
        detail = getattr(error, "detail", "recovery validation failed")
        if not isinstance(error, SynchronizationError):
            error = SynchronizationError(code, detail)
        return MemoryRecoveryResult(
            _recovery_observation(
                request,
                destination_digest,
                SynchronizationDisposition.FAILED,
                error.code,
                observed,
                quarantine_ref,
                _recovery_refs_retained(
                    checkout_path,
                    sync.limits.timeout_seconds,
                    quarantine_ref,
                    owner_ref,
                ),
            ),
            None,
            quarantine_ref,
            owner_ref,
        )
    retained = False
    code = "recovery.remote_verified"
    try:
        plumbing._update_refs(
            {
                quarantine_ref: (_ZERO_OBJECT_ID, sync.memory_state.commit),
                owner_ref: (_ZERO_OBJECT_ID, marker),
                sync.destination_ref: (None, sync.memory_state.commit),
            },
            reflog_message="peoplebot memory recovery cleanup v0",
            conflict_code="recovery.cleanup_conflict",
            conflict_detail="recovery cleanup identities changed and were preserved together",
            symbolic_code="recovery.cleanup_conflict",
            symbolic_detail="a cleanup ref became symbolic and was preserved",
            inspection_code="recovery.cleanup_failed",
            inspection_detail="Git could not establish exact cleanup identities",
            persistence_code="recovery.cleanup_failed",
            persistence_detail="Git could not atomically remove exact recovery refs",
        )
    except ProvenanceError:
        retained = _recovery_refs_retained(
            checkout_path,
            sync.limits.timeout_seconds,
            quarantine_ref,
            owner_ref,
        )
        code = "recovery.remote_verified_cleanup_retained"
    return MemoryRecoveryResult(
        _recovery_observation(
            request,
            destination_digest,
            SynchronizationDisposition.REMOTE_VERIFIED,
            code,
            observed,
            quarantine_ref,
            retained,
        ),
        sync.memory_state,
        quarantine_ref,
        owner_ref,
    )


def run_memory_synchronization_execution(
    runtime_root: str | Path,
    evidence_store: AttemptEvidenceStore,
    checkout: str | Path,
    start: ExecutionStart,
    request: MemorySynchronizationRequest,
    finished_at: Callable[[], str],
    *,
    reconcile_only: bool = False,
) -> MemorySynchronizationExecutionResult:
    """Run synchronization or reconciliation under admission and local provenance."""

    if (
        start.environment_id != request.environment_id
        or start.instance_id != request.instance_id
        or start.starting_state != request.memory_state
        or start.blueprint != request.blueprint
    ):
        raise ValueError("ExecutionStart does not match the synchronization request")
    if not callable(finished_at):
        raise ValueError("finished_at must be callable")
    captured: SynchronizationObservation | None = None

    def record(
        observation: SynchronizationObservation,
        record_failure: bool = False,
    ) -> ExecutionRecord:
        verified = (
            observation.disposition is SynchronizationDisposition.REMOTE_VERIFIED
            and not record_failure
        )
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
            status=ExecutionStatus.NO_CHANGE if verified else ExecutionStatus.FAILED,
            procedures=start.procedures,
            input_states=start.input_states,
            input_messages=start.input_messages,
            resulting_state=start.starting_state if verified else None,
            terminal_outcome=(
                None
                if verified
                else TerminalOutcome(
                    (
                        "synchronization.execution_record_failed"
                        if record_failure
                        else observation.code
                    ),
                    (
                        "Execution-record construction failed after the transport observation"
                        if record_failure
                        else "Instance-memory synchronization did not establish a verified remote State"
                    ),
                )
            ),
        )

    def task() -> ExecutionRecord:
        nonlocal captured
        try:
            captured = (
                reconcile_instance_memory(checkout, request)
                if reconcile_only
                else synchronize_instance_memory(checkout, request)
            )
        except SynchronizationError as error:
            captured = _observation(
                request,
                _destination_digest(request.expected_destination_url),
                SynchronizationDisposition.FAILED,
                error.code,
                None,
                operation="reconcile" if reconcile_only else "synchronize",
            )
        return record(captured)

    def failure(error: Exception) -> ExecutionRecord:
        nonlocal captured
        if captured is None:
            captured = _observation(
                request,
                _destination_digest(request.expected_destination_url),
                SynchronizationDisposition.FAILED,
                "synchronization.operation_raised",
                None,
                operation="reconcile" if reconcile_only else "synchronize",
            )
            return record(captured)
        return record(captured, record_failure=True)

    def artifacts(_: ExecutionRecord) -> dict[str, bytes]:
        assert captured is not None
        return {"synchronization-observation.json": captured.to_json_bytes()}

    provenance = run_with_execution_provenance(
        runtime_root,
        evidence_store,
        start,
        task,
        failure,
        artifacts,
    )
    return MemorySynchronizationExecutionResult(captured, provenance)


def run_memory_recovery_execution(
    runtime_root: str | Path,
    evidence_store: AttemptEvidenceStore,
    checkout: str | Path,
    start: ExecutionStart,
    request: MemoryRecoveryRequest,
    finished_at: Callable[[], str],
) -> MemoryRecoveryExecutionResult:
    """Recover memory under the owning Instance admission and provenance boundary."""

    sync = request.synchronization
    if (
        start.environment_id != sync.environment_id
        or start.instance_id != sync.instance_id
        or start.blueprint != sync.blueprint
        or start.starting_state.repository != sync.repository
        or start.starting_state.path is not None
    ):
        raise ValueError("ExecutionStart does not match the recovery request")
    if not callable(finished_at):
        raise ValueError("finished_at must be callable")
    captured: MemoryRecoveryResult | None = None

    def record(record_failure: bool = False) -> ExecutionRecord:
        assert captured is not None
        observation = captured.observation
        verified = captured.recovered_state is not None and not record_failure
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
            status=ExecutionStatus.NO_CHANGE if verified else ExecutionStatus.FAILED,
            procedures=start.procedures,
            input_states=start.input_states,
            input_messages=start.input_messages,
            resulting_state=start.starting_state if verified else None,
            terminal_outcome=(
                None
                if verified
                else TerminalOutcome(
                    (
                        "recovery.execution_record_failed"
                        if record_failure
                        else observation.code
                    ),
                    (
                        "Execution-record construction failed after the recovery observation"
                        if record_failure
                        else "Instance-memory recovery did not attach a verified local State"
                    ),
                )
            ),
            artifacts=(captured.recovered_state,) if captured.recovered_state is not None else (),
        )

    def task() -> ExecutionRecord:
        nonlocal captured
        captured = recover_instance_memory(checkout, request)
        return record()

    def failure(error: Exception) -> ExecutionRecord:
        nonlocal captured
        if captured is None:
            destination_digest = _destination_digest(sync.expected_destination_url)
            quarantine_ref, owner_ref = _recovery_refs(request, destination_digest)
            retained = _recovery_refs_retained(
                Path(checkout),
                sync.limits.timeout_seconds,
                quarantine_ref,
                owner_ref,
            )
            captured = MemoryRecoveryResult(
                _recovery_observation(
                    request,
                    destination_digest,
                    SynchronizationDisposition.FAILED,
                    getattr(error, "code", "recovery.operation_raised"),
                    None,
                    quarantine_ref,
                    retained,
                ),
                None,
                quarantine_ref,
                owner_ref,
            )
            return record()
        return record(record_failure=True)

    def artifacts(_: ExecutionRecord) -> dict[str, bytes]:
        assert captured is not None
        return {"synchronization-observation.json": captured.observation.to_json_bytes()}

    provenance = run_with_execution_provenance(
        runtime_root,
        evidence_store,
        start,
        task,
        failure,
        artifacts,
    )
    return MemoryRecoveryExecutionResult(captured, provenance)
