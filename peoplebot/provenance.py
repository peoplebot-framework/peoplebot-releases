"""Git-backed evidence for synchronous admission and Execution outcomes."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ._json import stable_json_bytes
from .admission import AdmissionError, ExecutionAdmission, try_acquire_execution
from .execution import ExecutionRecord, ExecutionStatus, TerminalOutcome, _require_text, _utc_timestamp
from .state import (
    _GIT_GLOBAL_OPTIONS,
    _git_environment,
    StateRef,
    StateResolutionError,
    resolve_state,
)


_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_ZERO_OBJECT_ID = "0" * 40
_ATTEMPT_PATH = "attempt.json"
_EXECUTION_PATH = "execution.json"
_ADAPTER_OBSERVATION_PATH = "adapter-observation.json"
_SYNCHRONIZATION_OBSERVATION_PATH = "synchronization-observation.json"


class AttemptPhase(StrEnum):
    REJECTED = "rejected"
    ADMITTED_START = "admitted_start"


@dataclass(frozen=True, slots=True)
class ExecutionStart:
    """The exact inputs known before protected task code can start."""

    execution_id: str
    environment_id: str
    instance_id: str
    objective: str
    started_at: str
    starting_state: StateRef
    blueprint: StateRef
    adapter: StateRef
    procedures: tuple[StateRef, ...] = ()
    input_states: tuple[StateRef, ...] = ()
    input_messages: tuple[StateRef, ...] = ()

    def __post_init__(self) -> None:
        for field, value in (
            ("execution_id", self.execution_id),
            ("environment_id", self.environment_id),
            ("instance_id", self.instance_id),
            ("objective", self.objective),
        ):
            _require_text(value, field)
        _utc_timestamp(self.started_at, "started_at")
        for field, value in (
            ("starting_state", self.starting_state),
            ("blueprint", self.blueprint),
            ("adapter", self.adapter),
        ):
            if not isinstance(value, StateRef):
                raise ValueError(f"{field} must be a StateRef")
        if self.starting_state.path is not None:
            raise ValueError("starting_state must identify a repository-level commit")
        for field, values in (
            ("procedures", self.procedures),
            ("input_states", self.input_states),
            ("input_messages", self.input_messages),
        ):
            if not isinstance(values, tuple) or not all(
                isinstance(value, StateRef) for value in values
            ):
                raise ValueError(f"{field} must be a tuple of StateRef")


@dataclass(frozen=True, slots=True)
class ExecutionAttemptRecord:
    """A rejected admission or the committed boundary immediately before task start."""

    start: ExecutionStart
    phase: AttemptPhase
    admission_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.start, ExecutionStart):
            raise ValueError("start must be an ExecutionStart")
        if not isinstance(self.phase, AttemptPhase):
            raise ValueError("phase must be an AttemptPhase")
        _require_text(self.admission_code, "admission_code")
        expected = {
            AttemptPhase.REJECTED: "instance.already_running",
            AttemptPhase.ADMITTED_START: "admission.acquired",
        }[self.phase]
        if self.admission_code != expected:
            raise ValueError(f"{self.phase.value} requires admission code {expected}")

    def to_dict(self) -> dict[str, object]:
        start = self.start
        return {
            "adapter": start.adapter.to_dict(),
            "admission_code": self.admission_code,
            "blueprint": start.blueprint.to_dict(),
            "environment_id": start.environment_id,
            "execution_id": start.execution_id,
            "format": "peoplebot.execution-attempt.v0",
            "input_messages": [reference.to_dict() for reference in start.input_messages],
            "input_states": [reference.to_dict() for reference in start.input_states],
            "instance_id": start.instance_id,
            "objective": start.objective,
            "phase": self.phase.value,
            "procedures": [reference.to_dict() for reference in start.procedures],
            "started_at": start.started_at,
            "starting_state": start.starting_state.to_dict(),
            "task_started": False,
        }

    def to_json_bytes(self) -> bytes:
        return stable_json_bytes(self.to_dict())


@dataclass(frozen=True, slots=True)
class PersistedEvidence:
    """An exact local evidence commit and its discovery ref."""

    state: StateRef
    ref_name: str
    locally_committed: bool = True
    remote_synchronized: bool = False


class ProvenanceError(RuntimeError):
    """A bounded, classified local provenance failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class UnresolvedExecutionOwnership(BaseException):
    """Narrow in-process signal that ordinary admission release is unsafe."""

    def __init__(self, message: str) -> None:
        self.retained_admission: ExecutionAdmission | None = None
        super().__init__(message)

    def retain_admission(self, admission: ExecutionAdmission) -> None:
        self.retained_admission = admission


