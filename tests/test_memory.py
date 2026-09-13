from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peoplebot import (
    ContextPolicy,
    ExecutionStart,
    GitAttemptStore,
    GitMemoryStore,
    MemoryCheckpointRequest,
    MemoryError,
    MemoryItem,
    StateRef,
    assemble_instance_memory_context,
    instance_memory_ref,
    run_instance_memory_execution,
    try_acquire_execution,
)
from peoplebot.provenance import ProvenanceError


REPOSITORY = "https://github.com/example/instance-memory-fixture"
ENVIRONMENT = "environment:memory-test"
INSTANCE = "instance:memory-test"


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


def git_result(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=False,
        encoding="utf-8",
        shell=False,
        timeout=15,
    )


class _TerminalFailureStore:
    def __init__(self, delegate: GitAttemptStore) -> None:
        self.delegate = delegate

    def persist_attempt(self, record: object) -> object:
        return self.delegate.persist_attempt(record)

    def persist_terminal(self, start_evidence: object, record: object) -> object:
        raise ProvenanceError("provenance.injected_terminal_failure", "terminal write failed")


@unittest.skipUnless(os.name == "nt", "Instance memory execution uses Windows admission v0")
class InstanceMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.name", "PeopleBot Test")
        git(self.repository, "config", "user.email", "test@example.invalid")
        (self.repository / "blueprint.json").write_text("blueprint v0\n", encoding="utf-8")
        (self.repository / "adapter.json").write_text("deterministic memory adapter\n", encoding="utf-8")
        (self.repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "seed State")
        self.seed = git(self.repository, "rev-parse", "HEAD")
        self.seed_state = StateRef(REPOSITORY, self.seed)
        self.blueprint = StateRef(REPOSITORY, self.seed, "blueprint.json")
        self.adapter = StateRef(REPOSITORY, self.seed, "adapter.json")
        self.memory_store = GitMemoryStore(self.repository, REPOSITORY)
        self.evidence_store = GitAttemptStore(self.repository, REPOSITORY)
        self.runtime_root = self.root / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(
        self,
        expected: StateRef,
        items: tuple[MemoryItem, ...],
        *,
        initial: bool,
        instance_id: str = INSTANCE,
        blueprint: StateRef | None = None,
        saved_at: str = "2026-09-10T12:00:00Z",
    ) -> MemoryCheckpointRequest:
        return MemoryCheckpointRequest(
            REPOSITORY,
            ENVIRONMENT,
            instance_id,
            self.blueprint if blueprint is None else blueprint,
            expected,
            items,
            saved_at,
            initial,
        )

    def start(
        self,
        execution_id: str,
        expected: StateRef,
        *,
        instance_id: str = INSTANCE,
        input_states: tuple[StateRef, ...] = (),
    ) -> ExecutionStart:
        return ExecutionStart(
            execution_id=execution_id,
            environment_id=ENVIRONMENT,
            instance_id=instance_id,
            objective="Checkpoint and resume exact Instance memory",
            started_at="2026-09-10T12:00:00Z",
            starting_state=expected,
            blueprint=self.blueprint,
            adapter=self.adapter,
            input_states=input_states,
        )

    def policy(self, state: StateRef) -> ContextPolicy:
        return ContextPolicy(
            StateRef(REPOSITORY, state.commit, "memory.json"),
            max_entries=16,
            max_blob_bytes=65_536,
            max_total_blob_bytes=262_144,
        )

    def worktree_snapshot(self, worktree: Path) -> tuple[object, ...]:
        head_ref = git_result(worktree, "symbolic-ref", "--quiet", "HEAD")
        head = git_result(worktree, "rev-parse", "--verify", "HEAD")
        index_path = Path(
            git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index")
        )
        return (
            head_ref.returncode,
            head_ref.stdout,
            head.returncode,
            head.stdout,
            index_path.read_bytes() if index_path.exists() else None,
            git(worktree, "status", "--porcelain=v1", "-uall"),
            git(self.repository, "worktree", "list", "--porcelain"),
        )

    def test_checkpoint_fresh_resume_descendant_isolation_and_unchanged_save(self) -> None:
        items_a = (
            MemoryItem("decisions.md", "Use exact Git State. ✅\n"),
            MemoryItem("progress/current.md", "checkpoint A\n"),
        )
        result_a = run_instance_memory_execution(
            self.runtime_root,
            self.evidence_store,
            self.memory_store,
            self.start("execution:memory-a", self.seed_state),
            self.request(self.seed_state, items_a, initial=True),
            lambda: "2026-09-10T12:00:01Z",
        )
        checkpoint_a = result_a.checkpoint
        self.assertIsNotNone(checkpoint_a)
        self.assertTrue(checkpoint_a.created)
        self.assertTrue(checkpoint_a.changed)
        self.assertTrue(checkpoint_a.locally_committed)
        self.assertFalse(checkpoint_a.remote_synchronized)
        self.assertTrue(result_a.provenance.terminal_committed)
        self.assertEqual(result_a.provenance.execution_record.resulting_state, checkpoint_a.state)
        state_a = checkpoint_a.state
        ref_a = checkpoint_a.ref_name
        del result_a, checkpoint_a

        script = """
import hashlib,json,sys
from peoplebot import ContextPolicy,StateRef,assemble_instance_memory_context
repository,checkout,commit,blueprint_commit=sys.argv[1:]
state=StateRef(repository,commit)
policy=ContextPolicy(StateRef(repository,commit,'memory.json'),16,65536,262144)
assembly=assemble_instance_memory_context(
    checkout,state,'environment:memory-test','instance:memory-test',
    StateRef(repository,blueprint_commit,'blueprint.json'),('decisions.md',),policy
)
content=assembly.documents[0].content
print(json.dumps({'content':content,'sha256':hashlib.sha256(content.encode()).hexdigest()}))
"""
        fresh = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                REPOSITORY,
                str(self.repository),
                state_a.commit,
                self.seed,
            ],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            check=True,
            encoding="utf-8",
            shell=False,
            timeout=30,
        )
        resumed = json.loads(fresh.stdout)
        self.assertEqual(resumed["content"], items_a[0].content)
        self.assertEqual(
            resumed["sha256"],
            hashlib.sha256(items_a[0].content.encode("utf-8")).hexdigest(),
        )

        other_instance = "instance:other"
        other = self.memory_store._checkpoint(
            self.request(
                self.seed_state,
                (MemoryItem("decisions.md", "other Instance\n"),),
                initial=True,
                instance_id=other_instance,
            )
        )
        other_before = git(self.repository, "rev-parse", other.ref_name)

        items_b = (
            items_a[0],
            MemoryItem("progress/current.md", "checkpoint B\n"),
        )
        result_b = run_instance_memory_execution(
            self.runtime_root,
            self.evidence_store,
            self.memory_store,
            self.start(
                "execution:memory-b",
                state_a,
                input_states=(StateRef(REPOSITORY, state_a.commit, "memory/decisions.md"),),
            ),
            self.request(state_a, items_b, initial=False, saved_at="2026-09-10T12:01:00Z"),
            lambda: "2026-09-10T12:01:01Z",
        )
        state_b = result_b.checkpoint.state
        self.assertEqual(git(self.repository, "rev-parse", f"{state_b.commit}^"), state_a.commit)
        self.assertEqual(git(self.repository, "rev-parse", ref_a), state_b.commit)
        self.assertEqual(git(self.repository, "rev-parse", other.ref_name), other_before)
        old = assemble_instance_memory_context(
            self.repository,
            state_a,
            ENVIRONMENT,
            INSTANCE,
            self.blueprint,
            ("progress/current.md",),
            self.policy(state_a),
        )
        new = assemble_instance_memory_context(
            self.repository,
            state_b,
            ENVIRONMENT,
            INSTANCE,
            self.blueprint,
            ("progress/current.md",),
            self.policy(state_b),
        )
        self.assertEqual(old.documents[0].content, "checkpoint A\n")
        self.assertEqual(new.documents[0].content, "checkpoint B\n")

        unchanged = run_instance_memory_execution(
            self.runtime_root,
            self.evidence_store,
            self.memory_store,
            self.start("execution:memory-unchanged", state_b),
            self.request(
                state_b,
                tuple(reversed(items_b)),
                initial=False,
                saved_at="2026-09-10T12:02:00Z",
            ),
            lambda: "2026-09-10T12:02:01Z",
        )
        self.assertFalse(unchanged.checkpoint.changed)
        self.assertEqual(unchanged.checkpoint.state, state_b)
        self.assertEqual(git(self.repository, "rev-parse", ref_a), state_b.commit)
        self.assertTrue(unchanged.provenance.terminal_committed)

    def test_utf8_and_input_bounds_are_enforced(self) -> None:
        exact = "résumé — 状態 — ✅\n"
        checkpoint = self.memory_store._checkpoint(
            self.request(
                self.seed_state,
                (MemoryItem("notes/unicode.md", exact),),
                initial=True,
            )
        )
        assembly = assemble_instance_memory_context(
            self.repository,
            checkpoint.state,
            ENVIRONMENT,
            INSTANCE,
            self.blueprint,
            ("notes/unicode.md",),
            self.policy(checkpoint.state),
        )
        self.assertEqual(assembly.documents[0].content.encode("utf-8"), exact.encode("utf-8"))
        with self.assertRaisesRegex(ValueError, "65536"):
            MemoryItem("too-large.md", "x" * 65_537)
        with self.assertRaisesRegex(ValueError, "NUL"):
            MemoryItem("nul.md", "bad\0content")
        with self.assertRaisesRegex(ValueError, "canonical"):
            MemoryItem("../escape.md", "bad")
        with self.assertRaisesRegex(ValueError, "printable"):
            MemoryItem("control\x7f.md", "bad")

    def test_identity_mismatch_stale_expected_and_competing_publication_are_refused(self) -> None:
        initial = self.memory_store._checkpoint(
            self.request(self.seed_state, (MemoryItem("one.md", "one\n"),), initial=True)
        )
        ref_name = initial.ref_name
        with self.assertRaises(MemoryError) as mismatch:
            self.memory_store._checkpoint(
                self.request(
                    initial.state,
                    (MemoryItem("one.md", "changed\n"),),
                    initial=False,
                    instance_id="instance:not-owner",
                )
            )
        self.assertEqual(mismatch.exception.code, "memory.identity_mismatch")

        with self.assertRaises(MemoryError) as blueprint_mismatch:
            self.memory_store._checkpoint(
                self.request(
                    initial.state,
                    (MemoryItem("one.md", "changed\n"),),
                    initial=False,
                    blueprint=StateRef(REPOSITORY, self.seed, "adapter.json"),
                )
            )
        self.assertEqual(blueprint_mismatch.exception.code, "memory.identity_mismatch")

        first = self.request(
            initial.state,
            (MemoryItem("one.md", "first contender\n"),),
            initial=False,
            saved_at="2026-09-10T12:01:00Z",
        )
        second = self.request(
            initial.state,
            (MemoryItem("one.md", "second contender\n"),),
            initial=False,
            saved_at="2026-09-10T12:01:00Z",
        )
        winner = self.memory_store._checkpoint(first)
        with self.assertRaises(MemoryError) as stale:
            self.memory_store._checkpoint(second)
        self.assertEqual(stale.exception.code, "memory.stale_expected_state")
        self.assertEqual(git(self.repository, "rev-parse", ref_name), winner.state.commit)

    def test_symbolic_destination_and_inspection_failure_preserve_refs(self) -> None:
        symbolic_instance = "instance:symbolic"
        symbolic_ref = instance_memory_ref(ENVIRONMENT, symbolic_instance)
        target_ref = "refs/heads/unrelated-memory-target"
        git(self.repository, "update-ref", target_ref, self.seed)
        git(self.repository, "symbolic-ref", symbolic_ref, target_ref)
        with self.assertRaises(MemoryError) as symbolic:
            self.memory_store._checkpoint(
                self.request(
                    self.seed_state,
                    (MemoryItem("one.md", "one\n"),),
                    initial=True,
                    instance_id=symbolic_instance,
                )
            )
        self.assertEqual(symbolic.exception.code, "memory.destination_symbolic")
        self.assertEqual(git(self.repository, "symbolic-ref", symbolic_ref), target_ref)
        self.assertEqual(git(self.repository, "rev-parse", target_ref), self.seed)

        inspection_instance = "instance:inspection"
        inspection_ref = instance_memory_ref(ENVIRONMENT, inspection_instance)
        real_git = self.memory_store._plumbing._git

        def fail_inspection(*arguments: str, **kwargs: object) -> object:
            if arguments[:3] == ("symbolic-ref", "--quiet", "--no-recurse"):
                return subprocess.CompletedProcess(arguments, 128, b"", b"injected")
            return real_git(*arguments, **kwargs)

        with patch.object(self.memory_store._plumbing, "_git", side_effect=fail_inspection):
            with self.assertRaises(MemoryError) as inspection:
                self.memory_store._checkpoint(
                    self.request(
                        self.seed_state,
                        (MemoryItem("one.md", "one\n"),),
                        initial=True,
                        instance_id=inspection_instance,
                    )
                )
        self.assertEqual(inspection.exception.code, "memory.ref_inspection_failed")
        self.assertNotEqual(0, git_result(self.repository, "show-ref", "--verify", inspection_ref).returncode)

    def test_busy_instance_does_not_run_memory_callback(self) -> None:
        owner = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:memory-owner",
        )
        self.assertTrue(owner.acquired)
        calls = 0
        real_checkpoint = self.memory_store._checkpoint

        def observe(request: MemoryCheckpointRequest) -> object:
            nonlocal calls
            calls += 1
            return real_checkpoint(request)

        try:
            with patch.object(self.memory_store, "_checkpoint", side_effect=observe):
                result = run_instance_memory_execution(
                    self.runtime_root,
                    self.evidence_store,
                    self.memory_store,
                    self.start("execution:memory-busy", self.seed_state),
                    self.request(
                        self.seed_state,
                        (MemoryItem("one.md", "one\n"),),
                        initial=True,
                    ),
                    lambda: "2026-09-10T12:00:01Z",
                )
        finally:
            owner.admission.release()
        self.assertFalse(result.provenance.task_started)
        self.assertIsNone(result.checkpoint)
        self.assertEqual(calls, 0)
        memory_ref = instance_memory_ref(ENVIRONMENT, INSTANCE)
        self.assertNotEqual(0, git_result(self.repository, "show-ref", "--verify", memory_ref).returncode)

    def test_saved_memory_survives_terminal_persistence_failure(self) -> None:
        result = run_instance_memory_execution(
            self.runtime_root,
            _TerminalFailureStore(self.evidence_store),
            self.memory_store,
            self.start("execution:memory-terminal-failure", self.seed_state),
            self.request(
                self.seed_state,
                (MemoryItem("one.md", "saved before terminal failure\n"),),
                initial=True,
            ),
            lambda: "2026-09-10T12:00:01Z",
        )
        self.assertIsNotNone(result.checkpoint)
        self.assertTrue(result.checkpoint.locally_committed)
        self.assertEqual(
            git(self.repository, "rev-parse", result.checkpoint.ref_name),
            result.checkpoint.state.commit,
        )
        self.assertIsNotNone(result.provenance.execution_record)
        self.assertIsNone(result.provenance.terminal_evidence)
        self.assertEqual(
            result.provenance.persistence_failure.code,
            "provenance.injected_terminal_failure",
        )
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:memory-after-terminal-failure",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()

    def test_record_failure_after_save_commits_exact_partial_state_once(self) -> None:
        checkpoint_calls = 0
        timestamp_calls = 0
        real_checkpoint = self.memory_store._checkpoint

        def observe(request: MemoryCheckpointRequest) -> object:
            nonlocal checkpoint_calls
            checkpoint_calls += 1
            return real_checkpoint(request)

        def finished_at() -> str:
            nonlocal timestamp_calls
            timestamp_calls += 1
            if timestamp_calls == 1:
                raise RuntimeError("injected successful-record construction failure")
            return "2026-09-10T12:00:01Z"

        with patch.object(self.memory_store, "_checkpoint", side_effect=observe):
            result = run_instance_memory_execution(
                self.runtime_root,
                self.evidence_store,
                self.memory_store,
                self.start("execution:memory-record-failure", self.seed_state),
                self.request(
                    self.seed_state,
                    (MemoryItem("one.md", "saved before record failure\n"),),
                    initial=True,
                ),
                finished_at,
            )

        self.assertEqual(checkpoint_calls, 1)
        self.assertEqual(timestamp_calls, 2)
        self.assertIsNotNone(result.checkpoint)
        self.assertEqual(
            git(self.repository, "rev-parse", result.checkpoint.ref_name),
            result.checkpoint.state.commit,
        )
        record = result.provenance.execution_record
        self.assertEqual(record.status.value, "failed")
        self.assertEqual(record.resulting_state, None)
        self.assertEqual(record.partial_state, result.checkpoint.state)
        self.assertEqual(record.artifacts, (result.checkpoint.state,))
        self.assertEqual(
            record.terminal_outcome.code,
            "memory.record_failed_after_checkpoint",
        )
        durable = self.evidence_store.read_evidence(result.provenance.terminal_evidence.state)
        self.assertEqual(durable["status"], "failed")
        self.assertEqual(durable["resulting_state"], None)
        self.assertEqual(durable["artifacts"], [result.checkpoint.state.to_dict()])
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:memory-after-record-failure",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()

    def test_checkpoint_preserves_dirty_staged_untracked_source(self) -> None:
        (self.repository / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        (self.repository / "staged.txt").write_text("staged\n", encoding="utf-8")
        git(self.repository, "add", "staged.txt")
        (self.repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        status_before = git(self.repository, "status", "--porcelain=v1", "-uall")
        branch_before = git(self.repository, "rev-parse", "refs/heads/main")

        self.memory_store._checkpoint(
            self.request(
                self.seed_state,
                (MemoryItem("one.md", "one\n"),),
                initial=True,
            )
        )

        self.assertEqual(status_before, git(self.repository, "status", "--porcelain=v1", "-uall"))
        self.assertEqual(branch_before, git(self.repository, "rev-parse", "refs/heads/main"))

    def test_changed_checkpoint_refuses_branch_checked_out_in_calling_worktree(self) -> None:
        initial = self.memory_store._checkpoint(
            self.request(
                self.seed_state,
                (MemoryItem("one.md", "checkpoint A\n"),),
                initial=True,
            )
        )
        git(
            self.repository,
            "checkout",
            initial.ref_name.removeprefix("refs/heads/"),
        )
        memory_file = self.repository / "memory" / "one.md"
        memory_file.write_text("staged user work\n", encoding="utf-8")
        git(self.repository, "add", "memory/one.md")
        memory_file.write_text("unstaged user work\n", encoding="utf-8")
        (self.repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        before = self.worktree_snapshot(self.repository)

        result = run_instance_memory_execution(
            self.runtime_root,
            self.evidence_store,
            self.memory_store,
            self.start("execution:memory-checked-out-calling", initial.state),
            self.request(
                initial.state,
                (MemoryItem("one.md", "checkpoint B\n"),),
                initial=False,
                saved_at="2026-09-10T12:01:00Z",
            ),
            lambda: "2026-09-10T12:01:01Z",
        )

        self.assertIsNone(result.checkpoint)
        self.assertEqual(
            result.provenance.execution_record.terminal_outcome.code,
            "memory.destination_checked_out",
        )
        self.assertEqual(git(self.repository, "rev-parse", initial.ref_name), initial.state.commit)
        self.assertEqual(before, self.worktree_snapshot(self.repository))

        unchanged = run_instance_memory_execution(
            self.runtime_root,
            self.evidence_store,
            self.memory_store,
            self.start("execution:memory-checked-out-unchanged", initial.state),
            self.request(
                initial.state,
                (MemoryItem("one.md", "checkpoint A\n"),),
                initial=False,
                saved_at="2026-09-10T12:02:00Z",
            ),
            lambda: "2026-09-10T12:02:01Z",
        )
        self.assertIsNotNone(unchanged.checkpoint)
        self.assertFalse(unchanged.checkpoint.changed)
        self.assertEqual(unchanged.checkpoint.state, initial.state)
        self.assertEqual(before, self.worktree_snapshot(self.repository))

    def test_changed_checkpoint_refuses_branch_checked_out_in_linked_worktree(self) -> None:
        initial = self.memory_store._checkpoint(
            self.request(
                self.seed_state,
                (MemoryItem("one.md", "checkpoint A\n"),),
                initial=True,
            )
        )
        linked = self.root / "linked"
        git(
            self.repository,
            "worktree",
            "add",
            str(linked),
            initial.ref_name.removeprefix("refs/heads/"),
        )
        try:
            (linked / "memory" / "one.md").write_text("linked dirty work\n", encoding="utf-8")
            (linked / "untracked.txt").write_text("linked untracked\n", encoding="utf-8")
            before = self.worktree_snapshot(linked)
            source_before = self.worktree_snapshot(self.repository)
            result = run_instance_memory_execution(
                self.runtime_root,
                self.evidence_store,
                self.memory_store,
                self.start("execution:memory-checked-out-linked", initial.state),
                self.request(
                    initial.state,
                    (MemoryItem("one.md", "checkpoint B\n"),),
                    initial=False,
                    saved_at="2026-09-10T12:01:00Z",
                ),
                lambda: "2026-09-10T12:01:01Z",
            )
            self.assertIsNone(result.checkpoint)
            self.assertEqual(
                result.provenance.execution_record.terminal_outcome.code,
                "memory.destination_checked_out",
            )
            self.assertEqual(git(self.repository, "rev-parse", initial.ref_name), initial.state.commit)
            self.assertEqual(before, self.worktree_snapshot(linked))
            self.assertEqual(source_before, self.worktree_snapshot(self.repository))
        finally:
            git(self.repository, "worktree", "remove", "--force", str(linked))

    def test_initial_checkpoint_refuses_unborn_checked_out_branch(self) -> None:
        unborn_instance = "instance:unborn"
        ref_name = instance_memory_ref(ENVIRONMENT, unborn_instance)
        unborn = self.root / "unborn"
        git(
            self.repository,
            "worktree",
            "add",
            "--orphan",
            "-b",
            ref_name.removeprefix("refs/heads/"),
            str(unborn),
        )
        try:
            before = self.worktree_snapshot(unborn)
            result = run_instance_memory_execution(
                self.runtime_root,
                self.evidence_store,
                self.memory_store,
                self.start(
                    "execution:memory-checked-out-unborn",
                    self.seed_state,
                    instance_id=unborn_instance,
                ),
                self.request(
                    self.seed_state,
                    (MemoryItem("one.md", "initial memory\n"),),
                    initial=True,
                    instance_id=unborn_instance,
                ),
                lambda: "2026-09-10T12:00:01Z",
            )
            self.assertIsNone(result.checkpoint)
            self.assertEqual(
                result.provenance.execution_record.terminal_outcome.code,
                "memory.destination_checked_out",
            )
            self.assertNotEqual(
                0,
                git_result(self.repository, "show-ref", "--verify", ref_name).returncode,
            )
            self.assertEqual(before, self.worktree_snapshot(unborn))
        finally:
            git(self.repository, "worktree", "remove", "--force", str(unborn))

    def test_worktree_inspection_failure_preserves_destination(self) -> None:
        real_git = self.memory_store._plumbing._git

        def fail_worktree_inspection(*arguments: str, **kwargs: object) -> object:
            if arguments == ("worktree", "list", "--porcelain", "-z"):
                return subprocess.CompletedProcess(arguments, 128, b"", b"injected")
            return real_git(*arguments, **kwargs)

        memory_ref = instance_memory_ref(ENVIRONMENT, INSTANCE)
        worktrees_before = git(self.repository, "worktree", "list", "--porcelain")
        with patch.object(
            self.memory_store._plumbing,
            "_git",
            side_effect=fail_worktree_inspection,
        ):
            result = run_instance_memory_execution(
                self.runtime_root,
                self.evidence_store,
                self.memory_store,
                self.start("execution:memory-worktree-inspection", self.seed_state),
                self.request(
                    self.seed_state,
                    (MemoryItem("one.md", "initial memory\n"),),
                    initial=True,
                ),
                lambda: "2026-09-10T12:00:01Z",
            )
        self.assertIsNone(result.checkpoint)
        self.assertEqual(
            result.provenance.execution_record.terminal_outcome.code,
            "memory.worktree_inspection_failed",
        )
        self.assertNotEqual(0, git_result(self.repository, "show-ref", "--verify", memory_ref).returncode)
        self.assertEqual(worktrees_before, git(self.repository, "worktree", "list", "--porcelain"))


class PartialCloneMemoryTests(unittest.TestCase):
    def test_missing_pinned_memory_objects_do_not_fetch_or_create_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin"
            origin.mkdir()
            git(origin, "init", "-b", "main")
            git(origin, "config", "user.name", "PeopleBot Test")
            git(origin, "config", "user.email", "test@example.invalid")
            (origin / "memory").mkdir()
            (origin / "memory" / "one.md").write_text("promised memory\n", encoding="utf-8")
            item_blob = git(origin, "hash-object", "-w", "memory/one.md")
            blueprint = StateRef(REPOSITORY, "0" * 40, "blueprint.json")
            metadata = {
                "blueprint": blueprint.to_dict(),
                "environment_id": ENVIRONMENT,
                "format": "peoplebot.instance-memory.v0",
                "instance_id": INSTANCE,
                "items": [
                    {
                        "bytes": len(b"promised memory\n"),
                        "git_path": "memory/one.md",
                        "object_id": item_blob,
                        "path": "one.md",
                        "sha256": hashlib.sha256(b"promised memory\n").hexdigest(),
                    }
                ],
                "ref_name": instance_memory_ref(ENVIRONMENT, INSTANCE),
                "repository": REPOSITORY,
            }
            metadata_bytes = (
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                + "\n"
            ).encode("utf-8")
            (origin / "memory.json").write_bytes(metadata_bytes)
            git(origin, "add", ".")
            git(origin, "commit", "-m", "memory fixture")
            git(origin, "config", "uploadpack.allowFilter", "true")
            commit = git(origin, "rev-parse", "HEAD")
            metadata_blob = git(origin, "rev-parse", "HEAD:memory.json")
            partial = root / "partial"
            subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "clone",
                    "--filter=blob:none",
                    "--no-checkout",
                    origin.resolve().as_uri(),
                    str(partial),
                ],
                capture_output=True,
                check=True,
                shell=False,
                timeout=30,
            )
            missing_before = subprocess.run(
                ["git", "--no-lazy-fetch", "-C", str(partial), "cat-file", "-e", item_blob],
                capture_output=True,
                check=False,
                shell=False,
                timeout=15,
            )
            self.assertNotEqual(0, missing_before.returncode)
            written = subprocess.run(
                ["git", "--no-lazy-fetch", "-C", str(partial), "hash-object", "-w", "--stdin"],
                input=metadata_bytes,
                capture_output=True,
                check=True,
                shell=False,
                timeout=15,
            )
            self.assertEqual(written.stdout.decode("ascii").strip(), metadata_blob)
            worktrees_before = git(partial, "worktree", "list", "--porcelain")
            state = StateRef(REPOSITORY, commit)
            policy = ContextPolicy(StateRef(REPOSITORY, commit, "memory.json"), 2, 65_536, 131_072)
            trace = root / "trace.json"
            with patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}, clear=False):
                with self.assertRaises(MemoryError) as raised:
                    assemble_instance_memory_context(
                        partial,
                        state,
                        ENVIRONMENT,
                        INSTANCE,
                        blueprint,
                        ("one.md",),
                        policy,
                    )
            self.assertEqual(raised.exception.code, "context.object_unavailable")
            missing_after = subprocess.run(
                ["git", "--no-lazy-fetch", "-C", str(partial), "cat-file", "-e", item_blob],
                capture_output=True,
                check=False,
                shell=False,
                timeout=15,
            )
            self.assertNotEqual(0, missing_after.returncode)
            self.assertEqual(worktrees_before, git(partial, "worktree", "list", "--porcelain"))
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            remote_children = [
                event
                for event in events
                if event.get("event") == "child_start"
                and (
                    event.get("child_class") == "promisor-remote"
                    or any(
                        token in {"fetch", "upload-pack"} or token.endswith("git-fetch")
                        for token in event.get("argv", [])
                    )
                )
            ]
            self.assertEqual([], remote_children)


if __name__ == "__main__":
    unittest.main()
