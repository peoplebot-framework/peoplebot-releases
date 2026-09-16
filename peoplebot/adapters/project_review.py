"""Bounded supplied-context project-review Blueprint and Codex Adapter v0."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .._json import stable_json_bytes
from ..execution import ExecutionRecord, ExecutionStatus, TerminalOutcome, UsageObservation, _require_text
from ..preparation import ContextAssembly, ContextPolicy, assemble_context
from ..provenance import AttemptEvidenceStore, ExecutionStart, ProvenanceRunResult, run_with_execution_provenance
from ..state import StateRef
from .codex_read_only import (
    UNKNOWN_USAGE,
    AdapterError,
    DirectProcessDisposition,
    EventStreamDiagnostics,
    ProcessRunner,
    WorkspaceCleanupDisposition,
    _OwnedWorkspace,
    _ResponseError,
    _SOURCE_AT_IMPORT_SHA256 as _PROCESS_OWNER_SOURCE_AT_IMPORT_SHA256,
    _create_owned_workspace,
    _freeze_json,
    _invoke_codex_json_transport,
    _run_process,
    _thaw_json,
    _unique_object,
)


BLUEPRINT_FORMAT = "peoplebot.project-review-blueprint.v0"
BLUEPRINT_PATH = "peoplebot/blueprints/project_review/blueprint.json"
CONFIG_PATH = "project_review/adapter.json"
DRIVER_PATH = "project_review.py"
PROCESS_OWNER_PATH = "codex_read_only.py"
CONFORMANCE_SCOPE = "protocol_conformance_only_not_factual_correctness_or_completeness"

try:
    _SOURCE_AT_IMPORT_SHA256: str | None = hashlib.sha256(
        Path(__file__).read_text(encoding="utf-8").encode("utf-8")
    ).hexdigest()
except (OSError, UnicodeError):
    _SOURCE_AT_IMPORT_SHA256 = None


def _json_object(content: bytes, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(content, object_pairs_hook=_unique_object)
    except (_ResponseError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} must be one UTF-8 JSON object without duplicate keys") from error
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _bounded_text(value: object, field: str, maximum: int) -> str:
    _require_text(value, field)  # type: ignore[arg-type]
    assert isinstance(value, str)
    if len(value) > maximum:
        raise _ResponseError(
            "adapter.response_limit_exceeded",
            f"{field} exceeds its configured character limit",
        )
    return value


def _strict_utf8_bytes(value: str, detail: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _ResponseError("adapter.response_invalid_unicode", detail) from error


def _validate_json_strings_utf8(value: object) -> None:
    if isinstance(value, str):
        _strict_utf8_bytes(value, "structured response contains invalid Unicode")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _validate_json_strings_utf8(key)
            _validate_json_strings_utf8(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json_strings_utf8(item)


def _require_exact_json_value(value: object, expected: object, label: str) -> None:
    if isinstance(expected, bool):
        if type(value) is not bool or value != expected:
            raise ValueError(f"{label} does not match project-review v0")
        return
    if isinstance(expected, int):
        if type(value) is not int or value != expected:
            raise ValueError(f"{label} does not match project-review v0")
        return
    if isinstance(expected, str):
        if type(value) is not str or value != expected:
            raise ValueError(f"{label} does not match project-review v0")
        return
    if isinstance(expected, list):
        if type(value) is not list or len(value) != len(expected):
            raise ValueError(f"{label} does not match project-review v0")
        for index, (item, expected_item) in enumerate(zip(value, expected, strict=True)):
            _require_exact_json_value(item, expected_item, f"{label}[{index}]")
        return
    if isinstance(expected, dict):
        if not isinstance(value, Mapping) or set(value) != set(expected):
            raise ValueError(f"{label} does not match project-review v0")
        for key, expected_item in expected.items():
            _require_exact_json_value(value[key], expected_item, f"{label}.{key}")
        return
    raise TypeError("unsupported expected Blueprint value")


@dataclass(frozen=True, slots=True)
class ProjectReviewBlueprint:
    """One validated exact project-review Blueprint State."""

    state: StateRef
    content_sha256: str
    content: Mapping[str, Any]

    @property
    def format(self) -> str:
        return BLUEPRINT_FORMAT

    @property
    def max_findings(self) -> int:
        output = self.content["expected_output"]
        assert isinstance(output, Mapping)
        value = output["max_findings"]
        assert isinstance(value, int)
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": _thaw_json(self.content),
            "content_sha256": self.content_sha256,
            "state": self.state.to_dict(),
        }


def _validate_blueprint(value: Mapping[str, Any]) -> None:
    expected = {
        "agent_type",
        "authority",
        "expected_output",
        "format",
        "memory",
        "purpose",
        "required_inputs",
        "stopping",
        "supported_task",
        "version",
    }
    if set(value) != expected or value.get("format") != BLUEPRINT_FORMAT:
        raise ValueError("Blueprint fields or format do not match project-review v0")
    if value.get("agent_type") != "peoplebot.project-review" or value.get("version") != "0":
        raise ValueError("Blueprint identity does not match project-review v0")
    if value.get("supported_task") != "bounded-supplied-context-project-review":
        raise ValueError("Blueprint task boundary does not match project-review v0")
    _require_text(value.get("purpose"), "Blueprint purpose")  # type: ignore[arg-type]
    _require_exact_json_value(value.get("required_inputs"), [
        "owning_environment_identity",
        "instance_identity",
        "objective",
        "pinned_project_state",
        "explicit_context_selection",
        "context_policy_state",
    ], "Blueprint required inputs")
    _require_exact_json_value(value.get("expected_output"), {
        "citation": "exact-repository-commit-path",
        "format": "peoplebot.project-review-response.v0",
        "insufficient_evidence": True,
        "max_findings": 8,
        "no_findings": True,
    }, "Blueprint output boundary")
    _require_exact_json_value(value.get("authority"), {
        "blueprint_modification": False,
        "external_publication": False,
        "framework_modification": False,
        "messaging": False,
        "project_access": "read-only-supplied-context",
        "scheduling": False,
    }, "Blueprint authority")
    _require_exact_json_value(value.get("stopping"), {
        "model_invocations_max": 1,
        "self_retry": False,
        "stop_on_insufficient_evidence": True,
        "stop_on_no_findings": True,
        "stop_on_runtime_or_validation_failure": True,
    }, "Blueprint stopping behavior")
    _require_exact_json_value(value.get("memory"), {
        "automatic_checkpoint": False,
        "save_authority": "explicit-caller-operation",
    }, "Blueprint memory boundary")


def load_project_review_blueprint(
    framework_checkout: str | Path,
    state: StateRef,
) -> ProjectReviewBlueprint:
    """Load and validate the exact Blueprint blob, not merely its StateRef shape."""

    if not isinstance(state, StateRef) or state.path != BLUEPRINT_PATH:
        raise ValueError(f"Blueprint State must select {BLUEPRINT_PATH}")
    policy = ContextPolicy(state, 1, 16_384, 16_384)
    assembly = assemble_context(
        framework_checkout,
        StateRef(state.repository, state.commit),
        (state.path,),
        policy,
    )
    if len(assembly.documents) != 1 or assembly.documents[0].source != state:
        raise ValueError("Blueprint State did not resolve to one supported exact blob")
    content = assembly.documents[0].to_source_bytes()
    value = _json_object(content, "Blueprint")
    _validate_blueprint(value)
    return ProjectReviewBlueprint(
        state,
        hashlib.sha256(content).hexdigest(),
        _freeze_json(value),
    )


@dataclass(frozen=True, slots=True)
class ProjectReviewConfiguration:
    runtime: str
    runtime_version: str
    model: str
    sandbox: str
    timeout_seconds: int
    max_prompt_bytes: int
    max_stdout_bytes: int
    max_stderr_bytes: int
    max_response_bytes: int
    max_objective_bytes: int
    max_context_entries: int
    max_context_blob_bytes: int
    max_context_total_blob_bytes: int
    max_findings: int
    max_citations_per_finding: int
    max_title_characters: int
    max_explanation_characters: int
    max_suggested_action_characters: int
    max_insufficient_evidence_characters: int
    blueprint_format: str
    response_schema: Mapping[str, Any]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProjectReviewConfiguration:
        if set(value) != {
            "blueprint_format",
            "format",
            "limits",
            "model",
            "response_schema",
            "runtime",
            "runtime_version",
            "sandbox",
            "timeout_seconds",
        } or value.get("format") != "peoplebot.codex-project-review-adapter.v0":
            raise ValueError("Adapter configuration fields or format do not match v0")
        for field in ("runtime", "runtime_version", "model", "sandbox", "blueprint_format"):
            _require_text(value.get(field), field)  # type: ignore[arg-type]
        if value["runtime"] != "codex-cli" or value["sandbox"] != "read-only":
            raise ValueError("project-review v0 requires codex-cli with read-only sandboxing")
        if value["blueprint_format"] != BLUEPRINT_FORMAT:
            raise ValueError("Adapter configuration requires the project-review v0 Blueprint")
        timeout = value["timeout_seconds"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
            raise ValueError("timeout_seconds must be an integer between 1 and 300")
        raw_limits = value["limits"]
        names = {
            "max_citations_per_finding",
            "max_context_blob_bytes",
            "max_context_entries",
            "max_context_total_blob_bytes",
            "max_explanation_characters",
            "max_findings",
            "max_insufficient_evidence_characters",
            "max_objective_bytes",
            "max_prompt_bytes",
            "max_response_bytes",
            "max_stderr_bytes",
            "max_stdout_bytes",
            "max_suggested_action_characters",
            "max_title_characters",
        }
        if not isinstance(raw_limits, Mapping) or set(raw_limits) != names:
            raise ValueError("Adapter limits do not match project-review v0")
        maxima = {
            "max_citations_per_finding": 16,
            "max_context_blob_bytes": 1_048_576,
            "max_context_entries": 256,
            "max_context_total_blob_bytes": 4_194_304,
            "max_explanation_characters": 4_000,
            "max_findings": 32,
            "max_insufficient_evidence_characters": 4_000,
            "max_objective_bytes": 16_384,
            "max_prompt_bytes": 262_144,
            "max_response_bytes": 65_536,
            "max_stderr_bytes": 65_536,
            "max_stdout_bytes": 262_144,
            "max_suggested_action_characters": 2_000,
            "max_title_characters": 512,
        }
        limits: dict[str, int] = {}
        for name, maximum in maxima.items():
            item = raw_limits[name]
            if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")
            limits[name] = item
        if limits["max_findings"] != 8:
            raise ValueError("Adapter and Blueprint finding bounds must agree")
        schema = value["response_schema"]
        if not isinstance(schema, Mapping):
            raise ValueError("response_schema must be an object")
        return cls(
            runtime=value["runtime"],
            runtime_version=value["runtime_version"],
            model=value["model"],
            sandbox=value["sandbox"],
            timeout_seconds=timeout,
            blueprint_format=value["blueprint_format"],
            response_schema=_freeze_json(schema),
            **limits,
        )

    def to_dict(self) -> dict[str, Any]:
        limits = {
            name: getattr(self, name)
            for name in (
                "max_citations_per_finding",
                "max_context_blob_bytes",
                "max_context_entries",
                "max_context_total_blob_bytes",
                "max_explanation_characters",
                "max_findings",
                "max_insufficient_evidence_characters",
                "max_objective_bytes",
                "max_prompt_bytes",
                "max_response_bytes",
                "max_stderr_bytes",
                "max_stdout_bytes",
                "max_suggested_action_characters",
                "max_title_characters",
            )
        }
        return {
            "blueprint_format": self.blueprint_format,
            "format": "peoplebot.codex-project-review-adapter.v0",
            "limits": limits,
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
class _AdoptedProjectReviewAdapter:
    state: StateRef
    configuration: ProjectReviewConfiguration
    configuration_state: StateRef
    configuration_blob_sha256: str
    driver_state: StateRef
    driver_sha256: str
    process_owner_state: StateRef
    process_owner_sha256: str


def _adopt_project_review_adapter(
    framework_checkout: str | Path,
    state: StateRef,
) -> _AdoptedProjectReviewAdapter:
    if not isinstance(state, StateRef) or state.path is None:
        raise ValueError("adapter State must select the versioned adapters directory")
    config_path = f"{state.path}/{CONFIG_PATH}"
    driver_path = f"{state.path}/{DRIVER_PATH}"
    owner_path = f"{state.path}/{PROCESS_OWNER_PATH}"
    config_state = StateRef(state.repository, state.commit, config_path)
    assembly = assemble_context(
        framework_checkout,
        StateRef(state.repository, state.commit),
        (config_path, driver_path, owner_path),
        ContextPolicy(config_state, 3, 262_144, 524_288),
    )
    documents = {item.source.path: item for item in assembly.documents}
    if set(documents) != {config_path, driver_path, owner_path}:
        raise AdapterError("adapter.configuration_unavailable", "project-review Adapter files are unavailable")
    config_bytes = documents[config_path].to_source_bytes()
    try:
        configuration = ProjectReviewConfiguration.from_dict(
            _json_object(config_bytes, "Adapter configuration")
        )
    except ValueError as error:
        raise AdapterError("adapter.configuration_invalid", str(error)) from error

    try:
        current_driver = Path(__file__).read_text(encoding="utf-8").encode("utf-8")
    except (OSError, UnicodeError) as error:
        raise AdapterError(
            "adapter.driver_source_unavailable", "current project-review driver is unavailable"
        ) from error
    pinned_driver = documents[driver_path].to_source_bytes()
    driver_digest = hashlib.sha256(pinned_driver).hexdigest()
    if hashlib.sha256(current_driver).hexdigest() != driver_digest:
        raise AdapterError("adapter.driver_source_mismatch", "current project-review driver differs from pinned State")
    if _SOURCE_AT_IMPORT_SHA256 != driver_digest:
        raise AdapterError("adapter.loaded_source_mismatch", "loaded project-review driver differs from pinned State")

    from . import codex_read_only as process_owner_module

    try:
        current_owner = (
            Path(process_owner_module.__file__).read_text(encoding="utf-8").encode("utf-8")
        )
    except (OSError, UnicodeError) as error:
        raise AdapterError(
            "adapter.process_owner_source_unavailable", "current shared process owner is unavailable"
        ) from error
    pinned_owner = documents[owner_path].to_source_bytes()
    owner_digest = hashlib.sha256(pinned_owner).hexdigest()
    if hashlib.sha256(current_owner).hexdigest() != owner_digest:
        raise AdapterError("adapter.process_owner_source_mismatch", "current shared process owner differs from pinned State")
    if _PROCESS_OWNER_SOURCE_AT_IMPORT_SHA256 != owner_digest:
        raise AdapterError("adapter.loaded_process_owner_mismatch", "loaded shared process owner differs from pinned State")
    return _AdoptedProjectReviewAdapter(
        state,
        configuration,
        config_state,
        hashlib.sha256(config_bytes).hexdigest(),
        StateRef(state.repository, state.commit, driver_path),
        driver_digest,
        StateRef(state.repository, state.commit, owner_path),
        owner_digest,
    )


@dataclass(frozen=True, slots=True)
class ProjectReviewFinding:
    severity: str
    title: str
    explanation: str
    suggested_action: str
    citations: tuple[StateRef, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "citations": [item.to_dict() for item in self.citations],
            "explanation": self.explanation,
            "severity": self.severity,
            "suggested_action": self.suggested_action,
            "title": self.title,
        }


@dataclass(frozen=True, slots=True)
class ProjectReviewResponse:
    outcome: str
    findings: tuple[ProjectReviewFinding, ...]
    insufficient_evidence: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [item.to_dict() for item in self.findings],
            "insufficient_evidence": self.insufficient_evidence,
            "outcome": self.outcome,
        }


def _project_review_response(
    text: str,
    context: ContextAssembly,
    config: ProjectReviewConfiguration,
) -> ProjectReviewResponse:
    response_bytes = _strict_utf8_bytes(
        text,
        "structured response text contains invalid Unicode",
    )
    if len(response_bytes) > config.max_response_bytes:
        raise _ResponseError("adapter.response_limit_exceeded", "structured response exceeds its byte limit")
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
    except _ResponseError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _ResponseError("adapter.response_invalid_json", "structured response is not one JSON object") from error
    _validate_json_strings_utf8(value)
    if not isinstance(value, Mapping) or set(value) != {"findings", "insufficient_evidence", "outcome"}:
        raise _ResponseError("adapter.response_field_invalid", "response fields do not match project-review v0")
    outcome = value["outcome"]
    if not isinstance(outcome, str) or outcome not in {
        "findings",
        "insufficient_evidence",
        "no_findings",
    }:
        raise _ResponseError("adapter.response_field_invalid", "response outcome is unsupported")
    raw_findings = value["findings"]
    if not isinstance(raw_findings, list):
        raise _ResponseError("adapter.response_field_invalid", "findings must be an array")
    if len(raw_findings) > config.max_findings:
        raise _ResponseError("adapter.response_limit_exceeded", "findings exceed the configured bound")
    insufficient = value["insufficient_evidence"]
    if outcome == "findings":
        if not raw_findings or insufficient is not None:
            raise _ResponseError("adapter.response_field_invalid", "findings outcome requires findings and no insufficiency reason")
    elif outcome == "insufficient_evidence":
        if raw_findings or not isinstance(insufficient, str):
            raise _ResponseError("adapter.response_field_invalid", "insufficient-evidence outcome requires no findings and one reason")
        insufficient = _bounded_text(
            insufficient,
            "insufficient_evidence",
            config.max_insufficient_evidence_characters,
        )
    elif raw_findings or insufficient is not None:
        raise _ResponseError(
            "adapter.response_field_invalid",
            "no-findings outcome requires no findings and no insufficiency reason",
        )
    allowed = frozenset(document.source for document in context.documents)
    findings: list[ProjectReviewFinding] = []
    for index, item in enumerate(raw_findings):
        if not isinstance(item, Mapping) or set(item) != {
            "citations", "explanation", "severity", "suggested_action", "title"
        }:
            raise _ResponseError("adapter.response_field_invalid", f"finding {index} fields are invalid")
        severity = item["severity"]
        if not isinstance(severity, str) or severity not in {"low", "medium", "high"}:
            raise _ResponseError("adapter.response_field_invalid", f"finding {index} severity is invalid")
        raw_citations = item["citations"]
        if not isinstance(raw_citations, list):
            raise _ResponseError("adapter.response_field_invalid", f"finding {index} citations must be an array")
        if not 1 <= len(raw_citations) <= config.max_citations_per_finding:
            raise _ResponseError("adapter.response_limit_exceeded", f"finding {index} citations exceed bounds")
        citations: list[StateRef] = []
        for raw_citation in raw_citations:
            if not isinstance(raw_citation, Mapping) or set(raw_citation) != {"repository", "commit", "path"}:
                raise _ResponseError("adapter.response_field_invalid", "citation fields do not match StateRef")
            repository = raw_citation.get("repository")
            if not isinstance(repository, str) or len(repository) > 512:
                raise _ResponseError("adapter.response_field_invalid", "citation repository exceeds its field bound")
            try:
                citation = StateRef(repository, raw_citation.get("commit"), raw_citation.get("path"))
            except (TypeError, ValueError) as error:
                raise _ResponseError("adapter.response_field_invalid", "citation is not a valid exact StateRef") from error
            if citation not in allowed:
                raise _ResponseError("adapter.response_citation_mismatch", "citation is not a supplied context document")
            if citation in citations:
                raise _ResponseError("adapter.response_field_invalid", "citations within one finding must be unique")
            citations.append(citation)
        findings.append(
            ProjectReviewFinding(
                severity,
                _bounded_text(item["title"], f"finding {index} title", config.max_title_characters),
                _bounded_text(item["explanation"], f"finding {index} explanation", config.max_explanation_characters),
                _bounded_text(item["suggested_action"], f"finding {index} suggested_action", config.max_suggested_action_characters),
                tuple(citations),
            )
        )
    return ProjectReviewResponse(outcome, tuple(findings), insufficient)


@dataclass(frozen=True, slots=True)
class ProjectReviewObservation:
    code: str
    detail: str
    runtime: str
    runtime_version: str
    model: str
    blueprint_state: StateRef
    blueprint_sha256: str
    adapter_state: StateRef
    configuration_state: StateRef
    configuration_sha256: str
    configuration_blob_sha256: str
    driver_state: StateRef
    driver_sha256: str
    process_owner_state: StateRef
    process_owner_sha256: str
    context_sha256: str
    objective_sha256: str
    process_started: bool
    direct_process_disposition: DirectProcessDisposition
    workspace_cleanup_disposition: WorkspaceCleanupDisposition
    process_exit_code: int | None
    response: ProjectReviewResponse | None
    response_sha256: str | None
    validated_response_sha256: str | None
    usage: tuple[UsageObservation, ...]
    event_diagnostics: EventStreamDiagnostics | None = None
    workspace_remnant: _OwnedWorkspace | None = None

    @property
    def succeeded(self) -> bool:
        return self.code == "adapter.completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_state": self.adapter_state.to_dict(),
            "blueprint_sha256": self.blueprint_sha256,
            "blueprint_state": self.blueprint_state.to_dict(),
            "code": self.code,
            "configuration_blob_sha256": self.configuration_blob_sha256,
            "configuration_sha256": self.configuration_sha256,
            "configuration_state": self.configuration_state.to_dict(),
            "conformance_scope": CONFORMANCE_SCOPE,
            "context_sha256": self.context_sha256,
            "detail": self.detail,
            "direct_process_disposition": self.direct_process_disposition.value,
            "driver_sha256": self.driver_sha256,
            "driver_state": self.driver_state.to_dict(),
            "executing_code_identity_verified": False,
            "event_diagnostics": (
                self.event_diagnostics.to_dict() if self.event_diagnostics else None
            ),
            "model": self.model,
            "objective_sha256": self.objective_sha256,
            "process_exit_code": self.process_exit_code,
            "process_owner_sha256": self.process_owner_sha256,
            "process_owner_state": self.process_owner_state.to_dict(),
            "process_started": self.process_started,
            "response": self.response.to_dict() if self.response else None,
            "response_sha256": self.response_sha256,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "usage": [item.to_dict() for item in self.usage],
            "validated_response_sha256": self.validated_response_sha256,
            "workspace_cleanup_disposition": self.workspace_cleanup_disposition.value,
        }


@dataclass(frozen=True, slots=True)
class ProjectReviewExecutionResult:
    blueprint: ProjectReviewBlueprint
    context: ContextAssembly
    adapter_observation: ProjectReviewObservation | None
    provenance: ProvenanceRunResult

    @property
    def observation_evidence(self) -> StateRef | None:
        terminal = self.provenance.terminal_evidence
        if terminal is None or self.adapter_observation is None:
            return None
        return StateRef(terminal.state.repository, terminal.state.commit, "adapter-observation.json")


class ProjectReviewAdapter:
    """One bounded Codex review of explicit pinned context."""

    __slots__ = ("_adopted", "_runner", "_workspace_factory", "codex_home", "executable")

    def __init__(
        self,
        framework_checkout: str | Path,
        state: StateRef,
        executable: str | Path,
        codex_home: str | Path,
        *,
        runner: ProcessRunner = _run_process,
        workspace_factory: Callable[[], _OwnedWorkspace] = _create_owned_workspace,
    ) -> None:
        self._adopted = _adopt_project_review_adapter(framework_checkout, state)
        self.executable = Path(executable).resolve(strict=True)
        self.codex_home = Path(codex_home).resolve(strict=True)
        if not self.codex_home.is_dir():
            raise ValueError("codex_home must be an existing directory")
        self._runner = runner
        self._workspace_factory = workspace_factory

    @property
    def state(self) -> StateRef:
        return self._adopted.state

    @property
    def configuration(self) -> ProjectReviewConfiguration:
        return self._adopted.configuration

    def _observation(
        self,
        blueprint: ProjectReviewBlueprint,
        context_digest: str,
        objective_digest: str,
        *,
        code: str,
        detail: str,
        process_started: bool,
        direct_process_disposition: DirectProcessDisposition,
        workspace_cleanup_disposition: WorkspaceCleanupDisposition,
        process_exit_code: int | None,
        response: ProjectReviewResponse | None,
        response_sha256: str | None,
        usage: tuple[UsageObservation, ...],
        event_diagnostics: EventStreamDiagnostics | None = None,
        workspace_remnant: _OwnedWorkspace | None = None,
    ) -> ProjectReviewObservation:
        adopted = self._adopted
        return ProjectReviewObservation(
            code,
            detail,
            adopted.configuration.runtime,
            adopted.configuration.runtime_version,
            adopted.configuration.model,
            blueprint.state,
            blueprint.content_sha256,
            adopted.state,
            adopted.configuration_state,
            adopted.configuration.digest,
            adopted.configuration_blob_sha256,
            adopted.driver_state,
            adopted.driver_sha256,
            adopted.process_owner_state,
            adopted.process_owner_sha256,
            context_digest,
            objective_digest,
            process_started,
            direct_process_disposition,
            workspace_cleanup_disposition,
            process_exit_code,
            response,
            response_sha256,
            hashlib.sha256(stable_json_bytes(response.to_dict())).hexdigest() if response else None,
            usage,
            event_diagnostics,
            workspace_remnant,
        )

    def invoke(
        self,
        objective: str,
        context: ContextAssembly,
        blueprint: ProjectReviewBlueprint,
        *,
        timeout_seconds: int | None = None,
    ) -> ProjectReviewObservation:
        _require_text(objective, "objective")
        config = self.configuration
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= config.timeout_seconds:
                raise ValueError("review timeout must be within the adopted Adapter timeout")
            config = replace(config, timeout_seconds=timeout_seconds)
        objective_bytes = objective.encode("utf-8")
        if len(objective_bytes) > config.max_objective_bytes:
            raise ValueError("objective exceeds the configured byte limit")
        if blueprint.format != config.blueprint_format or blueprint.max_findings != config.max_findings:
            raise ValueError("Blueprint and Adapter configuration are incompatible")
        policy = context.manifest.policy
        if (
            len(context.documents) > config.max_context_entries
            or policy.max_entries > config.max_context_entries
            or policy.max_blob_bytes > config.max_context_blob_bytes
            or policy.max_total_blob_bytes > config.max_context_total_blob_bytes
        ):
            raise ValueError("context policy exceeds the adopted Adapter bounds")
        context_bytes = context.to_json_bytes()
        context_digest = hashlib.sha256(context_bytes).hexdigest()
        objective_digest = hashlib.sha256(objective_bytes).hexdigest()
        request = {
            "adapter_state": self.state.to_dict(),
            "blueprint": blueprint.to_dict(),
            "configuration_sha256": config.digest,
            "context": context.to_dict(),
            "context_sha256": context_digest,
            "format": "peoplebot.project-review-request.v0",
            "instructions": (
                "Use only the supplied pinned context. Do not call tools, modify files, "
                "send messages, publish, schedule work, or perform external actions. Return "
                "one JSON object matching the response schema. Cite only supplied context "
                "documents. If an adequate review identifies no issue within the supplied "
                "scope, return no_findings with no findings and a null insufficient_evidence; "
                "that outcome is not proof of correctness or completeness. If the supplied "
                "evidence is inadequate to complete the requested review, return "
                "insufficient_evidence with no findings and one reason."
            ),
            "objective": objective,
            "objective_sha256": objective_digest,
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
        response_bytes: bytes | None = None
        if transport.response_text is not None:
            try:
                response_bytes = _strict_utf8_bytes(
                    transport.response_text,
                    "structured response text contains invalid Unicode",
                )
            except _ResponseError as error:
                return self._observation(
                    blueprint,
                    context_digest,
                    objective_digest,
                    code=error.code,
                    detail=error.detail,
                    process_started=transport.process_started,
                    direct_process_disposition=transport.direct_process_disposition,
                    workspace_cleanup_disposition=transport.workspace_cleanup_disposition,
                    process_exit_code=transport.process_exit_code,
                    response=None,
                    response_sha256=None,
                    usage=transport.usage,
                    event_diagnostics=transport.event_diagnostics,
                    workspace_remnant=transport.workspace_remnant,
                )
        if not transport.succeeded:
            return self._observation(
                blueprint,
                context_digest,
                objective_digest,
                code=transport.code,
                detail=transport.detail,
                process_started=transport.process_started,
                direct_process_disposition=transport.direct_process_disposition,
                workspace_cleanup_disposition=transport.workspace_cleanup_disposition,
                process_exit_code=transport.process_exit_code,
                response=None,
                response_sha256=(
                    hashlib.sha256(response_bytes).hexdigest()
                    if response_bytes is not None else None
                ),
                usage=transport.usage,
                event_diagnostics=transport.event_diagnostics,
                workspace_remnant=transport.workspace_remnant,
            )
        assert transport.response_text is not None
        assert response_bytes is not None
        response_sha256 = hashlib.sha256(response_bytes).hexdigest()
        try:
            response = _project_review_response(transport.response_text, context, config)
        except (_ResponseError, ValueError) as error:
            code = error.code if isinstance(error, _ResponseError) else "adapter.response_field_invalid"
            detail = error.detail if isinstance(error, _ResponseError) else str(error)
            return self._observation(
                blueprint,
                context_digest,
                objective_digest,
                code=code,
                detail=detail,
                process_started=True,
                direct_process_disposition=transport.direct_process_disposition,
                workspace_cleanup_disposition=transport.workspace_cleanup_disposition,
                process_exit_code=transport.process_exit_code,
                response=None,
                response_sha256=response_sha256,
                usage=transport.usage,
                event_diagnostics=transport.event_diagnostics,
            )
        return self._observation(
            blueprint,
            context_digest,
            objective_digest,
            code="adapter.completed",
            detail="Codex CLI returned one structurally valid bounded project review",
            process_started=True,
            direct_process_disposition=transport.direct_process_disposition,
            workspace_cleanup_disposition=transport.workspace_cleanup_disposition,
            process_exit_code=transport.process_exit_code,
            response=response,
            response_sha256=response_sha256,
            usage=transport.usage,
            event_diagnostics=transport.event_diagnostics,
        )


def run_project_review_execution(
    framework_checkout: str | Path,
    project_checkout: str | Path,
    runtime_root: str | Path,
    store: AttemptEvidenceStore,
    start: ExecutionStart,
    adapter: ProjectReviewAdapter,
    context_paths: Sequence[str],
    context_policy: ContextPolicy,
    finished_at: Callable[[], str],
    *,
    timeout_seconds: int | None = None,
    context_source_state: StateRef | None = None,
) -> ProjectReviewExecutionResult:
    """Validate exact inputs, then run one review through shared admission/provenance."""

    if start.adapter != adapter.state:
        raise ValueError("Execution start must pin the adopted project-review Adapter State")
    if isinstance(context_paths, (str, bytes)) or not context_paths:
        raise ValueError("context_paths must contain at least one explicit project path")
    blueprint = load_project_review_blueprint(framework_checkout, start.blueprint)
    config = adapter.configuration
    if len(start.objective.encode("utf-8")) > config.max_objective_bytes:
        raise ValueError("objective exceeds the adopted Adapter bound")
    context_source = context_source_state or start.starting_state
    if context_source.path is not None:
        raise ValueError("context source must be a repository-level State")
    if (
        context_policy.identity.repository != context_source.repository
        or context_policy.identity.commit != context_source.commit
    ):
        raise ValueError("context policy must be pinned to the exact context source State")
    if (
        context_policy.max_entries > config.max_context_entries
        or context_policy.max_blob_bytes > config.max_context_blob_bytes
        or context_policy.max_total_blob_bytes > config.max_context_total_blob_bytes
    ):
        raise ValueError("context policy exceeds the adopted Adapter bounds")
    policy_context = assemble_context(
        project_checkout,
        context_source,
        (context_policy.identity.path,),
        ContextPolicy(context_policy.identity, 1, 65_536, 65_536),
    )
    if len(policy_context.documents) != 1:
        raise ValueError("context policy State did not resolve to one supported exact blob")
    try:
        loaded_policy = ContextPolicy.from_dict(
            context_policy.identity,
            _json_object(policy_context.documents[0].to_source_bytes(), "context policy"),
        )
    except ValueError as error:
        raise ValueError("context policy State content is invalid") from error
    if loaded_policy != context_policy:
        raise ValueError("supplied context policy does not match its exact State content")
    context = assemble_context(project_checkout, context_source, context_paths, context_policy)
    expected_inputs = (context_policy.identity,) + tuple(item.source for item in context.documents)
    if start.input_states != expected_inputs:
        raise ValueError("Execution start must pin the context policy and selected document States")
    observation: ProjectReviewObservation | None = None

    def record(status: ExecutionStatus, outcome: TerminalOutcome | None) -> ExecutionRecord:
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
            usage=observation.usage if observation is not None else (UNKNOWN_USAGE,),
        )

    def task() -> ExecutionRecord:
        nonlocal observation
        observation = adapter.invoke(
            start.objective, context, blueprint, timeout_seconds=timeout_seconds
        )
        if observation.succeeded:
            return record(ExecutionStatus.NO_CHANGE, None)
        return record(ExecutionStatus.FAILED, TerminalOutcome(observation.code, observation.detail))

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
            raise ValueError("project-review observation is unavailable for terminal evidence")
        return {
            "adapter-observation.json": stable_json_bytes(
                {
                    "adapter_observation": observation.to_dict(),
                    "adapter_state": start.adapter.to_dict(),
                    "blueprint_state": start.blueprint.to_dict(),
                    "context_document_states": [item.source.to_dict() for item in context.documents],
                    "context_policy": context.manifest.policy.to_dict(),
                    "context_source_state": context.manifest.source_state.to_dict(),
                    "execution_id": record.execution_id,
                    "format": "peoplebot.project-review-observation.v0",
                    "objective_sha256": hashlib.sha256(start.objective.encode("utf-8")).hexdigest(),
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
    return ProjectReviewExecutionResult(blueprint, context, observation, provenance)