class AttemptEvidenceStore(Protocol):
    def persist_attempt(self, record: ExecutionAttemptRecord) -> PersistedEvidence: ...

    def persist_terminal(
        self,
        start_evidence: PersistedEvidence,
        record: ExecutionRecord,
        companion_artifacts: Mapping[str, bytes] | None = None,
    ) -> PersistedEvidence: ...


@dataclass(frozen=True, slots=True)
class ProvenanceRunResult:
    """Returned observations, distinct from locally committed evidence."""

    admission_code: str
    task_started: bool
    attempt_record: ExecutionAttemptRecord
    start_evidence: PersistedEvidence | None = None
    execution_record: ExecutionRecord | None = None
    terminal_evidence: PersistedEvidence | None = None
    task_failure: TerminalOutcome | None = None
    record_failure: TerminalOutcome | None = None
    persistence_failure: TerminalOutcome | None = None
    release_failure: TerminalOutcome | None = None
    retained_admission: ExecutionAdmission | None = None

    @property
    def terminal_committed(self) -> bool:
        return self.terminal_evidence is not None


def _attempt_digest(start: ExecutionStart) -> str:
    digest = hashlib.sha256(b"peoplebot.execution-provenance.v0\0attempt\0")
    for value in (
        start.starting_state.repository,
        start.environment_id,
        start.instance_id,
        start.execution_id,
    ):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _failure(code: str, summary: str) -> TerminalOutcome:
    return TerminalOutcome(code, summary)


def _exception_type(error: BaseException) -> str:
    error_type = type(error)
    return f"{error_type.__module__}.{error_type.__qualname__}"


def _persistence_failure(error: BaseException) -> TerminalOutcome:
    if isinstance(error, ProvenanceError):
        return _failure(error.code, error.detail)
    if isinstance(error, StateResolutionError):
        return _failure(error.code, error.detail)
    return _failure(
        "provenance.persistence_failed",
        f"evidence persistence raised {_exception_type(error)}",
    )


def _record_failure(error: BaseException) -> TerminalOutcome:
    return _failure(
        "provenance.execution_record_unavailable",
        f"terminal record production raised {_exception_type(error)}",
    )


def _validate_terminal_binding(start: ExecutionStart, record: ExecutionRecord) -> None:
    if not isinstance(record, ExecutionRecord):
        raise ValueError("task must return an ExecutionRecord")
    for field in (
        "execution_id",
        "environment_id",
        "instance_id",
        "objective",
        "started_at",
        "starting_state",
        "blueprint",
        "adapter",
        "procedures",
        "input_states",
        "input_messages",
    ):
        if getattr(record, field) != getattr(start, field):
            raise ValueError(f"terminal ExecutionRecord {field} does not match start evidence")


