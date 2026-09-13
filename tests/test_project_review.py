from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from peoplebot import (
    BLUEPRINT_PATH,
    ContextPolicy,
    ExecutionStart,
    ExecutionStatus,
    GitAttemptStore,
    GitMemoryStore,
    MemoryCheckpointRequest,
    MemoryItem,
    ProjectReviewAdapter,
    ProvenanceError,
    StateRef,
    StateResolutionError,
    read_instance_memory_metadata,
    run_instance_memory_execution,
    run_project_review_execution,
    try_acquire_execution,
)
from peoplebot.adapters.codex_read_only import (
    ProcessOwnershipUnresolved,
    _ProcessCleanupResult,
)


FRAMEWORK_REPOSITORY = "https://github.com/peoplebot-framework/peoplebot-releases"
PROJECT_REPOSITORY = "https://example.test/consumer/project"
ENVIRONMENT = "environment:synthetic-review"
INSTANCE = "instance:synthetic-review"
OBJECTIVE = "Identify concrete correctness risks in the supplied parser implementation."


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        encoding="utf-8",
        shell=False,
        timeout=15,
    )
    return result.stdout.strip()


class FakeRunner:
    def __init__(self, response: str | BaseException, on_exec=None) -> None:
        self.response = response
        self.on_exec = on_exec
        self.calls: list[tuple[tuple[str, ...], bytes, dict[str, str], int]] = []

    def __call__(self, command, input_bytes, environment, timeout_seconds):
        self.calls.append((command, input_bytes, dict(environment), timeout_seconds))
        if command[1:] == ("--version",):
            return subprocess.CompletedProcess(command, 0, b"codex-cli 0.153.4\n", b"")
        if self.on_exec is not None:
            self.on_exec()
        if isinstance(self.response, BaseException):
            raise self.response
        events = [
            {"type": "thread.started", "thread_id": "discarded"},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"id": "answer", "type": "agent_message", "text": self.response},
            },
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 90, "output_tokens": 30, "reasoning_output_tokens": 10},
            },
        ]
        stdout = b"\n".join(
            json.dumps(item, separators=(",", ":")).encode("utf-8") for item in events
        ) + b"\n"
        return subprocess.CompletedProcess(command, 0, stdout, b"")


