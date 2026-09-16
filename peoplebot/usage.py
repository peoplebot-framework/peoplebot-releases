"""Incremental collection of sanitized local provider usage metadata."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from ._json import stable_json_bytes
from .execution import _require_text, _utc_timestamp


_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_SOURCE_FORMATS = {"codex.token_usage_record.v0", "codex.token_count.v0"}
_STANDING_RULE = (
    "Each agent Instance saves its available usage from its configured environment/"
    "session for later analysis. Missing associations are recorded as unknown and do "
    "not block work. Environment, Instance and session are separate identities; task "
    "and launcher associations are optional metadata."
)
_RUN_OUTCOMES = {"success", "failed", "stopped", "idle", "unknown"}


@dataclass(frozen=True, slots=True)
class UsageCollectionConfig:
    environment_id: str
    instance_id: str
    session_id: str
    source_id: str
    source_format: str
    source_path: Path
    ledger_path: Path
    cursor_path: Path
    admission_path: Path
    stop_threshold_remaining_percent: int = 10
    allowance_freshness_seconds: int = 900
    conservative_on_missing_or_stale: bool = True

    def __post_init__(self) -> None:
        for name in ("environment_id", "instance_id", "session_id", "source_id"):
            _require_text(getattr(self, name), name)
        if self.source_format not in _SOURCE_FORMATS:
            raise ValueError("source_format is not an observed Codex format")
        for name in ("source_path", "ledger_path", "cursor_path", "admission_path"):
            if not getattr(self, name).is_absolute():
                raise ValueError(f"{name} must be absolute")
        if isinstance(self.stop_threshold_remaining_percent, bool) or not (
            0 <= self.stop_threshold_remaining_percent <= 100
        ):
            raise ValueError("stop threshold must be an integer from 0 through 100")
        if isinstance(self.allowance_freshness_seconds, bool) or not (
            1 <= self.allowance_freshness_seconds <= 86_400
        ):
            raise ValueError("allowance freshness must be 1 through 86400 seconds")
        if not isinstance(self.conservative_on_missing_or_stale, bool):
            raise ValueError("conservative_on_missing_or_stale must be a boolean")


def _strict_mapping(value: object, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{label} fields are invalid")
    return value


def load_usage_collection_config(path: str | Path) -> UsageCollectionConfig:
    value = json.loads(Path(path).read_bytes())
    value = _strict_mapping(
        value,
        {
            "allowance_freshness_seconds",
            "admission_path",
            "conservative_on_missing_or_stale",
            "cursor_path",
            "environment_id",
            "format",
            "instance_id",
            "ledger_path",
            "session_id",
            "source_format",
            "source_id",
            "source_path",
            "stop_threshold_remaining_percent",
        },
        "usage collection configuration",
    )
    if value["format"] != "peoplebot.usage-collection.v0":
        raise ValueError("usage collection configuration format is invalid")
    return UsageCollectionConfig(
        value["environment_id"],
        value["instance_id"],
        value["session_id"],
        value["source_id"],
        value["source_format"],
        Path(value["source_path"]),
        Path(value["ledger_path"]),
        Path(value["cursor_path"]),
        Path(value["admission_path"]),
        value["stop_threshold_remaining_percent"],
        value["allowance_freshness_seconds"],
        value["conservative_on_missing_or_stale"],
    )


def _timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        _utc_timestamp(value, "provider timestamp")
    except ValueError:
        return None
    return value


def _tokens(value: object) -> dict[str, int | None] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, int | None] = {}
    available = False
    for field in _TOKEN_FIELDS:
        amount = value.get(field)
        if amount is None:
            result[field] = None
        elif isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
            result[field] = amount
            available = True
        else:
            return None
    return result if available else None


def _explicit_text(*values: object) -> str | None:
    return next((value for value in values if isinstance(value, str) and value), None)


def _usage_record(
    config: UsageCollectionConfig,
    value: Mapping[str, Any],
    offset: int,
    execution_id: str | None,
    task_id: str | None,
) -> list[dict[str, Any]]:
    timestamp = _timestamp(value.get("timestamp"))
    payload = value.get("payload")
    if value.get("type") != "token_usage_record" or timestamp is None or not isinstance(payload, Mapping):
        return []
    session = _explicit_text(payload.get("session_id"), payload.get("thread_id"))
    if session != config.session_id:
        return []
    call = _tokens(payload.get("usage"))
    turn = _tokens(payload.get("turn_token_usage"))
    thread = _tokens(payload.get("thread_token_usage"))
    if call is None and turn is None and thread is None:
        return []
    return [_measurement(
        config,
        offset,
        timestamp,
        _explicit_text(payload.get("turn_id")),
        _explicit_text(payload.get("execution_id")),
        _explicit_text(payload.get("task_id")),
        call,
        turn,
        thread,
        _explicit_text(payload.get("model"), value.get("model")),
        _explicit_text(payload.get("reasoning_effort"), value.get("reasoning_effort")),
    )]


def _token_count_records(
    config: UsageCollectionConfig,
    value: Mapping[str, Any],
    offset: int,
    execution_id: str | None,
    task_id: str | None,
) -> list[dict[str, Any]]:
    timestamp = _timestamp(value.get("timestamp"))
    payload = value.get("payload")
    if value.get("type") == "event_msg" and isinstance(payload, Mapping) and payload.get("type") == "token_count":
        event = payload
    elif value.get("type") == "token_count" and isinstance(payload, Mapping):
        event = payload
    else:
        return []
    if timestamp is None:
        return []
    info = event.get("info")
    records: list[dict[str, Any]] = []
    call = _tokens(info.get("last_token_usage")) if isinstance(info, Mapping) else None
    cumulative = _tokens(info.get("total_token_usage")) if isinstance(info, Mapping) else None
    if call is not None or cumulative is not None:
        records.append(_measurement(
            config,
            offset,
            timestamp,
            _explicit_text(event.get("turn_id"), info.get("turn_id") if isinstance(info, Mapping) else None),
            _explicit_text(event.get("execution_id"), info.get("execution_id") if isinstance(info, Mapping) else None),
            _explicit_text(event.get("task_id"), info.get("task_id") if isinstance(info, Mapping) else None),
            call,
            None,
            cumulative,
            _explicit_text(event.get("model"), info.get("model") if isinstance(info, Mapping) else None),
            _explicit_text(event.get("reasoning_effort"), info.get("reasoning_effort") if isinstance(info, Mapping) else None),
        ))
    rate_limits = event.get("rate_limits")
    if isinstance(rate_limits, Mapping):
        windows = []
        limit_id = _explicit_text(rate_limits.get("limit_id"), event.get("limit_id"))
        for name in ("primary", "secondary"):
            window = rate_limits.get(name)
            if not isinstance(window, Mapping):
                continue
            used = window.get("used_percent")
            duration = window.get("window_minutes")
            reset = window.get("resets_at")
            if (
                isinstance(used, (int, float)) and not isinstance(used, bool)
                and 0 <= used <= 100
                and isinstance(duration, int) and not isinstance(duration, bool) and duration > 0
                and isinstance(reset, int) and not isinstance(reset, bool) and reset > 0
            ):
                windows.append({
                    "name": name,
                    "limit_id": limit_id,
                    "used_percent": used,
                    "remaining_percent": 100 - used,
                    "window_duration_minutes": duration,
                    "resets_at_unix": reset,
                })
        if windows:
            records.append(_event(
                config,
                offset,
                timestamp,
                "allowance",
                {
                    "scope": "account_allowance_windows",
                    "windows": windows,
                },
                _explicit_text(event.get("execution_id")),
                _explicit_text(event.get("task_id")),
            ))
    return records


def _measurement(
    config: UsageCollectionConfig,
    offset: int,
    timestamp: str,
    turn_id: str | None,
    execution_id: str | None,
    task_id: str | None,
    call: dict[str, int | None] | None,
    turn: dict[str, int | None] | None,
    session: dict[str, int | None] | None,
    model: str | None,
    effort: str | None,
) -> dict[str, Any]:
    return _event(
        config,
        offset,
        timestamp,
        "tokens",
        {
            "call_increment": call,
            "turn_cumulative": turn,
            "session_cumulative": session,
            "turn_id": turn_id,
            "model": model,
            "reasoning_effort": effort,
            "accounting_note": "cached input is within input; reasoning output is not added to total",
        },
        execution_id,
        task_id,
    )


def _event(
    config: UsageCollectionConfig,
    offset: int,
    timestamp: str,
    kind: str,
    measurement: Mapping[str, Any],
    execution_id: str | None,
    task_id: str | None,
) -> dict[str, Any]:
    identity = stable_json_bytes({
        "offset": offset,
        "session_id": config.session_id,
        "source_id": config.source_id,
        "timestamp": timestamp,
        "type": kind,
    })
    return {
        "environment_id": config.environment_id,
        "event_id": hashlib.sha256(identity).hexdigest(),
        "execution_id": execution_id,
        "format": "peoplebot.usage-event.v0",
        "instance_id": config.instance_id,
        "measurement": dict(measurement),
        "provider_timestamp": timestamp,
        "session_id": config.session_id,
        "source": {"format": config.source_format, "id": config.source_id, "scope": "local_structured_provider_record"},
        "task_id": task_id,
        "type": kind,
    }


def _load_cursor(config: UsageCollectionConfig) -> tuple[int, int, str | None]:
    if not config.cursor_path.exists():
        return 0, 0, None
    value = json.loads(config.cursor_path.read_bytes())
    value = _strict_mapping(
        value,
        {"byte_offset", "format", "prefix_bytes", "prefix_sha256", "source_id"},
        "usage cursor",
    )
    if value["format"] != "peoplebot.usage-cursor.v0" or value["source_id"] != config.source_id:
        raise ValueError("usage cursor identity is invalid")
    offset = value["byte_offset"]
    prefix_bytes = value["prefix_bytes"]
    prefix = value["prefix_sha256"]
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("usage cursor offset is invalid")
    if isinstance(prefix_bytes, bool) or not isinstance(prefix_bytes, int) or not (0 <= prefix_bytes <= 4096):
        raise ValueError("usage cursor prefix length is invalid")
    if prefix is not None and (not isinstance(prefix, str) or len(prefix) != 64):
        raise ValueError("usage cursor prefix is invalid")
    return offset, prefix_bytes, prefix


def _existing_event_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    result = set()
    with path.open("rb") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("usage ledger contains malformed JSONL") from None
            if not isinstance(value, Mapping) or value.get("format") != "peoplebot.usage-event.v0":
                raise ValueError("usage ledger contains an invalid event")
            event_id = value.get("event_id")
            if not isinstance(event_id, str):
                raise ValueError("usage ledger event identity is invalid")
            result.add(event_id)
    return result


def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(stable_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _admission(config: UsageCollectionConfig, events: list[Mapping[str, Any]], observed_at: str) -> dict[str, Any]:
    allowance = next((event for event in reversed(events) if event.get("type") == "allowance"), None)
    if allowance is None:
        status = "missing"
        windows: list[Mapping[str, Any]] = []
    else:
        age = (_parse_time(observed_at) - _parse_time(allowance["provider_timestamp"])).total_seconds()
        windows = allowance["measurement"]["windows"]
        status = "stale" if age < 0 or age > config.allowance_freshness_seconds else "current"
    below = [window for window in windows if window["remaining_percent"] <= config.stop_threshold_remaining_percent]
    admitted = status == "current" and not below
    if status in {"missing", "stale"} and not config.conservative_on_missing_or_stale:
        admitted = True
    reason = (
        "allowance_missing" if status == "missing" else
        "allowance_stale" if status == "stale" else
        "allowance_at_or_below_threshold" if below else
        "allowance_above_threshold"
    )
    return {
        "admitted": admitted,
        "allowance_status": status,
        "format": "peoplebot.autonomous-admission.v0",
        "reason": reason,
        "stop_threshold_remaining_percent": config.stop_threshold_remaining_percent,
        "freshness_seconds": config.allowance_freshness_seconds,
        "windows": windows,
    }


def collect_usage(
    config: UsageCollectionConfig,
    observed_at: str,
    *,
    phase: str,
    execution_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    _utc_timestamp(observed_at, "observed_at")
    if phase not in {"entry", "exit", "manual"}:
        raise ValueError("collection phase is invalid")
    for value, label in ((execution_id, "execution_id"), (task_id, "task_id")):
        if value is not None:
            _require_text(value, label)
    offset, prefix_bytes, prior_prefix = _load_cursor(config)
    if not config.source_path.is_file():
        events = _read_ledger(config.ledger_path)
        result = _admission(config, events, observed_at)
        result.update({
            "collected": 0,
            "collection_context": {"execution_id": execution_id, "task_id": task_id},
            "cursor_offset": offset,
            "phase": phase,
            "source_status": "missing",
        })
        _atomic_write(config.admission_path, result)
        return result
    size = config.source_path.stat().st_size
    if size < offset:
        raise ValueError("usage source is shorter than its durable cursor")
    with config.source_path.open("rb") as stream:
        if offset == 0 and prefix_bytes == 0 and size > 0:
            prior_prefix = None
        if prior_prefix is None:
            prefix_bytes = min(4096, size)
        prefix = hashlib.sha256(stream.read(prefix_bytes)).hexdigest()
        if prior_prefix is not None and prefix != prior_prefix:
            raise ValueError("usage source prefix changed after collection")
        stream.seek(offset)
        data = stream.read()
    complete_length = data.rfind(b"\n") + 1
    complete = data[:complete_length]
    existing = _existing_event_ids(config.ledger_path)
    new_events: list[dict[str, Any]] = []
    position = offset
    parser = _usage_record if config.source_format == "codex.token_usage_record.v0" else _token_count_records
    for line in complete.splitlines(keepends=True):
        line_offset = position
        position += len(line)
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, Mapping):
            continue
        for event in parser(config, value, line_offset, execution_id, task_id):
            if event["event_id"] not in existing:
                existing.add(event["event_id"])
                new_events.append(event)
    if new_events:
        config.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with config.ledger_path.open("ab") as stream:
            for event in new_events:
                stream.write(stable_json_bytes(event))
            stream.flush()
            os.fsync(stream.fileno())
    _atomic_write(config.cursor_path, {
        "byte_offset": offset + complete_length,
        "format": "peoplebot.usage-cursor.v0",
        "prefix_bytes": prefix_bytes,
        "prefix_sha256": prefix,
        "source_id": config.source_id,
    })
    events = _read_ledger(config.ledger_path)
    result = _admission(config, events, observed_at)
    result.update({
        "collected": len(new_events),
        "collection_context": {"execution_id": execution_id, "task_id": task_id},
        "cursor_offset": offset + complete_length,
        "incomplete_trailing_bytes": len(data) - complete_length,
        "phase": phase,
        "source_status": "available",
    })
    _atomic_write(config.admission_path, result)
    return result


def _read_ledger(path: Path) -> list[Mapping[str, Any]]:
    if not path.exists():
        return []
    values: list[Mapping[str, Any]] = []
    with path.open("rb") as stream:
        for line in stream:
            value = json.loads(line)
            if isinstance(value, Mapping):
                values.append(value)
    return values


@dataclass(frozen=True, slots=True)
class InstanceUsageProfile:
    environment_id: str
    instance_id: str
    session_id: str
    collection: UsageCollectionConfig
    run_records_path: Path
    synchronization: Mapping[str, str]
    standing_rule: str


def load_instance_usage_profile(path: str | Path) -> InstanceUsageProfile:
    profile_path = Path(path)
    value = json.loads(profile_path.read_bytes())
    if not isinstance(value, Mapping) or value.get("format") != "peoplebot.instance-chat-binding.v0":
        raise ValueError("Instance environment profile format is invalid")
    usage = value.get("usage_reporting")
    usage = _strict_mapping(
        usage,
        {
            "active",
            "collection_config",
            "collector_entry_point",
            "run_records_path",
            "standing_rule",
            "synchronization",
        },
        "Instance usage profile",
    )
    if usage["active"] is not True:
        raise ValueError("Instance usage reporting is not active")
    if usage["standing_rule"] != _STANDING_RULE:
        raise ValueError("Instance usage standing rule is invalid")
    if usage["collector_entry_point"] != "python -m peoplebot usage-report-run":
        raise ValueError("Instance usage collector entry point is invalid")
    collection_path = Path(usage["collection_config"])
    if not collection_path.is_absolute():
        collection_path = profile_path.parent / collection_path
    collection = load_usage_collection_config(collection_path)
    session = value.get("session")
    if not isinstance(session, Mapping):
        raise ValueError("Instance session binding is invalid")
    environment_id = value.get("environment_id")
    instance_id = value.get("instance_id")
    session_id = session.get("session_id")
    if (
        collection.environment_id != environment_id
        or collection.instance_id != instance_id
        or collection.session_id != session_id
    ):
        raise ValueError("usage collection does not match the Instance environment profile")
    records = Path(usage["run_records_path"])
    if not records.is_absolute():
        raise ValueError("run_records_path must be absolute")
    synchronization = _strict_mapping(
        usage["synchronization"],
        {"mode", "path_prefix", "repository"},
        "usage synchronization destination",
    )
    if synchronization["mode"] != "authorized_task_branch_batch":
        raise ValueError("usage synchronization mode is invalid")
    for field in ("path_prefix", "repository"):
        _require_text(synchronization[field], f"usage synchronization {field}")
    return InstanceUsageProfile(
        environment_id,
        instance_id,
        session_id,
        collection,
        records,
        dict(synchronization),
        usage["standing_rule"],
    )


def _run_path(profile: InstanceUsageProfile, run_id: str) -> Path:
    return profile.run_records_path / (hashlib.sha256(run_id.encode("utf-8")).hexdigest() + ".json")


def _aggregate_turns(
    events: list[Mapping[str, Any]], turn_ids: tuple[str, ...]
) -> dict[str, Any]:
    matched = [
        event for event in events
        if event.get("type") == "tokens"
        and isinstance(event.get("measurement"), Mapping)
        and event["measurement"].get("turn_id") in turn_ids
        and isinstance(event["measurement"].get("call_increment"), Mapping)
    ]
    totals: dict[str, int | None] = {}
    for field in _TOKEN_FIELDS:
        amounts = [event["measurement"]["call_increment"].get(field) for event in matched]
        totals[field] = (
            sum(amounts) if amounts and all(isinstance(item, int) and not isinstance(item, bool) for item in amounts)
            else None
        )
    models = {
        event["measurement"].get("model") for event in matched
        if isinstance(event["measurement"].get("model"), str)
    }
    efforts = {
        event["measurement"].get("reasoning_effort") for event in matched
        if isinstance(event["measurement"].get("reasoning_effort"), str)
    }
    return {
        "call_count": len(matched),
        "event_ids": [event["event_id"] for event in matched],
        "model": next(iter(models)) if len(models) == 1 else None,
        "reasoning_effort": next(iter(efforts)) if len(efforts) == 1 else None,
        "tokens": totals,
    }


def _latest_instance_observation(
    profile: InstanceUsageProfile, events: list[Mapping[str, Any]]
) -> dict[str, Any] | None:
    matching = [
        event for event in events
        if event.get("type") == "tokens"
        and event.get("environment_id") == profile.environment_id
        and event.get("instance_id") == profile.instance_id
        and event.get("session_id") == profile.session_id
        and isinstance(event.get("measurement"), Mapping)
    ]
    if not matching:
        return None
    latest = matching[-1]
    measurement = latest["measurement"]
    return {
        "association": "instance_via_explicit_session_binding",
        "call_increment": measurement.get("call_increment"),
        "event_id": latest.get("event_id"),
        "execution_id": latest.get("execution_id"),
        "model": measurement.get("model"),
        "provider_timestamp": latest.get("provider_timestamp"),
        "reasoning_effort": measurement.get("reasoning_effort"),
        "session_cumulative": measurement.get("session_cumulative"),
        "task_id": latest.get("task_id"),
        "turn_cumulative": measurement.get("turn_cumulative"),
        "turn_id": measurement.get("turn_id"),
    }


def report_run_usage(
    profile: InstanceUsageProfile,
    run_id: str,
    trigger: str,
    phase: str,
    observed_at: str,
    *,
    started_at: str | None = None,
    finished_at: str | None = None,
    outcome: str | None = None,
    provider_turn_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    _require_text(run_id, "run_id")
    if trigger not in {"manual", "scheduled"}:
        raise ValueError("run trigger is invalid")
    if phase not in {"start", "completion", "recovery"}:
        raise ValueError("run usage phase is invalid")
    _utc_timestamp(observed_at, "observed_at")
    for timestamp, label in ((started_at, "started_at"), (finished_at, "finished_at")):
        if timestamp is not None:
            _utc_timestamp(timestamp, label)
    if outcome is not None and outcome not in _RUN_OUTCOMES:
        raise ValueError("run outcome is invalid")
    if phase == "completion" and (finished_at is None or outcome is None):
        raise ValueError("completion requires finished_at and outcome")
    if phase == "start" and started_at is None:
        raise ValueError("start requires started_at")
    if len(set(provider_turn_ids)) != len(provider_turn_ids):
        raise ValueError("provider turn IDs must be unique")
    for turn_id in provider_turn_ids:
        _require_text(turn_id, "provider turn ID")

    path = _run_path(profile, run_id)
    prior: Mapping[str, Any] | None = None
    if path.exists():
        loaded = json.loads(path.read_bytes())
        if not isinstance(loaded, Mapping) or loaded.get("run_id") != run_id:
            raise ValueError("run usage record identity is invalid")
        prior = loaded
    elif phase == "completion":
        raise ValueError("completion requires an existing run usage start record")

    collection = collect_usage(
        profile.collection,
        observed_at,
        phase="entry" if phase in {"start", "recovery"} else "exit",
        execution_id=run_id,
    )
    prior_turns = tuple(prior.get("provider_turn_ids", ())) if prior else ()
    turns = tuple(dict.fromkeys((*prior_turns, *provider_turn_ids)))
    ledger_events = _read_ledger(profile.collection.ledger_path)
    aggregate = _aggregate_turns(ledger_events, turns)
    instance_observation = _latest_instance_observation(profile, ledger_events)
    prior_boundary = prior.get("source_boundary") if prior else None
    changed_after_completion = bool(
        prior
        and prior.get("phase") == "completion"
        and isinstance(prior_boundary, Mapping)
        and collection["cursor_offset"] > prior_boundary.get("cursor_offset", 0)
    )
    problems = []
    if collection["source_status"] != "available":
        problems.append("usage_source_missing")
    if phase == "recovery" and prior is None:
        problems.append("run_start_record_missing")
    finality = (
        "open" if phase == "start" else
        "provisional_after_reconciliation" if changed_after_completion else
        "provisional"
    )
    record = {
        "allowance_observation": {
            "scope": "account_allowance_windows",
            "status": collection["allowance_status"],
            "windows": collection["windows"],
        },
        "environment_id": profile.environment_id,
        "finished_at": finished_at if finished_at is not None else (prior.get("finished_at") if prior else None),
        "format": "peoplebot.run-usage.v0",
        "instance_id": profile.instance_id,
        "instance_observation": instance_observation,
        "last_observed_at": observed_at,
        "outcome": outcome if outcome is not None else (prior.get("outcome") if prior else "unknown"),
        "phase": phase,
        "provider_turn_ids": list(turns),
        "provider_turn_linkage": "explicit" if turns else "not_supplied_optional",
        "reporting_problems": problems,
        "run_id": run_id,
        "session_id": profile.session_id,
        "source": {
            "format": profile.collection.source_format,
            "id": profile.collection.source_id,
            "scope": "local_structured_provider_record",
        },
        "source_boundary": {
            "cursor_offset": collection["cursor_offset"],
            "incomplete_trailing_bytes": collection.get("incomplete_trailing_bytes"),
        },
        "standing_rule": profile.standing_rule,
        "started_at": started_at if started_at is not None else (prior.get("started_at") if prior else None),
        "synchronization": {**profile.synchronization, "status": "pending_batch"},
        "trigger": trigger,
        "usage": aggregate,
        "usage_finality": finality,
    }
    _atomic_write(path, record)
    return {
        "format": "peoplebot.run-usage-status.v0",
        "outcome": record["outcome"],
        "phase": phase,
        "instance_observation": instance_observation,
        "provider_turn_linkage": record["provider_turn_linkage"],
        "reporting_problems": problems,
        "run_id": run_id,
        "run_record_path": str(path),
        "source_boundary": record["source_boundary"],
        "synchronization_status": "pending_batch",
        "usage": aggregate,
        "usage_finality": finality,
    }
