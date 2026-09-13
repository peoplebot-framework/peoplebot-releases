"""Bounded Execution provenance records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from ._json import stable_json_bytes
from .state import StateRef


_DECIMAL = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")
_RFC3339_UTC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)


def _require_text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty and have no surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must not contain control characters")


def _utc_timestamp(value: str, field: str) -> datetime:
    _require_text(value, field)
    if not _RFC3339_UTC.fullmatch(value):
        raise ValueError(
            f"{field} must match the v0 RFC 3339 UTC subset "
            "YYYY-MM-DDTHH:MM:SS[.ffffff]Z with 1-6 fractional digits"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError(f"{field} must be a calendar-valid RFC 3339 UTC timestamp") from error
    return parsed


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    NO_CHANGE = "no_change"
    BLOCKED = "blocked"
    FAILED = "failed"


class UsageSource(StrEnum):
    PROVIDER_REPORTED = "provider_reported"
    LOCALLY_CALCULATED = "locally_calculated"
    UNKNOWN = "unknown"


class UsageConfidence(StrEnum):
    EXACT = "exact"
    ESTIMATED = "estimated"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TerminalOutcome:
    code: str
    summary: str

    def __post_init__(self) -> None:
        _require_text(self.code, "terminal outcome code")
        _require_text(self.summary, "terminal outcome summary")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class UsageObservation:
    metric: str
    value: int | str | None
    unit: str
    source: UsageSource
    confidence: UsageConfidence

    def __post_init__(self) -> None:
        _require_text(self.metric, "usage metric")
        _require_text(self.unit, "usage unit")
        if not isinstance(self.source, UsageSource):
            raise ValueError("usage source must be a UsageSource")
        if not isinstance(self.confidence, UsageConfidence):
            raise ValueError("usage confidence must be a UsageConfidence")
        if isinstance(self.value, bool) or not isinstance(self.value, (int, str, type(None))):
            raise ValueError("usage value must be a non-negative integer, decimal string, or null")
        if isinstance(self.value, int) and self.value < 0:
            raise ValueError("usage integer must be non-negative")
        if isinstance(self.value, str) and not _DECIMAL.fullmatch(self.value):
            raise ValueError("usage string must be a non-negative plain decimal")
        is_unknown = self.source is UsageSource.UNKNOWN or self.confidence is UsageConfidence.UNKNOWN
        if is_unknown and not (
            self.source is UsageSource.UNKNOWN
            and self.confidence is UsageConfidence.UNKNOWN
            and self.value is None
        ):
            raise ValueError("unknown usage requires null value, unknown source, and unknown confidence")
        if not is_unknown and self.value is None:
            raise ValueError("observed usage requires a value")

    def to_dict(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence.value,
            "metric": self.metric,
            "source": self.source.value,
            "unit": self.unit,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    execution_id: str
    environment_id: str
    instance_id: str
    objective: str
    started_at: str
    finished_at: str
    starting_state: StateRef
    blueprint: StateRef
    adapter: StateRef
    status: ExecutionStatus
    procedures: tuple[StateRef, ...] = ()
    input_states: tuple[StateRef, ...] = ()
    input_messages: tuple[StateRef, ...] = ()
    resulting_state: StateRef | None = None
    terminal_outcome: TerminalOutcome | None = None
    artifacts: tuple[StateRef, ...] = ()
    usage: tuple[UsageObservation, ...] = ()
    reusable_learning: tuple[StateRef, ...] = ()

    def __post_init__(self) -> None:
        for field, value in (
            ("execution_id", self.execution_id),
            ("environment_id", self.environment_id),
            ("instance_id", self.instance_id),
            ("objective", self.objective),
        ):
            _require_text(value, field)

        for field, value, expected_type in (
            ("starting_state", self.starting_state, StateRef),
            ("blueprint", self.blueprint, StateRef),
            ("adapter", self.adapter, StateRef),
            ("status", self.status, ExecutionStatus),
        ):
            if not isinstance(value, expected_type):
                raise ValueError(f"{field} must be a {expected_type.__name__}")

        if self.resulting_state is not None and not isinstance(self.resulting_state, StateRef):
            raise ValueError("resulting_state must be a StateRef or null")
        if self.terminal_outcome is not None and not isinstance(self.terminal_outcome, TerminalOutcome):
            raise ValueError("terminal_outcome must be a TerminalOutcome or null")

        started = _utc_timestamp(self.started_at, "started_at")
        finished = _utc_timestamp(self.finished_at, "finished_at")
        if finished < started:
            raise ValueError("finished_at must not precede started_at")

        for field, values, expected_type in (
            ("procedures", self.procedures, StateRef),
            ("input_states", self.input_states, StateRef),
            ("input_messages", self.input_messages, StateRef),
            ("artifacts", self.artifacts, StateRef),
            ("usage", self.usage, UsageObservation),
            ("reusable_learning", self.reusable_learning, StateRef),
        ):
            if not isinstance(values, tuple) or not all(isinstance(value, expected_type) for value in values):
                raise ValueError(f"{field} must be a tuple of {expected_type.__name__}")

        has_state = self.resulting_state is not None
        has_terminal = self.terminal_outcome is not None
        if self.status in {ExecutionStatus.COMPLETED, ExecutionStatus.NO_CHANGE}:
            if not has_state or has_terminal:
                raise ValueError("completed/no_change Execution requires resulting State only")
        elif not has_terminal or has_state:
            raise ValueError("blocked/failed Execution requires terminal outcome only")

        if self.status is ExecutionStatus.NO_CHANGE and self.resulting_state != self.starting_state:
            raise ValueError("no_change Execution must retain its starting State")

        if self.status in {ExecutionStatus.BLOCKED, ExecutionStatus.FAILED}:
            partial_states = self._partial_state_artifacts()
            if len(partial_states) > 1:
                raise ValueError(
                    "blocked/failed Execution permits at most one recoverable partial State"
                )

    def _partial_state_artifacts(self) -> tuple[StateRef, ...]:
        return tuple(
            artifact
            for artifact in self.artifacts
            if artifact.repository == self.starting_state.repository and artifact.path is None
        )

    @property
    def partial_state(self) -> StateRef | None:
        """Return the exact recoverable partial commit for a terminal Execution, if any."""

        if self.status not in {ExecutionStatus.BLOCKED, ExecutionStatus.FAILED}:
            return None
        partial_states = self._partial_state_artifacts()
        return partial_states[0] if partial_states else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter.to_dict(),
            "artifacts": [reference.to_dict() for reference in self.artifacts],
            "blueprint": self.blueprint.to_dict(),
            "environment_id": self.environment_id,
            "execution_id": self.execution_id,
            "finished_at": self.finished_at,
            "format": "peoplebot.execution.v0",
            "input_messages": [reference.to_dict() for reference in self.input_messages],
            "input_states": [reference.to_dict() for reference in self.input_states],
            "instance_id": self.instance_id,
            "objective": self.objective,
            "procedures": [reference.to_dict() for reference in self.procedures],
            "resulting_state": self.resulting_state.to_dict() if self.resulting_state else None,
            "reusable_learning": [reference.to_dict() for reference in self.reusable_learning],
            "started_at": self.started_at,
            "status": self.status.value,
            "terminal_outcome": self.terminal_outcome.to_dict() if self.terminal_outcome else None,
            "usage": [observation.to_dict() for observation in self.usage],
        }

    def to_json_bytes(self) -> bytes:
        return stable_json_bytes(self.to_dict())
