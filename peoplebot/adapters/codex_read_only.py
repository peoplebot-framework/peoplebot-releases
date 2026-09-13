"""One bounded, read-only Codex CLI Adapter experiment."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from .._json import stable_json_bytes
from ..execution import (
    ExecutionRecord,
    ExecutionStatus,
    TerminalOutcome,
    UsageConfidence,
    UsageObservation,
    UsageSource,
    _require_text,
)
from ..preparation import ContextAssembly, ContextPolicy, assemble_context
from ..provenance import (
    AttemptEvidenceStore,
    ExecutionStart,
    ProvenanceRunResult,
    UnresolvedExecutionOwnership,
    run_with_execution_provenance,
)
from ..state import StateRef


LICENSING_PATH = "LICENSING.md"
CONFIG_PATH = "codex_read_only/adapter.json"
UNKNOWN_USAGE = UsageObservation(
    "codex.token_usage",
    None,
    "tokens",
    UsageSource.UNKNOWN,
    UsageConfidence.UNKNOWN,
)
_ABSOLUTE_STDOUT_BYTES = 262_144
_ABSOLUTE_STDERR_BYTES = 65_536
_CLEANUP_TIMEOUT_SECONDS = 15
_ALLOWED_ENVIRONMENT_NAMES = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)
try:
    _SOURCE_AT_IMPORT_SHA256: str | None = hashlib.sha256(
        Path(__file__).read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
except (OSError, UnicodeError):
    _SOURCE_AT_IMPORT_SHA256 = None


class AdapterError(RuntimeError):
    """A classified failure before a model invocation can be attempted."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CodexReadOnlyConfiguration:
    runtime: str
    runtime_version: str
    model: str
    sandbox: str
    timeout_seconds: int
    max_prompt_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_response_bytes: int
    max_rendered_answer_characters: int
    response_schema: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CodexReadOnlyConfiguration:
        expected = {
            "format",
            "max_rendered_answer_characters",
            "max_prompt_bytes",
            "max_response_bytes",
            "max_stderr_bytes",
            "max_stdout_bytes",
            "model",
            "response_schema",
            "runtime",
            "runtime_version",
            "sandbox",
            "timeout_seconds",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("Adapter configuration fields do not match v0")
        if value["format"] != "peoplebot.codex-read-only-adapter.v0":
            raise ValueError("Adapter configuration format is unsupported")
        for field in ("runtime", "runtime_version", "model", "sandbox"):
            _require_text(value[field], field)
        if value["runtime"] != "codex-cli" or value["sandbox"] != "read-only":
            raise ValueError("Adapter v0 requires codex-cli with read-only sandboxing")
        bounds = {
            "timeout_seconds": (1, 300),
            "max_prompt_bytes": (1024, 65_536),
            "max_stdout_bytes": (1024, 262_144),
            "max_stderr_bytes": (1024, 65_536),
            "max_response_bytes": (256, 16_384),
            "max_rendered_answer_characters": (1, 4_000),
        }
        for field, (minimum, maximum) in bounds.items():
            item = value[field]
            if isinstance(item, bool) or not isinstance(item, int):
                raise ValueError(f"{field} must be an integer")
            if item < minimum or item > maximum:
                raise ValueError(f"{field} must be between {minimum} and {maximum}")
        schema = value["response_schema"]
        if not isinstance(schema, Mapping):
            raise ValueError("response_schema must be an object")
        return cls(
            runtime=value["runtime"],
            runtime_version=value["runtime_version"],
            model=value["model"],
            sandbox=value["sandbox"],
            timeout_seconds=value["timeout_seconds"],
            max_prompt_bytes=value["max_prompt_bytes"],
            max_stdout_bytes=value["max_stdout_bytes"],
            max_stderr_bytes=value["max_stderr_bytes"],
            max_response_bytes=value["max_response_bytes"],
            max_rendered_answer_characters=value["max_rendered_answer_characters"],
            response_schema=_freeze_json(schema),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "peoplebot.codex-read-only-adapter.v0",
            "max_rendered_answer_characters": self.max_rendered_answer_characters,
            "max_prompt_bytes": self.max_prompt_bytes,
            "max_response_bytes": self.max_response_bytes,
            "max_stderr_bytes": self.max_stderr_bytes,
            "max_stdout_bytes": self.max_stdout_bytes,
            "model": self.model,
            "response_schema": _thaw_json(self.response_schema),
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "sandbox": self.sandbox,
            "timeout_seconds": self.timeout_seconds,
        }

    def to_json_bytes(self) -> bytes:
        return stable_json_bytes(self.to_dict())

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class LicensingAnswer:
    gpl_version: str
    distribution_scope: str
    preserve_license: bool
    preserve_required_notices: bool
    provide_corresponding_source: bool
    preserve_existing_credit: bool
    advertising_required: bool
    citation: StateRef

    @property
    def answer(self) -> str:
        return (
            "Software-rendered from validated model-returned fields: PeopleBot is "
            "GPL-3.0-only. When covered material is distributed, preserve the GPL "
            "license and applicable notices, provide corresponding source as the "
            "license requires, and retain existing contributor credit and canonical "
            "origin. This does not require publishing private work that is not "
            "distributed, and it creates no advertising requirement. Source: "
            f"{self.citation.repository}@{self.citation.commit}:{self.citation.path}."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "advertising_required": self.advertising_required,
            "citation": self.citation.to_dict(),
            "distribution_scope": self.distribution_scope,
            "gpl_version": self.gpl_version,
            "preserve_existing_credit": self.preserve_existing_credit,
            "preserve_license": self.preserve_license,
            "preserve_required_notices": self.preserve_required_notices,
            "provide_corresponding_source": self.provide_corresponding_source,
            "rendered_answer": self.answer,
            "rendered_by": "peoplebot.software",
        }


class DirectProcessDisposition(StrEnum):
    NOT_STARTED = "not_started"
    STOPPED = "confirmed_stopped"
    UNRESOLVED = "unresolved_ownership"


class WorkspaceCleanupDisposition(StrEnum):
    NOT_CREATED = "not_created"
    REMOVED = "removed"
    RECOVERABLE_REMNANT = "recoverable_remnant"


@dataclass(frozen=True, slots=True)
class AdapterObservation:
    code: str
    detail: str
    runtime: str
    runtime_version: str
    model: str
    context_sha256: str
    configuration_sha256: str
    configuration_blob_sha256: str
    driver_state: StateRef
    driver_sha256: str
    driver_evidence: str
    executing_code_identity_verified: bool
    process_started: bool
    direct_process_disposition: DirectProcessDisposition
    workspace_cleanup_disposition: WorkspaceCleanupDisposition
    process_exit_code: int | None
    answer: LicensingAnswer | None
    response_sha256: str | None
    usage: tuple[UsageObservation, ...]
    workspace_remnant: _OwnedWorkspace | None = None

    @property
    def succeeded(self) -> bool:
        return self.code == "adapter.completed"

    @property
    def direct_process_stopped(self) -> bool:
        return self.direct_process_disposition is DirectProcessDisposition.STOPPED

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer.to_dict() if self.answer else None,
            "code": self.code,
            "configuration_sha256": self.configuration_sha256,
            "configuration_blob_sha256": self.configuration_blob_sha256,
            "context_sha256": self.context_sha256,
            "detail": self.detail,
            "direct_process_disposition": self.direct_process_disposition.value,
            "direct_process_stopped": self.direct_process_stopped,
            "driver_sha256": self.driver_sha256,
            "driver_state": self.driver_state.to_dict(),
            "driver_evidence": self.driver_evidence,
            "executing_code_identity_verified": self.executing_code_identity_verified,
            "model": self.model,
            "process_exit_code": self.process_exit_code,
            "process_started": self.process_started,
            "response_sha256": self.response_sha256,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "usage": [item.to_dict() for item in self.usage],
            "workspace_cleanup_disposition": self.workspace_cleanup_disposition.value,
        }


