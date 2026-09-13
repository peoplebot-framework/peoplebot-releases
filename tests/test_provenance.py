from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peoplebot import ExecutionRecord, ExecutionStatus, StateRef, TerminalOutcome
from peoplebot.admission import try_acquire_execution
from peoplebot.provenance import (
    AttemptPhase,
    ExecutionAttemptRecord,
    ExecutionStart,
    GitAttemptStore,
    ProvenanceError,
    _attempt_digest,
    run_with_execution_provenance,
)


REPOSITORY = "https://github.com/example/peoplebot-fixture"
ENVIRONMENT = "environment:provenance-test"
INSTANCE = "instance:provenance-test"


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


def ref_exists(repository: Path, ref_name: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repository), "show-ref", "--verify", "--quiet", ref_name],
        capture_output=True,
        check=False,
        shell=False,
        timeout=15,
    )
    return result.returncode == 0


def ref_lock_path(repository: Path, ref_name: str) -> Path:
    return Path(
        git(
            repository,
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            f"{ref_name}.lock",
        )
    )


class _StartFailureStore:
    def persist_attempt(self, record: object) -> object:
        raise ProvenanceError("provenance.injected_start_failure", "start write failed")

    def persist_terminal(self, start_evidence: object, record: object) -> object:
        raise AssertionError("terminal persistence must not run")


class _TerminalFailureStore:
    def __init__(self, delegate: GitAttemptStore) -> None:
        self.delegate = delegate

    def persist_attempt(self, record: object) -> object:
        return self.delegate.persist_attempt(record)

    def persist_terminal(self, start_evidence: object, record: object) -> object:
        raise ProvenanceError(
            "provenance.injected_terminal_failure",
            "terminal write failed",
        )


class _AdmissionCheckingStore:
    def __init__(self, delegate: GitAttemptStore, runtime_root: Path) -> None:
        self.delegate = delegate
        self.runtime_root = runtime_root
        self.terminal_saw_owner = False

    def persist_attempt(self, record: object) -> object:
        return self.delegate.persist_attempt(record)

    def persist_terminal(self, start_evidence: object, record: object) -> object:
        contender = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:terminal-contender",
        )
        self.terminal_saw_owner = contender.code == "instance.already_running"
        if contender.acquired:
            contender.admission.release()
        return self.delegate.persist_terminal(start_evidence, record)