@dataclass(frozen=True, slots=True)
class _GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class GitAttemptStore:
    """Write and read attempt evidence without touching a worktree or branch."""

    def __init__(
        self, checkout: str | Path, repository: str, *, timeout_seconds: int = 15
    ) -> None:
        _require_text(repository, "repository")
        checkout_path = Path(checkout)
        if not checkout_path.is_dir():
            raise ProvenanceError("repository.unavailable", "checkout directory does not exist")
        self.checkout = checkout_path
        self.repository = repository
        if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 15:
            raise ValueError("Git evidence timeout must be between 1 and 15 seconds")
        self.timeout_seconds = timeout_seconds

    def _git(
        self,
        *arguments: str,
        input_bytes: bytes | None = None,
        environment: dict[str, str] | None = None,
    ) -> _GitResult:
        git_environment = _git_environment()
        if environment:
            git_environment.update(environment)
        try:
            completed = subprocess.run(
                [
                    "git",
                    *_GIT_GLOBAL_OPTIONS,
                    "-C",
                    os.fspath(self.checkout),
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    *arguments,
                ],
                input=input_bytes,
                capture_output=True,
                check=False,
                env=git_environment,
                shell=False,
                timeout=self.timeout_seconds,
            )
        except FileNotFoundError as error:
            raise ProvenanceError("git.unavailable", "Git executable was not found") from error
        except subprocess.TimeoutExpired as error:
            raise ProvenanceError(
                "git.timeout",
                f"Git operation exceeded {self.timeout_seconds} seconds",
            ) from error
        return _GitResult(completed.returncode, completed.stdout, completed.stderr)

    @staticmethod
    def _object_id(result: _GitResult, operation: str) -> str:
        value = result.stdout.decode("ascii", "replace").strip()
        if result.returncode != 0 or not _OBJECT_ID.fullmatch(value):
            raise ProvenanceError(
                "provenance.persistence_failed",
                f"Git could not {operation}",
            )
        return value

    @staticmethod
    def _commit_date(timestamp: str) -> str:
        parsed = _utc_timestamp(timestamp, "record timestamp")
        return f"@{int(parsed.timestamp())} +0000"

    def _write_blob(self, content: bytes) -> str:
        return self._object_id(
            self._git(
                "hash-object",
                "-w",
                "--stdin",
                "--no-filters",
                input_bytes=content,
            ),
            "write the evidence blob",
        )

    def _write_tree(self, entries: Mapping[str, str]) -> str:
        serialized = bytearray()
        for path, blob in sorted(entries.items()):
            if path not in {
                _ATTEMPT_PATH,
                _EXECUTION_PATH,
                _ADAPTER_OBSERVATION_PATH,
                _SYNCHRONIZATION_OBSERVATION_PATH,
            }:
                raise ValueError("evidence path is unsupported")
            if not _OBJECT_ID.fullmatch(blob):
                raise ValueError("evidence blob must be an object ID")
            serialized.extend(f"100644 blob {blob}\t{path}\n".encode("ascii"))
        return self._object_id(
            self._git("mktree", input_bytes=bytes(serialized)),
            "write the evidence tree",
        )

    def _write_commit(
        self,
        tree: str,
        parents: tuple[str, ...],
        timestamp: str,
        message: str,
    ) -> str:
        identity_environment = {
            "GIT_AUTHOR_NAME": "PeopleBot",
            "GIT_AUTHOR_EMAIL": "peoplebot@invalid",
            "GIT_AUTHOR_DATE": self._commit_date(timestamp),
            "GIT_COMMITTER_NAME": "PeopleBot",
            "GIT_COMMITTER_EMAIL": "peoplebot@invalid",
            "GIT_COMMITTER_DATE": self._commit_date(timestamp),
        }
        arguments = ["-c", "commit.gpgsign=false", "commit-tree", tree]
        for parent in parents:
            if not _OBJECT_ID.fullmatch(parent):
                raise ValueError("evidence parent must be a commit object ID")
            arguments.extend(("-p", parent))
        return self._object_id(
            self._git(
                *arguments,
                input_bytes=(message + "\n").encode("utf-8"),
                environment=identity_environment,
            ),
            "write the evidence commit",
        )

    def _update_ref(
        self,
        ref_name: str,
        commit: str,
        expected: str,
        *,
        reflog_message: str = "peoplebot execution provenance v0",
        conflict_code: str = "provenance.attempt_exists",
        conflict_detail: str = "the isolated attempt ref already exists or changed",
        symbolic_code: str = "provenance.attempt_exists",
        symbolic_detail: str = "the isolated attempt ref is unexpectedly symbolic",
        inspection_code: str = "provenance.ref_inspection_failed",
        inspection_detail: str = (
            "Git could not establish that the isolated attempt ref is not symbolic"
        ),
        persistence_code: str = "provenance.persistence_failed",
        persistence_detail: str = "Git could not update the isolated attempt ref",
    ) -> None:
        self._update_refs(
            {ref_name: (commit, expected)},
            reflog_message=reflog_message,
            conflict_code=conflict_code,
            conflict_detail=conflict_detail,
            symbolic_code=symbolic_code,
            symbolic_detail=symbolic_detail,
            inspection_code=inspection_code,
            inspection_detail=inspection_detail,
            persistence_code=persistence_code,
            persistence_detail=persistence_detail,
        )

    def _update_refs(
        self,
        updates: Mapping[str, tuple[str | None, str]],
        *,
        reflog_message: str,
        conflict_code: str,
        conflict_detail: str,
        symbolic_code: str,
        symbolic_detail: str,
        inspection_code: str,
        inspection_detail: str,
        persistence_code: str,
        persistence_detail: str,
    ) -> None:
        """Apply direct ref verifies/updates as one prepared Git transaction.

        A mapping value is ``(new_object, expected_object)``. ``None`` verifies
        without updating; the all-zero object ID denotes absence or deletion.
        """

        if not isinstance(updates, Mapping) or not updates:
            raise ValueError("updates must be a non-empty mapping")
        operations: list[str] = []
        for ref_name, update in updates.items():
            if (
                not isinstance(ref_name, str)
                or not ref_name.startswith("refs/")
                or any(character in ref_name for character in "\0\r\n ")
                or not isinstance(update, tuple)
                or len(update) != 2
            ):
                raise ValueError("reference transaction input is invalid")
            commit, expected = update
            if not _OBJECT_ID.fullmatch(expected) or (
                commit is not None and not _OBJECT_ID.fullmatch(commit)
            ):
                raise ValueError("reference transaction objects must be full object IDs")
            if commit is None:
                operations.append(f"verify {ref_name} {expected}")
            elif commit == _ZERO_OBJECT_ID:
                if expected == _ZERO_OBJECT_ID:
                    raise ValueError("deleting an absent ref is unsupported")
                operations.append(f"delete {ref_name} {expected}")
            elif expected == _ZERO_OBJECT_ID:
                operations.append(f"create {ref_name} {commit}")
            else:
                operations.append(f"update {ref_name} {commit} {expected}")

        command = [
            "git",
            *_GIT_GLOBAL_OPTIONS,
            "-C",
            os.fspath(self.checkout),
            "-c",
            f"core.hooksPath={os.devnull}",
            "update-ref",
            "-m",
            reflog_message,
            "--stdin",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_git_environment(),
                shell=False,
            )
        except FileNotFoundError as error:
            raise ProvenanceError("git.unavailable", "Git executable was not found") from error

        def read_status() -> str:
            assert process.stdout is not None
            result: queue.Queue[bytes] = queue.Queue(maxsize=1)
            threading.Thread(
                target=lambda: result.put(process.stdout.readline()),
                daemon=True,
            ).start()
            try:
                return result.get(timeout=15).decode("utf-8", "replace").strip()
            except queue.Empty as error:
                process.kill()
                process.wait(timeout=15)
                raise ProvenanceError(
                    "git.timeout",
                    "Git reference transaction exceeded 15 seconds",
                ) from error

        def finish(command_name: str) -> str:
            assert process.stdin is not None
            process.stdin.write((command_name + "\n").encode("ascii"))
            process.stdin.flush()
            return read_status()

        try:
            assert process.stdin is not None
            process.stdin.write((
                "start\n"
                "option no-deref\n"
                + "\n".join(operations)
                + "\n"
                "prepare\n"
            ).encode("ascii"))
            process.stdin.flush()
            started = read_status()
            prepared = read_status()
            if started != "start: ok" or prepared != "prepare: ok":
                process.stdin.close()
                process.wait(timeout=15)
                conflict = False
                for ref_name, (_, expected) in updates.items():
                    symbolic = self._git(
                        "symbolic-ref", "--quiet", "--no-recurse", ref_name
                    )
                    if symbolic.returncode == 0:
                        raise ProvenanceError(symbolic_code, symbolic_detail)
                    if symbolic.returncode != 1:
                        raise ProvenanceError(inspection_code, inspection_detail)
                    current = self._git("rev-parse", "--verify", "--quiet", ref_name)
                    if current.returncode not in (0, 1):
                        raise ProvenanceError(inspection_code, inspection_detail)
                    observed = current.stdout.decode("ascii", "replace").strip()
                    if expected == _ZERO_OBJECT_ID:
                        conflict = conflict or current.returncode == 0
                    else:
                        conflict = conflict or current.returncode != 0 or observed != expected
                if conflict:
                    raise ProvenanceError(conflict_code, conflict_detail)
                raise ProvenanceError(
                    persistence_code,
                    persistence_detail,
                )

            for ref_name in updates:
                symbolic = self._git(
                    "symbolic-ref", "--quiet", "--no-recurse", ref_name
                )
                if symbolic.returncode == 0:
                    aborted = finish("abort")
                    process.stdin.close()
                    process.wait(timeout=15)
                    if aborted != "abort: ok":
                        raise ProvenanceError(persistence_code, persistence_detail)
                    raise ProvenanceError(symbolic_code, symbolic_detail)
                if symbolic.returncode != 1:
                    aborted = finish("abort")
                    process.stdin.close()
                    process.wait(timeout=15)
                    if aborted != "abort: ok" or process.returncode != 0:
                        raise ProvenanceError(persistence_code, persistence_detail)
                    raise ProvenanceError(inspection_code, inspection_detail)

            committed = finish("commit")
            process.stdin.close()
            process.wait(timeout=15)
            if committed != "commit: ok" or process.returncode != 0:
                raise ProvenanceError(
                    persistence_code,
                    persistence_detail,
                )
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait(timeout=15)
            raise ProvenanceError(
                "git.timeout",
                "Git reference transaction exceeded 15 seconds",
            ) from error
        except BaseException:
            if process.poll() is None:
                try:
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.write(b"abort\n")
                        process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=15)
            raise
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def persist_attempt(self, record: ExecutionAttemptRecord) -> PersistedEvidence:
        if not isinstance(record, ExecutionAttemptRecord):
            raise ValueError("record must be an ExecutionAttemptRecord")
        start = record.start
        if start.starting_state.repository != self.repository:
            raise ProvenanceError(
                "provenance.repository_mismatch",
                "starting State repository does not match evidence repository",
            )
        resolve_state(self.checkout, start.starting_state)
        ref_name = f"refs/peoplebot/attempts/v0/{_attempt_digest(start)}"
        blob = self._write_blob(record.to_json_bytes())
        tree = self._write_tree({_ATTEMPT_PATH: blob})
        commit = self._write_commit(
            tree,
            (start.starting_state.commit,),
            start.started_at,
            "PeopleBot execution attempt",
        )
        self._update_ref(ref_name, commit, _ZERO_OBJECT_ID)
        return PersistedEvidence(
            StateRef(self.repository, commit, _ATTEMPT_PATH),
            ref_name,
        )

    def persist_terminal(
        self,
        start_evidence: PersistedEvidence,
        record: ExecutionRecord,
        companion_artifacts: Mapping[str, bytes] | None = None,
    ) -> PersistedEvidence:
        if not isinstance(start_evidence, PersistedEvidence):
            raise ValueError("start_evidence must be PersistedEvidence")
        if not isinstance(record, ExecutionRecord):
            raise ValueError("record must be an ExecutionRecord")
        if (
            start_evidence.state.repository != self.repository
            or start_evidence.state.path != _ATTEMPT_PATH
        ):
            raise ProvenanceError(
                "provenance.start_mismatch",
                "terminal evidence requires this store's exact attempt State",
            )
        terminal_start = ExecutionStart(
            execution_id=record.execution_id,
            environment_id=record.environment_id,
            instance_id=record.instance_id,
            objective=record.objective,
            started_at=record.started_at,
            starting_state=record.starting_state,
            blueprint=record.blueprint,
            adapter=record.adapter,
            procedures=record.procedures,
            input_states=record.input_states,
            input_messages=record.input_messages,
        )
        expected_ref = f"refs/peoplebot/attempts/v0/{_attempt_digest(terminal_start)}"
        if start_evidence.ref_name != expected_ref:
            raise ProvenanceError(
                "provenance.start_mismatch",
                "attempt ref does not match the terminal Execution inputs",
            )
        resolve_state(self.checkout, start_evidence.state)
        expected_attempt = ExecutionAttemptRecord(
            terminal_start,
            AttemptPhase.ADMITTED_START,
            "admission.acquired",
        ).to_json_bytes()
        if self.read_evidence_bytes(start_evidence.state) != expected_attempt:
            raise ProvenanceError(
                "provenance.start_mismatch",
                "attempt evidence does not match the terminal Execution inputs",
            )
        blobs = {_EXECUTION_PATH: self._write_blob(record.to_json_bytes())}
        if companion_artifacts is not None:
            if set(companion_artifacts) not in (
                {_ADAPTER_OBSERVATION_PATH},
                {_SYNCHRONIZATION_OBSERVATION_PATH},
            ):
                raise ValueError("terminal companion artifacts do not match v0")
            path, content = next(iter(companion_artifacts.items()))
            if not isinstance(content, bytes):
                raise ValueError("terminal companion artifact must be bytes")
            blobs[path] = self._write_blob(content)
        parents = [start_evidence.state.commit]
        if record.adapter.repository == self.repository:
            resolve_state(self.checkout, record.adapter)
            if record.adapter.commit not in parents:
                parents.append(record.adapter.commit)
        tree = self._write_tree(blobs)
        commit = self._write_commit(
            tree,
            tuple(parents),
            record.finished_at,
            "PeopleBot execution terminal",
        )
        self._update_ref(start_evidence.ref_name, commit, start_evidence.state.commit)
        return PersistedEvidence(
            StateRef(self.repository, commit, _EXECUTION_PATH),
            start_evidence.ref_name,
        )

    def read_evidence_bytes(self, state: StateRef) -> bytes:
        if state.repository != self.repository or state.path is None:
            raise ProvenanceError(
                "provenance.evidence_mismatch",
                "evidence State must identify a path in this repository",
            )
        resolved = resolve_state(self.checkout, state)
        if resolved.selected_type != "blob":
            raise ProvenanceError(
                "provenance.evidence_invalid",
                "evidence State does not select a blob",
            )
        content = self._git("cat-file", "blob", resolved.selected_object)
        if content.returncode != 0:
            raise ProvenanceError(
                "state.object_unavailable",
                "evidence blob is unavailable locally",
            )
        return content.stdout

    def read_evidence(self, state: StateRef) -> dict[str, object]:
        try:
            value = json.loads(self.read_evidence_bytes(state))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProvenanceError(
                "provenance.evidence_invalid",
                "evidence blob is not valid UTF-8 JSON",
            ) from error
        if not isinstance(value, dict):
            raise ProvenanceError(
                "provenance.evidence_invalid",
                "evidence JSON must be an object",
            )
        return value


