from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import peoplebot.alpha as alpha_module
from peoplebot import (
    AlphaAdoptionRequest,
    AlphaError,
    AlphaFramework,
    AlphaSetupRequest,
    ExecutionStart,
    GitAttemptStore,
    GitMemoryStore,
    MemoryCheckpointRequest,
    MemoryItem,
    MemoryRecoveryRequest,
    MemorySynchronizationRequest,
    StateRef,
    SynchronizationDisposition,
    SynchronizationLimits,
    alpha_selection_ref,
    instance_memory_ref,
    read_alpha_selection,
    run_alpha_framework_adoption,
    run_instance_memory_execution,
    run_memory_recovery_execution,
    run_memory_synchronization_execution,
    setup_alpha_environment,
    try_acquire_execution,
)
from peoplebot.adapters.codex_read_only import ProcessOwnershipUnresolved, _run_process
from peoplebot.execution import ExecutionRecord as RealExecutionRecord


ENVIRONMENT_REPOSITORY = "https://example.invalid/synthetic-alpha/environment"
FRAMEWORK_REPOSITORY = "https://example.invalid/synthetic-alpha/framework"
ENVIRONMENT = "environment:synthetic-alpha"
INSTANCE = "instance:synthetic-alpha"


def git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=check,
        encoding="utf-8",
        shell=False,
        timeout=15,
    )
    return result.stdout.strip()


def init_repository(path: Path) -> None:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "PeopleBot Test")
    git(path, "config", "user.email", "test@example.invalid")


def commit_all(repository: Path, message: str, timestamp: str) -> str:
    git(repository, "add", ".")
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
    )
    result = subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", message],
        capture_output=True,
        check=True,
        encoding="utf-8",
        env=environment,
        shell=False,
        timeout=15,
    )
    return git(repository, "rev-parse", "HEAD")


class _InterruptingFixtureProcess:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        interrupted: threading.Event,
        contender_done: threading.Event,
        *,
        unresolved_once: bool = False,
    ) -> None:
        self._process = process
        self.stdin = process.stdin
        self.stdout = process.stdout
        self.stderr = process.stderr
        self._interrupted = interrupted
        self._contender_done = contender_done
        self._unresolved_once = unresolved_once
        self._wait_count = 0
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
            raise KeyboardInterrupt()
        if self._unresolved_once and self._wait_count in {2, 3}:
            raise subprocess.TimeoutExpired(self._process.args, timeout)
        return self._process.wait(timeout=timeout)

    def terminate(self) -> None:
        if self._unresolved_once and not self._terminate_failed:
            self._terminate_failed = True
            raise OSError("injected fixture termination uncertainty")
        self._process.terminate()

    def kill(self) -> None:
        if self._unresolved_once and not self._kill_failed:
            self._kill_failed = True
            raise OSError("injected fixture kill uncertainty")
        self._process.kill()