@unittest.skipUnless(os.name == "nt", "provenance lifecycle uses Windows admission v0")
class ProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.name", "PeopleBot Test")
        git(self.repository, "config", "user.email", "test@example.invalid")
        files = {
            "adapter.json": "adapter v0\n",
            "artifact.txt": "starting content\n",
            "blueprint.json": "blueprint v0\n",
            "message.json": "message v0\n",
            "procedure.md": "procedure v0\n",
        }
        for path, content in files.items():
            (self.repository / path).write_text(content, encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "starting State")
        self.start_commit = git(self.repository, "rev-parse", "HEAD")
        (self.repository / "artifact.txt").write_text("result content\n", encoding="utf-8")
        git(self.repository, "add", "artifact.txt")
        git(self.repository, "commit", "-m", "resulting State")
        self.result_commit = git(self.repository, "rev-parse", "HEAD")
        self.runtime_root = self.root / "native runtime"
        self.store = GitAttemptStore(self.repository, REPOSITORY)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start(self, execution_id: str = "execution:test") -> ExecutionStart:
        return ExecutionStart(
            execution_id=execution_id,
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Prove durable execution provenance",
            started_at="2026-09-08T12:00:00.123456Z",
            starting_state=StateRef(REPOSITORY, self.start_commit),
            blueprint=StateRef(REPOSITORY, self.start_commit, "blueprint.json"),
            adapter=StateRef(REPOSITORY, self.start_commit, "adapter.json"),
            procedures=(StateRef(REPOSITORY, self.start_commit, "procedure.md"),),
            input_states=(StateRef(REPOSITORY, self.start_commit, "artifact.txt"),),
            input_messages=(StateRef(REPOSITORY, self.start_commit, "message.json"),),
        )

    def terminal(
        self,
        start: ExecutionStart,
        *,
        status: ExecutionStatus = ExecutionStatus.COMPLETED,
    ) -> ExecutionRecord:
        if status is ExecutionStatus.COMPLETED:
            resulting_state = StateRef(REPOSITORY, self.result_commit)
            terminal_outcome = None
        elif status is ExecutionStatus.NO_CHANGE:
            resulting_state = start.starting_state
            terminal_outcome = None
        else:
            resulting_state = None
            terminal_outcome = TerminalOutcome("execution.task_failed", "task failed")
        return ExecutionRecord(
            execution_id=start.execution_id,
            environment_id=start.environment_id,
            instance_id=start.instance_id,
            objective=start.objective,
            started_at=start.started_at,
            finished_at="2026-09-08T12:00:01.123456Z",
            starting_state=start.starting_state,
            blueprint=start.blueprint,
            adapter=start.adapter,
            status=status,
            procedures=start.procedures,
            input_states=start.input_states,
            input_messages=start.input_messages,
            resulting_state=resulting_state,
            terminal_outcome=terminal_outcome,
        )

    def test_admitted_success_commits_bound_start_and_terminal_evidence(self) -> None:
        start = self.start("execution:success")
        branch_before = git(self.repository, "rev-parse", "refs/heads/main")
        (self.repository / "artifact.txt").write_text("dirty content\n", encoding="utf-8")
        (self.repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        source_status_before = git(self.repository, "status", "--porcelain=v1", "-uall")
        checking_store = _AdmissionCheckingStore(self.store, self.runtime_root)

        result = run_with_execution_provenance(
            self.runtime_root,
            checking_store,
            start,
            lambda: self.terminal(start),
            lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
        )

        self.assertTrue(result.task_started)
        self.assertTrue(result.terminal_committed, result.persistence_failure)
        self.assertTrue(checking_store.terminal_saw_owner)
        self.assertEqual(result.execution_record.status, ExecutionStatus.COMPLETED)
        self.assertEqual(result.start_evidence.state.path, "attempt.json")
        self.assertEqual(result.terminal_evidence.state.path, "execution.json")
        self.assertEqual(
            git(self.repository, "rev-parse", result.start_evidence.ref_name),
            result.terminal_evidence.state.commit,
        )
        self.assertEqual(
            git(self.repository, "rev-parse", f"{result.terminal_evidence.state.commit}^"),
            result.start_evidence.state.commit,
        )
        self.assertFalse(result.start_evidence.remote_synchronized)
        self.assertFalse(result.terminal_evidence.remote_synchronized)
        self.assertEqual(branch_before, git(self.repository, "rev-parse", "refs/heads/main"))
        self.assertEqual(
            source_status_before,
            git(self.repository, "status", "--porcelain=v1", "-uall"),
        )
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:after-success",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()

    def test_no_change_is_a_truthful_terminal_execution(self) -> None:
        start = self.start("execution:no-change")
        result = run_with_execution_provenance(
            self.runtime_root,
            self.store,
            start,
            lambda: self.terminal(start, status=ExecutionStatus.NO_CHANGE),
            lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
        )
        self.assertTrue(result.terminal_committed, result.persistence_failure)
        saved = self.store.read_evidence(result.terminal_evidence.state)
        self.assertEqual(saved["status"], "no_change")
        self.assertEqual(saved["resulting_state"], start.starting_state.to_dict())

    def test_task_failure_is_recorded_without_retry(self) -> None:
        start = self.start("execution:task-failure")
        calls = 0

        def fail() -> ExecutionRecord:
            nonlocal calls
            calls += 1
            raise RuntimeError("fixture task failure")

        result = run_with_execution_provenance(
            self.runtime_root,
            self.store,
            start,
            fail,
            lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
        )
        self.assertEqual(calls, 1)
        self.assertEqual(result.task_failure.code, "execution.task_raised")
        self.assertEqual(result.execution_record.status, ExecutionStatus.FAILED)
        self.assertTrue(result.terminal_committed)
        saved = self.store.read_evidence(result.terminal_evidence.state)
        self.assertEqual(saved["status"], "failed")

    def test_rejection_runs_no_task_and_does_not_mutate_active_branch(self) -> None:
        owner = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:owner",
        )
        start = self.start("execution:rejected")
        calls: list[bool] = []
        branch_before = git(self.repository, "rev-parse", "refs/heads/main")
        try:
            result = run_with_execution_provenance(
                self.runtime_root,
                self.store,
                start,
                lambda: calls.append(True),
                lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
            )
            contender = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:still-rejected",
            )
            self.assertEqual(contender.code, "instance.already_running")
        finally:
            owner.admission.release()
        self.assertFalse(result.task_started)
        self.assertEqual(result.admission_code, "instance.already_running")
        self.assertEqual(result.attempt_record.phase.value, "rejected")
        self.assertEqual(calls, [])
        self.assertIsNone(result.execution_record)
        self.assertIsNone(result.terminal_evidence)
        self.assertEqual(branch_before, git(self.repository, "rev-parse", "refs/heads/main"))
        saved = self.store.read_evidence(result.start_evidence.state)
        self.assertEqual(saved["phase"], "rejected")
        self.assertFalse(saved["task_started"])
        self.assertEqual(saved["input_states"], [start.input_states[0].to_dict()])
        self.assertNotIn("usage", saved)

    def test_start_record_failure_prevents_task(self) -> None:
        start = self.start("execution:start-write-failure")
        calls: list[bool] = []
        result = run_with_execution_provenance(
            self.runtime_root,
            _StartFailureStore(),
            start,
            lambda: calls.append(True),
            lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
        )
        self.assertFalse(result.task_started)
        self.assertEqual(calls, [])
        self.assertEqual(
            result.persistence_failure.code,
            "provenance.injected_start_failure",
        )
        self.assertIsNone(result.start_evidence)

    def test_terminal_failure_preserves_task_and_incomplete_start_facts(self) -> None:
        start = self.start("execution:terminal-write-failure")
        calls = 0

        def fail() -> ExecutionRecord:
            nonlocal calls
            calls += 1
            raise RuntimeError("fixture task failure")

        result = run_with_execution_provenance(
            self.runtime_root,
            _TerminalFailureStore(self.store),
            start,
            fail,
            lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
        )
        self.assertEqual(calls, 1)
        self.assertEqual(result.task_failure.code, "execution.task_raised")
        self.assertEqual(
            result.persistence_failure.code,
            "provenance.injected_terminal_failure",
        )
        self.assertEqual(result.execution_record.status, ExecutionStatus.FAILED)
        self.assertIsNotNone(result.start_evidence)
        self.assertIsNone(result.terminal_evidence)
        saved = self.store.read_evidence(result.start_evidence.state)
        self.assertEqual(saved["phase"], "admitted_start")
        self.assertFalse(saved["task_started"])
        self.assertEqual(
            git(self.repository, "rev-parse", result.start_evidence.ref_name),
            result.start_evidence.state.commit,
        )

    def test_exact_evidence_state_ignores_branch_and_working_file_changes(self) -> None:
        start = self.start("execution:exact-retrieval")
        result = run_with_execution_provenance(
            self.runtime_root,
            self.store,
            start,
            lambda: self.terminal(start),
            lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
        )
        expected = result.terminal_evidence.state
        before = self.store.read_evidence_bytes(expected)
        (self.repository / "execution.json").write_text(
            '{"working":"file"}\n',
            encoding="utf-8",
        )
        git(self.repository, "branch", "unrelated", self.start_commit)
        git(self.repository, "update-ref", result.terminal_evidence.ref_name, self.start_commit)
        after = self.store.read_evidence_bytes(expected)
        self.assertEqual(before, after)
        self.assertEqual(json.loads(after)["execution_id"], start.execution_id)

    def test_existing_attempt_ref_is_not_overwritten(self) -> None:
        start = self.start("execution:duplicate")
        first = run_with_execution_provenance(
            self.runtime_root,
            self.store,
            start,
            lambda: self.terminal(start),
            lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
        )
        original_tip = git(self.repository, "rev-parse", first.terminal_evidence.ref_name)
        second_calls: list[bool] = []
        second = run_with_execution_provenance(
            self.runtime_root,
            self.store,
            start,
            lambda: second_calls.append(True),
            lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
        )
        self.assertFalse(second.task_started)
        self.assertEqual(second_calls, [])
        self.assertEqual(second.persistence_failure.code, "provenance.attempt_exists")
        self.assertEqual(
            original_tip,
            git(self.repository, "rev-parse", first.terminal_evidence.ref_name),
        )

    def test_initial_publication_refuses_symbolic_attempt_ref(self) -> None:
        start = self.start("execution:symbolic-initial")
        record = ExecutionAttemptRecord(
            start,
            AttemptPhase.ADMITTED_START,
            "admission.acquired",
        )
        attempt_ref = f"refs/peoplebot/attempts/v0/{_attempt_digest(start)}"
        target_ref = "refs/heads/unrelated-initial-target"
        git(self.repository, "symbolic-ref", attempt_ref, target_ref)
        branch_before = git(self.repository, "rev-parse", "refs/heads/main")
        status_before = git(self.repository, "status", "--porcelain=v1", "-uall")

        with self.assertRaises(ProvenanceError) as raised:
            self.store.persist_attempt(record)

        self.assertEqual(raised.exception.code, "provenance.attempt_exists")
        self.assertEqual(git(self.repository, "symbolic-ref", attempt_ref), target_ref)
        self.assertFalse(ref_exists(self.repository, target_ref))
        self.assertEqual(branch_before, git(self.repository, "rev-parse", "refs/heads/main"))
        self.assertEqual(
            status_before,
            git(self.repository, "status", "--porcelain=v1", "-uall"),
        )

    def test_terminal_publication_refuses_symbolic_attempt_ref(self) -> None:
        start = self.start("execution:symbolic-terminal")
        attempt = ExecutionAttemptRecord(
            start,
            AttemptPhase.ADMITTED_START,
            "admission.acquired",
        )
        start_evidence = self.store.persist_attempt(attempt)
        target_ref = "refs/heads/unrelated-terminal-target"
        git(self.repository, "update-ref", target_ref, start_evidence.state.commit)
        git(self.repository, "symbolic-ref", start_evidence.ref_name, target_ref)
        target_before = git(self.repository, "rev-parse", target_ref)
        status_before = git(self.repository, "status", "--porcelain=v1", "-uall")

        with self.assertRaises(ProvenanceError) as raised:
            self.store.persist_terminal(start_evidence, self.terminal(start))

        self.assertEqual(raised.exception.code, "provenance.attempt_exists")
        self.assertEqual(
            git(self.repository, "symbolic-ref", start_evidence.ref_name),
            target_ref,
        )
        self.assertEqual(git(self.repository, "rev-parse", target_ref), target_before)
        self.assertEqual(
            self.store.read_evidence(start_evidence.state)["phase"],
            "admitted_start",
        )
        self.assertEqual(
            status_before,
            git(self.repository, "status", "--porcelain=v1", "-uall"),
        )

    def test_initial_publication_aborts_when_symbolic_inspection_fails(self) -> None:
        start = self.start("execution:inspection-error-initial")
        attempt_ref = f"refs/peoplebot/attempts/v0/{_attempt_digest(start)}"
        git(self.repository, "branch", "unrelated-inspection-initial", self.start_commit)
        (self.repository / "artifact.txt").write_text("dirty content\n", encoding="utf-8")
        (self.repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        branch_before = git(self.repository, "rev-parse", "refs/heads/main")
        unrelated_before = git(
            self.repository,
            "rev-parse",
            "refs/heads/unrelated-inspection-initial",
        )
        status_before = git(self.repository, "status", "--porcelain=v1", "-uall")
        task_calls = 0
        failure_record_calls = 0
        real_git = self.store._git

        def fail_inspection(*arguments: str, **kwargs: object) -> object:
            if arguments[:3] == ("symbolic-ref", "--quiet", "--no-recurse"):
                return subprocess.CompletedProcess(arguments, 128, b"", b"injected failure")
            return real_git(*arguments, **kwargs)

        def task() -> ExecutionRecord:
            nonlocal task_calls
            task_calls += 1
            return self.terminal(start)

        def failure_record(error: Exception) -> ExecutionRecord:
            nonlocal failure_record_calls
            failure_record_calls += 1
            return self.terminal(start, status=ExecutionStatus.FAILED)

        with patch.object(self.store, "_git", side_effect=fail_inspection):
            result = run_with_execution_provenance(
                self.runtime_root,
                self.store,
                start,
                task,
                failure_record,
            )

        self.assertEqual(result.admission_code, "admission.acquired")
        self.assertFalse(result.task_started)
        self.assertEqual(task_calls, 0)
        self.assertEqual(failure_record_calls, 0)
        self.assertIsNone(result.start_evidence)
        self.assertIsNone(result.execution_record)
        self.assertIsNone(result.terminal_evidence)
        self.assertEqual(
            result.persistence_failure.code,
            "provenance.ref_inspection_failed",
        )
        self.assertFalse(ref_exists(self.repository, attempt_ref))
        self.assertFalse(ref_lock_path(self.repository, attempt_ref).exists())
        self.assertEqual(branch_before, git(self.repository, "rev-parse", "refs/heads/main"))
        self.assertEqual(
            unrelated_before,
            git(self.repository, "rev-parse", "refs/heads/unrelated-inspection-initial"),
        )
        self.assertEqual(
            status_before,
            git(self.repository, "status", "--porcelain=v1", "-uall"),
        )
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:inspection-error-initial:reacquired",
        )
        self.assertTrue(reacquired.acquired, reacquired.code)
        reacquired.admission.release()

    def test_terminal_publication_aborts_when_symbolic_inspection_fails(self) -> None:
        start = self.start("execution:inspection-error-terminal")
        attempt = ExecutionAttemptRecord(
            start,
            AttemptPhase.ADMITTED_START,
            "admission.acquired",
        )
        start_evidence = self.store.persist_attempt(attempt)
        start_bytes = self.store.read_evidence_bytes(start_evidence.state)
        target_ref = "refs/heads/unrelated-inspection-terminal"
        git(self.repository, "update-ref", target_ref, start_evidence.state.commit)
        git(self.repository, "symbolic-ref", start_evidence.ref_name, target_ref)
        (self.repository / "artifact.txt").write_text("dirty content\n", encoding="utf-8")
        (self.repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        branch_before = git(self.repository, "rev-parse", "refs/heads/main")
        target_before = git(self.repository, "rev-parse", target_ref)
        status_before = git(self.repository, "status", "--porcelain=v1", "-uall")
        real_git = self.store._git

        def fail_inspection(*arguments: str, **kwargs: object) -> object:
            if arguments[:3] == ("symbolic-ref", "--quiet", "--no-recurse"):
                return subprocess.CompletedProcess(arguments, 128, b"", b"injected failure")
            return real_git(*arguments, **kwargs)

        with patch.object(self.store, "_git", side_effect=fail_inspection):
            with self.assertRaises(ProvenanceError) as raised:
                self.store.persist_terminal(start_evidence, self.terminal(start))

        self.assertEqual(raised.exception.code, "provenance.ref_inspection_failed")
        self.assertEqual(
            git(self.repository, "symbolic-ref", start_evidence.ref_name),
            target_ref,
        )
        self.assertEqual(target_before, git(self.repository, "rev-parse", target_ref))
        self.assertEqual(start_bytes, self.store.read_evidence_bytes(start_evidence.state))
        self.assertEqual(
            self.store.read_evidence(start_evidence.state)["phase"],
            "admitted_start",
        )
        self.assertFalse(ref_lock_path(self.repository, start_evidence.ref_name).exists())
        self.assertEqual(branch_before, git(self.repository, "rev-parse", "refs/heads/main"))
        self.assertEqual(
            status_before,
            git(self.repository, "status", "--porcelain=v1", "-uall"),
        )

    def test_invalid_normal_returns_are_explicit_incomplete_record_failures(self) -> None:
        for suffix, invalid_value in (("none", None), ("string", "not a record")):
            with self.subTest(value=suffix):
                start = self.start(f"execution:invalid-return:{suffix}")
                calls = 0

                def invalid_task() -> object:
                    nonlocal calls
                    calls += 1
                    return invalid_value

                result = run_with_execution_provenance(
                    self.runtime_root,
                    self.store,
                    start,
                    invalid_task,
                    lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
                )

                self.assertEqual(calls, 1)
                self.assertTrue(result.task_started)
                self.assertEqual(
                    result.record_failure.code,
                    "provenance.execution_record_unavailable",
                )
                self.assertIsNone(result.execution_record)
                self.assertIsNone(result.terminal_evidence)
                self.assertIsNone(result.task_failure)
                self.assertIsNone(result.persistence_failure)
                self.assertIsNone(result.release_failure)
                self.assertIsNotNone(result.start_evidence)
                self.assertEqual(
                    git(self.repository, "rev-parse", result.start_evidence.ref_name),
                    result.start_evidence.state.commit,
                )
                self.assertEqual(
                    self.store.read_evidence(result.start_evidence.state)["phase"],
                    "admitted_start",
                )
                reacquired = try_acquire_execution(
                    self.runtime_root,
                    ENVIRONMENT,
                    INSTANCE,
                    f"execution:after-invalid-return:{suffix}",
                )
                self.assertTrue(reacquired.acquired)
                reacquired.admission.release()

    def test_persistence_does_not_execute_repository_hooks_or_filters(self) -> None:
        hooks = self.root / "hostile hooks"
        hooks.mkdir()
        reference_hook = hooks / "reference-transaction"
        reference_hook.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8", newline="\n")
        reference_hook.chmod(0o755)
        git(self.repository, "config", "core.hooksPath", str(hooks))
        git(self.repository, "config", "filter.peoplebot.clean", "cmd /c exit 91")
        (self.repository / ".gitattributes").write_text(
            "*.json filter=peoplebot\n",
            encoding="utf-8",
        )
        start = self.start("execution:no-hooks-or-filters")
        result = run_with_execution_provenance(
            self.runtime_root,
            self.store,
            start,
            lambda: self.terminal(start),
            lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
        )
        self.assertTrue(result.terminal_committed)

    def test_linked_worktree_can_own_the_local_evidence_store(self) -> None:
        linked = self.root / "linked worktree"
        git(
            self.repository,
            "worktree",
            "add",
            "--detach",
            str(linked),
            self.result_commit,
        )
        linked_store = GitAttemptStore(linked, REPOSITORY)
        start = self.start("execution:linked-worktree")
        result = run_with_execution_provenance(
            self.runtime_root,
            linked_store,
            start,
            lambda: self.terminal(start),
            lambda error: self.terminal(start, status=ExecutionStatus.FAILED),
        )
        self.assertTrue(result.terminal_committed)
        self.assertEqual(
            git(self.repository, "rev-parse", result.terminal_evidence.ref_name),
            result.terminal_evidence.state.commit,
        )
        self.assertEqual(git(linked, "rev-parse", "HEAD"), self.result_commit)


if __name__ == "__main__":
    unittest.main()
