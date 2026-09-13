"""Deterministic synthetic alpha setup, compatible adoption, and exact resume."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from ._json import stable_json_bytes
from .adapters.codex_read_only import DirectProcessSetupFailure, _run_process
from .execution import ExecutionRecord, ExecutionStatus, TerminalOutcome
from .memory import MemoryError, assemble_instance_memory_context
from .preparation import ContextPolicy
from .provenance import (
    AttemptEvidenceStore,
    ExecutionStart,
    GitAttemptStore,
    ProvenanceError,
    ProvenanceRunResult,
    run_with_execution_provenance,
)
from .state import StateRef, StateResolutionError, resolve_state


_ZERO_OBJECT_ID = "0" * 40
_SELECTION_PATH = "environment.json"
_MAX_SOURCE_BYTES = 65_536
_SUPPORTED_MODE = "100644"
_FIXTURE_TIMEOUT_SECONDS = 5
_FIXTURE_LOADER = """
import json
import sys
from types import MappingProxyType

payload = json.loads(sys.stdin.buffer.read())
namespace = {"__builtins__": MappingProxyType({})}
code = compile(payload["source"], "git:alpha_runtime.py", "exec")
exec(code, namespace, namespace)
resume = namespace.get("resume")
if not callable(resume):
    raise ValueError("resume callable is absent")
result = resume(MappingProxyType(payload["memory"]))
serialized = json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
sys.stdout.buffer.write(serialized.encode("utf-8") + b"\\n")
"""


class AlphaError(RuntimeError):
    """A bounded, classified alpha setup/adoption/resume failure."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _require_identity(value: str, field: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{field} must be bounded non-empty text")


def alpha_selection_ref(environment_id: str, instance_id: str) -> str:
    """Return the stable discovery ref for one Instance's adopted framework."""

    _require_identity(environment_id, "environment_id")
    _require_identity(instance_id, "instance_id")
    digest = hashlib.sha256(
        b"peoplebot.alpha-selection.v0\0"
        + environment_id.encode("utf-8")
        + b"\0"
        + instance_id.encode("utf-8")
    ).hexdigest()
    return f"refs/peoplebot/environments/v0/framework/{digest}"