@dataclass(frozen=True, slots=True)
class ReadOnlyExecutionResult:
    context: ContextAssembly
    adapter_observation: AdapterObservation | None
    provenance: ProvenanceRunResult

    @property
    def observation_evidence(self) -> StateRef | None:
        terminal = self.provenance.terminal_evidence
        if terminal is None or self.adapter_observation is None:
            return None
        return StateRef(
            terminal.state.repository,
            terminal.state.commit,
            "adapter-observation.json",
        )


class ProcessRunner(Protocol):
    def __call__(
        self,
        command: tuple[str, ...],
        input_bytes: bytes,
        environment: Mapping[str, str],
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[bytes]: ...


class CodexJsonTransportConfiguration(Protocol):
    runtime: str
    runtime_version: str
    model: str
    sandbox: str
    timeout_seconds: int
    max_prompt_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    response_schema: Mapping[str, Any]


class DirectProcessTimeout(subprocess.TimeoutExpired):
    """The execution deadline expired after the direct child was confirmed stopped."""


class DirectProcessFailure(OSError):
    """A direct-child I/O failure after the child was confirmed stopped."""


class DirectProcessSetupFailure(RuntimeError):
    """Partial direct-child setup failed after confirmed cleanup."""


@dataclass(frozen=True, slots=True)
class _ProcessCleanupResult:
    process_stopped: bool
    workers_stopped: bool
    secondary_failures: tuple[str, ...]

    @property
    def ownership_resolved(self) -> bool:
        return self.process_stopped and self.workers_stopped

    def __bool__(self) -> bool:
        return self.ownership_resolved


class _OwnedWorkspace:
    """Exact invocation directory created by this Adapter operation."""

    def __init__(
        self,
        path: Path,
        cleanup: Callable[[Path], None] = shutil.rmtree,
    ) -> None:
        self.path = path
        self._cleanup = cleanup
        self.removed = False
        self._cleanup_authority_revoked = False
        self.secondary_failures: list[str] = []

    def cleanup(self) -> bool:
        if self.removed:
            return True
        if self._cleanup_authority_revoked:
            return False
        try:
            self._cleanup(self.path)
        except BaseException as error:
            self._cleanup_authority_revoked = True
            error_type = type(error)
            self.secondary_failures.append(
                f"workspace_cleanup:{error_type.__module__}.{error_type.__qualname__}"
            )
            return False
        self.removed = True
        return True


def _create_owned_workspace() -> _OwnedWorkspace:
    return _OwnedWorkspace(Path(tempfile.mkdtemp(prefix="peoplebot-codex-read-only-")))


class _OwnedProcess:
    """In-process authority for one child and its pipe workers."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process
        self.workers: list[threading.Thread] = []
        self.secondary_failures: list[str] = []

    def stopped(self) -> bool:
        return self.process.poll() is not None and not any(
            worker.is_alive() for worker in self.workers
        )

    def _record_cleanup_failure(self, stage: str, error: BaseException) -> None:
        error_type = type(error)
        self.secondary_failures.append(
            f"{stage}:{error_type.__module__}.{error_type.__qualname__}"
        )

    def cleanup(
        self,
        timeout_seconds: int = _CLEANUP_TIMEOUT_SECONDS,
    ) -> _ProcessCleanupResult:
        deadline = time.monotonic() + timeout_seconds
        if self.process.poll() is None:
            try:
                self.process.terminate()
            except BaseException as error:
                self._record_cleanup_failure("process_terminate", error)
            try:
                self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except BaseException as error:
                self._record_cleanup_failure("process_wait_after_terminate", error)
                if self.process.poll() is None:
                    try:
                        self.process.kill()
                    except BaseException as kill_error:
                        self._record_cleanup_failure("process_kill", kill_error)
                    try:
                        self.process.wait(timeout=max(0.0, deadline - time.monotonic()))
                    except BaseException as wait_error:
                        self._record_cleanup_failure("process_wait_after_kill", wait_error)
        for worker in self.workers:
            try:
                worker.join(timeout=max(0.0, deadline - time.monotonic()))
            except BaseException as error:
                self._record_cleanup_failure("worker_join", error)
        process_stopped = self.process.poll() is not None
        workers_stopped = not any(worker.is_alive() for worker in self.workers)
        if process_stopped and workers_stopped:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                if stream is None or stream.closed:
                    continue
                try:
                    stream.close()
                except BaseException as error:
                    self._record_cleanup_failure("stream_close", error)
        return _ProcessCleanupResult(
            process_stopped,
            workers_stopped,
            tuple(self.secondary_failures),
        )


class ProcessOwnershipUnresolved(UnresolvedExecutionOwnership):
    """Retain exact recovery authority when child shutdown cannot be established."""

    def __init__(self, owner: _OwnedProcess, original: BaseException) -> None:
        self.owner = owner
        self.original = original
        self.workspaces: list[_OwnedWorkspace] = []
        super().__init__(
            "direct child ownership remains unresolved after "
            f"{type(original).__module__}.{type(original).__qualname__}"
        )

    def add_workspace(self, workspace: _OwnedWorkspace) -> None:
        self.workspaces.append(workspace)

    @property
    def secondary_cleanup_failures(self) -> tuple[str, ...]:
        failures = list(self.owner.secondary_failures)
        for workspace in self.workspaces:
            failures.extend(workspace.secondary_failures)
        return tuple(failures)

    @property
    def recoverable_workspace_remnants(self) -> tuple[Path, ...]:
        return tuple(workspace.path for workspace in self.workspaces if not workspace.removed)

    def recover(self, timeout_seconds: int = _CLEANUP_TIMEOUT_SECONDS) -> bool:
        cleanup = self.owner.cleanup(timeout_seconds)
        if cleanup.ownership_resolved:
            for workspace in self.workspaces:
                workspace.cleanup()
        return cleanup.ownership_resolved

    def release_after_recovery(self) -> Any:
        if not self.owner.stopped():
            raise RuntimeError("direct child must be confirmed stopped before admission release")
        if self.retained_admission is None:
            raise RuntimeError("no retained admission is attached")
        return self.retained_admission.release()


class _OutputOverflow(RuntimeError):
    pass


def _run_process(
    command: tuple[str, ...],
    input_bytes: bytes,
    environment: Mapping[str, str],
    timeout_seconds: int,
    *,
    _popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.CompletedProcess[bytes]:
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    process = _popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(environment),
        shell=False,
        creationflags=creation_flags,
    )
    owner = _OwnedProcess(process)
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()
    worker_errors: list[BaseException] = []
    try:
        def drain(stream: Any, destination: bytearray, maximum: int) -> None:
            try:
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        return
                    remaining = maximum + 1 - len(destination)
                    if remaining > 0:
                        destination.extend(chunk[:remaining])
                    if len(destination) > maximum or len(chunk) > remaining:
                        overflow.set()
                        return
            except BaseException as error:
                worker_errors.append(error)

        def write_input() -> None:
            assert process.stdin is not None
            try:
                process.stdin.write(input_bytes)
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                if process.poll() is None:
                    worker_errors.append(
                        OSError("prompt delivery failed while child was active")
                    )
            except BaseException as error:
                worker_errors.append(error)
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        assert process.stdout is not None and process.stderr is not None
        workers = [
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout, _ABSOLUTE_STDOUT_BYTES),
                name="peoplebot-codex-stdout",
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, stderr, _ABSOLUTE_STDERR_BYTES),
                name="peoplebot-codex-stderr",
            ),
            threading.Thread(
                target=write_input,
                name="peoplebot-codex-stdin",
            ),
        ]
        deadline = time.monotonic() + timeout_seconds
        for worker in workers:
            try:
                worker.start()
            finally:
                if worker.ident is not None:
                    owner.workers.append(worker)
        while process.poll() is None:
            if overflow.is_set():
                raise _OutputOverflow("direct child exceeded an absolute output cap")
            if worker_errors:
                raise OSError("direct child pipe worker failed") from worker_errors[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout_seconds)
            try:
                process.wait(timeout=min(0.05, remaining))
            except subprocess.TimeoutExpired:
                continue
        for worker in workers:
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            raise subprocess.TimeoutExpired(command, timeout_seconds)
        if worker_errors:
            raise OSError("direct child pipe worker failed") from worker_errors[0]
        if overflow.is_set():
            raise _OutputOverflow("direct child exceeded an absolute output cap")
    except BaseException as original:
        cleanup = owner.cleanup()
        if not cleanup.ownership_resolved:
            raise ProcessOwnershipUnresolved(owner, original) from original
        if isinstance(original, subprocess.TimeoutExpired):
            raise DirectProcessTimeout(command, timeout_seconds) from original
        if isinstance(original, _OutputOverflow):
            if len(stdout) <= _ABSOLUTE_STDOUT_BYTES:
                stdout.append(0)
            if len(stderr) <= _ABSOLUTE_STDERR_BYTES:
                stderr.append(0)
        elif isinstance(original, OSError):
            raise DirectProcessFailure("direct child I/O failed and was stopped") from original
        elif isinstance(original, Exception):
            raise DirectProcessSetupFailure(
                "direct child setup or control failed and was stopped"
            ) from original
        else:
            if cleanup.secondary_failures:
                try:
                    setattr(
                        original,
                        "peoplebot_secondary_cleanup_failures",
                        cleanup.secondary_failures,
                    )
                    original.add_note(
                        "PeopleBot direct-child cleanup had secondary failures: "
                        + ", ".join(cleanup.secondary_failures)
                    )
                except (AttributeError, TypeError):
                    pass
            raise
    else:
        cleanup = owner.cleanup()
        if not cleanup.ownership_resolved:
            raise ProcessOwnershipUnresolved(
                owner,
                RuntimeError("pipe workers did not finish after direct child exit"),
            )
        if cleanup.secondary_failures:
            raise DirectProcessFailure(
                "direct child stopped but secondary process cleanup failed"
            )
    if overflow.is_set():
        if len(stdout) <= _ABSOLUTE_STDOUT_BYTES:
            stdout.append(0)
        if len(stderr) <= _ABSOLUTE_STDERR_BYTES:
            stderr.append(0)
    return subprocess.CompletedProcess(command, process.returncode, bytes(stdout), bytes(stderr))


@dataclass(frozen=True, slots=True)
class _AdoptedAdapter:
    state: StateRef
    configuration: CodexReadOnlyConfiguration
    configuration_blob_sha256: str
    driver_state: StateRef
    driver_sha256: str
    driver_evidence: str


def _adopt_adapter_from_state(
    checkout: str | Path,
    adapter_state: StateRef,
) -> _AdoptedAdapter:
    if adapter_state.path is None:
        raise ValueError("adapter_state must select the versioned Adapter directory")
    config_path = f"{adapter_state.path}/{CONFIG_PATH}"
    driver_path = f"{adapter_state.path}/codex_read_only.py"
    config_state = StateRef(adapter_state.repository, adapter_state.commit, config_path)
    policy = ContextPolicy(config_state, 2, 131_072, 196_608)
    assembly = assemble_context(
        checkout,
        StateRef(adapter_state.repository, adapter_state.commit),
        (config_path, driver_path),
        policy,
    )
    documents = {document.source.path: document for document in assembly.documents}
    if set(documents) != {config_path, driver_path}:
        raise AdapterError(
            "adapter.configuration_unavailable",
            "Adapter configuration and driver were not both selected",
        )
    try:
        config_bytes = documents[config_path].content.encode("utf-8")
        value = json.loads(config_bytes)
        configuration = CodexReadOnlyConfiguration.from_dict(value)
    except (json.JSONDecodeError, ValueError, TypeError) as error:
        raise AdapterError(
            "adapter.configuration_invalid",
            "versioned Adapter configuration is invalid",
        ) from error
    pinned_driver_text = documents[driver_path].content
    try:
        current_driver_text = Path(__file__).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise AdapterError(
            "adapter.driver_evidence_unavailable",
            "current Adapter source-file evidence is unavailable",
        ) from error
    driver_bytes = pinned_driver_text.encode("utf-8")
    pinned_driver_sha256 = hashlib.sha256(driver_bytes).hexdigest()
    current_driver_sha256 = hashlib.sha256(
        current_driver_text.encode("utf-8")
    ).hexdigest()
    if current_driver_sha256 != pinned_driver_sha256:
        raise AdapterError(
            "adapter.driver_source_mismatch",
            "current Adapter source file does not match pinned Adapter State",
        )
    if _SOURCE_AT_IMPORT_SHA256 is None:
        raise AdapterError(
            "adapter.driver_evidence_unavailable",
            "Adapter source-at-import evidence is unavailable",
        )
    if _SOURCE_AT_IMPORT_SHA256 != pinned_driver_sha256:
        raise AdapterError(
            "adapter.loaded_source_mismatch",
            "Adapter source at module import does not match pinned Adapter State",
        )
    return _AdoptedAdapter(
        adapter_state,
        configuration,
        hashlib.sha256(config_bytes).hexdigest(),
        StateRef(adapter_state.repository, adapter_state.commit, driver_path),
        pinned_driver_sha256,
        "pinned_source_matches_source_at_import_and_current_file",
    )


def _usage_observations(value: object) -> tuple[UsageObservation, ...]:
    if not isinstance(value, Mapping):
        return (UNKNOWN_USAGE,)
    observations: list[UsageObservation] = []
    for metric in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    ):
        amount = value.get(metric)
        if amount is None:
            continue
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("provider usage contains an invalid token value")
        observations.append(
            UsageObservation(
                f"codex.{metric}",
                amount,
                "tokens",
                UsageSource.PROVIDER_REPORTED,
                UsageConfidence.EXACT,
            )
        )
    return tuple(observations) or (UNKNOWN_USAGE,)


class _ResponseError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _ResponseError(
                "adapter.response_invalid_json",
                "structured response contains a duplicate object key",
            )
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class _CodexJsonTransportResult:
    code: str
    detail: str
    process_started: bool
    direct_process_disposition: DirectProcessDisposition
    workspace_cleanup_disposition: WorkspaceCleanupDisposition
    process_exit_code: int | None
    response_text: str | None
    usage: tuple[UsageObservation, ...]
    workspace_remnant: _OwnedWorkspace | None = None

    @property
    def succeeded(self) -> bool:
        return self.code == "adapter.transport_completed"


def _codex_environment(codex_home: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _ALLOWED_ENVIRONMENT_NAMES
    }
    environment["CODEX_HOME"] = os.fspath(codex_home)
    return environment


def _invoke_codex_json_transport(
    executable: Path,
    codex_home: Path,
    configuration: CodexJsonTransportConfiguration,
    prompt: bytes,
    *,
    runner: ProcessRunner,
    workspace_factory: Callable[[], _OwnedWorkspace],
) -> _CodexJsonTransportResult:
    """Run the established bounded, read-only JSONL process lifecycle once."""

    def failure(
        code: str,
        detail: str,
        *,
        started: bool,
        exit_code: int | None = None,
        usage: tuple[UsageObservation, ...] = (UNKNOWN_USAGE,),
        workspace: _OwnedWorkspace | None = None,
        workspace_disposition: WorkspaceCleanupDisposition | None = None,
    ) -> _CodexJsonTransportResult:
        if workspace_disposition is None:
            workspace_disposition = (
                WorkspaceCleanupDisposition.REMOVED
                if started
                else WorkspaceCleanupDisposition.NOT_CREATED
            )
        return _CodexJsonTransportResult(
            code,
            detail,
            started,
            (
                DirectProcessDisposition.STOPPED
                if started
                else DirectProcessDisposition.NOT_STARTED
            ),
            workspace_disposition,
            exit_code,
            None,
            usage,
            workspace,
        )

    if len(prompt) > configuration.max_prompt_bytes:
        return failure(
            "adapter.input_limit_exceeded",
            "exact invocation prompt exceeds the configured byte limit",
            started=False,
        )
    environment = _codex_environment(codex_home)
    try:
        version_result = runner(
            (os.fspath(executable), "--version"),
            b"",
            environment,
            15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return failure(
            "adapter.runtime_unavailable",
            "Codex CLI version could not be established",
            started=False,
        )
    version_text = version_result.stdout.decode("utf-8", "replace").strip()
    if version_result.returncode != 0 or version_text != (
        f"{configuration.runtime} {configuration.runtime_version}"
    ):
        return failure(
            "adapter.runtime_mismatch",
            "Codex CLI version does not match the adopted Adapter configuration",
            started=False,
            exit_code=version_result.returncode,
        )

    workspace_owner = workspace_factory()
    workspace = workspace_owner.path
    try:
        schema_path = workspace / "response.schema.json"
        schema_path.write_bytes(stable_json_bytes(_thaw_json(configuration.response_schema)))
        command = (
            os.fspath(executable),
            "exec",
            "--strict-config",
            "--sandbox",
            configuration.sandbox,
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--output-schema",
            os.fspath(schema_path),
            "--json",
            "--color",
            "never",
            "--model",
            configuration.model,
            "--cd",
            os.fspath(workspace),
            "-",
        )
        completed = runner(
            command,
            prompt,
            environment,
            configuration.timeout_seconds,
        )
    except ProcessOwnershipUnresolved as error:
        error.add_workspace(workspace_owner)
        raise
    except DirectProcessTimeout:
        removed = workspace_owner.cleanup()
        return failure(
            "adapter.timeout",
            "Codex CLI direct process exceeded the configured duration and was stopped",
            started=True,
            workspace=None if removed else workspace_owner,
            workspace_disposition=(
                WorkspaceCleanupDisposition.REMOVED
                if removed
                else WorkspaceCleanupDisposition.RECOVERABLE_REMNANT
            ),
        )
    except DirectProcessFailure:
        removed = workspace_owner.cleanup()
        return failure(
            "adapter.io_failed",
            "Codex CLI I/O or process cleanup failed after direct process shutdown",
            started=True,
            workspace=None if removed else workspace_owner,
            workspace_disposition=(
                WorkspaceCleanupDisposition.REMOVED
                if removed
                else WorkspaceCleanupDisposition.RECOVERABLE_REMNANT
            ),
        )
    except DirectProcessSetupFailure:
        removed = workspace_owner.cleanup()
        return failure(
            "adapter.process_setup_failed",
            "Codex CLI direct process setup failed and its child was stopped",
            started=True,
            workspace=None if removed else workspace_owner,
            workspace_disposition=(
                WorkspaceCleanupDisposition.REMOVED
                if removed
                else WorkspaceCleanupDisposition.RECOVERABLE_REMNANT
            ),
        )
    except OSError:
        removed = workspace_owner.cleanup()
        return failure(
            "adapter.runtime_failed",
            "Codex CLI direct process or invocation workspace could not be prepared",
            started=False,
            workspace=None if removed else workspace_owner,
            workspace_disposition=(
                WorkspaceCleanupDisposition.REMOVED
                if removed
                else WorkspaceCleanupDisposition.RECOVERABLE_REMNANT
            ),
        )
    except BaseException as error:
        removed = workspace_owner.cleanup()
        if not removed:
            try:
                setattr(error, "peoplebot_workspace_remnant", workspace_owner)
                setattr(
                    error,
                    "peoplebot_secondary_workspace_failures",
                    tuple(workspace_owner.secondary_failures),
                )
                error.add_note(
                    "PeopleBot retained a recoverable invocation-workspace remnant"
                )
            except (AttributeError, TypeError):
                pass
        raise
    else:
        removed = workspace_owner.cleanup()

    def completed_failure(
        code: str,
        detail: str,
        usage: tuple[UsageObservation, ...] = (UNKNOWN_USAGE,),
    ) -> _CodexJsonTransportResult:
        return failure(
            code,
            detail,
            started=True,
            exit_code=completed.returncode,
            usage=usage,
            workspace=None if removed else workspace_owner,
            workspace_disposition=(
                WorkspaceCleanupDisposition.REMOVED
                if removed
                else WorkspaceCleanupDisposition.RECOVERABLE_REMNANT
            ),
        )

    if len(completed.stdout) > configuration.max_stdout_bytes or len(
        completed.stderr
    ) > configuration.max_stderr_bytes:
        return completed_failure(
            "adapter.output_limit_exceeded",
            "Codex CLI transport output exceeds the configured byte limit",
        )
    if completed.returncode != 0:
        return completed_failure(
            "adapter.process_failed",
            "Codex CLI direct process returned a nonzero exit status",
        )
    usage = (UNKNOWN_USAGE,)
    events: list[Mapping[str, Any]] = []
    stream_failure: tuple[str, str] | None = None
    lines = [line for line in completed.stdout.splitlines() if line]
    if len(lines) > 128:
        stream_failure = (
            "adapter.event_unsupported",
            "Codex event stream exceeds the supported event count",
        )
    for line in lines[:129]:
        try:
            event = json.loads(line, object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, _ResponseError):
            stream_failure = stream_failure or (
                "adapter.response_invalid_json",
                "Codex event stream contains invalid JSON",
            )
            continue
        if not isinstance(event, Mapping):
            stream_failure = stream_failure or (
                "adapter.event_unsupported",
                "Codex event stream contains a non-object event",
            )
            continue
        events.append(event)

    messages: list[str] = []
    completed_turns: list[Mapping[str, Any]] = []
    restriction_violated = False
    supported_events = {
        "error",
        "item.completed",
        "item.started",
        "item.updated",
        "thread.started",
        "turn.completed",
        "turn.failed",
        "turn.started",
    }
    for event in events:
        event_type = event.get("type")
        if event_type not in supported_events:
            stream_failure = stream_failure or (
                "adapter.event_unsupported",
                "Codex emitted an unsupported event type",
            )
            continue
        if event_type in {"error", "turn.failed"}:
            stream_failure = stream_failure or (
                "adapter.process_failed",
                "Codex event stream reports failure",
            )
        if event_type == "turn.completed":
            completed_turns.append(event)
        if event_type in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, Mapping):
                stream_failure = stream_failure or (
                    "adapter.event_unsupported",
                    "Codex item event has an unsupported shape",
                )
                continue
            item_type = item.get("type")
            if item_type not in {"agent_message", "reasoning"}:
                restriction_violated = True
            if event_type == "item.completed" and item_type == "agent_message":
                if not isinstance(item.get("text"), str):
                    stream_failure = stream_failure or (
                        "adapter.event_unsupported",
                        "Codex agent-message event has an unsupported shape",
                    )
                else:
                    messages.append(item["text"])
    if len(completed_turns) == 1:
        try:
            usage = _usage_observations(completed_turns[0].get("usage"))
        except ValueError:
            stream_failure = stream_failure or (
                "adapter.usage_invalid",
                "Codex completion contains invalid provider usage",
            )
    if restriction_violated:
        return completed_failure(
            "adapter.restriction_violated",
            "Codex emitted a tool or command event for a context-only task",
            usage,
        )
    if stream_failure is not None:
        return completed_failure(stream_failure[0], stream_failure[1], usage)
    if len(messages) != 1 or len(completed_turns) != 1:
        return completed_failure(
            "adapter.response_missing_completion",
            "Codex output lacks exactly one final answer and completion",
            usage,
        )
    if not removed:
        return completed_failure(
            "adapter.workspace_cleanup_failed",
            "direct process stopped but its invocation workspace remains recoverable",
            usage,
        )
    return _CodexJsonTransportResult(
        "adapter.transport_completed",
        "Codex CLI returned one context-only structured response",
        True,
        DirectProcessDisposition.STOPPED,
        WorkspaceCleanupDisposition.REMOVED,
        completed.returncode,
        messages[0],
        usage,
    )


def _licensing_answer(
    text: str,
    source: StateRef,
    maximum_response_bytes: int,
    maximum_rendered_characters: int,
) -> LicensingAnswer:
    if len(text.encode("utf-8")) > maximum_response_bytes:
        raise _ResponseError(
            "adapter.response_limit_exceeded",
            "structured response exceeds the configured serialized byte limit",
        )
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except _ResponseError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _ResponseError(
            "adapter.response_invalid_json",
            "structured response is not one valid JSON object",
        ) from error
    expected = {
        "advertising_required",
        "citation",
        "distribution_scope",
        "gpl_version",
        "preserve_existing_credit",
        "preserve_license",
        "preserve_required_notices",
        "provide_corresponding_source",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise _ResponseError(
            "adapter.response_field_invalid",
            "structured response fields do not match v0",
        )
    if value["gpl_version"] != "GPL-3.0-only":
        raise _ResponseError(
            "adapter.response_field_invalid",
            "structured response does not identify GPL-3.0-only",
        )
    if value["distribution_scope"] != "distribution_of_covered_material":
        raise _ResponseError(
            "adapter.response_field_invalid",
            "structured response has an unsupported distribution scope",
        )
    for field, required in (
        ("preserve_license", True),
        ("preserve_required_notices", True),
        ("provide_corresponding_source", True),
        ("preserve_existing_credit", True),
        ("advertising_required", False),
    ):
        if value[field] is not required:
            raise _ResponseError(
                "adapter.response_field_invalid",
                f"structured response has an incorrect {field} value",
            )
    citation = value["citation"]
    if not isinstance(citation, Mapping) or set(citation) != {"repository", "commit", "path"}:
        raise _ResponseError(
            "adapter.response_field_invalid",
            "citation fields do not match StateRef",
        )
    if not isinstance(citation.get("repository"), str) or not (
        1 <= len(citation["repository"]) <= 512
    ):
        raise _ResponseError(
            "adapter.response_field_invalid",
            "citation repository exceeds its supported field bound",
        )
    try:
        cited_state = StateRef(citation["repository"], citation["commit"], citation["path"])
    except (TypeError, ValueError) as error:
        raise _ResponseError(
            "adapter.response_field_invalid",
            "citation values do not form a valid StateRef",
        ) from error
    if cited_state != source:
        raise _ResponseError(
            "adapter.response_citation_mismatch",
            "response citation does not match the supplied pinned context",
        )
    answer = LicensingAnswer(
        gpl_version=value["gpl_version"],
        distribution_scope=value["distribution_scope"],
        preserve_license=value["preserve_license"],
        preserve_required_notices=value["preserve_required_notices"],
        provide_corresponding_source=value["provide_corresponding_source"],
        preserve_existing_credit=value["preserve_existing_credit"],
        advertising_required=value["advertising_required"],
        citation=cited_state,
    )
    if len(answer.answer) > maximum_rendered_characters:
        raise _ResponseError(
            "adapter.response_limit_exceeded",
            "software-rendered explanation exceeds its configured character limit",
        )
    return answer


class CodexReadOnlyAdapter:
    """Invoke one authenticated Codex CLI child with exact pinned text context."""

    __slots__ = (
        "_state",
        "_adopted",
        "executable",
        "codex_home",
        "_runner",
        "_workspace_factory",
    )

    def __init__(
        self,
        checkout: str | Path,
        state: StateRef,
        executable: str | Path,
        codex_home: str | Path,
        *,
        runner: ProcessRunner = _run_process,
        workspace_factory: Callable[[], _OwnedWorkspace] = _create_owned_workspace,
    ) -> None:
        self._state = state
        self._adopted = _adopt_adapter_from_state(checkout, state)
        self.executable = Path(executable).resolve(strict=True)
        self.codex_home = Path(codex_home).resolve(strict=True)
        if not self.codex_home.is_dir():
            raise ValueError("codex_home must be an existing directory")
        self._runner = runner
        self._workspace_factory = workspace_factory

    @property
    def configuration(self) -> CodexReadOnlyConfiguration:
        return self._adopted.configuration

    @property
    def state(self) -> StateRef:
        return self._state

    def _environment(self) -> dict[str, str]:
        return _codex_environment(self.codex_home)

    def _failure(
        self,
        code: str,
        detail: str,
        context_digest: str,
        *,
        started: bool,
        exit_code: int | None = None,
        usage: tuple[UsageObservation, ...] = (UNKNOWN_USAGE,),
        workspace: _OwnedWorkspace | None = None,
        workspace_disposition: WorkspaceCleanupDisposition | None = None,
    ) -> AdapterObservation:
        config = self._adopted.configuration
        if workspace_disposition is None:
            workspace_disposition = (
                WorkspaceCleanupDisposition.REMOVED
                if started
                else WorkspaceCleanupDisposition.NOT_CREATED
            )
        return AdapterObservation(
            code=code,
            detail=detail,
            runtime=config.runtime,
            runtime_version=config.runtime_version,
            model=config.model,
            context_sha256=context_digest,
            configuration_sha256=config.digest,
            configuration_blob_sha256=self._adopted.configuration_blob_sha256,
            driver_state=self._adopted.driver_state,
            driver_sha256=self._adopted.driver_sha256,
            driver_evidence=self._adopted.driver_evidence,
            executing_code_identity_verified=False,
            process_started=started,
            direct_process_disposition=(
                DirectProcessDisposition.STOPPED
                if started
                else DirectProcessDisposition.NOT_STARTED
            ),
            workspace_cleanup_disposition=workspace_disposition,
            process_exit_code=exit_code,
            answer=None,
            response_sha256=None,
            usage=usage,
            workspace_remnant=workspace,
        )

    def invoke(self, objective: str, context: ContextAssembly) -> AdapterObservation:
        _require_text(objective, "objective")
        config = self._adopted.configuration
        if len(context.documents) != 1 or context.documents[0].source.path != LICENSING_PATH:
            raise ValueError("Adapter v0 requires exactly the pinned LICENSING.md document")
        context_bytes = context.to_json_bytes()
        context_digest = hashlib.sha256(context_bytes).hexdigest()
        request = {
            "configuration_sha256": config.digest,
            "context": context.to_dict(),
            "context_sha256": context_digest,
            "format": "peoplebot.codex-read-only-request.v0",
            "instructions": (
                "Use only the supplied context. Do not call tools or perform external actions. "
                "Return only one JSON object matching the supplied response schema."
            ),
            "objective": objective,
        }
        prompt = stable_json_bytes(request)
        transport = _invoke_codex_json_transport(
            self.executable,
            self.codex_home,
            config,
            prompt,
            runner=self._runner,
            workspace_factory=self._workspace_factory,
        )
        if not transport.succeeded:
            return self._failure(
                transport.code,
                transport.detail,
                context_digest,
                started=transport.process_started,
                exit_code=transport.process_exit_code,
                usage=transport.usage,
                workspace=transport.workspace_remnant,
                workspace_disposition=transport.workspace_cleanup_disposition,
            )
        assert transport.response_text is not None
        try:
            answer = _licensing_answer(
                transport.response_text,
                context.documents[0].source,
                config.max_response_bytes,
                config.max_rendered_answer_characters,
            )
        except _ResponseError as error:
            return self._failure(
                error.code,
                error.detail,
                context_digest,
                started=True,
                exit_code=transport.process_exit_code,
                usage=transport.usage,
                workspace_disposition=transport.workspace_cleanup_disposition,
            )
        response_bytes = stable_json_bytes(answer.to_dict())
        return AdapterObservation(
            code="adapter.completed",
            detail="Codex CLI returned one validated read-only licensing answer",
            runtime=config.runtime,
            runtime_version=config.runtime_version,
            model=config.model,
            context_sha256=context_digest,
            configuration_sha256=config.digest,
            configuration_blob_sha256=self._adopted.configuration_blob_sha256,
            driver_state=self._adopted.driver_state,
            driver_sha256=self._adopted.driver_sha256,
            driver_evidence=self._adopted.driver_evidence,
            executing_code_identity_verified=False,
            process_started=True,
            direct_process_disposition=DirectProcessDisposition.STOPPED,
            workspace_cleanup_disposition=WorkspaceCleanupDisposition.REMOVED,
            process_exit_code=transport.process_exit_code,
            answer=answer,
            response_sha256=hashlib.sha256(response_bytes).hexdigest(),
            usage=transport.usage,
        )


def _licensing_context(checkout: str | Path, source_state: StateRef) -> ContextAssembly:
    input_state = StateRef(source_state.repository, source_state.commit, LICENSING_PATH)
    policy = ContextPolicy(input_state, 1, 32_768, 32_768)
    return assemble_context(checkout, source_state, (LICENSING_PATH,), policy)


def run_read_only_licensing_execution(
    checkout: str | Path,
    runtime_root: str | Path,
    store: AttemptEvidenceStore,
    start: ExecutionStart,
    adapter: CodexReadOnlyAdapter,
    finished_at: Callable[[], str],
) -> ReadOnlyExecutionResult:
    """Run the one v0 licensing demonstration through admission and provenance."""

    if start.adapter != adapter.state:
        raise ValueError("Execution start must pin the adopted Adapter State")
    expected_input = StateRef(
        start.starting_state.repository,
        start.starting_state.commit,
        LICENSING_PATH,
    )
    if start.input_states != (expected_input,):
        raise ValueError("Execution start must pin exactly the selected licensing input State")
    context = _licensing_context(checkout, start.starting_state)
    observation: AdapterObservation | None = None

    def record(status: ExecutionStatus, outcome: TerminalOutcome | None) -> ExecutionRecord:
        usage = observation.usage if observation is not None else (UNKNOWN_USAGE,)
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
            resulting_state=start.starting_state if status is ExecutionStatus.NO_CHANGE else None,
            terminal_outcome=outcome,
            usage=usage,
        )

    def task() -> ExecutionRecord:
        nonlocal observation
        observation = adapter.invoke(start.objective, context)
        if observation.succeeded:
            return record(ExecutionStatus.NO_CHANGE, None)
        return record(
            ExecutionStatus.FAILED,
            TerminalOutcome(observation.code, observation.detail),
        )

    def failure_record(error: Exception) -> ExecutionRecord:
        return record(
            ExecutionStatus.FAILED,
            TerminalOutcome(
                "adapter.unexpected_failure",
                f"Adapter raised {type(error).__module__}.{type(error).__qualname__}",
            ),
        )

    def terminal_artifacts(record: ExecutionRecord) -> Mapping[str, bytes]:
        if observation is None:
            raise ValueError("Adapter observation is unavailable for durable terminal evidence")
        return {
            "adapter-observation.json": stable_json_bytes(
                {
                    "adapter_observation": observation.to_dict(),
                    "adapter_state": start.adapter.to_dict(),
                    "context_state": expected_input.to_dict(),
                    "execution_id": record.execution_id,
                    "format": "peoplebot.adapter-observation.v0",
                }
            )
        }

    provenance = run_with_execution_provenance(
        runtime_root,
        store,
        start,
        task,
        failure_record,
        terminal_artifacts,
    )
    return ReadOnlyExecutionResult(context, observation, provenance)