def run_with_execution_provenance(
    runtime_root: str | Path,
    store: AttemptEvidenceStore,
    start: ExecutionStart,
    task: Callable[[], ExecutionRecord],
    failure_record: Callable[[Exception], ExecutionRecord],
    terminal_artifacts: Callable[[ExecutionRecord], Mapping[str, bytes]] | None = None,
) -> ProvenanceRunResult:
    """Run one admitted synchronous task with ordered, local Git evidence."""

    if not isinstance(start, ExecutionStart):
        raise ValueError("start must be an ExecutionStart")
    if not callable(task):
        raise ValueError("task must be callable")
    if not callable(failure_record):
        raise ValueError("failure_record must be callable")

    admission_attempt = try_acquire_execution(
        runtime_root,
        start.environment_id,
        start.instance_id,
        start.execution_id,
    )
    phase = (
        AttemptPhase.ADMITTED_START if admission_attempt.acquired else AttemptPhase.REJECTED
    )
    attempt_record = ExecutionAttemptRecord(start, phase, admission_attempt.code)

    if not admission_attempt.acquired:
        try:
            rejection_evidence = store.persist_attempt(attempt_record)
        except Exception as error:
            return ProvenanceRunResult(
                admission_attempt.code,
                False,
                attempt_record,
                persistence_failure=_persistence_failure(error),
            )
        return ProvenanceRunResult(
            admission_attempt.code,
            False,
            attempt_record,
            start_evidence=rejection_evidence,
        )

    admission = admission_attempt.admission
    assert admission is not None
    task_started = False
    start_evidence: PersistedEvidence | None = None
    execution_record: ExecutionRecord | None = None
    terminal_evidence: PersistedEvidence | None = None
    task_failure: TerminalOutcome | None = None
    record_failure: TerminalOutcome | None = None
    persistence_failure: TerminalOutcome | None = None
    release_failure: TerminalOutcome | None = None
    retained_admission: ExecutionAdmission | None = None
    release_admission = True

    try:
        try:
            start_evidence = store.persist_attempt(attempt_record)
        except Exception as error:
            persistence_failure = _persistence_failure(error)
        else:
            task_started = True
            candidate_record: object | None
            try:
                candidate_record = task()
            except Exception as error:
                task_failure = _failure(
                    "execution.task_raised",
                    f"task raised {_exception_type(error)}",
                )
                try:
                    candidate_record = failure_record(error)
                    if (
                        not isinstance(candidate_record, ExecutionRecord)
                        or candidate_record.status is not ExecutionStatus.FAILED
                    ):
                        raise ValueError(
                            "failure_record must return a failed ExecutionRecord"
                        )
                except Exception as record_error:
                    candidate_record = None
                    record_failure = _record_failure(record_error)

            if record_failure is None:
                try:
                    _validate_terminal_binding(start, candidate_record)
                except Exception as error:
                    record_failure = _record_failure(error)
                else:
                    assert isinstance(candidate_record, ExecutionRecord)
                    execution_record = candidate_record

            if execution_record is not None:
                try:
                    if terminal_artifacts is None:
                        terminal_evidence = store.persist_terminal(
                            start_evidence,
                            execution_record,
                        )
                    else:
                        artifacts = terminal_artifacts(execution_record)
                        terminal_evidence = store.persist_terminal(
                            start_evidence,
                            execution_record,
                            artifacts,
                        )
                except Exception as error:
                    persistence_failure = _persistence_failure(error)
    except BaseException as error:
        if isinstance(error, UnresolvedExecutionOwnership):
            error.retain_admission(admission)
            release_admission = False
            raise
        raise
    finally:
        if release_admission:
            try:
                admission.release()
            except AdmissionError as error:
                release_failure = _failure(error.code, error.detail)
                retained_admission = admission

    return ProvenanceRunResult(
        admission_attempt.code,
        task_started,
        attempt_record,
        start_evidence=start_evidence,
        execution_record=execution_record,
        terminal_evidence=terminal_evidence,
        task_failure=task_failure,
        record_failure=record_failure,
        persistence_failure=persistence_failure,
        release_failure=release_failure,
        retained_admission=retained_admission,
    )