@dataclass(frozen=True, slots=True)
class AlphaFramework:
    """Exact framework inputs selected for the synthetic alpha fixture."""

    state: StateRef
    blueprint: StateRef
    compatibility: StateRef
    source: StateRef

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, StateRef)
            for value in (self.state, self.blueprint, self.compatibility, self.source)
        ):
            raise ValueError("framework inputs must be StateRef values")
        if self.state.path is not None:
            raise ValueError("framework state must be repository-level")
        for field, value in (
            ("blueprint", self.blueprint),
            ("compatibility", self.compatibility),
            ("source", self.source),
        ):
            if value.path is None:
                raise ValueError(f"{field} must identify an exact path")
            if value.repository != self.state.repository:
                raise ValueError(f"{field} must belong to the framework repository")
        if (
            self.compatibility.commit != self.state.commit
            or self.source.commit != self.state.commit
        ):
            raise ValueError("compatibility and source must belong to the exact framework state")

    def to_dict(self) -> dict[str, object]:
        return {
            "blueprint": self.blueprint.to_dict(),
            "compatibility": self.compatibility.to_dict(),
            "source": self.source.to_dict(),
            "state": self.state.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class AlphaSelection:
    repository: str
    environment_id: str
    instance_id: str
    framework: AlphaFramework
    compatibility_object: str
    blueprint_object: str
    source_object: str
    source_sha256: str
    source_bytes: int
    ref_name: str

    def __post_init__(self) -> None:
        _require_identity(self.repository, "repository")
        _require_identity(self.environment_id, "environment_id")
        _require_identity(self.instance_id, "instance_id")
        if not isinstance(self.framework, AlphaFramework):
            raise ValueError("framework must be AlphaFramework")
        for field, value in (
            ("compatibility_object", self.compatibility_object),
            ("blueprint_object", self.blueprint_object),
            ("source_object", self.source_object),
        ):
            if not isinstance(value, str) or len(value) not in (40, 64) or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise ValueError(f"{field} must be a full lowercase Git object ID")
        if (
            not isinstance(self.source_sha256, str)
            or len(self.source_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.source_sha256)
        ):
            raise ValueError("source_sha256 must be a lowercase SHA-256 digest")
        if isinstance(self.source_bytes, bool) or not 0 <= self.source_bytes <= _MAX_SOURCE_BYTES:
            raise ValueError("source_bytes is outside the supported bound")
        if self.ref_name != alpha_selection_ref(self.environment_id, self.instance_id):
            raise ValueError("ref_name is not the canonical alpha selection ref")

    def to_dict(self) -> dict[str, object]:
        return {
            "blueprint_object": self.blueprint_object,
            "compatibility_object": self.compatibility_object,
            "environment_id": self.environment_id,
            "format": "peoplebot.alpha-selection.v0",
            "framework": self.framework.to_dict(),
            "instance_id": self.instance_id,
            "ref_name": self.ref_name,
            "repository": self.repository,
            "source_bytes": self.source_bytes,
            "source_object": self.source_object,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class AlphaSetupRequest:
    repository: str
    environment_id: str
    instance_id: str
    framework_checkout: str | Path
    framework: AlphaFramework
    created_at: str


@dataclass(frozen=True, slots=True)
class AlphaSetupResult:
    state: StateRef
    selection: AlphaSelection

    def to_dict(self) -> dict[str, object]:
        return {
            "format": "peoplebot.alpha-setup-result.v0",
            "selection": self.selection.to_dict(),
            "state": self.state.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class AlphaAdoptionRequest:
    framework_checkout: str | Path
    current_selection_state: StateRef
    candidate_framework: AlphaFramework


@dataclass(frozen=True, slots=True)
class AlphaAdoptionExecutionResult:
    previous_selection: AlphaSelection
    adopted_state: StateRef | None
    adopted_selection: AlphaSelection | None
    provenance: ProvenanceRunResult

    def to_dict(self) -> dict[str, object]:
        record = self.provenance.execution_record
        return {
            "admission_code": self.provenance.admission_code,
            "adopted_selection": (
                self.adopted_selection.to_dict() if self.adopted_selection is not None else None
            ),
            "adopted_state": self.adopted_state.to_dict() if self.adopted_state else None,
            "format": "peoplebot.alpha-adoption-result.v0",
            "previous_selection": self.previous_selection.to_dict(),
            "status": record.status.value if record is not None else None,
            "task_started": self.provenance.task_started,
            "terminal_code": (
                record.terminal_outcome.code
                if record is not None and record.terminal_outcome is not None
                else None
            ),
            "terminal_committed": self.provenance.terminal_committed,
        }


@dataclass(frozen=True, slots=True)
class AlphaResumeObservation:
    selection_state: StateRef
    framework_state: StateRef
    blueprint: StateRef
    source_state: StateRef
    source_object: str
    source_sha256: str
    memory_state: StateRef
    memory_sha256: str
    requested_paths: tuple[str, ...]
    fixture_result: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "blueprint": self.blueprint.to_dict(),
            "fixture_result": dict(self.fixture_result),
            "format": "peoplebot.alpha-resume-observation.v0",
            "framework_state": self.framework_state.to_dict(),
            "memory_sha256": self.memory_sha256,
            "memory_state": self.memory_state.to_dict(),
            "requested_paths": list(self.requested_paths),
            "selection_state": self.selection_state.to_dict(),
            "source_object": self.source_object,
            "source_sha256": self.source_sha256,
            "source_state": self.source_state.to_dict(),
        }

    def to_json_bytes(self) -> bytes:
        return stable_json_bytes(self.to_dict())


@dataclass(frozen=True, slots=True)
class _ValidatedFramework:
    framework: AlphaFramework
    compatibility_object: str
    blueprint_object: str
    source_object: str
    source_sha256: str
    source_bytes: int


class _AlphaStore:
    def __init__(self, checkout: str | Path, repository: str) -> None:
        self.plumbing = GitAttemptStore(checkout, repository)
        self.checkout = self.plumbing.checkout
        self.repository = repository

    def blob(self, object_id: str, *, code: str, detail: str) -> bytes:
        result = self.plumbing._git("cat-file", "blob", object_id)
        if result.returncode != 0:
            raise AlphaError(code, detail)
        return result.stdout

    def write_selection(
        self,
        selection: AlphaSelection,
        parent: str | None,
        timestamp: str,
    ) -> StateRef:
        blob = self.plumbing._write_blob(stable_json_bytes(selection.to_dict()))
        tree = self.plumbing._object_id(
            self.plumbing._git(
                "mktree",
                input_bytes=f"100644 blob {blob}\t{_SELECTION_PATH}\n".encode("ascii"),
            ),
            "write the alpha environment tree",
        )
        commit = self.plumbing._write_commit(
            tree,
            () if parent is None else (parent,),
            timestamp,
            "PeopleBot synthetic alpha framework selection",
        )
        try:
            self.plumbing._update_ref(
                selection.ref_name,
                commit,
                _ZERO_OBJECT_ID if parent is None else parent,
                reflog_message="peoplebot alpha framework selection v0",
                conflict_code="alpha.selection_changed",
                conflict_detail="the adopted framework selection changed and was preserved",
                symbolic_code="alpha.selection_symbolic",
                symbolic_detail="the adopted framework selection is symbolic and was preserved",
                inspection_code="alpha.selection_inspection_failed",
                inspection_detail="Git could not establish the selection ref identity",
                persistence_code="alpha.selection_persistence_failed",
                persistence_detail="Git could not publish the adopted framework selection",
            )
        except ProvenanceError as error:
            raise AlphaError(error.code, error.detail) from error
        return StateRef(self.repository, commit)


def _git_common_directory(checkout: str | Path, repository: str) -> Path:
    plumbing = GitAttemptStore(checkout, repository)
    result = plumbing._git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode != 0:
        raise AlphaError("repository.invalid", "Git common directory could not be resolved")
    try:
        return Path(result.stdout.decode("utf-8", "strict").strip()).resolve(strict=True)
    except (UnicodeDecodeError, OSError) as error:
        raise AlphaError(
            "repository.invalid",
            "Git common directory could not be resolved exactly",
        ) from error


def _require_distinct_repositories(
    environment_checkout: str | Path,
    environment_repository: str,
    framework_checkout: str | Path,
    framework_repository: str,
) -> None:
    if environment_repository == framework_repository or _git_common_directory(
        environment_checkout,
        environment_repository,
    ) == _git_common_directory(framework_checkout, framework_repository):
        raise AlphaError(
            "alpha.repository_not_separate",
            "framework and environment must use separate Git object databases",
        )


def _tree_entry(
    checkout: Path,
    plumbing: GitAttemptStore,
    reference: StateRef,
) -> tuple[str, str, bytes]:
    try:
        resolved = resolve_state(checkout, reference)
    except StateResolutionError as error:
        raise AlphaError(error.code, error.detail) from error
    if resolved.selected_type != "blob":
        raise AlphaError("alpha.fixture_invalid", "fixture path must select a blob")
    result = plumbing._git("ls-tree", "-z", reference.commit, "--", reference.path or "")
    if result.returncode != 0:
        raise AlphaError("alpha.fixture_invalid", "Git could not inspect the fixture entry")
    entries = [entry for entry in result.stdout.split(b"\0") if entry]
    if len(entries) != 1 or b"\t" not in entries[0]:
        raise AlphaError("alpha.fixture_invalid", "fixture path did not resolve uniquely")
    header, path = entries[0].split(b"\t", 1)
    fields = header.decode("ascii", "replace").split(" ")
    if len(fields) != 3 or fields[1] != "blob" or fields[2] != resolved.selected_object:
        raise AlphaError("alpha.fixture_invalid", "fixture tree entry is inconsistent")
    if fields[0] != _SUPPORTED_MODE:
        raise AlphaError("alpha.fixture_invalid", "fixture paths must be regular non-executable files")
    if path.decode("utf-8", "strict") != reference.path:
        raise AlphaError("alpha.fixture_invalid", "fixture path encoding is inconsistent")
    content = plumbing._git("cat-file", "blob", resolved.selected_object)
    if content.returncode != 0:
        raise AlphaError("state.object_unavailable", "fixture blob is unavailable locally")
    return fields[0], resolved.selected_object, content.stdout


def _execute_fixture_source(source: bytes, memory: Mapping[str, str]) -> dict[str, object]:
    try:
        source_text = source.decode("utf-8")
        completed = _run_process(
            (sys.executable, "-I", "-c", _FIXTURE_LOADER),
            stable_json_bytes({"memory": dict(memory), "source": source_text}),
            os.environ,
            _FIXTURE_TIMEOUT_SECONDS,
        )
        if completed.returncode != 0 or len(completed.stdout) > _MAX_SOURCE_BYTES:
            raise ValueError("fixture process failed or exceeded its output bound")
        result = json.loads(completed.stdout)
        if (
            not isinstance(result, dict)
            or set(result) != {"fixture", "progress"}
            or not isinstance(result["fixture"], str)
            or not result["fixture"]
            or result["progress"] != memory["progress.md"]
        ):
            raise ValueError("fixture result does not satisfy the narrow interface")
        stable_json_bytes(result)
    except (
        OSError,
        subprocess.SubprocessError,
        DirectProcessSetupFailure,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as error:
        raise AlphaError(
            "alpha.source_invalid",
            "fixture source does not implement the deterministic resume interface",
        ) from error
    return result


def _validate_framework(
    checkout: str | Path,
    framework: AlphaFramework,
    current: AlphaSelection | None = None,
) -> _ValidatedFramework:
    checkout_path = Path(checkout)
    try:
        resolve_state(checkout_path, framework.state)
    except StateResolutionError as error:
        raise AlphaError(error.code, error.detail) from error
    plumbing = GitAttemptStore(checkout_path, framework.state.repository)
    _, blueprint_object, _ = _tree_entry(checkout_path, plumbing, framework.blueprint)
    _, compatibility_object, _ = _tree_entry(checkout_path, plumbing, framework.compatibility)
    _, source_object, source = _tree_entry(checkout_path, plumbing, framework.source)
    if len(source) > _MAX_SOURCE_BYTES:
        raise AlphaError("alpha.source_limit_exceeded", "fixture source exceeds 65536 bytes")
    try:
        source_text = source.decode("utf-8")
        compile(source_text, f"git:{framework.state.commit}:{framework.source.path}", "exec")
    except (UnicodeDecodeError, SyntaxError) as error:
        raise AlphaError("alpha.source_invalid", "fixture source is not valid UTF-8 Python") from error
    _execute_fixture_source(source, {"progress.md": "compatibility probe\n"})

    if current is not None:
        if framework.state.repository != current.framework.state.repository:
            raise AlphaError("alpha.candidate_incompatible", "candidate repository identity changed")
        if framework.state.commit == current.framework.state.commit:
            raise AlphaError("alpha.candidate_not_new", "candidate equals the adopted framework State")
        if (
            framework.blueprint.path != current.framework.blueprint.path
            or framework.compatibility.path != current.framework.compatibility.path
            or framework.source.path != current.framework.source.path
            or blueprint_object != current.blueprint_object
            or compatibility_object != current.compatibility_object
        ):
            raise AlphaError(
                "alpha.candidate_incompatible",
                "candidate changed the pinned Blueprint, interface anchor, or fixture paths",
            )
        ancestry = plumbing._git(
            "merge-base",
            "--is-ancestor",
            current.framework.state.commit,
            framework.state.commit,
        )
        if ancestry.returncode == 1:
            raise AlphaError("alpha.candidate_incompatible", "candidate does not extend adopted State")
        if ancestry.returncode != 0:
            raise AlphaError("alpha.candidate_unavailable", "Git could not verify candidate ancestry")

    selected_framework = framework
    if current is not None:
        selected_framework = AlphaFramework(
            framework.state,
            current.framework.blueprint,
            framework.compatibility,
            framework.source,
        )
    return _ValidatedFramework(
        framework=selected_framework,
        compatibility_object=compatibility_object,
        blueprint_object=blueprint_object,
        source_object=source_object,
        source_sha256=hashlib.sha256(source).hexdigest(),
        source_bytes=len(source),
    )


def setup_alpha_environment(
    environment_checkout: str | Path,
    request: AlphaSetupRequest,
) -> AlphaSetupResult:
    """Create one root environment selection State from an exact local framework State."""

    if not isinstance(request, AlphaSetupRequest):
        raise ValueError("request must be AlphaSetupRequest")
    _require_identity(request.repository, "repository")
    _require_identity(request.environment_id, "environment_id")
    _require_identity(request.instance_id, "instance_id")
    _require_distinct_repositories(
        environment_checkout,
        request.repository,
        request.framework_checkout,
        request.framework.state.repository,
    )
    observed = _validate_framework(request.framework_checkout, request.framework)
    selection = AlphaSelection(
        repository=request.repository,
        environment_id=request.environment_id,
        instance_id=request.instance_id,
        framework=observed.framework,
        compatibility_object=observed.compatibility_object,
        blueprint_object=observed.blueprint_object,
        source_object=observed.source_object,
        source_sha256=observed.source_sha256,
        source_bytes=observed.source_bytes,
        ref_name=alpha_selection_ref(request.environment_id, request.instance_id),
    )
    store = _AlphaStore(environment_checkout, request.repository)
    state = store.write_selection(selection, None, request.created_at)
    return AlphaSetupResult(state, selection)


def _state_from_dict(value: object, field: str) -> StateRef:
    if not isinstance(value, Mapping) or set(value) != {"repository", "commit", "path"}:
        raise AlphaError("alpha.selection_invalid", f"{field} State is invalid")
    try:
        return StateRef(value["repository"], value["commit"], value["path"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as error:
        raise AlphaError("alpha.selection_invalid", f"{field} State is invalid") from error


def read_alpha_selection(
    environment_checkout: str | Path,
    state: StateRef,
) -> AlphaSelection:
    """Read and validate one exact environment selection State."""

    if not isinstance(state, StateRef) or state.path is not None:
        raise ValueError("state must be a repository-level StateRef")
    path_state = StateRef(state.repository, state.commit, _SELECTION_PATH)
    try:
        resolved = resolve_state(environment_checkout, path_state)
    except StateResolutionError as error:
        raise AlphaError(error.code, error.detail) from error
    if resolved.selected_type != "blob":
        raise AlphaError("alpha.selection_invalid", "environment selection is not a blob")
    store = _AlphaStore(environment_checkout, state.repository)
    try:
        value = json.loads(
            store.blob(
                resolved.selected_object,
                code="state.object_unavailable",
                detail="environment selection blob is unavailable locally",
            )
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlphaError("alpha.selection_invalid", "environment selection is not UTF-8 JSON") from error
    required = {
        "blueprint_object",
        "compatibility_object",
        "environment_id",
        "format",
        "framework",
        "instance_id",
        "ref_name",
        "repository",
        "source_bytes",
        "source_object",
        "source_sha256",
    }
    if not isinstance(value, dict) or set(value) != required or value.get("format") != "peoplebot.alpha-selection.v0":
        raise AlphaError("alpha.selection_invalid", "environment selection schema is invalid")
    framework_value = value["framework"]
    if not isinstance(framework_value, dict) or set(framework_value) != {
        "state", "blueprint", "compatibility", "source"
    }:
        raise AlphaError("alpha.selection_invalid", "framework selection schema is invalid")
    try:
        framework = AlphaFramework(
            _state_from_dict(framework_value["state"], "framework"),
            _state_from_dict(framework_value["blueprint"], "blueprint"),
            _state_from_dict(framework_value["compatibility"], "compatibility"),
            _state_from_dict(framework_value["source"], "source"),
        )
        selection = AlphaSelection(
            repository=value["repository"],
            environment_id=value["environment_id"],
            instance_id=value["instance_id"],
            framework=framework,
            compatibility_object=value["compatibility_object"],
            blueprint_object=value["blueprint_object"],
            source_object=value["source_object"],
            source_sha256=value["source_sha256"],
            source_bytes=value["source_bytes"],
            ref_name=value["ref_name"],
        )
    except (TypeError, ValueError) as error:
        raise AlphaError("alpha.selection_invalid", "environment selection values are invalid") from error
    if selection.repository != state.repository:
        raise AlphaError("alpha.selection_invalid", "selection repository binding does not match State")
    return selection


def run_alpha_framework_adoption(
    runtime_root: str | Path,
    evidence_store: AttemptEvidenceStore,
    environment_checkout: str | Path,
    start: ExecutionStart,
    request: AlphaAdoptionRequest,
    finished_at: Callable[[], str],
) -> AlphaAdoptionExecutionResult:
    """Validate and guardedly adopt a compatible fixture under Instance admission."""

    if not isinstance(request, AlphaAdoptionRequest):
        raise ValueError("request must be AlphaAdoptionRequest")
    if start.starting_state != request.current_selection_state:
        raise ValueError("ExecutionStart does not match the current selection State")
    current = read_alpha_selection(environment_checkout, request.current_selection_state)
    _require_distinct_repositories(
        environment_checkout,
        current.repository,
        request.framework_checkout,
        current.framework.state.repository,
    )
    if (
        start.environment_id != current.environment_id
        or start.instance_id != current.instance_id
        or start.blueprint != current.framework.blueprint
        or start.adapter != current.framework.source
        or start.input_states
        != (
            request.candidate_framework.state,
            request.candidate_framework.blueprint,
            request.candidate_framework.compatibility,
            request.candidate_framework.source,
        )
    ):
        raise ValueError(
            "ExecutionStart does not match the selected Instance, Blueprint, Adapter, "
            "and exact candidate inputs"
        )
    if not callable(finished_at):
        raise ValueError("finished_at must be callable")
    adopted_state: StateRef | None = None
    adopted_selection: AlphaSelection | None = None
    store = _AlphaStore(environment_checkout, current.repository)

    def record(
        status: ExecutionStatus,
        *,
        resulting_state: StateRef | None = None,
        outcome: TerminalOutcome | None = None,
        completed_at: str | None = None,
        artifacts: tuple[StateRef, ...] = (),
    ) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=start.execution_id,
            environment_id=start.environment_id,
            instance_id=start.instance_id,
            objective=start.objective,
            started_at=start.started_at,
            finished_at=finished_at() if completed_at is None else completed_at,
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
        nonlocal adopted_state, adopted_selection
        observed_current = _validate_framework(
            request.framework_checkout,
            current.framework,
        )
        if (
            observed_current.blueprint_object != current.blueprint_object
            or observed_current.compatibility_object != current.compatibility_object
            or observed_current.source_object != current.source_object
            or observed_current.source_sha256 != current.source_sha256
            or observed_current.source_bytes != current.source_bytes
        ):
            raise AlphaError(
                "alpha.selection_invalid",
                "adopted framework objects do not match the exact selection record",
            )
        candidate = _validate_framework(
            request.framework_checkout,
            request.candidate_framework,
            current,
        )
        adopted_selection = AlphaSelection(
            repository=current.repository,
            environment_id=current.environment_id,
            instance_id=current.instance_id,
            framework=candidate.framework,
            compatibility_object=candidate.compatibility_object,
            blueprint_object=candidate.blueprint_object,
            source_object=candidate.source_object,
            source_sha256=candidate.source_sha256,
            source_bytes=candidate.source_bytes,
            ref_name=current.ref_name,
        )
        completion_time = finished_at()
        adopted_state = store.write_selection(
            adopted_selection,
            request.current_selection_state.commit,
            completion_time,
        )
        return record(
            ExecutionStatus.COMPLETED,
            resulting_state=adopted_state,
            completed_at=completion_time,
        )

    def failure(error: Exception) -> ExecutionRecord:
        if adopted_state is not None:
            outcome = TerminalOutcome(
                "alpha.record_failed_after_adoption",
                (
                    "terminal record construction failed after framework selection "
                    f"publication with {type(error).__module__}.{type(error).__qualname__}"
                ),
            )
            artifacts = (adopted_state,)
        else:
            outcome = TerminalOutcome(
                getattr(error, "code", "alpha.adoption_failed"),
                getattr(
                    error,
                    "detail",
                    f"framework adoption raised {type(error).__module__}.{type(error).__qualname__}",
                ),
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
    return AlphaAdoptionExecutionResult(current, adopted_state, adopted_selection, provenance)


def resume_alpha_instance(
    environment_checkout: str | Path,
    selection_state: StateRef,
    framework_checkout: str | Path,
    memory_checkout: str | Path,
    memory_state: StateRef,
    environment_id: str,
    instance_id: str,
    requested_paths: Sequence[str],
) -> AlphaResumeObservation:
    """Load the selected fixture source and exact memory in the calling fresh process."""

    selection = read_alpha_selection(environment_checkout, selection_state)
    _require_identity(environment_id, "environment_id")
    _require_identity(instance_id, "instance_id")
    if selection.environment_id != environment_id or selection.instance_id != instance_id:
        raise AlphaError(
            "alpha.identity_mismatch",
            "selection belongs to another environment or Instance",
        )
    canonical_paths = tuple(requested_paths)
    if canonical_paths != ("progress.md",):
        raise AlphaError(
            "alpha.paths_unsupported",
            "the synthetic resume fixture requires exactly progress.md",
        )
    _require_distinct_repositories(
        environment_checkout,
        selection.repository,
        framework_checkout,
        selection.framework.state.repository,
    )
    _require_distinct_repositories(
        memory_checkout,
        memory_state.repository,
        framework_checkout,
        selection.framework.state.repository,
    )
    observed = _validate_framework(framework_checkout, selection.framework)
    if (
        observed.blueprint_object != selection.blueprint_object
        or observed.compatibility_object != selection.compatibility_object
        or observed.source_object != selection.source_object
        or observed.source_sha256 != selection.source_sha256
        or observed.source_bytes != selection.source_bytes
    ):
        raise AlphaError("alpha.selection_invalid", "selected framework objects do not match record")
    policy = ContextPolicy(
        StateRef(memory_state.repository, memory_state.commit, "memory.json"),
        max_entries=64,
        max_blob_bytes=65_536,
        max_total_blob_bytes=262_144,
    )
    try:
        assembly = assemble_instance_memory_context(
            memory_checkout,
            memory_state,
            environment_id,
            instance_id,
            selection.framework.blueprint,
            canonical_paths,
            policy,
        )
    except MemoryError as error:
        raise AlphaError(error.code, error.detail) from error
    memory = {
        (document.source.path or "").removeprefix("memory/"): document.content
        for document in assembly.documents
    }
    memory_bytes = stable_json_bytes(memory)
    plumbing = GitAttemptStore(framework_checkout, selection.framework.state.repository)
    source = plumbing._git("cat-file", "blob", selection.source_object)
    if source.returncode != 0:
        raise AlphaError("state.object_unavailable", "selected framework source is unavailable")
    fixture_result = _execute_fixture_source(source.stdout, memory)
    return AlphaResumeObservation(
        selection_state=selection_state,
        framework_state=selection.framework.state,
        blueprint=selection.framework.blueprint,
        source_state=selection.framework.source,
        source_object=selection.source_object,
        source_sha256=selection.source_sha256,
        memory_state=memory_state,
        memory_sha256=hashlib.sha256(memory_bytes).hexdigest(),
        requested_paths=canonical_paths,
        fixture_result=MappingProxyType(fixture_result),
    )
