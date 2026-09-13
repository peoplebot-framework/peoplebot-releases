from __future__ import annotations

import json
import unittest

from peoplebot import (
    ExecutionRecord,
    ExecutionStatus,
    StateRef,
    TerminalOutcome,
    UsageConfidence,
    UsageObservation,
    UsageSource,
)


REPOSITORY = "example.test/owner/repo"
START = StateRef(REPOSITORY, "a" * 40)
RESULT = StateRef(REPOSITORY, "b" * 40)
BLUEPRINT = StateRef(REPOSITORY, "c" * 40, "blueprints/base.md")
ADAPTER = StateRef(REPOSITORY, "d" * 40, "adapters/test.json")
PROCEDURE = StateRef(REPOSITORY, "e" * 40, "procedures/resolve.md")


def completed_record(**changes: object) -> ExecutionRecord:
    fields: dict[str, object] = {
        "execution_id": "execution-example-1",
        "environment_id": "environment/example",
        "instance_id": "instance/example",
        "objective": "Prove exact State resolution",
        "started_at": "2026-09-07T12:00:00Z",
        "finished_at": "2026-09-07T12:01:00Z",
        "starting_state": START,
        "blueprint": BLUEPRINT,
        "adapter": ADAPTER,
        "status": ExecutionStatus.COMPLETED,
        "procedures": (PROCEDURE,),
        "resulting_state": RESULT,
        "usage": (
            UsageObservation(
                "model_tokens",
                None,
                "tokens",
                UsageSource.UNKNOWN,
                UsageConfidence.UNKNOWN,
            ),
        ),
    }
    fields.update(changes)
    return ExecutionRecord(**fields)  # type: ignore[arg-type]


class ExecutionRecordTests(unittest.TestCase):
    def test_serialization_is_stable_and_complete(self) -> None:
        record = completed_record()
        first = record.to_json_bytes()
        second = record.to_json_bytes()
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(b"\n"))

        decoded = json.loads(first)
        self.assertEqual("peoplebot.execution.v0", decoded["format"])
        self.assertEqual(PROCEDURE.commit, decoded["procedures"][0]["commit"])
        self.assertEqual("unknown", decoded["usage"][0]["source"])
        self.assertIsNone(decoded["terminal_outcome"])

    def test_blocked_execution_requires_terminal_outcome(self) -> None:
        record = completed_record(
            status=ExecutionStatus.BLOCKED,
            resulting_state=None,
            terminal_outcome=TerminalOutcome("access.denied", "Repository read was denied"),
        )
        self.assertEqual("access.denied", record.to_dict()["terminal_outcome"]["code"])

        with self.assertRaisesRegex(ValueError, "terminal outcome"):
            completed_record(status=ExecutionStatus.BLOCKED, resulting_state=None)

    def test_no_change_retains_starting_state(self) -> None:
        record = completed_record(status=ExecutionStatus.NO_CHANGE, resulting_state=START)
        self.assertEqual(record.starting_state, record.resulting_state)
        with self.assertRaisesRegex(ValueError, "retain its starting State"):
            completed_record(status=ExecutionStatus.NO_CHANGE)

    def test_usage_never_fabricates_unknown_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown usage"):
            UsageObservation(
                "model_tokens",
                10,
                "tokens",
                UsageSource.UNKNOWN,
                UsageConfidence.UNKNOWN,
            )

        observation = UsageObservation(
            "model_tokens",
            10,
            "tokens",
            UsageSource.PROVIDER_REPORTED,
            UsageConfidence.EXACT,
        )
        self.assertEqual(10, observation.to_dict()["value"])

    def test_finished_timestamp_cannot_precede_start(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not precede"):
            completed_record(finished_at="2026-09-07T11:59:59Z")

    def test_timestamp_requires_extended_utc_seconds(self) -> None:
        invalid = (
            "20260907T120000Z",
            "2026-W37-1T12:00:00Z",
            "2026-09-07t12:00:00Z",
            "2026-09-07T12:00Z",
            "2026-09-07T12:00:00z",
            "2026-09-07T12:00:00+00:00",
        )
        for timestamp in invalid:
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(
                ValueError,
                "RFC 3339 UTC subset",
            ):
                completed_record(started_at=timestamp)

    def test_timestamp_supports_up_to_six_fractional_digits(self) -> None:
        record = completed_record(
            started_at="2026-09-07T12:00:00.1Z",
            finished_at="2026-09-07T12:00:00.123456Z",
        )
        self.assertEqual("2026-09-07T12:00:00.1Z", record.started_at)

    def test_timestamp_rejects_excess_fractional_precision(self) -> None:
        with self.assertRaisesRegex(ValueError, "RFC 3339 UTC subset"):
            completed_record(started_at="2026-09-07T12:00:00.1234567Z")

    def test_timestamp_rejects_invalid_calendar_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "calendar-valid"):
            completed_record(started_at="2026-02-30T12:00:00Z")

    def test_fractional_timestamp_order_is_exact(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not precede"):
            completed_record(
                started_at="2026-09-07T12:00:00.123456Z",
                finished_at="2026-09-07T12:00:00.123455Z",
            )

    def test_record_rejects_untyped_status(self) -> None:
        with self.assertRaisesRegex(ValueError, "ExecutionStatus"):
            completed_record(status="completed")

    def test_blocked_execution_can_reference_one_recoverable_partial_state(self) -> None:
        partial = StateRef(REPOSITORY, "f" * 40)
        record = completed_record(
            status=ExecutionStatus.BLOCKED,
            resulting_state=None,
            terminal_outcome=TerminalOutcome("dependency.missing", "Dependency is unavailable"),
            artifacts=(partial,),
        )
        self.assertEqual(partial, record.partial_state)
        self.assertEqual(partial.commit, record.to_dict()["artifacts"][0]["commit"])
        self.assertEqual("blocked", record.to_dict()["status"])

    def test_terminal_execution_rejects_ambiguous_partial_states(self) -> None:
        first_partial = StateRef(REPOSITORY, "f" * 40)
        second_partial = StateRef(REPOSITORY, "1" * 40)
        with self.assertRaisesRegex(ValueError, "at most one recoverable partial State"):
            completed_record(
                status=ExecutionStatus.FAILED,
                resulting_state=None,
                terminal_outcome=TerminalOutcome("execution.failed", "Execution failed"),
                artifacts=(first_partial, second_partial),
            )
