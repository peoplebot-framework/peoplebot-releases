from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from peoplebot import (
    ContextPolicy,
    CycleBindings,
    OutboundDestination,
    PeerSource,
    StateRef,
    TaskPolicy,
    TaskRoute,
    assemble_instance_memory_context,
    outbound_message_ref,
    reader_progress_ref,
    try_acquire_execution,
)
from peoplebot.adapters.project_review import (
    ProjectReviewAdapter,
    ProjectReviewFinding,
    ProjectReviewResponse,
)
from peoplebot.adapters.codex_read_only import ProcessOwnershipUnresolved
from peoplebot.development import (
    CodexDevelopmentAdapter,
    DevelopmentAuthority,
    DevelopmentError,
    DevelopmentCycleOperations,
    DevelopmentProgressStore,
    DevelopmentTask,
    DevelopmentWorkflowHandler,
    ExactProjectReviewer,
    ReviewResult,
    _diagnose_development_process_failure,
    create_coordination_reply_file,
    development_progress_ref,
    import_selected_coordination_task,
    load_development_task,
    publish_coordination_reply_file,
    render_coordination_reply,
    run_development_cycle_command,
)
from peoplebot.messaging import Message, MessageKind
from peoplebot.work_cycle import ReaderProgressStore, TaskDisposition


def git(path: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", "-C", str(path), *arguments), capture_output=True,
        check=False, text=True, encoding="utf-8",
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class FakeCodex:
    def __init__(self) -> None:
        self.invocations = 0
        self.prompts: list[dict] = []
        self.timeouts: list[int] = []

    def __call__(self, command, input_bytes, environment, timeout_seconds):
        del environment
        self.timeouts.append(timeout_seconds)
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, b"codex-cli 0.fake\n", b"")
        self.invocations += 1
        self.prompts.append(json.loads(input_bytes))
        worktree = Path(command[command.index("-C") + 1])
        target = worktree / "src" / "candidate.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        content = "verified candidate\n" if self.invocations == 1 else "verified candidate corrected\n"
        target.write_text(content, encoding="utf-8")
        events = (
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 4,
            }},
        )
        stdout = b"\n".join(json.dumps(item).encode("utf-8") for item in events) + b"\n"
        return subprocess.CompletedProcess(command, 0, stdout, b"")


class FailingCodex:
    def __init__(self) -> None:
        self.invocations = 0
        self.stdout = b"\n".join((
            b'{"type":"thread.started","thread_id":"private-thread"}',
            b'{"type":"turn.started"}',
            b'{"type":"turn.failed","error":{"type":"authentication_error","message":"Bearer raw-secret at https://provider.invalid/private"}}',
            b'{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":3,"output_tokens":2}}',
        )) + b"\n"
        self.stderr = b"Authorization: Bearer stderr-secret; https://provider.invalid/private\n"

    def __call__(self, command, input_bytes, environment, timeout_seconds):
        del input_bytes, environment, timeout_seconds
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, b"codex-cli 0.fake\n", b"")
        self.invocations += 1
        return subprocess.CompletedProcess(command, 1, self.stdout, self.stderr)


class EditingFailingCodex(FailingCodex):
    def __call__(self, command, input_bytes, environment, timeout_seconds):
        if command[-1] != "--version":
            worktree = Path(command[command.index("-C") + 1])
            target = worktree / "src" / "candidate.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("incomplete attempt\n", encoding="utf-8")
        return super().__call__(command, input_bytes, environment, timeout_seconds)


class FakeReviewer:
    def __init__(self, *, stale: bool = False) -> None:
        self.invocations = 0
        self.stale = stale

    def review(
        self, authority, task, candidate, implementation_evidence,
        execution_id, started_at, timeout_seconds, finished_at,
    ):
        del authority, task, implementation_evidence, execution_id, started_at, timeout_seconds, finished_at
        self.invocations += 1
        reviewed = StateRef(candidate.repository, "f" * 40) if self.stale else candidate
        return ReviewResult(
            reviewed, ProjectReviewResponse("no_findings", (), None),
            StateRef(candidate.repository, candidate.commit, "review.json"), True,
            "adapter.completed",
        )


class CapturingReviewProcess:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.timeouts: list[int] = []

    def __call__(self, command, input_bytes, environment, timeout_seconds):
        del environment
        self.timeouts.append(timeout_seconds)
        if command[1:] == ("--version",):
            return subprocess.CompletedProcess(command, 0, b"codex-cli 0.153.4\n", b"")
        self.requests.append(json.loads(input_bytes))
        response = json.dumps(
            {"findings": [], "insufficient_evidence": None, "outcome": "no_findings"},
            separators=(",", ":"),
        )
        events = (
            {"type": "thread.started", "thread_id": "discarded"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": response}},
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5, "reasoning_output_tokens": 0}},
        )
        stdout = b"\n".join(
            json.dumps(item, separators=(",", ":")).encode() for item in events
        ) + b"\n"
        return subprocess.CompletedProcess(command, 0, stdout, b"")


class CrashImplementer:
    def __init__(self) -> None:
        self.invocations = 0

    def run(self, *args, **kwargs):
        del args, kwargs
        self.invocations += 1
        raise KeyboardInterrupt("synthetic interruption after durable reservation")


class FindingThenNoReviewer(FakeReviewer):
    def review(self, authority, task, candidate, implementation_evidence, execution_id, started_at, timeout_seconds, finished_at):
        del authority, task, implementation_evidence, execution_id, started_at, timeout_seconds, finished_at
        self.invocations += 1
        if self.invocations == 1:
            response = ProjectReviewResponse(
                "findings",
                (ProjectReviewFinding(
                    "medium", "Correct the candidate marker",
                    "The marker must explicitly identify the corrected result.",
                    "Append the word corrected.",
                    (StateRef(candidate.repository, candidate.commit, "candidate.diff"),),
                ),),
                None,
            )
        else:
            response = ProjectReviewResponse("no_findings", (), None)
        return ReviewResult(
            candidate, response,
            StateRef(candidate.repository, candidate.commit, "review.json"),
            True, "adapter.completed",
        )


class DevelopmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Fixture")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "peoplebot/blueprints/development").mkdir(parents=True)
        (self.repo / "peoplebot/adapters/development").mkdir(parents=True)
        (self.repo / "peoplebot/blueprints/project_review").mkdir(parents=True)
        (self.repo / "peoplebot/adapters/project_review").mkdir(parents=True)
        source = Path(__file__).parents[1]
        for relative in (
            "peoplebot/blueprints/development/blueprint.json",
            "peoplebot/adapters/development/adapter.json",
            "peoplebot/blueprints/project_review/blueprint.json",
            "peoplebot/adapters/project_review/adapter.json",
            "peoplebot/adapters/project_review.py",
            "peoplebot/adapters/codex_read_only.py",
        ):
            target = self.repo / relative
            target.write_bytes((source / relative).read_bytes())
        adapter_path = self.repo / "peoplebot/adapters/development/adapter.json"
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        adapter["runtime_version"] = "0.fake"
        adapter_path.write_text(json.dumps(adapter, sort_keys=True), encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src/base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.task = DevelopmentTask(
            "task-007", "proposal-007", "Add the bounded candidate file.", "fixture",
            self.base, ("src",), "refs/heads/codex/task-007",
            ((sys.executable, "-c", "from pathlib import Path; assert Path('src/candidate.txt').read_text() == 'verified candidate\\n'"),),
            ("src/base.txt", "src/candidate.txt"), "context-policy.json",
            2, 0, 900, 1800, "Implement task 007",
        )
        (self.repo / "task.json").write_bytes(
            (json.dumps(self.task.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
        (self.repo / "coordination").mkdir()
        (self.repo / "coordination/request.md").write_text("request 007\n", encoding="utf-8")
        git(self.repo, "add", "task.json", "coordination/request.md")
        git(self.repo, "commit", "-m", "task")
        self.task_commit = git(self.repo, "rev-parse", "HEAD")
        self.runtime = self.root / "runtime"
        self.home = self.root / "home"
        self.home.mkdir()
        self.worktrees = self.root / "worktrees"
        self.authority = DevelopmentAuthority(
            True, "environment-fixture", "coordinator", "implementer", "reviewer",
            self.repo, self.repo, self.runtime, self.worktrees, Path(sys.executable),
            self.home, StateRef("fixture", self.task_commit, "task.json"),
            StateRef("fixture", self.task_commit, "coordination/request.md"),
            "message-007", StateRef("fixture", self.base, "peoplebot/blueprints/development/blueprint.json"),
            StateRef("fixture", self.base, "peoplebot/adapters/development/adapter.json"),
            StateRef("fixture", self.base, "peoplebot/blueprints/project_review/blueprint.json"),
            StateRef("fixture", self.base, "peoplebot/adapters"),
        )
        self.message = import_selected_coordination_task(
            self.authority, self.task, created_at="2026-09-14T01:00:00Z",
            sender="coordinator", recipient="environment-fixture",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _adapter(self, fake: FakeCodex) -> CodexDevelopmentAdapter:
        return CodexDevelopmentAdapter(
            self.repo, self.authority.development_adapter, sys.executable,
            self.home, self.worktrees, runner=fake,
        )

    def test_production_composition_candidate_review_progress_and_restart(self) -> None:
        fake = FakeCodex()
        reviewer = FakeReviewer()
        handler = DevelopmentWorkflowHandler(
            self.authority, self._adapter(fake), reviewer,
            now=lambda: datetime(2026, 9, 14, 1, 1, tzinfo=UTC),
        )
        outer = try_acquire_execution(self.runtime, "environment-fixture", "coordinator", "outer-007")
        self.assertTrue(outer.acquired)
        try:
            result = handler(self.message)
        finally:
            outer.admission.release()  # type: ignore[union-attr]
        self.assertEqual(result.disposition, TaskDisposition.COMPLETED)
        self.assertEqual(fake.invocations, 1)
        self.assertEqual(reviewer.invocations, 1)
        candidate = git(self.repo, "rev-parse", "refs/heads/codex/task-007")
        self.assertEqual(git(self.repo, "show", f"{candidate}:src/candidate.txt"), "verified candidate")
        progress, _ = DevelopmentProgressStore(
            self.repo, "fixture", development_progress_ref("environment-fixture", "task-007")
        ).load()
        self.assertEqual(progress.stage.value, "accepted")  # type: ignore[union-attr]
        self.assertEqual(len(progress.invocation_reservations), 2)  # type: ignore[union-attr]
        self.assertEqual(len(progress.invocation_reports), 2)  # type: ignore[union-attr]
        implementer_report, reviewer_report = progress.invocation_reports  # type: ignore[union-attr]
        self.assertEqual(implementer_report.role, "implementer")
        self.assertEqual([item.value for item in implementer_report.usage], [20, 5, 4])
        self.assertTrue(implementer_report.provider_response_observed)
        self.assertEqual(reviewer_report.role, "reviewer")
        self.assertIsNone(reviewer_report.usage[0].value)
        evidence = git(self.repo, "show", f"{progress.implementation_evidence.commit}:adapter-observation.json")  # type: ignore[union-attr]
        self.assertEqual(json.loads(evidence)["workspace_cleanup_disposition"], "removed")
        restarted = DevelopmentWorkflowHandler(self.authority, self._adapter(fake), reviewer)
        self.assertEqual(restarted(self.message).disposition, TaskDisposition.COMPLETED)
        self.assertEqual((fake.invocations, reviewer.invocations), (1, 1))

    def test_nonzero_process_diagnostic_is_bounded_persisted_and_reported(self) -> None:
        process = FailingCodex()
        reviewer = FakeReviewer()
        result = DevelopmentWorkflowHandler(
            self.authority, self._adapter(process), reviewer,
            now=lambda: datetime(2026, 9, 14, 1, 1, tzinfo=UTC),
        )(self.message)

        self.assertEqual(result.disposition, TaskDisposition.FAILED)
        self.assertTrue(result.provider_invoked)
        self.assertEqual(process.invocations, 1)
        self.assertEqual(reviewer.invocations, 0)
        progress, _ = DevelopmentProgressStore(
            self.repo, "fixture", development_progress_ref("environment-fixture", "task-007")
        ).load()
        self.assertIsNotNone(progress)
        self.assertEqual(progress.invocation_reservations, ("task-007-implementer-1",))  # type: ignore[union-attr]
        diagnostic = progress.implementation_diagnostic  # type: ignore[union-attr]
        self.assertIsNotNone(diagnostic)
        self.assertEqual(diagnostic.classification, "authentication")  # type: ignore[union-attr]
        self.assertEqual(diagnostic.classification_source, "structured_event")  # type: ignore[union-attr]
        self.assertEqual(diagnostic.failure_event_types, ("turn.failed",))  # type: ignore[union-attr]
        self.assertTrue(diagnostic.provider_processing_observed)  # type: ignore[union-attr]
        self.assertFalse(diagnostic.provider_response_observed)  # type: ignore[union-attr]
        self.assertEqual(diagnostic.stdout.captured_bytes, len(process.stdout))  # type: ignore[union-attr]
        self.assertEqual(diagnostic.stderr.captured_bytes, len(process.stderr))  # type: ignore[union-attr]
        self.assertIn("development.process_failed", result.reply_content)
        self.assertIn("reservations 1", result.reply_content)
        self.assertIn("Local-only implementation evidence commit", result.reply_content)
        self.assertIn("codex.input_tokens=10 tokens", result.reply_content)
        self.assertEqual(diagnostic.provider_response_observed, False)  # type: ignore[union-attr]

        report = progress.invocation_reports[0]  # type: ignore[union-attr]
        self.assertEqual(report.role, "implementer")
        self.assertEqual(report.outcome, "development.process_failed")
        self.assertEqual(report.process_exit_code, 1)
        self.assertTrue(report.child_launched)
        self.assertEqual(report.environment_id, "environment-fixture")
        self.assertEqual(report.machine_id, "environment-fixture")
        self.assertIsNotNone(report.attempt_ref)
        self.assertEqual(git(self.repo, "rev-parse", report.attempt_ref), self.base)
        self.assertTrue((self.worktrees / report.execution_id).is_dir())

        evidence = git(
            self.repo, "show",
            f"{progress.implementation_evidence.commit}:adapter-observation.json",  # type: ignore[union-attr]
        )
        terminal = git(
            self.repo, "show", f"{progress.implementation_evidence.commit}:execution.json",  # type: ignore[union-attr]
        )
        memory = result.memory_items[0][1]
        rendered = evidence + terminal + memory + result.reply_content
        self.assertIn("authentication or authorization failure", rendered)
        for secret in (
            "raw-secret", "stderr-secret", "provider.invalid", "private-thread",
            "Authorization", "Bearer",
        ):
            self.assertNotIn(secret, rendered)

    def test_nonzero_process_classifies_known_safe_failure_categories(self) -> None:
        cases = (
            (b"authentication failed", "authentication"),
            (b"configuration file is invalid", "configuration"),
            (b"unsupported model", "model"),
            (b"network connection timed out", "network"),
            (b"usage limit reached", "service_limit"),
            (b"internal runtime failure", "runtime"),
            (b"opaque failure", "unknown"),
        )
        for stderr, expected in cases:
            with self.subTest(expected=expected):
                diagnostic, usage = _diagnose_development_process_failure(
                    subprocess.CompletedProcess(("codex",), 1, b"", stderr)
                )
                self.assertEqual(diagnostic.classification, expected)
                self.assertEqual(usage[0].metric, "codex.token_usage")
                self.assertIsNone(usage[0].value)

    def test_stale_review_cannot_accept_candidate(self) -> None:
        fake = FakeCodex()
        result = DevelopmentWorkflowHandler(
            self.authority, self._adapter(fake), FakeReviewer(stale=True),
            now=lambda: datetime(2026, 9, 14, 1, 1, tzinfo=UTC),
        )(self.message)
        self.assertEqual(result.disposition, TaskDisposition.UNRESOLVED)
        self.assertIn("unresolved", result.reply_content)

    def test_interruption_after_reservation_does_not_repeat(self) -> None:
        crash = CrashImplementer()
        handler = DevelopmentWorkflowHandler(
            self.authority, crash, FakeReviewer(),
            now=lambda: datetime(2026, 9, 14, 1, 1, tzinfo=UTC),
        )
        with self.assertRaises(KeyboardInterrupt):
            handler(self.message)
        restarted = DevelopmentWorkflowHandler(self.authority, crash, FakeReviewer())
        result = restarted(self.message)
        self.assertEqual(result.disposition, TaskDisposition.UNRESOLVED)
        self.assertEqual(crash.invocations, 1)

    def test_bridge_selection_correlation_create_only_and_inactive(self) -> None:
        altered = DevelopmentTask(
            self.task.task_id, self.task.proposal_id, "different", self.task.repository,
            self.task.base_commit, self.task.allowed_paths, self.task.candidate_ref,
            self.task.verification_commands, self.task.review_context_paths,
            self.task.context_policy_path, 2, 0, 900, 1800, self.task.commit_message,
        )
        with self.assertRaises(DevelopmentError):
            import_selected_coordination_task(
                self.authority, altered, created_at="2026-09-14T01:00:00Z",
                sender="coordinator", recipient="environment-fixture",
            )
        reply = Message(
            "reply-007", MessageKind.REPLY, "environment-fixture", "coordinator",
            self.task.task_id, self.task.proposal_id, "development.result", "Accepted.",
            "2026-09-14T01:02:00Z", self.message.message_id,
            (StateRef("fixture", self.base),),
        )
        rendered = render_coordination_reply(reply, self.message)
        path = create_coordination_reply_file(
            self.root / "coordination", "coordination/replies/environment-fixture/reply-007.md", rendered
        )
        self.assertEqual(path.read_bytes(), rendered)
        self.assertEqual(create_coordination_reply_file(
            self.root / "coordination", "coordination/replies/environment-fixture/reply-007.md", rendered
        ), path)
        with self.assertRaises(DevelopmentError):
            create_coordination_reply_file(
                self.root / "coordination", "coordination/replies/environment-fixture/reply-007.md", b"conflict"
            )
        inactive = DevelopmentAuthority(
            False, self.authority.environment_id, self.authority.coordinator_instance_id,
            self.authority.implementer_instance_id, self.authority.reviewer_instance_id,
            self.repo, self.repo, self.runtime, self.worktrees, Path(sys.executable), self.home,
            self.authority.task_state, self.authority.request_state,
            self.authority.selected_message_id, self.authority.development_blueprint,
            self.authority.development_adapter, self.authority.review_blueprint,
            self.authority.review_adapter,
        )
        self.assertEqual(
            DevelopmentWorkflowHandler(inactive, CrashImplementer(), FakeReviewer())(self.message).disposition,
            TaskDisposition.UNRESOLVED,
        )

    def test_symbolic_candidate_ref_is_preserved_and_main_does_not_move(self) -> None:
        git(self.repo, "update-ref", "refs/heads/main", self.base)
        git(self.repo, "symbolic-ref", self.task.candidate_ref, "refs/heads/main")
        adapter = self._adapter(FakeCodex())
        adapter.project_checkout = self.repo
        result = adapter.invoke(
            self.task, StateRef("fixture", self.base), "symbolic-ref", self.task.objective
        )
        self.assertEqual(result.code, "development.candidate_ref_symbolic")
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main"), self.base)

    def test_verification_byte_change_is_not_committed(self) -> None:
        adapter = self._adapter(FakeCodex())
        adapter.project_checkout = self.repo

        def changing_check(command, cwd, environment, timeout):
            del environment, timeout
            (cwd / "src/candidate.txt").write_text("changed after verification\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, b"", b"")

        adapter.command_runner = changing_check
        result = adapter.invoke(
            self.task, StateRef("fixture", self.base), "changed-bytes", self.task.objective
        )
        self.assertEqual(result.code, "development.verified_content_changed")
        unresolved = subprocess.run(
            ("git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet", self.task.candidate_ref),
            capture_output=True, check=False,
        )
        self.assertEqual(unresolved.returncode, 1)

    def test_verification_unresolved_ownership_preserves_worktree(self) -> None:
        adapter = self._adapter(FakeCodex())
        adapter.project_checkout = self.repo
        signal = ProcessOwnershipUnresolved(
            SimpleNamespace(), RuntimeError("synthetic verification shutdown uncertainty")
        )

        def unresolved_check(command, cwd, environment, timeout):
            del command, cwd, environment, timeout
            raise signal

        adapter.command_runner = unresolved_check
        with self.assertRaises(ProcessOwnershipUnresolved) as caught:
            adapter.invoke(
                self.task, StateRef("fixture", self.base),
                "verification-ownership", self.task.objective,
            )
        self.assertIs(caught.exception, signal)
        self.assertTrue((self.worktrees / "verification-ownership").is_dir())
        self.assertEqual(
            git(self.repo, "worktree", "list", "--porcelain").count("worktree "), 2
        )

    def test_elapsed_during_implementation_prevents_review(self) -> None:
        clock = [datetime(2026, 9, 14, 1, 1, tzinfo=UTC)]
        reviewer = FakeReviewer()
        fixture = self

        class Implementer:
            def run(self, *args, **kwargs):
                del args, kwargs
                clock[0] += timedelta(seconds=fixture.task.maximum_elapsed_seconds + 1)
                observation = SimpleNamespace(
                    process_started=True,
                    candidate_state=StateRef("fixture", fixture.base),
                    code="development.completed",
                )
                return SimpleNamespace(
                    observation=observation,
                    observation_evidence=StateRef("fixture", fixture.base, "implementation.json"),
                )

        result = DevelopmentWorkflowHandler(
            self.authority, Implementer(), reviewer, now=lambda: clock[0]
        )(self.message)
        self.assertEqual(reviewer.invocations, 0)
        self.assertEqual(result.disposition, TaskDisposition.FAILED)
        self.assertTrue(result.provider_invoked)
        self.assertIn(StateRef("fixture", self.base), result.states)

    def test_completed_review_save_failure_returns_known_evidence(self) -> None:
        fixture = self

        class Implementer:
            def run(self, *args, **kwargs):
                del args, kwargs
                observation = SimpleNamespace(
                    process_started=True,
                    candidate_state=StateRef("fixture", fixture.base),
                    code="development.completed",
                )
                return SimpleNamespace(
                    observation=observation,
                    observation_evidence=StateRef("fixture", fixture.base, "implementation.json"),
                )

        original = DevelopmentProgressStore.persist

        def fail_final(store, progress, expected):
            if progress.stage.value == "accepted":
                raise DevelopmentError("synthetic.save_failed", "offline")
            return original(store, progress, expected)

        with patch.object(DevelopmentProgressStore, "persist", fail_final):
            result = DevelopmentWorkflowHandler(
                self.authority, Implementer(), FakeReviewer()
            )(self.message)
        self.assertEqual(result.disposition, TaskDisposition.UNRESOLVED)
        self.assertTrue(result.provider_invoked)
        self.assertIn(StateRef("fixture", self.base), result.states)
        self.assertTrue(any(state.path == "review.json" for state in result.states))
        saved, _ = DevelopmentProgressStore(
            self.repo, "fixture", development_progress_ref("environment-fixture", self.task.task_id)
        ).load()
        self.assertEqual(saved.stage.value, "reviewer_reserved")  # type: ignore[union-attr]

    def test_candidate_ready_save_failure_returns_candidate_and_evidence(self) -> None:
        original = DevelopmentProgressStore.persist

        def fail_candidate(store, progress, expected):
            if progress.stage.value == "candidate_ready":
                raise DevelopmentError("synthetic.save_failed", "offline")
            return original(store, progress, expected)

        with patch.object(DevelopmentProgressStore, "persist", fail_candidate):
            result = DevelopmentWorkflowHandler(
                self.authority, self._adapter(FakeCodex()), FakeReviewer(),
                now=lambda: datetime(2026, 9, 14, 1, 1, tzinfo=UTC),
            )(self.message)
        self.assertEqual(result.disposition, TaskDisposition.UNRESOLVED)
        self.assertTrue(result.provider_invoked)
        self.assertTrue(any(state.path is None for state in result.states))
        self.assertTrue(any(state.path == "adapter-observation.json" for state in result.states))
        saved, _ = DevelopmentProgressStore(
            self.repo, "fixture", development_progress_ref("environment-fixture", self.task.task_id)
        ).load()
        self.assertEqual(saved.stage.value, "implementer_reserved")  # type: ignore[union-attr]

    def test_missing_implementation_terminal_evidence_keeps_candidate(self) -> None:
        fixture = self

        class Implementer:
            def run(self, *args, **kwargs):
                del args, kwargs
                return SimpleNamespace(
                    observation=SimpleNamespace(
                        process_started=True,
                        candidate_state=StateRef("fixture", fixture.base),
                        code="development.completed",
                    ),
                    observation_evidence=None,
                )

        result = DevelopmentWorkflowHandler(
            self.authority, Implementer(), FakeReviewer()
        )(self.message)
        self.assertEqual(result.disposition, TaskDisposition.UNRESOLVED)
        self.assertTrue(result.provider_invoked)
        self.assertIn(StateRef("fixture", self.base), result.states)

    def test_adapter_total_deadline_expires_before_verification(self) -> None:
        clock = [0.0]

        class ExpiringProcess(FakeCodex):
            def __call__(self, command, input_bytes, environment, timeout_seconds):
                result = super().__call__(command, input_bytes, environment, timeout_seconds)
                if command[-1] != "--version":
                    clock[0] = 5.0
                return result

        adapter = CodexDevelopmentAdapter(
            self.repo, self.authority.development_adapter, sys.executable,
            self.home, self.worktrees, runner=ExpiringProcess(),
            monotonic=lambda: clock[0],
        )
        adapter.project_checkout = self.repo
        result = adapter.invoke(
            replace(self.task, per_invocation_timeout_seconds=5),
            StateRef("fixture", self.base), "deadline", self.task.objective,
        )
        self.assertEqual(result.code, "development.timeout")
        self.assertTrue(result.process_started)
        self.assertIsNone(result.candidate_state)
        self.assertLessEqual(adapter.runner.timeouts[-1], 5)  # type: ignore[attr-defined]

    def test_elapsed_after_finding_prevents_correction_launch(self) -> None:
        correction_task = replace(
            self.task, maximum_invocations=4, maximum_corrections=1
        )
        (self.repo / "task-deadline-correction.json").write_bytes(
            (json.dumps(correction_task.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
        git(self.repo, "add", "task-deadline-correction.json")
        git(self.repo, "commit", "-m", "deadline correction task")
        commit = git(self.repo, "rev-parse", "HEAD")
        authority = replace(
            self.authority,
            task_state=StateRef("fixture", commit, "task-deadline-correction.json"),
            request_state=StateRef("fixture", commit, "coordination/request.md"),
        )
        message = import_selected_coordination_task(
            authority, correction_task, created_at="2026-09-14T01:00:00Z",
            sender="coordinator", recipient="environment-fixture",
        )
        clock = [datetime(2026, 9, 14, 1, 1, tzinfo=UTC)]
        fixture = self

        class Implementer:
            calls = 0

            def run(self, *args, **kwargs):
                del args, kwargs
                self.calls += 1
                return SimpleNamespace(
                    observation=SimpleNamespace(
                        process_started=True,
                        candidate_state=StateRef("fixture", fixture.base),
                        code="development.completed",
                    ),
                    observation_evidence=StateRef("fixture", fixture.base, "implementation.json"),
                )

        class Reviewer(FindingThenNoReviewer):
            def review(self, *args, **kwargs):
                result = super().review(*args, **kwargs)
                clock[0] += timedelta(seconds=correction_task.maximum_elapsed_seconds + 1)
                return result

        implementer = Implementer()
        result = DevelopmentWorkflowHandler(
            authority, implementer, Reviewer(), now=lambda: clock[0]
        )(message)
        self.assertEqual(result.disposition, TaskDisposition.FAILED)
        self.assertEqual(implementer.calls, 1)
        self.assertIn("elapsed", result.reply_content)

    def test_actual_reviewer_wrapper_receives_requirements_diff_and_verification(self) -> None:
        transport = CapturingReviewProcess()
        reviewer = ExactProjectReviewer(ProjectReviewAdapter(
            self.repo, self.authority.review_adapter, sys.executable,
            self.home, runner=transport,
        ))
        result = DevelopmentWorkflowHandler(
            self.authority, self._adapter(FakeCodex()), reviewer,
            now=lambda: datetime(2026, 9, 14, 1, 1, tzinfo=UTC),
        )(self.message)
        self.assertEqual(result.disposition, TaskDisposition.COMPLETED)
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.timeouts[-1], 120)
        request = transport.requests[0]
        self.assertIn(self.task.objective, request["objective"])
        documents = {item["source"]["path"]: item for item in request["context"]["documents"]}
        self.assertEqual(
            set(documents),
            {
                "requirements.json", "coordination-request.md", "candidate.diff",
                "implementation-observation.json", "review-source-01.txt",
                "review-source-02.txt",
            },
        )
        requirements = json.loads(documents["requirements.json"]["content"])
        self.assertEqual(requirements["task"]["objective"], self.task.objective)
        self.assertEqual(
            [item["source"]["path"] for item in requirements["review_sources"]],
            list(self.task.review_context_paths),
        )
        self.assertEqual(documents["review-source-01.txt"]["content"], "base\n")
        self.assertEqual(
            documents["review-source-02.txt"]["content"], "verified candidate\n"
        )
        self.assertIn("candidate.txt", documents["candidate.diff"]["content"])
        observation = json.loads(documents["implementation-observation.json"]["content"])
        self.assertEqual(observation["verification_commands"], [list(self.task.verification_commands[0])])

    def test_allowed_correction_provider_input_contains_exact_prior_findings(self) -> None:
        corrected_task = replace(
            self.task,
            maximum_invocations=4,
            maximum_corrections=1,
            verification_commands=((
                sys.executable, "-c",
                "from pathlib import Path; assert Path('src/candidate.txt').read_text().startswith('verified candidate')",
            ),),
        )
        (self.repo / "task-correction.json").write_bytes(
            (json.dumps(corrected_task.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
        git(self.repo, "add", "task-correction.json")
        git(self.repo, "commit", "-m", "correction task")
        state_commit = git(self.repo, "rev-parse", "HEAD")
        authority = replace(
            self.authority,
            task_state=StateRef("fixture", state_commit, "task-correction.json"),
            request_state=StateRef("fixture", state_commit, "coordination/request.md"),
        )
        message = import_selected_coordination_task(
            authority, corrected_task, created_at="2026-09-14T01:00:00Z",
            sender="coordinator", recipient="environment-fixture",
        )
        process = FakeCodex()
        result = DevelopmentWorkflowHandler(
            authority, self._adapter(process), FindingThenNoReviewer(),
            now=lambda: datetime(2026, 9, 14, 1, 1, tzinfo=UTC),
        )(message)
        self.assertEqual(result.disposition, TaskDisposition.COMPLETED)
        self.assertEqual(process.invocations, 2)
        correction_objective = process.prompts[1]["objective"]
        self.assertIn("Correct the candidate marker", correction_objective)
        self.assertIn("review.json", correction_objective)

    def test_complete_command_actual_wrappers_memory_remotes_and_fresh_process(self) -> None:
        owner_remote = self.root / "owner.git"
        git(self.root, "init", "--bare", str(owner_remote))
        git(self.repo, "remote", "add", "owner", str(owner_remote))
        outbound = outbound_message_ref("environment-fixture")
        status_path = self.root / "cycle-status.json"
        bindings = CycleBindings(
            "environment-fixture", "coordinator", self.runtime, self.repo, "fixture", outbound,
            OutboundDestination("fixture", "owner", str(owner_remote), outbound),
            (PeerSource(
                "fixture", "owner", str(owner_remote), outbound,
                ("coordination-bootstrap", "environment-fixture"),
            ),),
            reader_progress_ref("environment-fixture", "coordinator"),
            status_path, self.root / "STOP",
        )
        policy = TaskPolicy(
            (TaskRoute("development.execute", "development.execute"),), 4, False
        )

        coordination = self.root / "coordination-repo"
        coordination.mkdir()
        git(coordination, "init", "-b", "coordination")
        git(coordination, "config", "user.name", "Fixture")
        git(coordination, "config", "user.email", "fixture@example.invalid")
        (coordination / "README.md").write_text("coordination\n", encoding="utf-8")
        git(coordination, "add", "README.md")
        git(coordination, "commit", "-m", "coordination base")
        coordination_remote = self.root / "coordination.git"
        git(self.root, "init", "--bare", str(coordination_remote))
        git(coordination, "remote", "add", "coordination-origin", str(coordination_remote))
        git(coordination, "push", "-u", "coordination-origin", "coordination")
        operations = DevelopmentCycleOperations(
            True, "coordination-bootstrap", "2026-09-14T01:00:00Z",
            self.repo, "fixture", StateRef("fixture", self.base), True,
            coordination, "fixture-coordination", "coordination-origin",
            str(coordination_remote), "refs/heads/coordination",
            "coordination/replies/environment-fixture/offline-007.md",
        )
        review_process = CapturingReviewProcess()
        reviewer = ExactProjectReviewer(ProjectReviewAdapter(
            self.repo, self.authority.review_adapter, sys.executable,
            self.home, runner=review_process,
        ))
        editing_process = FakeCodex()
        result = run_development_cycle_command(
            self.authority, operations, bindings, policy,
            self._adapter(editing_process), reviewer,
            "offline-command-1", "2026-09-14T01:01:00Z",
        )
        self.assertEqual(result.code, "development.completed")
        self.assertTrue(result.provider_invoked)
        self.assertIsNotNone(result.cycle.memory_state)  # type: ignore[union-attr]
        self.assertEqual(editing_process.invocations, 1)
        self.assertEqual(len(review_process.requests), 1)

        self.assertEqual(
            git(coordination, "rev-parse", "HEAD"),
            git(self.root, "--git-dir", str(coordination_remote), "rev-parse", "refs/heads/coordination"),
        )
        self.assertTrue((coordination / operations.coordination_reply_path).is_file())
        memory_state = result.cycle.memory_state  # type: ignore[union-attr]
        resumed = assemble_instance_memory_context(
            self.repo, memory_state, "environment-fixture", "coordinator",
            self.authority.development_blueprint, ("development/latest.json",),
            ContextPolicy(
                StateRef("fixture", memory_state.commit, "metadata.json"), 1, 65_536, 65_536
            ),
        )
        self.assertIn("Accepted exact candidate", resumed.documents[0].content)

        authority_json = self.root / "authority.json"
        operations_json = self.root / "operations.json"
        bindings_json = self.root / "bindings.json"
        policy_json = self.root / "policy.json"
        authority_json.write_text(json.dumps({
            "active": True, "codex_home": str(self.home),
            "coordinator_instance_id": "coordinator",
            "development_adapter": self.authority.development_adapter.to_dict(),
            "development_blueprint": self.authority.development_blueprint.to_dict(),
            "environment_id": "environment-fixture", "executable": sys.executable,
            "format": "peoplebot.development-authority.v0",
            "framework_checkout": str(self.repo), "implementer_instance_id": "implementer",
            "project_checkout": str(self.repo), "request_state": self.authority.request_state.to_dict(),
            "review_adapter": self.authority.review_adapter.to_dict(),
            "review_blueprint": self.authority.review_blueprint.to_dict(),
            "reviewer_instance_id": "reviewer", "runtime_root": str(self.runtime),
            "selected_message_id": "message-007", "task_state": self.authority.task_state.to_dict(),
            "worktree_root": str(self.worktrees),
        }), encoding="utf-8")
        operations_json.write_text(json.dumps({
            "active": True, "coordination_checkout": str(coordination),
            "coordination_expected_url": str(coordination_remote),
            "coordination_ref": "refs/heads/coordination",
            "coordination_remote": "coordination-origin",
            "coordination_reply_path": operations.coordination_reply_path,
            "coordination_repository": "fixture-coordination",
            "format": "peoplebot.development-cycle-operations.v0",
            "import_created_at": operations.import_created_at,
            "import_sender": operations.import_sender,
            "memory_checkout": str(self.repo),
            "memory_expected_state": memory_state.to_dict(),
            "memory_initial": False, "memory_repository": "fixture",
        }), encoding="utf-8")
        bindings_json.write_text(json.dumps({
            "destination": {"expected_url": str(owner_remote), "ref_name": outbound, "remote": "owner", "repository": "fixture"},
            "environment_id": "environment-fixture", "format": "peoplebot.cycle-bindings.v0",
            "instance_id": "coordinator", "local_checkout": str(self.repo),
            "local_repository": "fixture", "outbound_ref": outbound,
            "progress_ref": reader_progress_ref("environment-fixture", "coordinator"),
            "runtime_root": str(self.runtime),
            "sources": [{"allowed_senders": ["coordination-bootstrap", "environment-fixture"], "expected_url": str(owner_remote), "ref_name": outbound, "remote": "owner", "repository": "fixture"}],
            "status_path": str(status_path), "stop_path": str(self.root / "STOP"),
        }), encoding="utf-8")
        policy_json.write_text(json.dumps({
            "allow_stop_messages": False, "format": "peoplebot.task-policy.v0",
            "maximum_completed_tasks": 4,
            "routes": [{"handler": "development.execute", "purpose": "development.execute"}],
        }), encoding="utf-8")
        second = subprocess.run(
            (
                sys.executable, "-m", "peoplebot", "development-cycle-tick",
                "--bindings", str(bindings_json), "--policy", str(policy_json),
                "--authority", str(authority_json), "--operations", str(operations_json),
                "--execution-id", "offline-command-2",
            ),
            cwd=Path(__file__).parents[1], capture_output=True, check=False,
            text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(second.returncode, 13)
        second_result = json.loads(second.stdout)
        self.assertFalse(second_result["provider_invoked"])
        self.assertEqual(
            second_result["cycle"]["disposition"], "idle",
            msg=second.stdout + second.stderr,
        )
        self.assertEqual(editing_process.invocations, 1)
        self.assertEqual(len(review_process.requests), 1)

    def test_coordination_existing_unpushed_commit_is_uncertain_without_retry(self) -> None:
        coordination = self.root / "coordination-uncertain"
        coordination.mkdir()
        git(coordination, "init", "-b", "coordination")
        git(coordination, "config", "user.name", "Fixture")
        git(coordination, "config", "user.email", "fixture@example.invalid")
        (coordination / "README.md").write_text("coordination\n", encoding="utf-8")
        git(coordination, "add", "README.md")
        git(coordination, "commit", "-m", "coordination base")
        remote = self.root / "coordination-uncertain.git"
        git(self.root, "init", "--bare", str(remote))
        git(coordination, "remote", "add", "coordination-origin", str(remote))
        git(coordination, "push", "-u", "coordination-origin", "coordination")
        relative = "coordination/replies/environment-fixture/offline-uncertain.md"
        content = b"# Matching local completion\n"
        destination = coordination / relative
        destination.parent.mkdir(parents=True)
        destination.write_bytes(content)
        git(coordination, "add", relative)
        git(coordination, "commit", "-m", "locally committed reply")
        local = git(coordination, "rev-parse", "HEAD")
        remote_before = git(
            self.root, "--git-dir", str(remote), "rev-parse", "refs/heads/coordination"
        )
        operations = DevelopmentCycleOperations(
            True, "coordination-bootstrap", "2026-09-14T01:00:00Z",
            self.repo, "fixture", StateRef("fixture", self.base), True,
            coordination, "fixture-coordination", "coordination-origin",
            str(remote), "refs/heads/coordination", relative,
        )

        result = publish_coordination_reply_file(
            operations, content, "2026-09-14T01:01:00Z"
        )

        self.assertEqual(result.disposition.value, "uncertain")
        self.assertEqual(result.code, "bridge.prior_publication_unresolved")
        self.assertEqual(result.state.commit, local)  # type: ignore[union-attr]
        self.assertEqual(result.observed_remote, remote_before)
        self.assertEqual(
            git(self.root, "--git-dir", str(remote), "rev-parse", "refs/heads/coordination"),
            remote_before,
        )

    def test_ordinary_failed_attempt_is_preserved_and_next_task_proceeds(self) -> None:
        owner_remote = self.root / "recovery-owner.git"
        git(self.root, "init", "--bare", str(owner_remote))
        git(self.repo, "remote", "add", "recovery-owner", str(owner_remote))
        outbound = outbound_message_ref("environment-fixture")
        status_path = self.root / "recovery-cycle-status.json"
        bindings = CycleBindings(
            "environment-fixture", "coordinator", self.runtime, self.repo, "fixture", outbound,
            OutboundDestination(
                "fixture", "recovery-owner", str(owner_remote), outbound
            ),
            (PeerSource(
                "fixture", "recovery-owner", str(owner_remote), outbound,
                ("coordination-bootstrap", "environment-fixture"),
            ),),
            reader_progress_ref("environment-fixture", "coordinator"),
            status_path, self.root / "recovery-STOP",
        )
        policy = TaskPolicy(
            (TaskRoute("development.execute", "development.execute"),), 2, False
        )
        coordination = self.root / "recovery-coordination"
        coordination.mkdir()
        git(coordination, "init", "-b", "coordination")
        git(coordination, "config", "user.name", "Fixture")
        git(coordination, "config", "user.email", "fixture@example.invalid")
        (coordination / "README.md").write_text("coordination\n", encoding="utf-8")
        git(coordination, "add", "README.md")
        git(coordination, "commit", "-m", "coordination base")
        coordination_remote = self.root / "recovery-coordination.git"
        git(self.root, "init", "--bare", str(coordination_remote))
        git(coordination, "remote", "add", "coordination-origin", str(coordination_remote))
        git(coordination, "push", "-u", "coordination-origin", "coordination")

        old_operations = DevelopmentCycleOperations(
            True, "coordination-bootstrap", "2026-09-14T01:00:00Z",
            self.repo, "fixture", StateRef("fixture", self.base), True,
            coordination, "fixture-coordination", "coordination-origin",
            str(coordination_remote), "refs/heads/coordination",
            "coordination/replies/environment-fixture/old-failure.md",
        )
        old_process = EditingFailingCodex()
        old = run_development_cycle_command(
            self.authority, old_operations, bindings, policy,
            self._adapter(old_process), FakeReviewer(),
            "offline-recovery-old", "2026-09-14T01:01:00Z",
        )
        self.assertEqual(old.code, "development.failed", old.to_dict())
        self.assertTrue(old.provider_invoked)
        self.assertEqual(old_process.invocations, 1)
        old_reader = ReaderProgressStore(
            self.repo, "fixture", bindings.progress_ref
        ).load()
        self.assertIsNotNone(old_reader)
        old_task = old_reader.progress.tasks[0]  # type: ignore[union-attr]
        self.assertEqual(old_task.disposition, TaskDisposition.FAILED)
        old_progress, old_development_state = DevelopmentProgressStore(
            self.repo, "fixture", development_progress_ref("environment-fixture", self.task.task_id)
        ).load()
        self.assertIsNotNone(old_progress)
        self.assertIsNotNone(old_development_state)
        old_implementation = old_progress.implementation_evidence  # type: ignore[union-attr]
        self.assertIsNotNone(old_implementation)

        next_task = replace(
            self.task,
            task_id="task-recovery-next",
            proposal_id="proposal-recovery-next",
            candidate_ref="refs/heads/codex/task-recovery-next",
            commit_message="Implement recovery follow-up",
        )
        (self.repo / "task-recovery-next.json").write_bytes(
            (json.dumps(next_task.to_dict(), sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
        (self.repo / "coordination/recovery-next.md").write_text(
            "recovery next\n", encoding="utf-8"
        )
        git(self.repo, "add", "task-recovery-next.json", "coordination/recovery-next.md")
        git(self.repo, "commit", "-m", "next recovery task")
        next_commit = git(self.repo, "rev-parse", "HEAD")
        next_authority = replace(
            self.authority,
            task_state=StateRef("fixture", next_commit, "task-recovery-next.json"),
            request_state=StateRef("fixture", next_commit, "coordination/recovery-next.md"),
            selected_message_id="message-recovery-next",
        )
        next_operations = DevelopmentCycleOperations(
            True, "coordination-bootstrap", "2026-09-14T01:02:00Z",
            self.repo, "fixture", old.cycle.memory_state, False,  # type: ignore[union-attr]
            coordination, "fixture-coordination", "coordination-origin",
            str(coordination_remote), "refs/heads/coordination",
            "coordination/replies/environment-fixture/recovery-next.md",
        )
        next_process = FakeCodex()
        next_reviewer = FakeReviewer()
        completed = run_development_cycle_command(
            next_authority, next_operations, bindings, policy,
            self._adapter(next_process), next_reviewer,
            "offline-next-after-failure", "2026-09-14T01:03:00Z",
        )
        self.assertEqual(completed.code, "development.completed")
        self.assertTrue(completed.provider_invoked)
        self.assertIsNotNone(completed.coordination)
        self.assertEqual(next_process.invocations, 1)
        self.assertEqual(next_reviewer.invocations, 1)
        self.assertEqual(old_process.invocations, 1)
        preserved_progress, preserved_state = DevelopmentProgressStore(
            self.repo, "fixture", development_progress_ref("environment-fixture", self.task.task_id)
        ).load()
        self.assertEqual(preserved_state, old_development_state)
        self.assertEqual(
            preserved_progress.invocation_reservations,  # type: ignore[union-attr]
            ("task-007-implementer-1",),
        )
        failed_report = preserved_progress.invocation_reports[0]  # type: ignore[union-attr]
        self.assertEqual(failed_report.outcome, "development.process_failed")
        self.assertEqual(
            git(self.repo, "show", f"{failed_report.attempt_state.commit}:src/candidate.txt"),  # type: ignore[union-attr]
            "incomplete attempt",
        )
        self.assertTrue((self.worktrees / failed_report.execution_id).is_dir())
        self.assertEqual(
            git(self.worktrees / failed_report.execution_id, "symbolic-ref", "HEAD"),
            failed_report.attempt_ref,
        )
        latest_reader = ReaderProgressStore(
            self.repo, "fixture", bindings.progress_ref
        ).load()
        self.assertEqual(
            [item.disposition for item in latest_reader.progress.tasks],  # type: ignore[union-attr]
            [TaskDisposition.FAILED, TaskDisposition.COMPLETED],
        )
        self.assertEqual(len(latest_reader.progress.reconciliations), 0)  # type: ignore[union-attr]


if __name__ == "__main__":
    unittest.main()
