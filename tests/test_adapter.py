from __future__ import annotations

import dataclasses
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from peoplebot import (
    CodexReadOnlyAdapter,
    ExecutionStart,
    ExecutionStatus,
    GitAttemptStore,
    ProvenanceError,
    StateRef,
    run_read_only_licensing_execution,
    try_acquire_execution,
)
from peoplebot.adapters.codex_read_only import (
    AdapterError,
    _ABSOLUTE_STDERR_BYTES,
    _ABSOLUTE_STDOUT_BYTES,
    DirectProcessDisposition,
    DirectProcessTimeout,
    ProcessOwnershipUnresolved,
    WorkspaceCleanupDisposition,
    _OwnedWorkspace,
    _run_process,
)


REPOSITORY = "https://github.com/peoplebot-framework/peoplebot"
ENVIRONMENT = "environment:read-only-test"
INSTANCE = "instance:read-only-test"
OBJECTIVE = (
    "Using only the supplied pinned PeopleBot licensing content, identify the "
    "project’s GPL version and explain the ordinary attribution requirement, "
    "citing the supplied repository, full commit, and path."
)


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


class FixtureRunner:
    def __init__(
        self,
        result: subprocess.CompletedProcess[bytes] | BaseException,
        on_exec: object | None = None,
    ) -> None:
        self.result = result
        self.on_exec = on_exec
        self.calls: list[tuple[tuple[str, ...], bytes, dict[str, str], int]] = []

    def __call__(
        self,
        command: tuple[str, ...],
        input_bytes: bytes,
        environment: object,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[bytes]:
        copied_environment = dict(environment)  # type: ignore[arg-type]
        self.calls.append((command, input_bytes, copied_environment, timeout_seconds))
        if command[1:] == ("--version",):
            return subprocess.CompletedProcess(command, 0, b"codex-cli 0.153.4\n", b"")
        if callable(self.on_exec):
            self.on_exec()
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@unittest.skipUnless(os.name == "nt", "read-only Execution uses Windows admission v0")
class ReadOnlyAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.name", "PeopleBot Test")
        git(self.repository, "config", "user.email", "test@example.invalid")
        (self.repository / "LICENSING.md").write_text(
            "PeopleBot is GPL-3.0-only. When covered material is conveyed, preserve "
            "the license, required notices, corresponding source, existing contributor "
            "credit, and origin. No advertising is required.\n",
            encoding="utf-8",
        )
        (self.repository / "blueprint.md").write_text("read-only blueprint\n", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "pinned licensing State")
        self.source_commit = git(self.repository, "rev-parse", "HEAD")

        package_root = Path(__file__).parents[1] / "peoplebot" / "adapters"
        adapter_root = self.repository / "peoplebot" / "adapters"
        config_root = adapter_root / "codex_read_only"
        config_root.mkdir(parents=True)
        (adapter_root / "__init__.py").write_text("adapter package\n", encoding="utf-8")
        (adapter_root / "codex_read_only.py").write_bytes(
            (package_root / "codex_read_only.py").read_bytes()
        )
        (config_root / "adapter.json").write_bytes(
            (package_root / "codex_read_only" / "adapter.json").read_bytes()
        )
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "versioned Adapter State")
        self.adapter_commit = git(self.repository, "rev-parse", "HEAD")
        self.source_state = StateRef(REPOSITORY, self.source_commit)
        self.adapter_state = StateRef(
            REPOSITORY,
            self.adapter_commit,
            "peoplebot/adapters",
        )
        self.runtime_root = self.root / "runtime"
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.executable = self.root / "codex.exe"
        self.executable.write_bytes(b"fixture")
        self.store = GitAttemptStore(self.repository, REPOSITORY)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start(self, execution_id: str) -> ExecutionStart:
        return ExecutionStart(
            execution_id=execution_id,
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective=OBJECTIVE,
            started_at="2026-09-09T12:00:00Z",
            starting_state=self.source_state,
            blueprint=StateRef(REPOSITORY, self.source_commit, "blueprint.md"),
            adapter=self.adapter_state,
            input_states=(StateRef(REPOSITORY, self.source_commit, "LICENSING.md"),),
        )

    def answer(self, **changes: object) -> dict[str, object]:
        value: dict[str, object] = {
            "advertising_required": False,
            "citation": StateRef(
                REPOSITORY,
                self.source_commit,
                "LICENSING.md",
            ).to_dict(),
            "distribution_scope": "distribution_of_covered_material",
            "gpl_version": "GPL-3.0-only",
            "preserve_existing_credit": True,
            "preserve_license": True,
            "preserve_required_notices": True,
            "provide_corresponding_source": True,
        }
        value.update(changes)
        return value

    def events(
        self,
        answer: dict[str, object] | str | None = None,
        *,
        extra_item: str | None = None,
    ) -> bytes:
        events: list[dict[str, object]] = [
            {"type": "thread.started", "thread_id": "fixture-thread"},
            {"type": "turn.started"},
        ]
        if extra_item:
            events.append(
                {
                    "type": "item.completed",
                    "item": {"id": "item-tool", "type": extra_item},
                }
            )
        events.extend(
            [
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item-answer",
                        "type": "agent_message",
                        "text": (
                            answer
                            if isinstance(answer, str)
                            else json.dumps(answer or self.answer(), separators=(",", ":"))
                        ),
                    },
                },
                {
                    "type": "turn.completed",
                    "usage": {
                        "cached_input_tokens": 10,
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "reasoning_output_tokens": 5,
                    },
                },
            ]
        )
        return b"\n".join(
            json.dumps(event, separators=(",", ":")).encode("utf-8") for event in events
        ) + b"\n"

    def adapter(self, runner: FixtureRunner) -> CodexReadOnlyAdapter:
        return CodexReadOnlyAdapter(
            self.repository,
            self.adapter_state,
            self.executable,
            self.codex_home,
            runner=runner,
        )

    def real_event_command(self, events: bytes) -> tuple[str, ...]:
        encoded = base64.b64encode(events).decode("ascii")
        return (
            sys.executable,
            "-c",
            "import base64,sys;sys.stdin.buffer.read();"
            f"sys.stdout.buffer.write(base64.b64decode('{encoded}'))",
        )

    def test_success_binds_context_config_admission_and_terminal_evidence(self) -> None:
        start = self.start("execution:read-only-success")
        admission_codes: list[str] = []

        def observe_admission() -> None:
            contender = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:read-only-contender",
            )
            admission_codes.append(contender.code)
            if contender.acquired:
                contender.admission.release()

        runner = FixtureRunner(
            subprocess.CompletedProcess(("codex",), 0, self.events(), b""),
            observe_admission,
        )
        adapter = self.adapter(runner)
        result = run_read_only_licensing_execution(
            self.repository,
            self.runtime_root,
            self.store,
            start,
            adapter,
            lambda: "2026-09-09T12:00:01Z",
        )

        self.assertEqual(admission_codes, ["instance.already_running"])
        self.assertTrue(result.provenance.terminal_committed)
        self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.NO_CHANGE)
        self.assertEqual(result.provenance.execution_record.resulting_state, self.source_state)
        self.assertEqual(result.adapter_observation.code, "adapter.completed")
        self.assertEqual(result.adapter_observation.answer.citation, start.input_states[0])
        self.assertTrue(result.adapter_observation.answer.answer.startswith("Software-rendered"))
        self.assertEqual(len([call for call in runner.calls if call[0][1] == "exec"]), 1)
        command, prompt_bytes, environment, timeout = runner.calls[-1]
        prompt = json.loads(prompt_bytes)
        self.assertEqual(prompt["objective"], OBJECTIVE)
        self.assertEqual(prompt["context_sha256"], result.adapter_observation.context_sha256)
        self.assertEqual(
            prompt["configuration_sha256"],
            result.adapter_observation.configuration_sha256,
        )
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("UNRELATED_SECRET", environment)
        self.assertEqual(
            set(environment) - {"CODEX_HOME"},
            {name for name in os.environ if name.upper() in {
                "APPDATA", "COMSPEC", "LOCALAPPDATA", "PATH", "PATHEXT",
                "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE", "WINDIR",
            }},
        )
        self.assertEqual(timeout, 120)
        self.assertEqual(
            [item.value for item in result.provenance.execution_record.usage],
            [100, 10, 25, 5],
        )
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:read-only-after",
        )
        self.assertTrue(reacquired.acquired, reacquired.code)
        reacquired.admission.release()

    def test_rejected_attempt_never_invokes_adapter(self) -> None:
        owner = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:read-only-owner",
        )
        self.assertTrue(owner.acquired, owner.code)
        runner = FixtureRunner(AssertionError("model invocation must not run"))
        try:
            result = run_read_only_licensing_execution(
                self.repository,
                self.runtime_root,
                self.store,
                self.start("execution:read-only-rejected"),
                self.adapter(runner),
                lambda: "2026-09-09T12:00:01Z",
            )
        finally:
            owner.admission.release()
        self.assertFalse(result.provenance.task_started)
        self.assertIsNone(result.adapter_observation)
        self.assertEqual(runner.calls, [])

    def test_invalid_response_and_tool_event_are_truthful_failed_executions(self) -> None:
        cases = (
            (
                "wrong-answer",
                self.events(self.answer(gpl_version="GPL-2.0")),
                "adapter.response_field_invalid",
            ),
            ("tool-event", self.events(extra_item="command_execution"), "adapter.restriction_violated"),
        )
        for suffix, stdout, expected_code in cases:
            with self.subTest(case=suffix):
                runner = FixtureRunner(subprocess.CompletedProcess(("codex",), 0, stdout, b""))
                result = run_read_only_licensing_execution(
                    self.repository,
                    self.runtime_root,
                    self.store,
                    self.start(f"execution:read-only-{suffix}"),
                    self.adapter(runner),
                    lambda: "2026-09-09T12:00:01Z",
                )
                self.assertEqual(result.adapter_observation.code, expected_code)
                self.assertEqual(
                    [item.value for item in result.adapter_observation.usage],
                    [100, 10, 25, 5],
                )
                self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.FAILED)
                self.assertEqual(
                    result.provenance.execution_record.terminal_outcome.code,
                    expected_code,
                )
                self.assertTrue(result.provenance.terminal_committed)
                self.assertEqual(len([call for call in runner.calls if call[0][1] == "exec"]), 1)

    def test_timeout_and_output_limit_are_classified_without_retry(self) -> None:
        cases: tuple[tuple[str, subprocess.CompletedProcess[bytes] | BaseException, str], ...] = (
            (
                "timeout",
                DirectProcessTimeout(("codex", "exec"), 120),
                "adapter.timeout",
            ),
            (
                "output-limit",
                subprocess.CompletedProcess(("codex",), 0, b"x" * 65_537, b""),
                "adapter.output_limit_exceeded",
            ),
        )
        for suffix, process_result, expected_code in cases:
            with self.subTest(case=suffix):
                runner = FixtureRunner(process_result)
                result = run_read_only_licensing_execution(
                    self.repository,
                    self.runtime_root,
                    self.store,
                    self.start(f"execution:read-only-{suffix}"),
                    self.adapter(runner),
                    lambda: "2026-09-09T12:00:01Z",
                )
                self.assertEqual(result.adapter_observation.code, expected_code)
                self.assertEqual(
                    result.adapter_observation.direct_process_disposition,
                    DirectProcessDisposition.STOPPED,
                )
                self.assertEqual(len([call for call in runner.calls if call[0][1] == "exec"]), 1)
                self.assertTrue(result.provenance.terminal_committed)

    def test_context_binding_is_checked_before_admission_or_invocation(self) -> None:
        start = self.start("execution:read-only-context-mismatch")
        mismatched = ExecutionStart(
            execution_id=start.execution_id,
            environment_id=start.environment_id,
            instance_id=start.instance_id,
            objective=start.objective,
            started_at=start.started_at,
            starting_state=start.starting_state,
            blueprint=start.blueprint,
            adapter=start.adapter,
            input_states=(),
        )
        runner = FixtureRunner(AssertionError("model invocation must not run"))
        with self.assertRaisesRegex(ValueError, "selected licensing input State"):
            run_read_only_licensing_execution(
                self.repository,
                self.runtime_root,
                self.store,
                mismatched,
                self.adapter(runner),
                lambda: "2026-09-09T12:00:01Z",
            )
        self.assertEqual(runner.calls, [])

    def test_prompt_limit_prevents_process_start(self) -> None:
        config_path = (
            self.repository
            / "peoplebot"
            / "adapters"
            / "codex_read_only"
            / "adapter.json"
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["max_prompt_bytes"] = 1024
        config_path.write_text(json.dumps(config), encoding="utf-8")
        git(self.repository, "add", str(config_path))
        git(self.repository, "commit", "-m", "small prompt fixture")
        adapter_state = StateRef(
            REPOSITORY,
            git(self.repository, "rev-parse", "HEAD"),
            "peoplebot/adapters",
        )
        start = self.start("execution:read-only-input-limit")
        start = ExecutionStart(
            execution_id=start.execution_id,
            environment_id=start.environment_id,
            instance_id=start.instance_id,
            objective=start.objective,
            started_at=start.started_at,
            starting_state=start.starting_state,
            blueprint=start.blueprint,
            adapter=adapter_state,
            input_states=start.input_states,
        )
        runner = FixtureRunner(AssertionError("model invocation must not run"))
        adapter = CodexReadOnlyAdapter(
            self.repository,
            adapter_state,
            self.executable,
            self.codex_home,
            runner=runner,
        )
        result = run_read_only_licensing_execution(
            self.repository,
            self.runtime_root,
            self.store,
            start,
            adapter,
            lambda: "2026-09-09T12:00:01Z",
        )
        self.assertEqual(result.adapter_observation.code, "adapter.input_limit_exceeded")
        self.assertFalse(result.adapter_observation.process_started)
        self.assertEqual(runner.calls, [])
        self.assertTrue(result.provenance.terminal_committed)

    def test_direct_process_capture_has_an_absolute_bound(self) -> None:
        result = _run_process(
            (
                sys.executable,
                "-c",
                f"import sys;sys.stdout.buffer.write(b'x'*{_ABSOLUTE_STDOUT_BYTES + 8192})",
            ),
            b"",
            os.environ,
            15,
        )
        self.assertEqual(len(result.stdout), _ABSOLUTE_STDOUT_BYTES + 1)

    def test_structured_contract_rejects_prose_duplicates_and_size_mismatch(self) -> None:
        valid = self.answer()
        cases: tuple[tuple[str, str, str], ...] = (
            (
                "contradictory-prose",
                json.dumps(
                    {
                        **valid,
                        "answer": (
                            "GPL-3.0-only means you can remove all notices and keep every "
                            "distributed derivative proprietary."
                        ),
                    },
                    separators=(",", ":"),
                ),
                "adapter.response_field_invalid",
            ),
            (
                "duplicate-key",
                '{"gpl_version":"GPL-3.0-only","gpl_version":"GPL-3.0-only"}',
                "adapter.response_invalid_json",
            ),
            (
                "serialized-limit",
                " " * 4097,
                "adapter.response_limit_exceeded",
            ),
            (
                "field-limit",
                json.dumps(
                    {
                        **valid,
                        "citation": {
                            **valid["citation"],  # type: ignore[dict-item]
                            "repository": "r" * 513,
                        },
                    },
                    separators=(",", ":"),
                ),
                "adapter.response_field_invalid",
            ),
        )
        for suffix, answer_text, expected in cases:
            with self.subTest(case=suffix):
                runner = FixtureRunner(
                    subprocess.CompletedProcess(("codex",), 0, self.events(answer_text), b"")
                )
                result = run_read_only_licensing_execution(
                    self.repository,
                    self.runtime_root,
                    self.store,
                    self.start(f"execution:structured-{suffix}"),
                    self.adapter(runner),
                    lambda: "2026-09-09T12:00:01Z",
                )
                self.assertEqual(result.adapter_observation.code, expected)
                self.assertTrue(result.provenance.terminal_committed)

    def test_adopted_configuration_and_nested_schema_cannot_be_replaced(self) -> None:
        adapter = self.adapter(FixtureRunner(AssertionError("must not invoke")))
        replacement = dataclasses.replace(adapter.configuration, model="unrecorded-model")
        with self.assertRaises(AttributeError):
            adapter.configuration = replacement  # type: ignore[misc]
        with self.assertRaises(TypeError):
            adapter.configuration.response_schema["type"] = "array"  # type: ignore[index]
        properties = adapter.configuration.response_schema["properties"]
        with self.assertRaises(TypeError):
            properties["gpl_version"] = {"const": "GPL-2.0"}  # type: ignore[index]
        self.assertEqual(adapter.configuration.model, "gpt-5.6-luna")

    def test_loaded_driver_must_match_pinned_adapter_state(self) -> None:
        driver = self.repository / "peoplebot" / "adapters" / "codex_read_only.py"
        driver.write_text("different driver\n", encoding="utf-8")
        git(self.repository, "add", str(driver))
        git(self.repository, "commit", "-m", "mismatched driver fixture")
        state = StateRef(
            REPOSITORY,
            git(self.repository, "rev-parse", "HEAD"),
            "peoplebot/adapters",
        )
        with self.assertRaisesRegex(AdapterError, "adapter.driver_source_mismatch"):
            CodexReadOnlyAdapter(
                self.repository,
                state,
                self.executable,
                self.codex_home,
                runner=FixtureRunner(AssertionError("must not invoke")),
            )

    def test_source_changed_after_import_cannot_claim_new_driver_identity(self) -> None:
        copy_root = self.root / "stale-import"
        shutil.copytree(Path(__file__).parents[1] / "peoplebot", copy_root / "peoplebot")
        git(copy_root, "init", "-b", "main")
        git(copy_root, "config", "user.name", "PeopleBot Test")
        git(copy_root, "config", "user.email", "test@example.invalid")
        git(copy_root, "add", ".")
        git(copy_root, "commit", "-m", "source before import")
        script = """
import subprocess
import sys
from pathlib import Path

from peoplebot import CodexReadOnlyAdapter, StateRef
from peoplebot.adapters.codex_read_only import AdapterError
import peoplebot.adapters.codex_read_only as driver

source = Path(driver.__file__)
source.write_text(source.read_text(encoding="utf-8") + "\\n# changed after import\\n", encoding="utf-8")
subprocess.run(["git", "add", "peoplebot/adapters/codex_read_only.py"], check=True)
subprocess.run(["git", "commit", "-m", "source changed after import"], check=True)
commit = subprocess.run(
    ["git", "rev-parse", "HEAD"], capture_output=True, check=True, text=True
).stdout.strip()
try:
    CodexReadOnlyAdapter(
        Path.cwd(),
        StateRef("fixture:stale-import", commit, "peoplebot/adapters"),
        Path(sys.executable),
        Path.cwd(),
    )
except AdapterError as error:
    print(error.code)
else:
    raise AssertionError("stale imported code was reported as the newer driver State")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=copy_root,
            capture_output=True,
            check=True,
            encoding="utf-8",
            shell=False,
            timeout=30,
        )
        self.assertEqual(result.stdout.strip().splitlines()[-1], "adapter.loaded_source_mismatch")

    def test_worker_start_failures_are_cleaned_under_public_admission(self) -> None:
        for fail_at in (1, 2):
            with self.subTest(fail_at=fail_at):
                contender_codes: list[str] = []
                processes: list[subprocess.Popen[bytes]] = []
                original_start = threading.Thread.start
                starts = 0

                def popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
                    process = subprocess.Popen(*args, **kwargs)
                    processes.append(process)
                    return process

                def injected_start(worker: threading.Thread) -> None:
                    nonlocal starts
                    if worker.name.startswith("peoplebot-codex-"):
                        starts += 1
                        if starts == fail_at:
                            contender = try_acquire_execution(
                                self.runtime_root,
                                ENVIRONMENT,
                                INSTANCE,
                                f"execution:worker-start-contender-{fail_at}",
                            )
                            contender_codes.append(contender.code)
                            if contender.acquired:
                                contender.admission.release()
                            raise RuntimeError("injected worker startup failure")
                    original_start(worker)

                class Runner(FixtureRunner):
                    def __call__(self, command, input_bytes, environment, timeout_seconds):
                        if command[1:] == ("--version",):
                            return subprocess.CompletedProcess(
                                command, 0, b"codex-cli 0.153.4\n", b""
                            )
                        with mock.patch.object(threading.Thread, "start", injected_start):
                            return _run_process(
                                (sys.executable, "-c", "import time;time.sleep(60)"),
                                input_bytes,
                                environment,
                                timeout_seconds,
                                _popen=popen,
                            )

                result = run_read_only_licensing_execution(
                    self.repository,
                    self.runtime_root,
                    self.store,
                    self.start(f"execution:worker-start-failure-{fail_at}"),
                    self.adapter(Runner(AssertionError("unused"))),
                    lambda: "2026-09-09T12:00:01Z",
                )
                self.assertEqual(contender_codes, ["instance.already_running"])
                self.assertEqual(
                    result.adapter_observation.code,
                    "adapter.process_setup_failed",
                )
                self.assertTrue(result.adapter_observation.direct_process_stopped)
                self.assertTrue(result.provenance.terminal_committed)
                self.assertTrue(processes)
                self.assertTrue(all(process.poll() is not None for process in processes))
                reacquired = try_acquire_execution(
                    self.runtime_root,
                    ENVIRONMENT,
                    INSTANCE,
                    f"execution:worker-start-after-{fail_at}",
                )
                self.assertTrue(reacquired.acquired)
                reacquired.admission.release()

    def test_unresolved_child_survives_workspace_cleanup_failure(self) -> None:
        interrupted = threading.Event()
        contender_done = threading.Event()
        contender_done.set()
        workspace_path = self.root / "unresolved-workspace"

        def workspace_factory() -> _OwnedWorkspace:
            workspace_path.mkdir()

            def fail_cleanup(path: Path) -> None:
                raise PermissionError(f"fixture cannot remove {path.name}")

            return _OwnedWorkspace(workspace_path, fail_cleanup)

        def popen(*args: object, **kwargs: object) -> _InterruptingProcess:
            return _InterruptingProcess(
                subprocess.Popen(*args, **kwargs),
                interrupted,
                contender_done,
                unresolved_once=True,
            )

        class Runner(FixtureRunner):
            def __call__(self, command, input_bytes, environment, timeout_seconds):
                if command[1:] == ("--version",):
                    return subprocess.CompletedProcess(command, 0, b"codex-cli 0.153.4\n", b"")
                return _run_process(
                    DirectProcessLifetimeTests().child_command(),
                    input_bytes,
                    environment,
                    timeout_seconds,
                    _popen=popen,
                )

        adapter = CodexReadOnlyAdapter(
            self.repository,
            self.adapter_state,
            self.executable,
            self.codex_home,
            runner=Runner(AssertionError("unused")),
            workspace_factory=workspace_factory,
        )
        try:
            run_read_only_licensing_execution(
                self.repository,
                self.runtime_root,
                self.store,
                self.start("execution:unresolved-workspace-cleanup"),
                adapter,
                lambda: "2026-09-09T12:00:01Z",
            )
        except ProcessOwnershipUnresolved as error:
            self.assertIsNotNone(error.retained_admission)
            self.assertEqual(len(error.workspaces), 1)
            contender = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:workspace-unresolved-contender",
            )
            self.assertEqual(contender.code, "instance.already_running")
            self.assertTrue(error.recover(5))
            self.assertTrue(any("PermissionError" in item for item in error.secondary_cleanup_failures))
            self.assertEqual(error.recoverable_workspace_remnants, (workspace_path,))
            self.assertEqual(error.release_after_recovery().code, "admission.released")
        else:
            self.fail("expected unresolved process ownership")
        shutil.rmtree(workspace_path)

    def test_retained_workspace_refuses_cleanup_after_path_replacement(self) -> None:
        workspace_path = self.root / "retained-replaced"
        moved_path = self.root / "retained-moved-original"
        workspace_path.mkdir()
        (workspace_path / "owned.txt").write_text("original\n", encoding="utf-8")
        attempts = 0

        def fail_once_then_remove(path: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("fixture retains the original workspace")
            shutil.rmtree(path)

        owner = _OwnedWorkspace(workspace_path, fail_once_then_remove)
        self.assertFalse(owner.cleanup())
        workspace_path.rename(moved_path)
        workspace_path.mkdir()
        replacement = workspace_path / "user-work.txt"
        replacement.write_text("replacement work\n", encoding="utf-8")

        self.assertFalse(owner.cleanup())
        self.assertEqual(attempts, 1)
        self.assertTrue((moved_path / "owned.txt").is_file())
        self.assertEqual(replacement.read_text(encoding="utf-8"), "replacement work\n")
        shutil.rmtree(moved_path)
        shutil.rmtree(workspace_path)

    def test_retained_workspace_refuses_cleanup_after_new_user_work(self) -> None:
        workspace_path = self.root / "retained-user-work"
        workspace_path.mkdir()
        attempts = 0

        def fail_once_then_remove(path: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("fixture retains the original workspace")
            shutil.rmtree(path)

        owner = _OwnedWorkspace(workspace_path, fail_once_then_remove)
        self.assertFalse(owner.cleanup())
        user_work = workspace_path / "user-work.txt"
        user_work.write_text("new work\n", encoding="utf-8")

        self.assertFalse(owner.cleanup())
        self.assertEqual(attempts, 1)
        self.assertEqual(user_work.read_text(encoding="utf-8"), "new work\n")
        shutil.rmtree(workspace_path)

    def test_owned_workspace_removes_untouched_directory_once(self) -> None:
        workspace_path = self.root / "untouched-workspace"
        workspace_path.mkdir()
        owner = _OwnedWorkspace(workspace_path)

        self.assertTrue(owner.cleanup())
        self.assertFalse(workspace_path.exists())
        self.assertTrue(owner.cleanup())

    def test_stopped_child_workspace_failure_reports_remnant_without_holding_admission(self) -> None:
        workspace_path = self.root / "stopped-workspace"

        def workspace_factory() -> _OwnedWorkspace:
            workspace_path.mkdir()

            def fail_cleanup(path: Path) -> None:
                raise PermissionError(f"fixture cannot remove {path.name}")

            return _OwnedWorkspace(workspace_path, fail_cleanup)

        command = self.real_event_command(self.events())

        class Runner(FixtureRunner):
            def __call__(self, runtime_command, input_bytes, environment, timeout_seconds):
                if runtime_command[1:] == ("--version",):
                    return subprocess.CompletedProcess(
                        runtime_command, 0, b"codex-cli 0.153.4\n", b""
                    )
                return _run_process(command, input_bytes, environment, timeout_seconds)

        adapter = CodexReadOnlyAdapter(
            self.repository,
            self.adapter_state,
            self.executable,
            self.codex_home,
            runner=Runner(AssertionError("unused")),
            workspace_factory=workspace_factory,
        )
        result = run_read_only_licensing_execution(
            self.repository,
            self.runtime_root,
            self.store,
            self.start("execution:stopped-workspace-cleanup"),
            adapter,
            lambda: "2026-09-09T12:00:01Z",
        )
        self.assertEqual(result.adapter_observation.code, "adapter.workspace_cleanup_failed")
        self.assertTrue(result.adapter_observation.direct_process_stopped)
        self.assertEqual(
            result.adapter_observation.workspace_cleanup_disposition,
            WorkspaceCleanupDisposition.RECOVERABLE_REMNANT,
        )
        self.assertEqual(result.adapter_observation.workspace_remnant.path, workspace_path)
        expected_usage = [100, 10, 25, 5]
        self.assertEqual(
            [item.value for item in result.adapter_observation.usage],
            expected_usage,
        )
        self.assertEqual(
            [item.value for item in result.provenance.execution_record.usage],
            expected_usage,
        )
        self.assertIsNone(result.adapter_observation.answer)
        terminal_state = result.provenance.terminal_evidence.state
        persisted_execution = self.store.read_evidence(
            StateRef(REPOSITORY, terminal_state.commit, "execution.json")
        )
        persisted_observation = self.store.read_evidence(
            StateRef(REPOSITORY, terminal_state.commit, "adapter-observation.json")
        )
        self.assertEqual(
            [item["value"] for item in persisted_execution["usage"]],
            expected_usage,
        )
        self.assertEqual(
            [
                item["value"]
                for item in persisted_observation["adapter_observation"]["usage"]
            ],
            expected_usage,
        )
        self.assertTrue(result.provenance.terminal_committed)
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:stopped-workspace-after",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()
        shutil.rmtree(workspace_path)

    def test_sanitized_observation_is_retrievable_from_exact_terminal_evidence(self) -> None:
        result = run_read_only_licensing_execution(
            self.repository,
            self.runtime_root,
            self.store,
            self.start("execution:durable-observation"),
            self.adapter(
                FixtureRunner(
                    subprocess.CompletedProcess(("codex",), 0, self.events(), b"")
                )
            ),
            lambda: "2026-09-09T12:00:01Z",
        )
        observation_state = result.observation_evidence
        terminal_commit = result.provenance.terminal_evidence.state.commit
        discovery_ref = result.provenance.terminal_evidence.ref_name
        self.assertIsNotNone(observation_state)
        self.assertEqual(
            set(git(self.repository, "ls-tree", "--name-only", terminal_commit).splitlines()),
            {"adapter-observation.json", "execution.json"},
        )
        git(self.repository, "merge-base", "--is-ancestor", self.adapter_commit, terminal_commit)

        (self.repository / "LICENSING.md").write_text("changed working content\n", encoding="utf-8")
        config = self.repository / "peoplebot" / "adapters" / "codex_read_only" / "adapter.json"
        config.write_text("{}\n", encoding="utf-8")
        git(self.repository, "update-ref", "refs/heads/main", self.source_commit)
        del result

        persisted = self.store.read_evidence(observation_state)
        self.assertEqual(persisted["format"], "peoplebot.adapter-observation.v0")
        self.assertEqual(persisted["adapter_state"]["commit"], self.adapter_commit)
        self.assertEqual(persisted["context_state"]["commit"], self.source_commit)
        self.assertEqual(
            persisted["adapter_observation"]["direct_process_disposition"],
            "confirmed_stopped",
        )
        self.assertNotIn("thread_id", json.dumps(persisted))
        self.assertEqual(git(self.repository, "rev-parse", discovery_ref), terminal_commit)

    def test_required_observation_persistence_failure_is_not_durable_success(self) -> None:
        class FailingTerminalStore(GitAttemptStore):
            def persist_terminal(self, start_evidence, record, companion_artifacts=None):
                self.assert_companion = companion_artifacts
                raise ProvenanceError(
                    "provenance.persistence_failed",
                    "fixture companion persistence failed",
                )

        store = FailingTerminalStore(self.repository, REPOSITORY)
        result = run_read_only_licensing_execution(
            self.repository,
            self.runtime_root,
            store,
            self.start("execution:companion-persistence-failure"),
            self.adapter(
                FixtureRunner(
                    subprocess.CompletedProcess(("codex",), 0, self.events(), b"")
                )
            ),
            lambda: "2026-09-09T12:00:01Z",
        )
        self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.NO_CHANGE)
        self.assertIsNone(result.provenance.terminal_evidence)
        self.assertEqual(
            result.provenance.persistence_failure.code,
            "provenance.persistence_failed",
        )
        self.assertIn("adapter-observation.json", store.assert_companion)

    def test_public_wrapper_holds_admission_until_interrupted_child_is_reaped(self) -> None:
        interrupted = threading.Event()
        contender_done = threading.Event()
        contender_codes: list[str] = []

        def contend() -> None:
            self.assertTrue(interrupted.wait(5))
            attempt = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:interrupt-contender",
            )
            contender_codes.append(attempt.code)
            if attempt.acquired:
                attempt.admission.release()
            contender_done.set()

        contender = threading.Thread(target=contend, name="fixture-contender")
        contender.start()

        def factory(*args: object, **kwargs: object) -> _InterruptingProcess:
            return _InterruptingProcess(
                subprocess.Popen(*args, **kwargs),
                interrupted,
                contender_done,
            )

        class Runner(FixtureRunner):
            def __call__(self, command, input_bytes, environment, timeout_seconds):
                if command[1:] == ("--version",):
                    return subprocess.CompletedProcess(command, 0, b"codex-cli 0.153.4\n", b"")
                return _run_process(
                    DirectProcessLifetimeTests().child_command(),
                    input_bytes,
                    environment,
                    timeout_seconds,
                    _popen=factory,
                )

        with self.assertRaises(KeyboardInterrupt):
            run_read_only_licensing_execution(
                self.repository,
                self.runtime_root,
                self.store,
                self.start("execution:interrupted-child"),
                self.adapter(Runner(AssertionError("unused"))),
                lambda: "2026-09-09T12:00:01Z",
            )
        contender.join(5)
        self.assertEqual(contender_codes, ["instance.already_running"])
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:after-interrupted-child",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()

    def test_unresolved_child_keeps_admission_until_exact_owner_recovers(self) -> None:
        interrupted = threading.Event()
        contender_done = threading.Event()
        contender_done.set()

        def factory(*args: object, **kwargs: object) -> _InterruptingProcess:
            return _InterruptingProcess(
                subprocess.Popen(*args, **kwargs),
                interrupted,
                contender_done,
                unresolved_once=True,
            )

        class Runner(FixtureRunner):
            def __call__(self, command, input_bytes, environment, timeout_seconds):
                if command[1:] == ("--version",):
                    return subprocess.CompletedProcess(command, 0, b"codex-cli 0.153.4\n", b"")
                return _run_process(
                    DirectProcessLifetimeTests().child_command(),
                    input_bytes,
                    environment,
                    timeout_seconds,
                    _popen=factory,
                )

        try:
            run_read_only_licensing_execution(
                self.repository,
                self.runtime_root,
                self.store,
                self.start("execution:unresolved-child"),
                self.adapter(Runner(AssertionError("unused"))),
                lambda: "2026-09-09T12:00:01Z",
            )
        except ProcessOwnershipUnresolved as error:
            contender = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:unresolved-contender",
            )
            self.assertEqual(contender.code, "instance.already_running")
            self.assertTrue(error.recover(5))
            release = error.release_after_recovery()
            self.assertEqual(release.code, "admission.released")
        else:
            self.fail("expected unresolved process ownership")
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:after-recovery",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()


class _InterruptingProcess:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        interrupted: threading.Event,
        contender_done: threading.Event,
        *,
        unresolved_once: bool = False,
        interruption: BaseException | None = None,
    ) -> None:
        self._process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self._interrupted = interrupted
        self._contender_done = contender_done
        self._wait_count = 0
        self._unresolved_once = unresolved_once
        self._interruption = interruption or KeyboardInterrupt()
        self._terminate_failed = False
        self._kill_failed = False

    def poll(self) -> int | None:
        return self._process.poll()

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def wait(self, timeout: float | None = None) -> int:
        self._wait_count += 1
        if self._wait_count == 1:
            self._interrupted.set()
            self._contender_done.wait(5)
            raise self._interruption
        if self._unresolved_once and self._wait_count in {2, 3}:
            raise subprocess.TimeoutExpired(self._process.args, timeout)
        return self._process.wait(timeout=timeout)

    def terminate(self) -> None:
        if self._unresolved_once and not self._terminate_failed:
            self._terminate_failed = True
            raise OSError("fixture termination uncertainty")
        self._process.terminate()

    def kill(self) -> None:
        if self._unresolved_once and not self._kill_failed:
            self._kill_failed = True
            raise OSError("fixture kill uncertainty")
        self._process.kill()


@unittest.skipUnless(os.name == "nt", "native child lifetime fixtures require Windows")
class DirectProcessLifetimeTests(unittest.TestCase):
    def child_command(self) -> tuple[str, ...]:
        return (
            sys.executable,
            "-c",
            "import os,sys,time;os.close(sys.stdout.fileno());"
            "os.close(sys.stderr.fileno());time.sleep(60)",
        )

    def test_deadline_covers_blocked_prompt_delivery_and_reaps_child(self) -> None:
        started = time.monotonic()
        with self.assertRaises(DirectProcessTimeout):
            _run_process(self.child_command(), b"x" * 1_048_576, os.environ, 1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertFalse(
            any(
                thread.name.startswith("peoplebot-codex-") and thread.is_alive()
                for thread in threading.enumerate()
            )
        )

    def test_simultaneous_output_and_error_overflow_is_bounded_and_reaped(self) -> None:
        script = (
            "import sys,threading,time;"
            f"a=threading.Thread(target=lambda:sys.stdout.buffer.write(b'x'*{_ABSOLUTE_STDOUT_BYTES + 8192}));"
            f"b=threading.Thread(target=lambda:sys.stderr.buffer.write(b'y'*{_ABSOLUTE_STDERR_BYTES + 8192}));"
            "a.start();b.start();a.join();b.join();time.sleep(60)"
        )
        result = _run_process((sys.executable, "-c", script), b"", os.environ, 10)
        self.assertLessEqual(len(result.stdout), _ABSOLUTE_STDOUT_BYTES + 1)
        self.assertLessEqual(len(result.stderr), _ABSOLUTE_STDERR_BYTES + 1)
        self.assertTrue(
            len(result.stdout) > _ABSOLUTE_STDOUT_BYTES
            or len(result.stderr) > _ABSOLUTE_STDERR_BYTES
        )

    def test_keyboard_interrupt_stops_child_before_public_admission_release(self) -> None:
        # The full public-wrapper version is exercised below with a real Adapter fixture.
        interrupted = threading.Event()
        contender_done = threading.Event()
        captured: list[_InterruptingProcess] = []

        def factory(*args: object, **kwargs: object) -> _InterruptingProcess:
            process = subprocess.Popen(*args, **kwargs)
            wrapped = _InterruptingProcess(process, interrupted, contender_done)
            captured.append(wrapped)
            return wrapped

        contender_done.set()
        with self.assertRaises(KeyboardInterrupt):
            _run_process(
                self.child_command(),
                b"",
                os.environ,
                10,
                _popen=factory,
            )
        self.assertIsNotNone(captured[0].returncode)

    def test_system_exit_stops_child_and_pipe_workers(self) -> None:
        interrupted = threading.Event()
        contender_done = threading.Event()
        contender_done.set()
        captured: list[_InterruptingProcess] = []

        def factory(*args: object, **kwargs: object) -> _InterruptingProcess:
            wrapped = _InterruptingProcess(
                subprocess.Popen(*args, **kwargs),
                interrupted,
                contender_done,
                interruption=SystemExit(9),
            )
            captured.append(wrapped)
            return wrapped

        with self.assertRaisesRegex(SystemExit, "9"):
            _run_process(
                self.child_command(),
                b"",
                os.environ,
                10,
                _popen=factory,
            )
        self.assertIsNotNone(captured[0].returncode)
        self.assertFalse(
            any(
                thread.name.startswith("peoplebot-codex-") and thread.is_alive()
                for thread in threading.enumerate()
            )
        )

    def test_unresolved_ownership_retains_exact_handle_for_recovery(self) -> None:
        interrupted = threading.Event()
        contender_done = threading.Event()
        contender_done.set()

        def factory(*args: object, **kwargs: object) -> _InterruptingProcess:
            return _InterruptingProcess(
                subprocess.Popen(*args, **kwargs),
                interrupted,
                contender_done,
                unresolved_once=True,
            )

        try:
            _run_process(
                self.child_command(),
                b"",
                os.environ,
                10,
                _popen=factory,
            )
        except ProcessOwnershipUnresolved as error:
            self.assertIsNone(error.owner.process.poll())
            self.assertTrue(error.recover(5))
            self.assertIsNotNone(error.owner.process.poll())
        else:
            self.fail("expected unresolved process ownership")