@unittest.skipUnless(os.name == "nt", "project-review execution uses Windows admission v0")
class ProjectReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.framework = self.root / "framework"
        self.project = self.root / "project"
        self.framework.mkdir()
        self.project.mkdir()
        for repository in (self.framework, self.project):
            git(repository, "init", "-b", "main")
            git(repository, "config", "user.name", "PeopleBot Test")
            git(repository, "config", "user.email", "test@example.invalid")

        package_root = Path(__file__).parents[1] / "peoplebot"
        for relative in (
            "adapters/codex_read_only.py",
            "adapters/project_review.py",
            "adapters/project_review/adapter.json",
            "blueprints/project_review/blueprint.json",
        ):
            destination = self.framework / "peoplebot" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((package_root / relative).read_bytes())
        git(self.framework, "add", ".")
        git(self.framework, "commit", "-m", "reviewed framework distribution")
        self.framework_commit = git(self.framework, "rev-parse", "HEAD")
        self.blueprint_state = StateRef(
            FRAMEWORK_REPOSITORY, self.framework_commit, BLUEPRINT_PATH
        )
        self.adapter_state = StateRef(
            FRAMEWORK_REPOSITORY, self.framework_commit, "peoplebot/adapters"
        )

        (self.project / "src").mkdir()
        (self.project / "src" / "parser.py").write_text(
            "def first(items):\n    return items[0]\n", encoding="utf-8"
        )
        policy_value = {
            "exclusions": [],
            "format": "peoplebot.context-policy.v0",
            "max_blob_bytes": 32768,
            "max_entries": 4,
            "max_total_blob_bytes": 49152,
        }
        (self.project / "review-context-policy.json").write_text(
            json.dumps(policy_value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        git(self.project, "add", ".")
        git(self.project, "commit", "-m", "synthetic project State")
        self.project_commit = git(self.project, "rev-parse", "HEAD")
        self.project_state = StateRef(PROJECT_REPOSITORY, self.project_commit)
        self.policy_state = StateRef(
            PROJECT_REPOSITORY, self.project_commit, "review-context-policy.json"
        )
        self.policy = ContextPolicy.from_dict(self.policy_state, policy_value)
        self.document_state = StateRef(
            PROJECT_REPOSITORY, self.project_commit, "src/parser.py"
        )
        self.runtime_root = self.root / "runtime"
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.executable = self.root / "codex.exe"
        self.executable.write_bytes(b"fake")
        self.store = GitAttemptStore(self.project, PROJECT_REPOSITORY)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def response(self, **changes) -> str:
        finding = {
            "citations": [self.document_state.to_dict()],
            "explanation": "first() indexes without establishing that items is non-empty.",
            "severity": "medium",
            "suggested_action": "Handle an empty sequence explicitly before indexing.",
            "title": "Empty input raises IndexError",
        }
        value = {"findings": [finding], "insufficient_evidence": None, "outcome": "findings"}
        value.update(changes)
        return json.dumps(value, separators=(",", ":"))

    def start(self, execution_id: str, **changes) -> ExecutionStart:
        value = {
            "execution_id": execution_id,
            "environment_id": ENVIRONMENT,
            "instance_id": INSTANCE,
            "objective": OBJECTIVE,
            "started_at": "2026-09-11T12:00:00Z",
            "starting_state": self.project_state,
            "blueprint": self.blueprint_state,
            "adapter": self.adapter_state,
            "input_states": (self.policy_state, self.document_state),
        }
        value.update(changes)
        return ExecutionStart(**value)

    def adapter(self, runner: FakeRunner) -> ProjectReviewAdapter:
        return ProjectReviewAdapter(
            self.framework,
            self.adapter_state,
            self.executable,
            self.codex_home,
            runner=runner,
        )

    def execute(self, execution_id: str, runner: FakeRunner, *, start=None):
        return run_project_review_execution(
            self.framework,
            self.project,
            self.runtime_root,
            self.store,
            start or self.start(execution_id),
            self.adapter(runner),
            ("src/parser.py",),
            self.policy,
            lambda: "2026-09-11T12:00:01Z",
        )

    def test_valid_review_binds_exact_inputs_and_retrievable_evidence(self) -> None:
        contention: list[str] = []

        def contend() -> None:
            attempt = try_acquire_execution(
                self.runtime_root, ENVIRONMENT, INSTANCE, "execution:contender"
            )
            contention.append(attempt.code)

        runner = FakeRunner(self.response(), contend)
        result = self.execute("execution:valid-review", runner)
        self.assertEqual(contention, ["instance.already_running"])
        self.assertTrue(result.provenance.terminal_committed)
        self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.NO_CHANGE)
        self.assertEqual(result.adapter_observation.response.findings[0].citations, (self.document_state,))
        self.assertEqual([item.value for item in result.adapter_observation.usage], [90, 30, 10])
        self.assertEqual(len([call for call in runner.calls if call[0][1] == "exec"]), 1)
        request = json.loads(runner.calls[-1][1])
        self.assertEqual(request["blueprint"]["state"], self.blueprint_state.to_dict())
        self.assertEqual(request["adapter_state"], self.adapter_state.to_dict())
        self.assertEqual(request["context"]["documents"][0]["source"], self.document_state.to_dict())
        persisted = self.store.read_evidence(result.observation_evidence)
        self.assertEqual(persisted["format"], "peoplebot.project-review-observation.v0")
        self.assertEqual(persisted["blueprint_state"], self.blueprint_state.to_dict())
        self.assertEqual(
            persisted["adapter_observation"]["conformance_scope"],
            "protocol_conformance_only_not_factual_correctness_or_completeness",
        )
        self.assertNotIn("thread_id", json.dumps(persisted))

    def test_missing_or_incompatible_blueprint_and_adapter_fail_before_provider(self) -> None:
        blueprint_path = self.framework / BLUEPRINT_PATH
        blueprint_path.write_text("{}\n", encoding="utf-8")
        git(self.framework, "add", BLUEPRINT_PATH)
        git(self.framework, "commit", "-m", "invalid Blueprint fixture")
        invalid_blueprint = StateRef(
            FRAMEWORK_REPOSITORY, git(self.framework, "rev-parse", "HEAD"), BLUEPRINT_PATH
        )
        runner = FakeRunner(AssertionError("provider must not run"))
        with self.assertRaises(StateResolutionError):
            self.execute(
                "execution:missing-blueprint",
                runner,
                start=self.start(
                    "execution:missing-blueprint",
                    blueprint=StateRef(FRAMEWORK_REPOSITORY, "0" * 40, BLUEPRINT_PATH),
                ),
            )
        self.assertEqual(runner.calls, [])
        with self.assertRaisesRegex(ValueError, "Blueprint fields or format"):
            self.execute(
                "execution:invalid-blueprint",
                runner,
                start=self.start("execution:invalid-blueprint", blueprint=invalid_blueprint),
            )
        self.assertEqual(runner.calls, [])
        with self.assertRaisesRegex(ValueError, "pin the adopted project-review Adapter"):
            self.execute(
                "execution:adapter-mismatch",
                runner,
                start=self.start(
                    "execution:adapter-mismatch",
                    adapter=StateRef(FRAMEWORK_REPOSITORY, self.framework_commit, "peoplebot"),
                ),
            )
        self.assertEqual(runner.calls, [])

    def test_invalid_oversized_duplicate_and_bad_citation_responses_are_refused(self) -> None:
        finding = json.loads(self.response())["findings"][0]
        cases = {
            "duplicate": '{"outcome":"insufficient_evidence","findings":[],"findings":[],"insufficient_evidence":"missing behavior"}',
            "oversized": json.dumps(
                {"outcome": "findings", "findings": [finding] * 9, "insufficient_evidence": None},
                separators=(",", ":"),
            ),
            "bad-citation": self.response(
                findings=[
                    {
                        **finding,
                        "citations": [
                            StateRef(PROJECT_REPOSITORY, self.project_commit, "not-supplied.py").to_dict()
                        ],
                    }
                ]
            ),
            "oversized-field": self.response(findings=[{**finding, "title": "x" * 161}]),
            "invalid-type": json.dumps(
                {"outcome": [], "findings": [], "insufficient_evidence": None},
                separators=(",", ":"),
            ),
            "empty-findings": json.dumps(
                {"outcome": "findings", "findings": [], "insufficient_evidence": None},
                separators=(",", ":"),
            ),
            "findings-with-insufficiency": self.response(
                insufficient_evidence="The evidence is inadequate."
            ),
            "insufficient-without-reason": json.dumps(
                {"outcome": "insufficient_evidence", "findings": [], "insufficient_evidence": None},
                separators=(",", ":"),
            ),
            "no-findings-with-finding": json.dumps(
                {"outcome": "no_findings", "findings": [finding], "insufficient_evidence": None},
                separators=(",", ":"),
            ),
            "no-findings-with-insufficiency": json.dumps(
                {"outcome": "no_findings", "findings": [], "insufficient_evidence": "Maybe."},
                separators=(",", ":"),
            ),
        }
        expected = {
            "duplicate": "adapter.response_invalid_json",
            "oversized": "adapter.response_limit_exceeded",
            "bad-citation": "adapter.response_citation_mismatch",
            "oversized-field": "adapter.response_limit_exceeded",
            "invalid-type": "adapter.response_field_invalid",
            "empty-findings": "adapter.response_field_invalid",
            "findings-with-insufficiency": "adapter.response_field_invalid",
            "insufficient-without-reason": "adapter.response_field_invalid",
            "no-findings-with-finding": "adapter.response_field_invalid",
            "no-findings-with-insufficiency": "adapter.response_field_invalid",
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                result = self.execute(f"execution:{name}", FakeRunner(response))
                self.assertEqual(result.adapter_observation.code, expected[name])
                self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.FAILED)
                self.assertEqual([item.value for item in result.adapter_observation.usage], [90, 30, 10])
                self.assertTrue(result.provenance.terminal_committed)

    def test_insufficient_evidence_is_valid_without_findings(self) -> None:
        response = json.dumps(
            {
                "findings": [],
                "insufficient_evidence": "The supplied file does not describe caller behavior.",
                "outcome": "insufficient_evidence",
            },
            separators=(",", ":"),
        )
        result = self.execute("execution:insufficient", FakeRunner(response))
        self.assertTrue(result.adapter_observation.succeeded)
        self.assertEqual(result.adapter_observation.response.findings, ())
        self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.NO_CHANGE)

    def test_no_findings_is_valid_with_adequate_supplied_evidence(self) -> None:
        response = json.dumps(
            {
                "findings": [],
                "insufficient_evidence": None,
                "outcome": "no_findings",
            },
            separators=(",", ":"),
        )
        result = self.execute("execution:no-findings", FakeRunner(response))
        self.assertTrue(result.adapter_observation.succeeded)
        self.assertEqual(result.adapter_observation.response.outcome, "no_findings")
        self.assertEqual(result.adapter_observation.response.findings, ())
        self.assertIsNone(result.adapter_observation.response.insufficient_evidence)
        self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.NO_CHANGE)

    def test_valid_non_ascii_and_emoji_response_text_is_preserved(self) -> None:
        value = json.loads(self.response())
        value["findings"][0]["title"] = "Résumé review 🧪"
        value["findings"][0]["explanation"] = "The café path is valid; the parser risk remains."
        response = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        result = self.execute("execution:valid-unicode", FakeRunner(response))
        self.assertTrue(result.adapter_observation.succeeded)
        finding = result.adapter_observation.response.findings[0]
        self.assertEqual(finding.title, "Résumé review 🧪")
        self.assertEqual(finding.explanation, "The café path is valid; the parser risk remains.")

    def test_invalid_unicode_is_classified_and_preserves_usage(self) -> None:
        escaped = (
            '{"outcome":"insufficient_evidence","findings":[],'
            '"insufficient_evidence":"\\ud800"}'
        )
        parsed_field = self.execute("execution:escaped-surrogate", FakeRunner(escaped))
        self.assertEqual(parsed_field.adapter_observation.code, "adapter.response_invalid_unicode")
        self.assertEqual([item.value for item in parsed_field.adapter_observation.usage], [90, 30, 10])
        self.assertEqual([item.value for item in parsed_field.provenance.execution_record.usage], [90, 30, 10])

        raw_text = self.execute("execution:raw-surrogate", FakeRunner("\ud800"))
        self.assertEqual(raw_text.adapter_observation.code, "adapter.response_invalid_unicode")
        self.assertIsNone(raw_text.adapter_observation.response_sha256)
        self.assertEqual([item.value for item in raw_text.adapter_observation.usage], [90, 30, 10])

    def test_invalid_unicode_usage_survives_terminal_persistence_failure(self) -> None:
        class FailingTerminalStore(GitAttemptStore):
            def persist_terminal(self, start_evidence, record, companion_artifacts=None):
                raise ProvenanceError("provenance.persistence_failed", "synthetic terminal failure")

        self.store = FailingTerminalStore(self.project, PROJECT_REPOSITORY)
        response = (
            '{"outcome":"insufficient_evidence","findings":[],'
            '"insufficient_evidence":"\\ud800"}'
        )
        result = self.execute("execution:unicode-persistence-failure", FakeRunner(response))
        self.assertEqual(result.adapter_observation.code, "adapter.response_invalid_unicode")
        self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.FAILED)
        self.assertEqual([item.value for item in result.adapter_observation.usage], [90, 30, 10])
        self.assertEqual([item.value for item in result.provenance.execution_record.usage], [90, 30, 10])
        self.assertIsNone(result.provenance.terminal_evidence)
        self.assertEqual(result.provenance.persistence_failure.code, "provenance.persistence_failed")

    def test_blueprint_nested_scalar_types_are_exact_before_provider_execution(self) -> None:
        blueprint_path = self.framework / BLUEPRINT_PATH
        original = json.loads(blueprint_path.read_text(encoding="utf-8"))
        cases = (
            (("authority", "external_publication"), 0),
            (("expected_output", "insufficient_evidence"), 1),
            (("expected_output", "max_findings"), 8.0),
            (("memory", "automatic_checkpoint"), 0),
            (("stopping", "model_invocations_max"), True),
            (("stopping", "self_retry"), 0),
        )
        runner = FakeRunner(AssertionError("provider must not run"))
        for index, (path, replacement) in enumerate(cases):
            with self.subTest(path=path, replacement=replacement):
                value = json.loads(json.dumps(original))
                value[path[0]][path[1]] = replacement
                blueprint_path.write_text(
                    json.dumps(value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                git(self.framework, "add", BLUEPRINT_PATH)
                git(self.framework, "commit", "-m", f"invalid Blueprint type {index}")
                state = StateRef(
                    FRAMEWORK_REPOSITORY,
                    git(self.framework, "rev-parse", "HEAD"),
                    BLUEPRINT_PATH,
                )
                with self.assertRaisesRegex(ValueError, "does not match project-review v0"):
                    self.execute(
                        f"execution:invalid-blueprint-type-{index}",
                        runner,
                        start=self.start(
                            f"execution:invalid-blueprint-type-{index}",
                            blueprint=state,
                        ),
                    )
        self.assertEqual(runner.calls, [])

    def test_context_policy_must_match_its_exact_pinned_blob(self) -> None:
        runner = FakeRunner(AssertionError("provider must not run"))
        mismatched = ContextPolicy(self.policy_state, 3, 32768, 49152)
        with self.assertRaisesRegex(ValueError, "does not match its exact State content"):
            run_project_review_execution(
                self.framework,
                self.project,
                self.runtime_root,
                self.store,
                self.start("execution:policy-mismatch"),
                self.adapter(runner),
                ("src/parser.py",),
                mismatched,
                lambda: "2026-09-11T12:00:01Z",
            )
        self.assertEqual(runner.calls, [])

    def test_busy_rejection_occurs_before_provider_execution(self) -> None:
        owner = try_acquire_execution(self.runtime_root, ENVIRONMENT, INSTANCE, "execution:owner")
        self.assertTrue(owner.acquired)
        runner = FakeRunner(AssertionError("provider must not run"))
        try:
            result = self.execute("execution:busy", runner)
        finally:
            owner.admission.release()
        self.assertFalse(result.provenance.task_started)
        self.assertIsNone(result.adapter_observation)
        self.assertEqual(runner.calls, [])

    def test_unresolved_process_retains_admission_through_public_wrapper(self) -> None:
        class FakeOwner:
            def __init__(self) -> None:
                self.secondary_failures = []
                self.resolved = False

            def cleanup(self, timeout_seconds=15):
                self.resolved = True
                return _ProcessCleanupResult(True, True, ())

            def stopped(self):
                return self.resolved

        error = ProcessOwnershipUnresolved(FakeOwner(), KeyboardInterrupt())  # type: ignore[arg-type]
        with self.assertRaises(ProcessOwnershipUnresolved) as raised:
            self.execute("execution:unresolved", FakeRunner(error))
        contender = try_acquire_execution(
            self.runtime_root, ENVIRONMENT, INSTANCE, "execution:while-unresolved"
        )
        self.assertEqual(contender.code, "instance.already_running")
        self.assertTrue(raised.exception.recover())
        self.assertEqual(raised.exception.release_after_recovery().code, "admission.released")

    def test_usage_survives_invalid_response_and_terminal_persistence_failure(self) -> None:
        invalid = self.execute("execution:usage-invalid", FakeRunner("{}"))
        self.assertEqual([item.value for item in invalid.adapter_observation.usage], [90, 30, 10])
        self.assertEqual([item.value for item in invalid.provenance.execution_record.usage], [90, 30, 10])

        class FailingTerminalStore(GitAttemptStore):
            def persist_terminal(self, start_evidence, record, companion_artifacts=None):
                raise ProvenanceError("provenance.persistence_failed", "synthetic terminal failure")

        self.store = FailingTerminalStore(self.project, PROJECT_REPOSITORY)
        failed = self.execute("execution:persistence-failure", FakeRunner(self.response()))
        self.assertIsNotNone(failed.provenance.start_evidence)
        self.assertIsNone(failed.provenance.terminal_evidence)
        self.assertEqual(failed.provenance.execution_record.status, ExecutionStatus.NO_CHANGE)
        self.assertEqual([item.value for item in failed.adapter_observation.usage], [90, 30, 10])
        self.assertEqual(failed.provenance.persistence_failure.code, "provenance.persistence_failed")

    def test_exact_blueprint_binds_explicit_memory_without_framework_ancestry(self) -> None:
        memory_store = GitMemoryStore(self.project, PROJECT_REPOSITORY)
        request = MemoryCheckpointRequest(
            repository=PROJECT_REPOSITORY,
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            blueprint=self.blueprint_state,
            expected_state=self.project_state,
            items=(MemoryItem("review/decision.md", "Review accepted for local follow-up.\n"),),
            saved_at="2026-09-11T12:00:02Z",
            initial=True,
        )
        start = ExecutionStart(
            execution_id="execution:explicit-memory",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Save the explicitly selected review decision.",
            started_at="2026-09-11T12:00:02Z",
            starting_state=self.project_state,
            blueprint=self.blueprint_state,
            adapter=StateRef(FRAMEWORK_REPOSITORY, self.framework_commit, "peoplebot/memory.py"),
        )
        result = run_instance_memory_execution(
            self.runtime_root,
            self.store,
            memory_store,
            start,
            request,
            lambda: "2026-09-11T12:00:03Z",
        )
        metadata = read_instance_memory_metadata(self.project, result.checkpoint.state)
        self.assertEqual(metadata["blueprint"], self.blueprint_state.to_dict())
        parents = git(self.project, "rev-list", "--parents", "-n", "1", result.checkpoint.state.commit).split()
        self.assertEqual(parents[1:], [self.project_commit])
        serialized = json.dumps(metadata)
        self.assertNotIn(str(self.codex_home), serialized)
        self.assertNotIn("credential", serialized.lower())


if __name__ == "__main__":
    unittest.main()