@unittest.skipUnless(os.name == "nt", "alpha adoption uses Windows admission v0")
class AlphaBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.framework_checkout = self.root / "framework"
        init_repository(self.framework_checkout)
        (self.framework_checkout / "blueprint.json").write_text(
            '{"kind":"synthetic-agent","schema":0}\n', encoding="utf-8"
        )
        (self.framework_checkout / "alpha-interface.json").write_text(
            '{"input":"mapping[str,str]","output":"json-object"}\n', encoding="utf-8"
        )
        (self.framework_checkout / "alpha_runtime.py").write_text(
            "def resume(memory):\n"
            "    return {'fixture': 'A', 'progress': memory['progress.md']}\n",
            encoding="utf-8",
        )
        self.commit_a = commit_all(
            self.framework_checkout,
            "synthetic framework fixture A",
            "2026-09-10T12:00:00Z",
        )
        (self.framework_checkout / "alpha_runtime.py").write_text(
            "def resume(memory):\n"
            "    return {'fixture': 'B', 'progress': memory['progress.md']}\n",
            encoding="utf-8",
        )
        self.commit_b = commit_all(
            self.framework_checkout,
            "synthetic framework fixture B",
            "2026-09-10T12:00:01Z",
        )
        (self.framework_checkout / "alpha_runtime.py").write_text(
            "def resume(memory):\n"
            "    while True:\n"
            "        pass\n",
            encoding="utf-8",
        )
        self.commit_looping = commit_all(
            self.framework_checkout,
            "synthetic looping fixture",
            "2026-09-10T12:00:01.100000Z",
        )
        (self.framework_checkout / "alpha_runtime.py").write_text(
            "def resume(memory):\n"
            "    return {'fixture': 'x' * 300000, 'progress': memory['progress.md']}\n",
            encoding="utf-8",
        )
        self.commit_overflow = commit_all(
            self.framework_checkout,
            "synthetic overflowing fixture",
            "2026-09-10T12:00:01.200000Z",
        )
        (self.framework_checkout / "alpha_runtime.py").write_text(
            "def resume(memory):\n"
            "    return {'fixture': 'B', 'progress': memory['progress.md']}\n",
            encoding="utf-8",
        )
        (self.framework_checkout / "alpha-interface.json").write_text(
            '{"input":"changed","output":"json-object"}\n', encoding="utf-8"
        )
        self.commit_incompatible = commit_all(
            self.framework_checkout,
            "synthetic incompatible fixture",
            "2026-09-10T12:00:02Z",
        )
        (self.framework_checkout / "alpha-interface.json").write_text(
            '{"input":"mapping[str,str]","output":"json-object"}\n', encoding="utf-8"
        )
        (self.framework_checkout / "alpha_runtime.py").write_text(
            "def resume(memory):\n"
            "    return {'fixture': 'broken', 'progress': 'not the input'}\n",
            encoding="utf-8",
        )
        self.commit_broken_behavior = commit_all(
            self.framework_checkout,
            "synthetic behaviorally incompatible fixture",
            "2026-09-10T12:00:03Z",
        )
        self.environment_checkout = self.root / "environment"
        init_repository(self.environment_checkout)
        self.runtime_root = self.root / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def framework(self, commit: str) -> AlphaFramework:
        return AlphaFramework(
            StateRef(FRAMEWORK_REPOSITORY, commit),
            StateRef(FRAMEWORK_REPOSITORY, commit, "blueprint.json"),
            StateRef(FRAMEWORK_REPOSITORY, commit, "alpha-interface.json"),
            StateRef(FRAMEWORK_REPOSITORY, commit, "alpha_runtime.py"),
        )

    def setup(self, checkout: Path | None = None):
        return setup_alpha_environment(
            self.environment_checkout if checkout is None else checkout,
            AlphaSetupRequest(
                repository=ENVIRONMENT_REPOSITORY,
                environment_id=ENVIRONMENT,
                instance_id=INSTANCE,
                framework_checkout=self.framework_checkout,
                framework=self.framework(self.commit_a),
                created_at="2026-09-10T12:01:00Z",
            ),
        )

    def start(
        self,
        execution_id: str,
        state: StateRef,
        blueprint: StateRef,
        *,
        objective: str,
        input_states: tuple[StateRef, ...] = (),
    ) -> ExecutionStart:
        return ExecutionStart(
            execution_id=execution_id,
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective=objective,
            started_at="2026-09-10T12:02:00Z",
            starting_state=state,
            blueprint=blueprint,
            adapter=StateRef(FRAMEWORK_REPOSITORY, self.commit_a, "alpha_runtime.py"),
            input_states=input_states,
        )

    def adopt(
        self,
        checkout: Path,
        setup_state: StateRef,
        candidate: str,
        execution_id: str,
    ):
        selection = read_alpha_selection(checkout, setup_state)
        candidate_framework = self.framework(candidate)
        return run_alpha_framework_adoption(
            self.runtime_root,
            GitAttemptStore(checkout, ENVIRONMENT_REPOSITORY),
            checkout,
            self.start(
                execution_id,
                setup_state,
                selection.framework.blueprint,
                objective="Adopt compatible fixture B",
                input_states=(
                    candidate_framework.state,
                    candidate_framework.blueprint,
                    candidate_framework.compatibility,
                    candidate_framework.source,
                ),
            ),
            AlphaAdoptionRequest(
                self.framework_checkout,
                setup_state,
                candidate_framework,
            ),
            lambda: "2026-09-10T12:02:01Z",
        )

    def seed_memory(self, setup_state: StateRef, blueprint: StateRef) -> StateRef:
        return GitMemoryStore(
            self.environment_checkout,
            ENVIRONMENT_REPOSITORY,
        )._checkpoint(
            MemoryCheckpointRequest(
                repository=ENVIRONMENT_REPOSITORY,
                environment_id=ENVIRONMENT,
                instance_id=INSTANCE,
                blueprint=blueprint,
                expected_state=setup_state,
                items=(MemoryItem("progress.md", "preserve this exact memory\n"),),
                saved_at="2026-09-10T12:01:30Z",
                initial=True,
            )
        ).state

    def test_setup_is_deterministic_and_has_no_framework_ancestry(self) -> None:
        first = self.setup()
        second_checkout = self.root / "environment-two"
        init_repository(second_checkout)
        second = self.setup(second_checkout)

        self.assertEqual(first.state, second.state)
        self.assertEqual(first.selection, second.selection)
        self.assertEqual(git(self.environment_checkout, "rev-list", "--parents", "-n", "1", first.state.commit), first.state.commit)
        self.assertEqual(git(self.environment_checkout, "status", "--porcelain=v1"), "")
        self.assertEqual(
            git(self.environment_checkout, "rev-parse", alpha_selection_ref(ENVIRONMENT, INSTANCE)),
            first.state.commit,
        )
        self.assertNotEqual(first.state.commit, self.commit_a)
        with self.assertRaisesRegex(AlphaError, "repository_not_separate"):
            setup_alpha_environment(
                self.framework_checkout,
                AlphaSetupRequest(
                    repository=ENVIRONMENT_REPOSITORY,
                    environment_id="environment:not-separate",
                    instance_id=INSTANCE,
                    framework_checkout=self.framework_checkout,
                    framework=self.framework(self.commit_a),
                    created_at="2026-09-10T12:01:00Z",
                ),
            )

    def test_complete_a_to_b_save_sync_recover_resume_and_extend(self) -> None:
        setup_a = self.setup()
        blueprint = setup_a.selection.framework.blueprint
        memory_a_result = run_instance_memory_execution(
            self.runtime_root,
            GitAttemptStore(self.environment_checkout, ENVIRONMENT_REPOSITORY),
            GitMemoryStore(self.environment_checkout, ENVIRONMENT_REPOSITORY),
            self.start(
                "execution:memory-a",
                setup_a.state,
                blueprint,
                objective="Save synthetic alpha memory",
            ),
            MemoryCheckpointRequest(
                repository=ENVIRONMENT_REPOSITORY,
                environment_id=ENVIRONMENT,
                instance_id=INSTANCE,
                blueprint=blueprint,
                expected_state=setup_a.state,
                items=(MemoryItem("progress.md", "exact alpha memory ✅\n"),),
                saved_at="2026-09-10T12:03:00Z",
                initial=True,
            ),
            lambda: "2026-09-10T12:03:01Z",
        )
        self.assertTrue(memory_a_result.provenance.terminal_committed)
        memory_a = memory_a_result.checkpoint
        assert memory_a is not None

        remote = self.root / "memory.git"
        subprocess.run(
            ["git", "init", "--bare", str(remote)],
            capture_output=True,
            check=True,
            shell=False,
            timeout=15,
        )
        git(self.environment_checkout, "remote", "add", "memory", str(remote))
        destination = git(self.environment_checkout, "remote", "get-url", "--push", "memory")
        sync_request = MemorySynchronizationRequest(
            repository=ENVIRONMENT_REPOSITORY,
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            blueprint=blueprint,
            memory_state=memory_a.state,
            authorized_baseline=setup_a.state,
            destination_ref=instance_memory_ref(ENVIRONMENT, INSTANCE),
            remote_name="memory",
            expected_destination_url=destination,
            expected_remote_state=None,
            limits=SynchronizationLimits(timeout_seconds=15, max_lineage_commits=8),
        )
        synchronized = run_memory_synchronization_execution(
            self.runtime_root,
            GitAttemptStore(self.environment_checkout, ENVIRONMENT_REPOSITORY),
            self.environment_checkout,
            self.start(
                "execution:sync-a",
                memory_a.state,
                blueprint,
                objective="Synchronize synthetic alpha memory",
            ),
            sync_request,
            lambda: "2026-09-10T12:04:01Z",
        )
        self.assertEqual(
            synchronized.observation.disposition,  # type: ignore[union-attr]
            SynchronizationDisposition.REMOTE_VERIFIED,
        )
        remote_line = git(
            self.environment_checkout,
            "ls-remote",
            "--refs",
            str(remote),
            instance_memory_ref(ENVIRONMENT, INSTANCE),
        )
        self.assertEqual(remote_line.split("\t", 1)[0], memory_a.state.commit)

        fresh = self.root / "fresh-environment"
        init_repository(fresh)
        fresh_setup = self.setup(fresh)
        self.assertEqual(fresh_setup.state, setup_a.state)
        git(fresh, "remote", "add", "memory", str(remote))
        fresh_destination = git(fresh, "remote", "get-url", "--push", "memory")
        recovery_sync = replace(
            sync_request,
            expected_destination_url=fresh_destination,
            expected_remote_state=memory_a.state,
        )
        recovered = run_memory_recovery_execution(
            self.runtime_root,
            GitAttemptStore(fresh, ENVIRONMENT_REPOSITORY),
            fresh,
            self.start(
                "execution:recover-a",
                fresh_setup.state,
                blueprint,
                objective="Recover synthetic alpha memory",
            ),
            MemoryRecoveryRequest(
                synchronization=recovery_sync,
                operation_id="recovery:alpha-a",
                expected_local_state=None,
                original_execution_stopped=True,
            ),
            lambda: "2026-09-10T12:05:01Z",
        )
        self.assertEqual(recovered.recovery.recovered_state, memory_a.state)  # type: ignore[union-attr]

        adopted = self.adopt(fresh, fresh_setup.state, self.commit_b, "execution:adopt-b")
        self.assertTrue(adopted.provenance.terminal_committed)
        self.assertIsNotNone(adopted.adopted_state)
        assert adopted.adopted_state is not None
        self.assertEqual(adopted.adopted_selection.framework.state.commit, self.commit_b)  # type: ignore[union-attr]
        self.assertEqual(adopted.previous_selection.framework.blueprint, blueprint)

        command = [
            sys.executable,
            "-m",
            "peoplebot",
            "alpha-resume",
            "--environment-checkout",
            str(fresh),
            "--environment-repository",
            ENVIRONMENT_REPOSITORY,
            "--environment-id",
            ENVIRONMENT,
            "--instance-id",
            INSTANCE,
            "--framework-checkout",
            str(self.framework_checkout),
            "--selection-commit",
            adopted.adopted_state.commit,
            "--memory-checkout",
            str(fresh),
            "--memory-commit",
            memory_a.state.commit,
            "--memory-path",
            "progress.md",
        ]
        resumed_process = subprocess.run(
            command,
            cwd=Path(__file__).parents[1],
            capture_output=True,
            check=False,
            shell=False,
            timeout=30,
        )
        self.assertEqual(
            resumed_process.returncode,
            0,
            resumed_process.stderr.decode("utf-8", "replace"),
        )
        resumed = json.loads(resumed_process.stdout)
        self.assertEqual(resumed["framework_state"]["commit"], self.commit_b)
        self.assertEqual(resumed["source_state"]["commit"], self.commit_b)
        self.assertEqual(resumed["fixture_result"]["fixture"], "B")
        self.assertEqual(resumed["fixture_result"]["progress"], "exact alpha memory ✅\n")
        expected_memory = b'{"progress.md":"exact alpha memory \xe2\x9c\x85\\n"}\n'
        self.assertEqual(resumed["memory_sha256"], hashlib.sha256(expected_memory).hexdigest())

        memory_b_result = run_instance_memory_execution(
            self.runtime_root,
            GitAttemptStore(fresh, ENVIRONMENT_REPOSITORY),
            GitMemoryStore(fresh, ENVIRONMENT_REPOSITORY),
            self.start(
                "execution:memory-b",
                memory_a.state,
                blueprint,
                objective="Extend synthetic alpha memory after B adoption",
            ),
            MemoryCheckpointRequest(
                repository=ENVIRONMENT_REPOSITORY,
                environment_id=ENVIRONMENT,
                instance_id=INSTANCE,
                blueprint=blueprint,
                expected_state=memory_a.state,
                items=(MemoryItem("progress.md", "exact alpha memory ✅\ncontinued on B\n"),),
                saved_at="2026-09-10T12:06:00Z",
                initial=False,
            ),
            lambda: "2026-09-10T12:06:01Z",
        )
        memory_b = memory_b_result.checkpoint
        assert memory_b is not None
        self.assertEqual(
            git(fresh, "rev-parse", f"{memory_b.state.commit}^"),
            memory_a.state.commit,
        )
        self.assertEqual(read_alpha_selection(fresh, adopted.adopted_state).instance_id, INSTANCE)

    def test_fixture_interrupt_stops_child_before_admission_release(self) -> None:
        setup_a = self.setup()
        interrupted = threading.Event()
        contender_done = threading.Event()
        contender_codes: list[str] = []
        captured: list[_InterruptingFixtureProcess] = []
        runner_calls = 0

        def contend() -> None:
            if not interrupted.wait(5):
                contender_done.set()
                return
            attempt = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:alpha-interrupt-contender",
            )
            contender_codes.append(attempt.code)
            if attempt.acquired:
                attempt.admission.release()
            contender_done.set()

        def factory(*args: object, **kwargs: object) -> _InterruptingFixtureProcess:
            wrapped = _InterruptingFixtureProcess(
                subprocess.Popen(*args, **kwargs),
                interrupted,
                contender_done,
            )
            captured.append(wrapped)
            return wrapped

        def run_fixture(command, input_bytes, environment, timeout_seconds):
            nonlocal runner_calls
            runner_calls += 1
            if runner_calls == 2:
                return _run_process(
                    command,
                    input_bytes,
                    environment,
                    timeout_seconds,
                    _popen=factory,
                )
            return _run_process(command, input_bytes, environment, timeout_seconds)

        contender = threading.Thread(target=contend, name="alpha-fixture-contender")
        contender.start()
        with patch.object(alpha_module, "_run_process", side_effect=run_fixture):
            with self.assertRaises(KeyboardInterrupt):
                self.adopt(
                    self.environment_checkout,
                    setup_a.state,
                    self.commit_looping,
                    "execution:alpha-interrupted-fixture",
                )
        contender.join(5)

        self.assertEqual(contender_codes, ["instance.already_running"])
        self.assertEqual(len(captured), 1)
        self.assertIsNotNone(captured[0].returncode)
        self.assertFalse(
            any(
                thread.name.startswith("peoplebot-codex-") and thread.is_alive()
                for thread in threading.enumerate()
            )
        )
        self.assertEqual(
            git(self.environment_checkout, "rev-parse", setup_a.selection.ref_name),
            setup_a.state.commit,
        )
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:after-alpha-interrupt",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()

    def test_unresolved_fixture_retains_admission_until_exact_recovery(self) -> None:
        setup_a = self.setup()
        interrupted = threading.Event()
        contender_done = threading.Event()
        contender_done.set()
        captured: list[_InterruptingFixtureProcess] = []
        runner_calls = 0

        def factory(*args: object, **kwargs: object) -> _InterruptingFixtureProcess:
            wrapped = _InterruptingFixtureProcess(
                subprocess.Popen(*args, **kwargs),
                interrupted,
                contender_done,
                unresolved_once=True,
            )
            captured.append(wrapped)
            return wrapped

        def run_fixture(command, input_bytes, environment, timeout_seconds):
            nonlocal runner_calls
            runner_calls += 1
            if runner_calls == 2:
                return _run_process(
                    command,
                    input_bytes,
                    environment,
                    timeout_seconds,
                    _popen=factory,
                )
            return _run_process(command, input_bytes, environment, timeout_seconds)

        try:
            with patch.object(alpha_module, "_run_process", side_effect=run_fixture):
                self.adopt(
                    self.environment_checkout,
                    setup_a.state,
                    self.commit_looping,
                    "execution:alpha-unresolved-fixture",
                )
        except ProcessOwnershipUnresolved as error:
            self.assertIsInstance(error.original, KeyboardInterrupt)
            self.assertIsNotNone(error.retained_admission)
            self.assertEqual(len(captured), 1)
            self.assertIsNone(captured[0].returncode)
            contender = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:alpha-unresolved-contender",
            )
            self.assertEqual(contender.code, "instance.already_running")
            self.assertTrue(error.recover(5))
            self.assertIsNotNone(captured[0].returncode)
            self.assertEqual(error.release_after_recovery().code, "admission.released")
        else:
            self.fail("expected unresolved fixture ownership")

        self.assertFalse(
            any(
                thread.name.startswith("peoplebot-codex-") and thread.is_alive()
                for thread in threading.enumerate()
            )
        )
        self.assertEqual(
            git(self.environment_checkout, "rev-parse", setup_a.selection.ref_name),
            setup_a.state.commit,
        )
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:after-alpha-recovery",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()

    def test_fixture_output_overflow_is_bounded_reaped_and_classified(self) -> None:
        setup_a = self.setup()
        result = self.adopt(
            self.environment_checkout,
            setup_a.state,
            self.commit_overflow,
            "execution:alpha-output-overflow",
        )

        self.assertEqual(
            result.provenance.execution_record.terminal_outcome.code,
            "alpha.source_invalid",
        )
        self.assertTrue(result.provenance.terminal_committed)
        self.assertEqual(
            git(self.environment_checkout, "rev-parse", setup_a.selection.ref_name),
            setup_a.state.commit,
        )
        self.assertFalse(
            any(
                thread.name.startswith("peoplebot-codex-") and thread.is_alive()
                for thread in threading.enumerate()
            )
        )
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:after-alpha-output-overflow",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()

    def test_record_failure_after_adoption_commits_exact_partial_state_once(self) -> None:
        setup_a = self.setup()
        memory = self.seed_memory(setup_a.state, setup_a.selection.framework.blueprint)
        memory_ref = instance_memory_ref(ENVIRONMENT, INSTANCE)
        real_write = alpha_module._AlphaStore.write_selection
        constructor_calls = 0
        publication_calls = 0

        def construct(*args: object, **kwargs: object) -> RealExecutionRecord:
            nonlocal constructor_calls
            constructor_calls += 1
            if constructor_calls == 1:
                raise RuntimeError("injected successful-record construction failure")
            return RealExecutionRecord(*args, **kwargs)

        def publish(store, *args, **kwargs):
            nonlocal publication_calls
            publication_calls += 1
            return real_write(store, *args, **kwargs)

        with (
            patch.object(alpha_module, "ExecutionRecord", side_effect=construct),
            patch.object(
                alpha_module._AlphaStore,
                "write_selection",
                autospec=True,
                side_effect=publish,
            ),
        ):
            result = self.adopt(
                self.environment_checkout,
                setup_a.state,
                self.commit_b,
                "execution:alpha-record-failure-once",
            )

        self.assertEqual(publication_calls, 1)
        self.assertEqual(constructor_calls, 2)
        self.assertIsNotNone(result.adopted_state)
        self.assertEqual(result.adopted_selection.framework.state.commit, self.commit_b)
        record = result.provenance.execution_record
        self.assertEqual(record.status.value, "failed")
        self.assertIsNone(record.resulting_state)
        self.assertEqual(record.artifacts, (result.adopted_state,))
        self.assertEqual(record.partial_state, result.adopted_state)
        self.assertEqual(
            record.terminal_outcome.code,
            "alpha.record_failed_after_adoption",
        )
        self.assertTrue(result.provenance.terminal_committed)
        durable = GitAttemptStore(
            self.environment_checkout,
            ENVIRONMENT_REPOSITORY,
        ).read_evidence(result.provenance.terminal_evidence.state)
        self.assertEqual(durable["status"], "failed")
        self.assertIsNone(durable["resulting_state"])
        self.assertEqual(durable["artifacts"], [result.adopted_state.to_dict()])
        self.assertEqual(
            git(self.environment_checkout, "rev-parse", setup_a.selection.ref_name),
            result.adopted_state.commit,
        )
        self.assertEqual(git(self.environment_checkout, "rev-parse", memory_ref), memory.commit)
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:after-alpha-record-failure-once",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()

    def test_persistent_record_failure_keeps_adoption_and_incomplete_evidence(self) -> None:
        setup_a = self.setup()
        memory = self.seed_memory(setup_a.state, setup_a.selection.framework.blueprint)
        memory_ref = instance_memory_ref(ENVIRONMENT, INSTANCE)
        real_write = alpha_module._AlphaStore.write_selection
        publication_calls = 0

        def publish(store, *args, **kwargs):
            nonlocal publication_calls
            publication_calls += 1
            return real_write(store, *args, **kwargs)

        with (
            patch.object(
                alpha_module,
                "ExecutionRecord",
                side_effect=RuntimeError("injected persistent record construction failure"),
            ) as constructor,
            patch.object(
                alpha_module._AlphaStore,
                "write_selection",
                autospec=True,
                side_effect=publish,
            ),
        ):
            result = self.adopt(
                self.environment_checkout,
                setup_a.state,
                self.commit_b,
                "execution:alpha-record-failure-persistent",
            )

        self.assertEqual(publication_calls, 1)
        self.assertEqual(constructor.call_count, 2)
        self.assertIsNotNone(result.adopted_state)
        self.assertEqual(result.adopted_selection.framework.state.commit, self.commit_b)
        self.assertEqual(
            git(self.environment_checkout, "rev-parse", setup_a.selection.ref_name),
            result.adopted_state.commit,
        )
        self.assertEqual(git(self.environment_checkout, "rev-parse", memory_ref), memory.commit)
        self.assertIsNotNone(result.provenance.start_evidence)
        self.assertIsNone(result.provenance.execution_record)
        self.assertIsNone(result.provenance.terminal_evidence)
        self.assertEqual(result.provenance.task_failure.code, "execution.task_raised")
        self.assertEqual(
            result.provenance.record_failure.code,
            "provenance.execution_record_unavailable",
        )
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:after-alpha-record-failure-persistent",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()

    def test_busy_missing_incompatible_and_prepublication_failure_preserve_a(self) -> None:
        setup_a = self.setup()
        selection_ref = setup_a.selection.ref_name
        memory = GitMemoryStore(
            self.environment_checkout,
            ENVIRONMENT_REPOSITORY,
        )._checkpoint(
            MemoryCheckpointRequest(
                repository=ENVIRONMENT_REPOSITORY,
                environment_id=ENVIRONMENT,
                instance_id=INSTANCE,
                blueprint=setup_a.selection.framework.blueprint,
                expected_state=setup_a.state,
                items=(MemoryItem("progress.md", "preserve this exact memory\n"),),
                saved_at="2026-09-10T12:01:30Z",
                initial=True,
            )
        ).state
        memory_ref = instance_memory_ref(ENVIRONMENT, INSTANCE)
        owner = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:owner",
        )
        self.assertTrue(owner.acquired)
        try:
            busy = self.adopt(fresh := self.environment_checkout, setup_a.state, self.commit_b, "execution:busy")
        finally:
            owner.admission.release()  # type: ignore[union-attr]
        self.assertEqual(busy.provenance.admission_code, "instance.already_running")
        self.assertFalse(busy.provenance.task_started)
        self.assertEqual(git(fresh, "rev-parse", selection_ref), setup_a.state.commit)

        missing = self.adopt(fresh, setup_a.state, "f" * 40, "execution:missing")
        self.assertEqual(missing.provenance.execution_record.terminal_outcome.code, "state.commit_unavailable")  # type: ignore[union-attr]
        self.assertEqual(git(fresh, "rev-parse", selection_ref), setup_a.state.commit)

        incompatible = self.adopt(
            fresh,
            setup_a.state,
            self.commit_incompatible,
            "execution:incompatible",
        )
        self.assertEqual(
            incompatible.provenance.execution_record.terminal_outcome.code,  # type: ignore[union-attr]
            "alpha.candidate_incompatible",
        )
        self.assertEqual(git(fresh, "rev-parse", selection_ref), setup_a.state.commit)

        broken = self.adopt(
            fresh,
            setup_a.state,
            self.commit_broken_behavior,
            "execution:broken-behavior",
        )
        self.assertEqual(
            broken.provenance.execution_record.terminal_outcome.code,  # type: ignore[union-attr]
            "alpha.source_invalid",
        )
        self.assertEqual(git(fresh, "rev-parse", selection_ref), setup_a.state.commit)

        with patch("peoplebot.alpha._AlphaStore.write_selection", side_effect=AlphaError("alpha.injected_failure", "before publication")):
            failed = self.adopt(fresh, setup_a.state, self.commit_b, "execution:failure")
        self.assertEqual(failed.provenance.execution_record.terminal_outcome.code, "alpha.injected_failure")  # type: ignore[union-attr]
        self.assertEqual(git(fresh, "rev-parse", selection_ref), setup_a.state.commit)
        self.assertEqual(git(fresh, "rev-parse", memory_ref), memory.commit)
        self.assertEqual(read_alpha_selection(fresh, setup_a.state).instance_id, INSTANCE)


if __name__ == "__main__":
    unittest.main()
