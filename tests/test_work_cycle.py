from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from peoplebot import (
    ContextPolicy,
    ExecutionStart,
    GitAttemptStore,
    GitMemoryStore,
    MemoryCheckpointRequest,
    MemoryItem,
    StateRef,
    assemble_instance_memory_context,
    run_instance_memory_execution,
    try_acquire_execution,
)
from peoplebot.messaging import (
    Message,
    MessageKind,
    MessagePublication,
    OwnedMessagePublication,
    OutboundDestination,
    OutboundMessageStore,
    PeerSource,
    PublicationDisposition,
    append_owned_message,
    outbound_message_ref,
    publish_message,
    read_peer_messages,
    validate_correlated_reply,
)
from peoplebot.work_cycle import (
    CycleBindings,
    CycleError,
    ReaderProgressStore,
    TaskDisposition,
    TaskHandlerResult,
    TaskPolicy,
    TaskRoute,
    reader_progress_ref,
    run_work_cycle_tick,
)


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        check=True,
        encoding="utf-8",
        shell=False,
        timeout=15,
    ).stdout.strip()


@unittest.skipUnless(os.name == "nt", "work-cycle admission uses Windows lock")
class WorkCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.a = self._repository("a")
        self.b = self._repository("b")
        self.a_remote = self.root / "a-outbound.git"
        self.b_remote = self.root / "b-outbound.git"
        git(self.root, "init", "--bare", str(self.a_remote))
        git(self.root, "init", "--bare", str(self.b_remote))
        git(self.a, "remote", "add", "outbound", str(self.a_remote))
        git(self.a, "remote", "add", "peer-b", str(self.b_remote))
        git(self.b, "remote", "add", "outbound", str(self.b_remote))
        git(self.b, "remote", "add", "peer-a", str(self.a_remote))
        self.a_id = "environment:a"
        self.b_id = "environment:b"
        self.instance = "instance:b-worker"
        self.a_repo = "https://example.test/a-outbound"
        self.b_repo = "https://example.test/b-outbound"
        self.a_ref = outbound_message_ref(self.a_id)
        self.b_ref = outbound_message_ref(self.b_id)
        self.status_path = self.root / "status" / "cycle.json"
        self.stop_path = self.root / "control" / "stop.request"
        self.bindings = CycleBindings(
            environment_id=self.b_id,
            instance_id=self.instance,
            runtime_root=self.runtime,
            local_checkout=self.b,
            local_repository=self.b_repo,
            outbound_ref=self.b_ref,
            destination=OutboundDestination(
                self.b_repo, "outbound", str(self.b_remote), self.b_ref
            ),
            sources=(
                PeerSource(
                    self.a_repo,
                    "peer-a",
                    str(self.a_remote),
                    self.a_ref,
                    (self.a_id,),
                ),
            ),
            progress_ref=reader_progress_ref(self.b_id, self.instance),
            status_path=self.status_path,
            stop_path=self.stop_path,
        )
        self.policy = TaskPolicy((TaskRoute("fixture.task", "fixture.complete"),), 10)

    def _write_configuration(
        self, name: str, bindings_override: CycleBindings | None = None
    ) -> tuple[Path, Path]:
        cycle_bindings = bindings_override or self.bindings
        bindings = {
            "destination": {
                "expected_url": cycle_bindings.destination.expected_url,
                "ref_name": cycle_bindings.destination.ref_name,
                "remote": cycle_bindings.destination.remote,
                "repository": cycle_bindings.destination.repository,
            },
            "environment_id": cycle_bindings.environment_id,
            "format": "peoplebot.cycle-bindings.v0",
            "instance_id": cycle_bindings.instance_id,
            "local_checkout": str(cycle_bindings.local_checkout),
            "local_repository": cycle_bindings.local_repository,
            "outbound_ref": cycle_bindings.outbound_ref,
            "progress_ref": cycle_bindings.progress_ref,
            "runtime_root": str(cycle_bindings.runtime_root),
            "sources": [
                {
                    "allowed_senders": list(source.allowed_senders),
                    "expected_url": source.expected_url,
                    "ref_name": source.ref_name,
                    "remote": source.remote,
                    "repository": source.repository,
                }
                for source in cycle_bindings.sources
            ],
            "status_path": str(cycle_bindings.status_path),
            "stop_path": str(cycle_bindings.stop_path),
        }
        policy = {
            "allow_stop_messages": self.policy.allow_stop_messages,
            "format": "peoplebot.task-policy.v0",
            "maximum_completed_tasks": self.policy.maximum_completed_tasks,
            "routes": [
                {"handler": route.handler, "purpose": route.purpose}
                for route in self.policy.routes
            ],
        }
        bindings_path = self.root / f"{name}-bindings.json"
        policy_path = self.root / f"{name}-policy.json"
        bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        return bindings_path, policy_path

    def _fresh_process(
        self,
        message_id: str,
        disposition: str,
        counter: Path,
        execution_id: str,
        *,
        blueprint: StateRef | None = None,
        expected_memory: str | None = None,
        expected_reply_commit: str | None = None,
    ) -> dict:
        bindings, policy = self._write_configuration(execution_id.replace(":", "-"))
        arguments = [
            sys.executable,
            "tests/work_cycle_process_helper.py",
            "--bindings",
            str(bindings),
            "--policy",
            str(policy),
            "--message-id",
            message_id,
            "--expected-disposition",
            disposition,
            "--counter",
            str(counter),
            "--execution-id",
            execution_id,
            "--started-at",
            "2026-09-14T01:01:00Z",
            "--finished-at",
            "2026-09-14T01:01:01Z",
        ]
        if expected_memory is not None:
            assert blueprint is not None
            arguments.extend(
                (
                    "--blueprint-repository",
                    blueprint.repository,
                    "--blueprint-commit",
                    blueprint.commit,
                    "--blueprint-path",
                    blueprint.path,
                    "--memory-path",
                    "task/progress.txt",
                    "--expected-memory",
                    expected_memory,
                )
            )
        if expected_reply_commit is not None:
            arguments.extend(("--expected-reply-commit", expected_reply_commit))
        result = subprocess.run(
            arguments,
            cwd=Path(__file__).parents[1],
            capture_output=True,
            check=False,
            encoding="utf-8",
            shell=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _repository(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        git(path, "init", "-b", "main")
        git(path, "config", "user.name", "PeopleBot Test")
        git(path, "config", "user.email", "test@example.invalid")
        for filename in ("blueprint.txt", "adapter.txt", "policy.json"):
            (path / filename).write_text(f"{name} {filename}\n", encoding="utf-8")
        git(path, "add", ".")
        git(path, "commit", "-m", "fixture baseline")
        return path

    def _publish_task(self, suffix: str = "001", kind=MessageKind.TASK) -> Message:
        message = Message(
            message_id=f"message-{suffix}",
            kind=kind,
            sender=self.a_id,
            recipient=self.b_id,
            task_id=f"task-{suffix}",
            correlation_id=f"correlation-{suffix}",
            purpose="fixture.task" if kind is MessageKind.TASK else "cycle.stop",
            content="Run the locally approved synthetic handler.",
            created_at=f"2026-09-14T01:00:{int(suffix):02d}Z",
        )
        store = OutboundMessageStore(self.a, self.a_repo, self.a_ref)
        before = None
        existing = store.messages()
        if existing:
            before = existing[-1].state.commit
        local = append_owned_message(
            self.runtime,
            self.a_id,
            f"execution:publish-{suffix}",
            store,
            message,
        )
        result = publish_message(
            self.a,
            local,
            OutboundDestination(self.a_repo, "outbound", str(self.a_remote), self.a_ref),
            before,
        )
        self.assertEqual(result.code, "message.remote_verified")
        return message

    def _initialize_memory(self):
        base = git(self.b, "rev-parse", "HEAD")
        blueprint = StateRef(self.b_repo, base, "blueprint.txt")
        adapter = StateRef(self.b_repo, base, "adapter.txt")
        request = MemoryCheckpointRequest(
            repository=self.b_repo,
            environment_id=self.b_id,
            instance_id=self.instance,
            blueprint=blueprint,
            expected_state=StateRef(self.b_repo, base),
            items=(MemoryItem("task/progress.txt", "No task completed.\n"),),
            saved_at="2026-09-14T00:59:58Z",
            initial=True,
        )
        start = ExecutionStart(
            execution_id="execution:memory-initial",
            environment_id=self.b_id,
            instance_id=self.instance,
            objective="Initialize synthetic work-cycle memory.",
            started_at="2026-09-14T00:59:57Z",
            starting_state=request.expected_state,
            blueprint=blueprint,
            adapter=adapter,
        )
        result = run_instance_memory_execution(
            self.runtime,
            GitAttemptStore(self.b, self.b_repo),
            GitMemoryStore(self.b, self.b_repo),
            start,
            request,
            lambda: "2026-09-14T00:59:59Z",
        )
        self.assertTrue(result.provenance.terminal_committed)
        return result.checkpoint.state, blueprint, adapter

    def test_actionable_reply_memory_and_fresh_process_do_not_redispatch(self) -> None:
        task = self._publish_task()
        memory_state, blueprint, adapter = self._initialize_memory()
        handler_calls: list[str] = []
        counter = self.root / "completed-dispatch-count.txt"
        counter.write_text("0", encoding="ascii")

        def handler(message: Message) -> TaskHandlerResult:
            handler_calls.append(message.message_id)
            counter.write_text("1", encoding="ascii")
            return TaskHandlerResult(
                TaskDisposition.COMPLETED,
                "Synthetic task completed.",
                memory_items=(("task/progress.txt", "Task message-001 completed.\n"),),
            )

        def checkpoint(published, result):
            nonlocal memory_state
            request = MemoryCheckpointRequest(
                repository=self.b_repo,
                environment_id=self.b_id,
                instance_id=self.instance,
                blueprint=blueprint,
                expected_state=memory_state,
                items=tuple(MemoryItem(path, content) for path, content in result.memory_items),
                saved_at="2026-09-14T01:00:02Z",
            )
            start = ExecutionStart(
                execution_id="execution:memory-task-001",
                environment_id=self.b_id,
                instance_id=self.instance,
                objective="Save selected synthetic task progress.",
                started_at="2026-09-14T01:00:01Z",
                starting_state=memory_state,
                blueprint=blueprint,
                adapter=adapter,
                input_messages=(published.state,),
            )
            saved = run_instance_memory_execution(
                self.runtime,
                GitAttemptStore(self.b, self.b_repo),
                GitMemoryStore(self.b, self.b_repo),
                start,
                request,
                lambda: "2026-09-14T01:00:03Z",
            )
            self.assertTrue(saved.provenance.terminal_committed)
            memory_state = saved.checkpoint.state
            return memory_state

        status = run_work_cycle_tick(
            self.bindings,
            self.policy,
            {"fixture.complete": handler},
            "execution:tick-active",
            "2026-09-14T01:00:00Z",
            "2026-09-14T01:00:04Z",
            memory_checkpoint=checkpoint,
        )
        self.assertEqual(status.code, "cycle.completed")
        self.assertEqual(handler_calls, [task.message_id])
        self.assertEqual(json.loads(self.status_path.read_text())["provider_invoked"], False)

        _, replies = read_peer_messages(
            self.a,
            PeerSource(self.b_repo, "peer-b", str(self.b_remote), self.b_ref, (self.b_id,)),
        )
        validate_correlated_reply(replies[-1].message, task)
        self.assertEqual(replies[-1].state, status.reply_state)

        context = assemble_instance_memory_context(
            self.b,
            memory_state,
            self.b_id,
            self.instance,
            blueprint,
            ("task/progress.txt",),
            ContextPolicy(
                StateRef(self.b_repo, memory_state.commit, "memory.json"), 1, 4096, 4096
            ),
        )
        self.assertEqual(context.documents[0].content, "Task message-001 completed.\n")

        recovered = self._fresh_process(
            task.message_id,
            TaskDisposition.COMPLETED.value,
            counter,
            "execution:fresh-completed",
            blueprint=blueprint,
            expected_memory="Task message-001 completed.\n",
        )
        self.assertEqual(recovered["status"]["code"], "cycle.idle")
        self.assertEqual(recovered["counter"], 1)
        self.assertEqual(handler_calls, [task.message_id])

    def test_logical_owner_blocks_post_handler_competing_tick(self) -> None:
        self._publish_task("001")
        self._publish_task("002")
        checkpoint_entered = threading.Event()
        continue_checkpoint = threading.Event()
        calls: list[str] = []
        first_result: list[object] = []

        def handler(message: Message) -> TaskHandlerResult:
            calls.append(message.message_id)
            return TaskHandlerResult(
                TaskDisposition.COMPLETED,
                "Synthetic task completed.",
                memory_items=(("gap.txt", "post-handler gap\n"),),
            )

        def checkpoint(_published, _result):
            checkpoint_entered.set()
            if not continue_checkpoint.wait(10):
                raise RuntimeError("fixture checkpoint wait expired")
            return None

        def first_tick() -> None:
            first_result.append(
                run_work_cycle_tick(
                    self.bindings,
                    self.policy,
                    {"fixture.complete": handler},
                    "execution:tick-gap-owner",
                    "2026-09-14T01:00:00Z",
                    "2026-09-14T01:00:03Z",
                    memory_checkpoint=checkpoint,
                )
            )

        thread = threading.Thread(target=first_tick)
        thread.start()
        self.assertTrue(checkpoint_entered.wait(10))
        contender = run_work_cycle_tick(
            self.bindings,
            self.policy,
            {"fixture.complete": handler},
            "execution:tick-gap-contender",
            "2026-09-14T01:00:01Z",
            "2026-09-14T01:00:02Z",
        )
        self.assertEqual(contender.code, "cycle.busy")
        self.assertEqual(calls, ["message-001"])
        continue_checkpoint.set()
        thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(first_result[0].code, "cycle.completed")

        follow_up = run_work_cycle_tick(
            self.bindings,
            self.policy,
            {"fixture.complete": handler},
            "execution:tick-gap-follow-up",
            "2026-09-14T01:00:04Z",
            "2026-09-14T01:00:05Z",
        )
        self.assertEqual(follow_up.code, "cycle.completed")
        self.assertEqual(calls, ["message-001", "message-002"])

    def test_busy_stopped_exhausted_and_unresolved_never_duplicate_work(self) -> None:
        self._publish_task()
        calls: list[str] = []

        def handler(message: Message) -> TaskHandlerResult:
            calls.append(message.message_id)
            return TaskHandlerResult(TaskDisposition.COMPLETED, "Completed.")

        owner = try_acquire_execution(
            self.runtime, self.b_id, self.instance, "execution:busy-owner"
        )
        self.assertTrue(owner.acquired)
        try:
            busy = run_work_cycle_tick(
                self.bindings,
                self.policy,
                {"fixture.complete": handler},
                "execution:tick-busy",
                "2026-09-14T01:00:00Z",
                "2026-09-14T01:00:01Z",
            )
        finally:
            owner.admission.release()
        self.assertEqual(busy.code, "cycle.busy")
        self.assertEqual(calls, [])

        completed = run_work_cycle_tick(
            self.bindings,
            self.policy,
            {"fixture.complete": handler},
            "execution:tick-complete",
            "2026-09-14T01:00:02Z",
            "2026-09-14T01:00:03Z",
        )
        self.assertEqual(completed.code, "cycle.completed")
        duplicate = run_work_cycle_tick(
            self.bindings,
            self.policy,
            {"fixture.complete": handler},
            "execution:tick-duplicate",
            "2026-09-14T01:00:04Z",
            "2026-09-14T01:00:05Z",
        )
        self.assertEqual(duplicate.code, "cycle.idle")
        self.assertEqual(calls, ["message-001"])

        exhausted = run_work_cycle_tick(
            self.bindings,
            TaskPolicy((TaskRoute("fixture.task", "fixture.complete"),), 1),
            {"fixture.complete": handler},
            "execution:tick-exhausted",
            "2026-09-14T01:00:05Z",
            "2026-09-14T01:00:06Z",
        )
        self.assertEqual(exhausted.code, "cycle.exhausted")
        self.assertFalse(exhausted.provider_invoked)

        self._publish_task("002")
        failed_calls: list[str] = []
        unresolved_counter = self.root / "unresolved-dispatch-count.txt"
        unresolved_counter.write_text("0", encoding="ascii")

        def fail(message: Message) -> TaskHandlerResult:
            failed_calls.append(message.message_id)
            unresolved_counter.write_text("1", encoding="ascii")
            raise RuntimeError("synthetic interruption")

        unresolved = run_work_cycle_tick(
            self.bindings,
            self.policy,
            {"fixture.complete": fail},
            "execution:tick-unresolved",
            "2026-09-14T01:00:06Z",
            "2026-09-14T01:00:07Z",
        )
        self.assertEqual(unresolved.code, "cycle.unresolved")
        resumed = self._fresh_process(
            "message-002",
            TaskDisposition.UNRESOLVED.value,
            unresolved_counter,
            "execution:fresh-unresolved",
        )
        self.assertEqual(resumed["status"]["code"], "cycle.unresolved_halt")
        self.assertTrue(resumed["progress_unchanged"])
        self.assertEqual(resumed["counter"], 1)
        self.assertEqual(failed_calls, ["message-002"])

        self.stop_path.parent.mkdir(exist_ok=True)
        self.stop_path.write_text("stop\n", encoding="utf-8")
        stopped = run_work_cycle_tick(
            self.bindings,
            self.policy,
            {"fixture.complete": handler},
            "execution:tick-stopped",
            "2026-09-14T01:00:10Z",
            "2026-09-14T01:00:11Z",
        )
        self.assertEqual(stopped.code, "cycle.stopped")
        self.assertFalse(stopped.work_invoked)

    def test_uncertain_publication_halts_fresh_process_with_waiting_task(self) -> None:
        self._publish_task("001")
        self._publish_task("002")
        counter = self.root / "uncertain-publication-dispatch-count.txt"
        counter.write_text("0", encoding="ascii")

        def handler(_message: Message) -> TaskHandlerResult:
            counter.write_text("1", encoding="ascii")
            return TaskHandlerResult(TaskDisposition.COMPLETED, "Completed before uncertainty.")

        def uncertain_publication(
            _runtime_root,
            _environment_id,
            _execution_id,
            store,
            message,
            _destination,
        ):
            local = store.append(message)
            return OwnedMessagePublication(
                local,
                MessagePublication(
                    "message.publication_uncertain",
                    PublicationDisposition.UNCERTAIN,
                    local.state,
                    None,
                    None,
                ),
            )

        with mock.patch(
            "peoplebot.work_cycle.append_and_publish_owned_message",
            side_effect=uncertain_publication,
        ):
            unresolved = run_work_cycle_tick(
                self.bindings,
                self.policy,
                {"fixture.complete": handler},
                "execution:tick-publication-uncertain",
                "2026-09-14T01:00:00Z",
                "2026-09-14T01:00:03Z",
            )
        self.assertEqual(unresolved.code, "cycle.unresolved")
        self.assertIsNotNone(unresolved.reply_state)
        local_reply_count = len(OutboundMessageStore(self.b, self.b_repo, self.b_ref).messages())

        recovered = self._fresh_process(
            "message-001",
            TaskDisposition.UNRESOLVED.value,
            counter,
            "execution:fresh-publication-uncertain",
            expected_reply_commit=unresolved.reply_state.commit,
        )
        self.assertEqual(recovered["status"]["code"], "cycle.unresolved_halt")
        self.assertEqual(recovered["status"]["message_state"], unresolved.message_state.to_dict())
        self.assertEqual(recovered["status"]["reply_state"], unresolved.reply_state.to_dict())
        self.assertTrue(recovered["progress_unchanged"])
        self.assertEqual(recovered["counter"], 1)
        self.assertEqual(
            len(OutboundMessageStore(self.b, self.b_repo, self.b_ref).messages()),
            local_reply_count,
        )

    def test_permitted_stop_is_a_durable_fresh_process_halt(self) -> None:
        stop = self._publish_task("001", MessageKind.STOP)
        self._publish_task("002")
        counter = self.root / "stopped-dispatch-count.txt"
        counter.write_text("0", encoding="ascii")

        def unexpected_handler(_message: Message) -> TaskHandlerResult:
            raise AssertionError("stop handling must not invoke a task handler")

        stopped = run_work_cycle_tick(
            self.bindings,
            self.policy,
            {"fixture.complete": unexpected_handler},
            "execution:tick-stop-message",
            "2026-09-14T01:00:00Z",
            "2026-09-14T01:00:03Z",
        )
        self.assertEqual(stopped.code, "cycle.stopped")
        self.assertIsNotNone(stopped.reply_state)

        recovered = self._fresh_process(
            stop.message_id,
            TaskDisposition.STOPPED.value,
            counter,
            "execution:fresh-stopped",
            expected_reply_commit=stopped.reply_state.commit,
        )
        self.assertEqual(recovered["status"]["code"], "cycle.stopped_halt")
        self.assertEqual(recovered["status"]["message_state"], stopped.message_state.to_dict())
        self.assertEqual(recovered["status"]["reply_state"], stopped.reply_state.to_dict())
        self.assertTrue(recovered["progress_unchanged"])
        self.assertEqual(recovered["counter"], 0)

    def test_final_progress_conflict_preserves_result_and_does_not_repeat_effects(self) -> None:
        self._publish_task("001")
        handler_calls: list[str] = []

        def handler(message: Message) -> TaskHandlerResult:
            handler_calls.append(message.message_id)
            return TaskHandlerResult(TaskDisposition.COMPLETED, "Known completed result.")

        original_save = ReaderProgressStore.save
        save_calls = 0
        competing_states = []

        def save_with_conflict(store, progress, expected, saved_at):
            nonlocal save_calls
            save_calls += 1
            if save_calls == 2:
                assert expected is not None
                competing_states.append(
                    original_save(
                        store,
                        expected.progress,
                        expected,
                        "2026-09-14T01:00:02Z",
                    )
                )
            return original_save(store, progress, expected, saved_at)

        with mock.patch.object(
            ReaderProgressStore,
            "save",
            autospec=True,
            side_effect=save_with_conflict,
        ):
            status = run_work_cycle_tick(
                self.bindings,
                self.policy,
                {"fixture.complete": handler},
                "execution:tick-terminal-progress-conflict",
                "2026-09-14T01:00:00Z",
                "2026-09-14T01:00:03Z",
            )

        self.assertEqual(status.code, "cycle.terminal_progress_persist_failed")
        self.assertEqual(status.original_code, "cycle.completed")
        self.assertEqual(status.original_disposition, "completed")
        self.assertFalse(status.terminal_progress_persisted)
        self.assertTrue(status.status_persisted)
        self.assertIsNotNone(status.message_state)
        self.assertIsNotNone(status.reply_state)
        self.assertEqual(handler_calls, ["message-001"])
        current = ReaderProgressStore(
            self.b, self.b_repo, self.bindings.progress_ref
        ).load()
        self.assertIsNotNone(current)
        self.assertEqual(current.state, competing_states[0].state)
        self.assertEqual(
            json.loads(self.status_path.read_text(encoding="utf-8")), status.to_dict()
        )
        _, replies = read_peer_messages(
            self.a,
            PeerSource(self.b_repo, "peer-b", str(self.b_remote), self.b_ref, (self.b_id,)),
        )
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].state, status.reply_state)

        blocked = run_work_cycle_tick(
            self.bindings,
            self.policy,
            {"fixture.complete": handler},
            "execution:tick-after-terminal-progress-conflict",
            "2026-09-14T01:00:04Z",
            "2026-09-14T01:00:05Z",
        )
        self.assertEqual(blocked.code, "cycle.claimed_unresolved")
        self.assertEqual(handler_calls, ["message-001"])
        _, replies_after = read_peer_messages(
            self.a,
            PeerSource(self.b_repo, "peer-b", str(self.b_remote), self.b_ref, (self.b_id,)),
        )
        self.assertEqual(len(replies_after), 1)

    def test_cli_emits_non_durable_diagnostic_when_status_write_fails(self) -> None:
        self._publish_task("001")
        invalid_status_target = self.root / "status-target-is-a-directory"
        invalid_status_target.mkdir()
        bindings = replace(self.bindings, status_path=invalid_status_target)
        bindings_path, policy_path = self._write_configuration(
            "status-write-failure", bindings
        )
        result = subprocess.run(
            (
                sys.executable,
                "-m",
                "peoplebot",
                "work-cycle-tick",
                "--bindings",
                str(bindings_path),
                "--policy",
                str(policy_path),
                "--execution-id",
                "execution:cli-status-write-failure",
                "--started-at",
                "2026-09-14T01:00:00Z",
                "--finished-at",
                "2026-09-14T01:00:03Z",
                "--offline-fixture",
            ),
            cwd=Path(__file__).parents[1],
            capture_output=True,
            check=False,
            encoding="utf-8",
            shell=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 13, result.stderr)
        diagnostic = json.loads(result.stdout)
        self.assertEqual(diagnostic["code"], "cycle.status_persist_failed")
        self.assertEqual(diagnostic["original_code"], "cycle.completed")
        self.assertEqual(diagnostic["original_disposition"], "completed")
        self.assertTrue(diagnostic["terminal_progress_persisted"])
        self.assertFalse(diagnostic["status_persisted"])
        self.assertIsNotNone(diagnostic["message_state"])
        self.assertIsNotNone(diagnostic["reply_state"])
        self.assertTrue(invalid_status_target.is_dir())
        progress = ReaderProgressStore(self.b, self.b_repo, self.bindings.progress_ref).load()
        self.assertEqual(progress.progress.tasks[0].disposition, TaskDisposition.COMPLETED)
        _, replies = read_peer_messages(
            self.a,
            PeerSource(self.b_repo, "peer-b", str(self.b_remote), self.b_ref, (self.b_id,)),
        )
        self.assertEqual(len(replies), 1)

    def test_interrupted_claim_is_durable_and_blocks_fresh_process_redispatch(self) -> None:
        self._publish_task()
        counter = self.root / "interrupted-dispatch-count.txt"
        counter.write_text("0", encoding="ascii")

        def interrupt(_message: Message) -> TaskHandlerResult:
            counter.write_text("1", encoding="ascii")
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            run_work_cycle_tick(
                self.bindings,
                self.policy,
                {"fixture.complete": interrupt},
                "execution:tick-interrupted",
                "2026-09-14T01:00:00Z",
                "2026-09-14T01:00:01Z",
            )
        recovered = self._fresh_process(
            "message-001",
            TaskDisposition.CLAIMED.value,
            counter,
            "execution:fresh-interrupted",
        )
        self.assertEqual(recovered["status"]["code"], "cycle.claimed_unresolved")
        self.assertEqual(recovered["counter"], 1)


if __name__ == "__main__":
    unittest.main()
