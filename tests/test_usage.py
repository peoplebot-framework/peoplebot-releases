from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from peoplebot.usage import (
    collect_usage,
    load_instance_usage_profile,
    load_usage_collection_config,
    report_run_usage,
)


class UsageCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "rollout.jsonl"
        self.ledger = self.root / "usage" / "ledger.jsonl"
        self.cursor = self.root / "usage" / "cursor.json"
        self.admission = self.root / "usage" / "admission.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _config(self, source_format: str, *, freshness: int = 900):
        path = self.root / "config.json"
        path.write_text(json.dumps({
            "admission_path": str(self.admission),
            "allowance_freshness_seconds": freshness,
            "conservative_on_missing_or_stale": True,
            "cursor_path": str(self.cursor),
            "environment_id": "environment:fixture",
            "format": "peoplebot.usage-collection.v0",
            "instance_id": "instance:fixture",
            "ledger_path": str(self.ledger),
            "session_id": "session-fixture",
            "source_format": source_format,
            "source_id": "codex-session-fixture",
            "source_path": str(self.source),
            "stop_threshold_remaining_percent": 10,
        }), encoding="utf-8")
        return load_usage_collection_config(path)

    def _profile(self, source_format: str = "codex.token_usage_record.v0"):
        self._config(source_format)
        profile_path = self.root / "instance-profile.json"
        profile_path.write_text(json.dumps({
            "active": True,
            "binding_revision": 2,
            "environment_id": "environment:fixture",
            "format": "peoplebot.instance-chat-binding.v0",
            "instance_id": "instance:fixture",
            "session": {"session_id": "session-fixture"},
            "usage_reporting": {
                "active": True,
                "collection_config": "config.json",
                "collector_entry_point": "python -m peoplebot usage-report-run",
                "run_records_path": str(self.root / "runs"),
                "standing_rule": "Each agent Instance saves its available usage from its configured environment/session for later analysis. Missing associations are recorded as unknown and do not block work. Environment, Instance and session are separate identities; task and launcher associations are optional metadata.",
                "synchronization": {
                    "mode": "authorized_task_branch_batch",
                    "path_prefix": "operations/private-trials/FIXTURE",
                    "repository": "example/owner",
                },
            },
        }), encoding="utf-8")
        return load_instance_usage_profile(profile_path)

    def test_new_token_event_is_incremental_deduplicated_and_retains_trailing_data(self) -> None:
        first = {
            "timestamp": "2026-09-15T18:00:00Z",
            "type": "token_usage_record",
            "payload": {
                "session_id": "session-fixture",
                "thread_id": "session-fixture",
                "turn_id": "turn-1",
                "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 5, "reasoning_output_tokens": 2, "total_tokens": 15},
                "turn_token_usage": {"input_tokens": 30, "cached_input_tokens": 20, "output_tokens": 7, "reasoning_output_tokens": 3, "total_tokens": 37},
                "thread_token_usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 25, "reasoning_output_tokens": 10, "total_tokens": 125},
            },
        }
        complete = (json.dumps(first) + "\n").encode()
        self.source.write_bytes(complete + b'{"timestamp":"incomplete"')
        config = self._config("codex.token_usage_record.v0")

        collected = collect_usage(config, "2026-09-15T18:00:01Z", phase="entry", execution_id="execution:one", task_id="task:one")
        self.assertEqual(collected["collected"], 1)
        self.assertEqual(collected["incomplete_trailing_bytes"], 25)
        self.assertFalse(collected["admitted"])
        event = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(event["measurement"]["call_increment"]["total_tokens"], 15)
        self.assertEqual(event["measurement"]["session_cumulative"]["total_tokens"], 125)
        self.assertIsNone(event["execution_id"])
        self.assertIsNone(event["task_id"])
        self.assertEqual(collected["collection_context"], {
            "execution_id": "execution:one", "task_id": "task:one"
        })

        repeated = collect_usage(config, "2026-09-15T18:00:02Z", phase="exit")
        self.assertEqual(repeated["collected"], 0)
        self.assertEqual(len(self.ledger.read_text(encoding="utf-8").splitlines()), 1)

    def test_token_count_allowance_above_below_and_stale_threshold(self) -> None:
        config = self._config("codex.token_count.v0", freshness=60)

        def record(timestamp: str, used: int, total: int) -> bytes:
            return (json.dumps({
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 8, "cached_input_tokens": 3, "output_tokens": 2, "total_tokens": 10},
                        "total_token_usage": {"input_tokens": total - 5, "cached_input_tokens": 20, "output_tokens": 5, "total_tokens": total},
                    },
                    "rate_limits": {
                        "limit_id": "codex",
                        "primary": {"used_percent": used, "window_minutes": 10080, "resets_at": 1790013985},
                        "secondary": None,
                    },
                },
            }) + "\n").encode()

        self.source.write_bytes(record("2026-09-15T18:00:00Z", 89, 50))
        above = collect_usage(config, "2026-09-15T18:00:10Z", phase="entry")
        self.assertTrue(above["admitted"])
        self.assertEqual(above["windows"][0]["remaining_percent"], 11)

        with self.source.open("ab") as stream:
            stream.write(record("2026-09-15T18:00:20Z", 90, 60))
        below = collect_usage(config, "2026-09-15T18:00:21Z", phase="entry")
        self.assertFalse(below["admitted"])
        self.assertEqual(below["reason"], "allowance_at_or_below_threshold")
        self.assertEqual(below["collected"], 2)

        stale = collect_usage(config, "2026-09-15T18:02:00Z", phase="entry")
        self.assertFalse(stale["admitted"])
        self.assertEqual(stale["reason"], "allowance_stale")
        self.assertEqual(json.loads(self.admission.read_text(encoding="utf-8"))["reason"], "allowance_stale")

    def test_missing_allowance_is_unknown_and_conservatively_deferred(self) -> None:
        config = self._config("codex.token_count.v0")
        result = collect_usage(config, "2026-09-15T18:00:00Z", phase="entry")
        self.assertFalse(result["admitted"])
        self.assertEqual(result["allowance_status"], "missing")
        self.assertEqual(result["windows"], [])

    def test_profile_operation_records_manual_scheduled_and_late_usage_without_duplication(self) -> None:
        self.source.write_bytes(b"")
        profile = self._profile()
        manual = "execution:manual"
        started = report_run_usage(
            profile, manual, "manual", "start", "2026-09-15T18:00:00Z",
            started_at="2026-09-15T18:00:00Z",
        )
        self.assertEqual(started["usage_finality"], "open")

        def event(timestamp: str, input_tokens: int, output_tokens: int) -> bytes:
            return (json.dumps({
                "timestamp": timestamp,
                "type": "token_usage_record",
                "payload": {
                    "session_id": "session-fixture",
                    "turn_id": "turn-manual",
                    "usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": input_tokens // 2,
                        "output_tokens": output_tokens,
                        "total_tokens": input_tokens + output_tokens,
                    },
                },
            }) + "\n").encode()

        with self.source.open("ab") as stream:
            stream.write(event("2026-09-15T18:00:01Z", 10, 2))
        completed = report_run_usage(
            profile, manual, "manual", "completion", "2026-09-15T18:00:02Z",
            finished_at="2026-09-15T18:00:02Z", outcome="failed",
            provider_turn_ids=("turn-manual",),
        )
        self.assertEqual(completed["outcome"], "failed")
        self.assertEqual(completed["usage"]["tokens"]["total_tokens"], 12)
        self.assertEqual(completed["usage_finality"], "provisional")

        with self.source.open("ab") as stream:
            stream.write(event("2026-09-15T18:00:03Z", 6, 1))
        reconciled = report_run_usage(
            profile, manual, "manual", "recovery", "2026-09-15T18:00:04Z",
            provider_turn_ids=("turn-manual",),
        )
        self.assertEqual(reconciled["usage"]["call_count"], 2)
        self.assertEqual(reconciled["usage"]["tokens"]["total_tokens"], 19)
        self.assertEqual(reconciled["usage_finality"], "provisional_after_reconciliation")
        repeated = report_run_usage(
            profile, manual, "manual", "recovery", "2026-09-15T18:00:05Z",
            provider_turn_ids=("turn-manual",),
        )
        self.assertEqual(repeated["usage"]["call_count"], 2)
        self.assertEqual(repeated["usage"]["tokens"]["total_tokens"], 19)

        scheduled = "execution:scheduled"
        report_run_usage(
            profile, scheduled, "scheduled", "start", "2026-09-15T18:01:00Z",
            started_at="2026-09-15T18:01:00Z",
        )
        stopped = report_run_usage(
            profile, scheduled, "scheduled", "completion", "2026-09-15T18:01:01Z",
            finished_at="2026-09-15T18:01:01Z", outcome="stopped",
        )
        self.assertEqual(stopped["outcome"], "stopped")
        self.assertEqual(stopped["provider_turn_linkage"], "not_supplied_optional")
        self.assertEqual(stopped["reporting_problems"], [])
        self.assertEqual(
            stopped["instance_observation"]["association"],
            "instance_via_explicit_session_binding",
        )
        self.assertEqual(
            stopped["instance_observation"]["call_increment"]["total_tokens"], 7
        )


if __name__ == "__main__":
    unittest.main()
