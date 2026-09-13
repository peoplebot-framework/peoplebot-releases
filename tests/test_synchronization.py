from __future__ import annotations

import subprocess
import os
import sys
import tempfile
import unittest
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import peoplebot.synchronization as synchronization_module
from peoplebot.adapters.codex_read_only import (
    DirectProcessTimeout,
    ProcessOwnershipUnresolved,
    _run_process,
)
from peoplebot.execution import ExecutionStatus
from peoplebot.memory import (
    GitMemoryStore,
    MemoryCheckpointRequest,
    MemoryItem,
    assemble_instance_memory_context,
    instance_memory_ref,
)
from peoplebot.preparation import ContextPolicy
from peoplebot.provenance import ExecutionStart, GitAttemptStore
from peoplebot.admission import try_acquire_execution
from peoplebot.state import StateRef
from peoplebot.synchronization import (
    MemorySynchronizationRequest,
    MemoryRecoveryRequest,
    SynchronizationDisposition,
    SynchronizationError,
    SynchronizationLimits,
    SynchronizationObservation,
    reconcile_instance_memory,
    run_memory_synchronization_execution,
    run_memory_recovery_execution,
    synchronize_instance_memory,
    validate_memory_synchronization,
)


REPOSITORY = "https://example.invalid/owning-environment/memory"
ENVIRONMENT = "environment:sync-test"
INSTANCE = "instance:sync-test"


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


class SynchronizationValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.remote = self.root / "remote.git"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.name", "PeopleBot Test")
        git(self.repository, "config", "user.email", "test@example.invalid")
        (self.repository / "blueprint.json").write_text("blueprint\n", encoding="utf-8")
        git(self.repository, "add", "blueprint.json")
        git(self.repository, "commit", "-m", "authorized synthetic baseline")
        self.baseline = git(self.repository, "rev-parse", "HEAD")
        self.blueprint = StateRef(REPOSITORY, self.baseline, "blueprint.json")
        self.ref_name = instance_memory_ref(ENVIRONMENT, INSTANCE)
        self.memory = GitMemoryStore(self.repository, REPOSITORY)._checkpoint(
            MemoryCheckpointRequest(
                repository=REPOSITORY,
                environment_id=ENVIRONMENT,
                instance_id=INSTANCE,
                blueprint=self.blueprint,
                expected_state=StateRef(REPOSITORY, self.baseline),
                items=(MemoryItem("resume.md", "exact UTF-8 memory ✅\n"),),
                saved_at="2026-09-10T12:00:00Z",
                initial=True,
            )
        ).state
        subprocess.run(
            ["git", "init", "--bare", str(self.remote)],
            capture_output=True,
            check=True,
            shell=False,
            timeout=15,
        )
        git(self.repository, "remote", "add", "memory", str(self.remote))
        git(self.repository, "remote", "set-url", "--push", "memory", str(self.remote))
        self.destination = git(self.repository, "remote", "get-url", "--push", "memory")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, **changes: object) -> MemorySynchronizationRequest:
        values: dict[str, object] = {
            "repository": REPOSITORY,
            "environment_id": ENVIRONMENT,
            "instance_id": INSTANCE,
            "blueprint": self.blueprint,
            "memory_state": self.memory,
            "authorized_baseline": StateRef(REPOSITORY, self.baseline),
            "destination_ref": self.ref_name,
            "remote_name": "memory",
            "expected_destination_url": self.destination,
            "expected_remote_state": None,
            "limits": SynchronizationLimits(timeout_seconds=15, max_lineage_commits=8),
        }
        values.update(changes)
        return MemorySynchronizationRequest(**values)  # type: ignore[arg-type]

    def memory_descendant_with_extra(self, *, remove_later: bool) -> tuple[str, str | None]:
        worktree = self.root / f"crafted-{len(tuple(self.root.iterdir()))}"
        git(self.repository, "worktree", "add", "--detach", str(worktree), self.memory.commit)
        git(worktree, "config", "user.name", "PeopleBot Test")
        git(worktree, "config", "user.email", "test@example.invalid")
        (worktree / "undeclared.txt").write_text("must never publish\n", encoding="utf-8")
        git(worktree, "add", "undeclared.txt")
        git(worktree, "commit", "-m", "invalid undeclared content")
        invalid = git(worktree, "rev-parse", "HEAD")
        if not remove_later:
            return invalid, None
        git(worktree, "rm", "undeclared.txt")
        git(worktree, "commit", "-m", "hide earlier undeclared content")
        return invalid, git(worktree, "rev-parse", "HEAD")

    def publish_for_recovery(self) -> Path:
        git(self.repository, "push", str(self.remote), f"{self.baseline}:refs/heads/main")
        result = synchronize_instance_memory(self.repository, self.request())
        self.assertEqual(result.disposition, SynchronizationDisposition.REMOTE_VERIFIED)
        fresh = self.root / "fresh"
        subprocess.run(
            [
                "git",
                "clone",
                "--single-branch",
                "--branch",
                "main",
                str(self.remote),
                str(fresh),
            ],
            capture_output=True,
            check=True,
            shell=False,
            timeout=15,
        )
        git(fresh, "remote", "rename", "origin", "memory")
        git(fresh, "remote", "set-url", "--push", "memory", str(self.remote))
        return fresh

    def recovery_request(
        self,
        *,
        expected_local_state: StateRef | None = None,
        operation_id: str = "recovery:fresh",
    ) -> MemoryRecoveryRequest:
        return MemoryRecoveryRequest(
            synchronization=self.request(expected_remote_state=self.memory),
            operation_id=operation_id,
            expected_local_state=expected_local_state,
            original_execution_stopped=True,
        )

    def checkpoint_second(self) -> StateRef:
        return GitMemoryStore(self.repository, REPOSITORY)._checkpoint(
            MemoryCheckpointRequest(
                repository=REPOSITORY,
                environment_id=ENVIRONMENT,
                instance_id=INSTANCE,
                blueprint=self.blueprint,
                expected_state=self.memory,
                items=(MemoryItem("resume.md", "second exact checkpoint ✅\n"),),
                saved_at="2026-09-10T12:00:01Z",
                initial=False,
            )
        ).state

    def test_exact_absent_remote_preflight_binds_single_memory_lineage(self) -> None:
        result = validate_memory_synchronization(self.repository, self.request())
        self.assertEqual(result.lineage, (self.memory.commit,))
        self.assertIsNone(result.observed_remote_commit)
        self.assertEqual(result.destination_url, self.destination)
        self.assertEqual(len(result.destination_digest), 64)
        self.assertEqual(git(self.repository, "status", "--porcelain=v1"), "")

    def test_different_fetch_url_does_not_change_validated_push_destination(self) -> None:
        other = self.root / "fetch-only.git"
        subprocess.run(
            ["git", "init", "--bare", str(other)],
            capture_output=True,
            check=True,
            shell=False,
            timeout=15,
        )
        git(self.repository, "remote", "set-url", "memory", str(other))
        result = validate_memory_synchronization(self.repository, self.request())
        self.assertEqual(result.destination_url, self.destination)

    def test_multiple_push_destinations_are_refused(self) -> None:
        second = self.root / "second.git"
        subprocess.run(
            ["git", "init", "--bare", str(second)],
            capture_output=True,
            check=True,
            shell=False,
            timeout=15,
        )
        git(self.repository, "remote", "set-url", "--add", "--push", "memory", str(second))
        with self.assertRaisesRegex(SynchronizationError, "destination_ambiguous"):
            validate_memory_synchronization(self.repository, self.request())

    def test_url_rewrite_chain_is_refused_before_either_repository_changes(self) -> None:
        unintended = self.root / "unintended.git"
        subprocess.run(
            ["git", "init", "--bare", str(unintended)],
            capture_output=True,
            check=True,
            shell=False,
            timeout=15,
        )
        authorized_url = self.remote.as_uri()
        unintended_url = unintended.as_uri()
        git(self.repository, "remote", "set-url", "--push", "memory", "pb-alias:")
        git(
            self.repository,
            "config",
            "--add",
            f"url.{authorized_url}.pushInsteadOf",
            "pb-alias:",
        )
        synthetic_global = self.root / "synthetic-global.gitconfig"
        git(
            self.repository,
            "config",
            "--file",
            str(synthetic_global),
            "--add",
            f"url.{unintended_url}.insteadOf",
            authorized_url,
        )
        request = self.request(expected_destination_url=authorized_url)

        with (
            patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(synthetic_global)}),
            self.assertRaisesRegex(SynchronizationError, "url_rewrite_unsupported"),
        ):
            synchronize_instance_memory(self.repository, request)

        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), "")
        self.assertEqual(git(unintended, "for-each-ref", "--format=%(refname)"), "")

    def test_source_ref_movement_is_refused(self) -> None:
        git(self.repository, "update-ref", self.ref_name, self.baseline, self.memory.commit)
        with self.assertRaisesRegex(SynchronizationError, "source_ref_mismatch"):
            validate_memory_synchronization(self.repository, self.request())

    def test_remote_divergence_is_refused_before_transport(self) -> None:
        git(
            self.repository,
            "push",
            str(self.remote),
            f"{self.baseline}:{self.ref_name}",
        )
        with self.assertRaisesRegex(SynchronizationError, "remote_state_mismatch"):
            validate_memory_synchronization(self.repository, self.request())

    def test_unauthorized_baseline_is_refused(self) -> None:
        orphan_tree = git(self.repository, "show", "-s", "--format=%T", self.baseline)
        unrelated = subprocess.run(
            ["git", "-C", str(self.repository), "commit-tree", orphan_tree],
            input="unrelated\n",
            capture_output=True,
            check=True,
            encoding="utf-8",
            shell=False,
            timeout=15,
        ).stdout.strip()
        with self.assertRaisesRegex(SynchronizationError, "unauthorized_ancestry"):
            validate_memory_synchronization(
                self.repository,
                self.request(authorized_baseline=StateRef(REPOSITORY, unrelated)),
            )

    def test_merge_above_authorized_baseline_is_refused(self) -> None:
        tree = git(self.repository, "show", "-s", "--format=%T", self.memory.commit)
        merged = subprocess.run(
            [
                "git",
                "-C",
                str(self.repository),
                "commit-tree",
                tree,
                "-p",
                self.memory.commit,
                "-p",
                self.baseline,
            ],
            input="unsupported merge\n",
            capture_output=True,
            check=True,
            encoding="utf-8",
            shell=False,
            timeout=15,
        ).stdout.strip()
        git(self.repository, "update-ref", self.ref_name, merged, self.memory.commit)
        with self.assertRaisesRegex(SynchronizationError, "unsupported_lineage"):
            validate_memory_synchronization(
                self.repository,
                self.request(memory_state=StateRef(REPOSITORY, merged)),
            )

    def test_undeclared_tip_content_is_refused_before_publication(self) -> None:
        invalid, _ = self.memory_descendant_with_extra(remove_later=False)
        git(self.repository, "update-ref", self.ref_name, invalid, self.memory.commit)
        with self.assertRaisesRegex(SynchronizationError, "memory_tree_invalid"):
            synchronize_instance_memory(
                self.repository,
                self.request(memory_state=StateRef(REPOSITORY, invalid)),
            )
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), "")
        self.assertEqual(git(self.repository, "rev-parse", self.ref_name), invalid)

    def test_undeclared_historical_content_is_refused_even_when_tip_removes_it(self) -> None:
        invalid, cleaned = self.memory_descendant_with_extra(remove_later=True)
        assert cleaned is not None
        git(self.repository, "update-ref", self.ref_name, cleaned, self.memory.commit)
        with self.assertRaisesRegex(SynchronizationError, "memory_tree_invalid"):
            synchronize_instance_memory(
                self.repository,
                self.request(memory_state=StateRef(REPOSITORY, cleaned)),
            )
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), "")
        self.assertEqual(git(self.repository, "rev-parse", self.ref_name), cleaned)
        self.assertEqual(git(self.repository, "rev-parse", f"{cleaned}^"), invalid)

    def test_observation_is_stable_and_contains_no_destination_url(self) -> None:
        preflight = validate_memory_synchronization(self.repository, self.request())
        observation = SynchronizationObservation(
            operation="synchronize",
            disposition=SynchronizationDisposition.LOCAL_ONLY,
            code="synchronization.preflight_complete",
            repository=REPOSITORY,
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            blueprint=self.blueprint,
            authorized_baseline=StateRef(REPOSITORY, self.baseline),
            limits=self.request().limits,
            attempted_state=self.memory,
            remote_name="memory",
            destination_ref=self.ref_name,
            destination_digest=preflight.destination_digest,
            expected_remote_commit=None,
            observed_remote_commit=None,
        )
        first = observation.to_json_bytes()
        self.assertEqual(first, observation.to_json_bytes())
        self.assertNotIn(self.destination.encode("utf-8"), first)
        self.assertTrue(first.endswith(b"\n"))

    def test_credential_bearing_url_and_transport_override_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "credential-bearing"):
            self.request(expected_destination_url="https://user:secret@example.invalid/repo.git")
        git(self.repository, "config", "remote.memory.mirror", "false")
        validate_memory_synchronization(self.repository, self.request())
        git(self.repository, "config", "remote.memory.receivepack", "custom-receive-pack")
        with self.assertRaisesRegex(SynchronizationError, "transport_override_unsupported"):
            validate_memory_synchronization(self.repository, self.request())

    def test_exact_push_ignores_hostile_defaults_and_publishes_no_other_refs(self) -> None:
        (self.repository / "hook-marker").unlink(missing_ok=True)
        hooks = self.repository / ".git" / "hooks"
        hook = hooks / "pre-push"
        hook.write_text("#!/bin/sh\necho ran > ../hook-marker\nexit 99\n", encoding="utf-8")
        git(self.repository, "config", "push.default", "matching")
        git(self.repository, "config", "push.followTags", "true")
        git(self.repository, "config", "remote.memory.push", "refs/heads/*:refs/heads/*")
        git(self.repository, "tag", "should-remain-local", self.memory.commit)
        git(self.repository, "update-ref", "refs/peoplebot/attempts/v0/local", self.memory.commit)

        result = synchronize_instance_memory(self.repository, self.request())

        self.assertEqual(result.disposition, SynchronizationDisposition.REMOTE_VERIFIED)
        refs = git(self.remote, "for-each-ref", "--format=%(refname) %(objectname)")
        self.assertEqual(refs, f"{self.ref_name} {self.memory.commit}")
        self.assertFalse((self.repository / "hook-marker").exists())
        self.assertEqual(git(self.repository, "rev-parse", self.ref_name), self.memory.commit)

    def test_fast_forward_synchronizes_exact_descendant(self) -> None:
        first = synchronize_instance_memory(self.repository, self.request())
        self.assertEqual(first.disposition, SynchronizationDisposition.REMOTE_VERIFIED)
        second = self.checkpoint_second()
        request = self.request(
            memory_state=second,
            expected_remote_state=self.memory,
        )

        result = synchronize_instance_memory(self.repository, request)

        self.assertEqual(result.disposition, SynchronizationDisposition.REMOTE_VERIFIED)
        self.assertEqual(result.observed_remote_commit, second.commit)
        self.assertEqual(git(self.remote, "rev-parse", self.ref_name), second.commit)
        self.assertEqual(git(self.repository, "rev-parse", self.ref_name), second.commit)

    def test_reconciliation_distinguishes_attempted_prior_and_intervening(self) -> None:
        prior = reconcile_instance_memory(self.repository, self.request())
        self.assertEqual(prior.disposition, SynchronizationDisposition.UNCERTAIN)
        self.assertEqual(prior.code, "synchronization.prior_state_observed")

        git(self.repository, "push", str(self.remote), f"{self.memory.commit}:{self.ref_name}")
        attempted = reconcile_instance_memory(self.repository, self.request())
        self.assertEqual(attempted.disposition, SynchronizationDisposition.REMOTE_VERIFIED)

        tree = git(self.repository, "show", "-s", "--format=%T", self.memory.commit)
        intervening = subprocess.run(
            ["git", "-C", str(self.repository), "commit-tree", tree, "-p", self.memory.commit],
            input="intervening\n",
            capture_output=True,
            check=True,
            encoding="utf-8",
            shell=False,
            timeout=15,
        ).stdout.strip()
        git(self.repository, "push", str(self.remote), f"{intervening}:{self.ref_name}")
        conflicted = reconcile_instance_memory(self.repository, self.request())
        self.assertEqual(conflicted.disposition, SynchronizationDisposition.FAILED)
        self.assertEqual(conflicted.code, "synchronization.intervening_state")

    def test_admitted_sync_persists_sanitized_terminal_observation(self) -> None:
        start = ExecutionStart(
            execution_id="execution:memory-sync",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Synchronize one exact Instance-memory State",
            started_at="2026-09-10T12:01:00Z",
            starting_state=self.memory,
            blueprint=self.blueprint,
            adapter=self.blueprint,
            input_states=(StateRef(REPOSITORY, self.baseline),),
        )
        store = GitAttemptStore(self.repository, REPOSITORY)
        result = run_memory_synchronization_execution(
            self.root / "runtime",
            store,
            self.repository,
            start,
            self.request(),
            lambda: "2026-09-10T12:01:01Z",
        )
        self.assertTrue(result.provenance.task_started)
        self.assertTrue(result.provenance.terminal_committed, result.provenance)
        self.assertEqual(
            result.observation.disposition,
            SynchronizationDisposition.REMOTE_VERIFIED,
        )
        evidence = result.observation_evidence
        self.assertIsNotNone(evidence)
        content = store.read_evidence_bytes(evidence)
        self.assertEqual(content, result.observation.to_json_bytes())
        self.assertNotIn(self.destination.encode("utf-8"), content)
        remote_refs = git(self.remote, "for-each-ref", "--format=%(refname)")
        self.assertEqual(remote_refs, self.ref_name)

    def test_post_push_inspection_timeout_retains_uncertain_observation(self) -> None:
        start = ExecutionStart(
            execution_id="execution:verification-timeout",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Preserve an uncertain post-publication observation",
            started_at="2026-09-10T12:02:00Z",
            starting_state=self.memory,
            blueprint=self.blueprint,
            adapter=self.blueprint,
        )
        store = GitAttemptStore(self.repository, REPOSITORY)
        with (
            patch(
                "peoplebot.synchronization._inspect_remote",
                side_effect=[None, DirectProcessTimeout(("git",), 15)],
            ),
            patch(
                "peoplebot.synchronization._transport_git",
                wraps=synchronization_module._transport_git,
            ) as transport,
        ):
            result = run_memory_synchronization_execution(
                self.root / "runtime",
                store,
                self.repository,
                start,
                self.request(),
                lambda: "2026-09-10T12:02:01Z",
            )

        push_calls = [call for call in transport.call_args_list if "push" in call.args]
        self.assertEqual(len(push_calls), 1)
        self.assertEqual(git(self.remote, "rev-parse", self.ref_name), self.memory.commit)
        self.assertEqual(result.observation.disposition, SynchronizationDisposition.UNCERTAIN)
        self.assertEqual(result.observation.code, "synchronization.verification_unavailable")
        self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.FAILED)
        evidence = result.observation_evidence
        self.assertIsNotNone(evidence)
        self.assertEqual(store.read_evidence_bytes(evidence), result.observation.to_json_bytes())

    def test_push_timeout_is_uncertain_single_shot_and_preserves_local_state(self) -> None:
        start = ExecutionStart(
            execution_id="execution:push-timeout",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Preserve exact local State after a transport timeout",
            started_at="2026-09-10T12:02:10Z",
            starting_state=self.memory,
            blueprint=self.blueprint,
            adapter=self.blueprint,
        )
        push_calls = 0
        original_transport = synchronization_module._transport_git

        def timeout_push(checkout: Path, timeout: int, *arguments: str) -> object:
            nonlocal push_calls
            if "push" in arguments:
                push_calls += 1
                raise DirectProcessTimeout(("git", *arguments), timeout)
            return original_transport(checkout, timeout, *arguments)

        with patch("peoplebot.synchronization._transport_git", side_effect=timeout_push):
            result = run_memory_synchronization_execution(
                self.root / "runtime",
                GitAttemptStore(self.repository, REPOSITORY),
                self.repository,
                start,
                self.request(),
                lambda: "2026-09-10T12:02:11Z",
            )
        self.assertEqual(push_calls, 1)
        self.assertEqual(result.observation.disposition, SynchronizationDisposition.UNCERTAIN)
        self.assertEqual(result.observation.code, "synchronization.transport_uncertain")
        self.assertEqual(git(self.repository, "rev-parse", self.ref_name), self.memory.commit)
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), "")
        self.assertTrue(result.provenance.terminal_committed)

    def test_disconnect_after_push_is_uncertain_without_retry(self) -> None:
        start = ExecutionStart(
            execution_id="execution:push-disconnect",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Reconcile an unreadable completed push",
            started_at="2026-09-10T12:02:20Z",
            starting_state=self.memory,
            blueprint=self.blueprint,
            adapter=self.blueprint,
        )
        push_calls = 0
        original_transport = synchronization_module._transport_git

        def disconnect_after_push(checkout: Path, timeout: int, *arguments: str) -> object:
            nonlocal push_calls
            completed = original_transport(checkout, timeout, *arguments)
            if "push" in arguments:
                push_calls += 1
                return subprocess.CompletedProcess(completed.args, 1, completed.stdout, b"disconnect")
            return completed

        with patch(
            "peoplebot.synchronization._transport_git",
            side_effect=disconnect_after_push,
        ):
            result = run_memory_synchronization_execution(
                self.root / "runtime",
                GitAttemptStore(self.repository, REPOSITORY),
                self.repository,
                start,
                self.request(),
                lambda: "2026-09-10T12:02:21Z",
            )
        self.assertEqual(push_calls, 1)
        self.assertEqual(result.observation.disposition, SynchronizationDisposition.UNCERTAIN)
        self.assertEqual(result.observation.code, "synchronization.transport_result_uncertain")
        self.assertEqual(git(self.remote, "rev-parse", self.ref_name), self.memory.commit)
        self.assertTrue(result.provenance.terminal_committed)

    def test_pipe_holding_transport_descendant_retains_admission_until_recovered(self) -> None:
        start = ExecutionStart(
            execution_id="execution:transport-descendant",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Retain ownership while a transport descendant holds pipes",
            started_at="2026-09-10T12:02:30Z",
            starting_state=self.memory,
            blueprint=self.blueprint,
            adapter=self.blueprint,
        )
        original_transport = synchronization_module._transport_git
        child_script = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(17)'],"
            "stdout=sys.stdout,stderr=sys.stderr);"
            "time.sleep(60)"
        )

        def descendant_transport(checkout: Path, timeout: int, *arguments: str) -> object:
            if "push" not in arguments:
                return original_transport(checkout, timeout, *arguments)
            return _run_process(
                (sys.executable, "-c", child_script),
                b"",
                os.environ.copy(),
                1,
            )

        with (
            patch(
                "peoplebot.synchronization._transport_git",
                side_effect=descendant_transport,
            ),
            self.assertRaises(ProcessOwnershipUnresolved) as caught,
        ):
            run_memory_synchronization_execution(
                self.root / "runtime",
                GitAttemptStore(self.repository, REPOSITORY),
                self.repository,
                start,
                self.request(),
                lambda: "2026-09-10T12:02:31Z",
            )

        unresolved = caught.exception
        self.assertIsNotNone(unresolved.retained_admission)
        self.assertTrue(unresolved.retained_admission.owns_admission)
        contender = try_acquire_execution(
            self.root / "runtime",
            ENVIRONMENT,
            INSTANCE,
            "execution:blocked-by-descendant",
        )
        self.assertFalse(contender.acquired)
        self.assertTrue(unresolved.recover(timeout_seconds=5))
        unresolved.release_after_recovery()
        self.assertFalse(unresolved.retained_admission.owns_admission)
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), "")

    def test_late_record_failure_preserves_verified_transport_observation(self) -> None:
        start = ExecutionStart(
            execution_id="execution:late-record-failure",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Keep verified transport evidence after record failure",
            started_at="2026-09-10T12:03:00Z",
            starting_state=self.memory,
            blueprint=self.blueprint,
            adapter=self.blueprint,
        )
        store = GitAttemptStore(self.repository, REPOSITORY)
        finish_calls = 0

        def finished_at() -> str:
            nonlocal finish_calls
            finish_calls += 1
            if finish_calls == 1:
                raise RuntimeError("injected one-time finish failure")
            return "2026-09-10T12:03:01Z"

        with patch(
            "peoplebot.synchronization._transport_git",
            wraps=synchronization_module._transport_git,
        ) as transport:
            result = run_memory_synchronization_execution(
                self.root / "runtime",
                store,
                self.repository,
                start,
                self.request(),
                finished_at,
            )

        push_calls = [call for call in transport.call_args_list if "push" in call.args]
        self.assertEqual(len(push_calls), 1)
        self.assertEqual(finish_calls, 2)
        self.assertEqual(git(self.remote, "rev-parse", self.ref_name), self.memory.commit)
        self.assertEqual(
            result.observation.disposition,
            SynchronizationDisposition.REMOTE_VERIFIED,
        )
        self.assertEqual(result.observation.observed_remote_commit, self.memory.commit)
        self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.FAILED)
        self.assertEqual(
            result.provenance.execution_record.terminal_outcome.code,
            "synchronization.execution_record_failed",
        )
        self.assertIsNotNone(result.provenance.task_failure)
        evidence = result.observation_evidence
        self.assertIsNotNone(evidence)
        self.assertEqual(store.read_evidence_bytes(evidence), result.observation.to_json_bytes())

    def test_fresh_checkout_recovery_preserves_checkout_and_resumes_exact_utf8(self) -> None:
        fresh = self.publish_for_recovery()
        (fresh / "blueprint.json").write_text("staged change\n", encoding="utf-8")
        git(fresh, "add", "blueprint.json")
        (fresh / "blueprint.json").write_text("working change\n", encoding="utf-8")
        (fresh / "untracked.txt").write_text("preserve me\n", encoding="utf-8")
        git(fresh, "update-ref", "refs/heads/unrelated-local", self.baseline)
        index_path = Path(git(fresh, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        before = (
            git(fresh, "symbolic-ref", "HEAD"),
            index_path.read_bytes(),
            git(fresh, "status", "--porcelain=v1", "-uall"),
            git(fresh, "worktree", "list", "--porcelain"),
        )
        fetch_head = Path(git(fresh, "rev-parse", "--path-format=absolute", "--git-path", "FETCH_HEAD"))
        transport_refs_before = (
            git(fresh, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/remotes"),
            git(fresh, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/tags"),
            fetch_head.read_bytes() if fetch_head.exists() else None,
        )
        start = ExecutionStart(
            execution_id="execution:fresh-recovery",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Recover exact Instance memory in the owning environment",
            started_at="2026-09-10T12:04:00Z",
            starting_state=StateRef(REPOSITORY, self.baseline),
            blueprint=self.blueprint,
            adapter=self.blueprint,
            input_states=(self.memory,),
        )
        store = GitAttemptStore(fresh, REPOSITORY)

        result = run_memory_recovery_execution(
            self.root / "recovery-runtime",
            store,
            fresh,
            start,
            self.recovery_request(),
            lambda: "2026-09-10T12:04:01Z",
        )

        self.assertTrue(result.provenance.terminal_committed, result.provenance)
        self.assertEqual(
            result.observation.disposition,
            SynchronizationDisposition.REMOTE_VERIFIED,
            result.recovery,
        )
        self.assertEqual(git(fresh, "rev-parse", self.ref_name), self.memory.commit)
        after = (
            git(fresh, "symbolic-ref", "HEAD"),
            index_path.read_bytes(),
            git(fresh, "status", "--porcelain=v1", "-uall"),
            git(fresh, "worktree", "list", "--porcelain"),
        )
        self.assertEqual(after, before)
        self.assertEqual(
            (
                git(fresh, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/remotes"),
                git(fresh, "for-each-ref", "--format=%(refname)%00%(objectname)", "refs/tags"),
                fetch_head.read_bytes() if fetch_head.exists() else None,
            ),
            transport_refs_before,
        )
        self.assertEqual(git(fresh, "rev-parse", "refs/heads/unrelated-local"), self.baseline)
        self.assertEqual(git(fresh, "rev-parse", "--verify", "--quiet", result.recovery.quarantine_ref, check=False), "")
        self.assertEqual(git(fresh, "rev-parse", "--verify", "--quiet", result.recovery.owner_ref, check=False), "")
        policy = ContextPolicy(
            StateRef(REPOSITORY, self.memory.commit, "memory.json"),
            max_entries=4,
            max_blob_bytes=65_536,
            max_total_blob_bytes=65_536,
        )
        resumed = assemble_instance_memory_context(
            fresh,
            self.memory,
            ENVIRONMENT,
            INSTANCE,
            self.blueprint,
            ("resume.md",),
            policy,
        )
        self.assertEqual(resumed.documents[0].content, "exact UTF-8 memory ✅\n")
        evidence = result.observation_evidence
        self.assertIsNotNone(evidence)
        self.assertEqual(store.read_evidence_bytes(evidence), result.observation.to_json_bytes())
        remote_refs = git(self.remote, "for-each-ref", "--format=%(refname)")
        self.assertEqual(
            remote_refs.splitlines(),
            ["refs/heads/main", self.ref_name],
        )

    def test_uncertain_recovery_requires_exact_explicit_continuation(self) -> None:
        fresh = self.publish_for_recovery()
        request = self.recovery_request(operation_id="recovery:explicit-continuation")
        original_transport = synchronization_module._transport_git
        fetch_calls = 0

        def timeout_fetch(checkout: Path, timeout: int, *arguments: str) -> object:
            nonlocal fetch_calls
            if "fetch" in arguments:
                fetch_calls += 1
                raise DirectProcessTimeout(("git", *arguments), timeout)
            return original_transport(checkout, timeout, *arguments)

        with patch(
            "peoplebot.synchronization._transport_git",
            side_effect=timeout_fetch,
        ):
            uncertain = synchronization_module.recover_instance_memory(fresh, request)

        self.assertEqual(fetch_calls, 1)
        self.assertEqual(
            uncertain.observation.disposition,
            SynchronizationDisposition.UNCERTAIN,
        )
        self.assertTrue(uncertain.observation.recovery_refs_retained)
        self.assertFalse(uncertain.observation.recovery_resume)
        self.assertNotEqual(git(fresh, "rev-parse", uncertain.owner_ref), "")
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", uncertain.quarantine_ref, check=False),
            "",
        )
        with self.assertRaisesRegex(SynchronizationError, "quarantine_owned"):
            synchronization_module.recover_instance_memory(fresh, request)

        resumed = synchronization_module.recover_instance_memory(
            fresh,
            replace(request, resume_existing=True),
        )

        self.assertEqual(
            resumed.observation.disposition,
            SynchronizationDisposition.REMOTE_VERIFIED,
        )
        self.assertTrue(resumed.observation.recovery_resume)
        self.assertEqual(git(fresh, "rev-parse", self.ref_name), self.memory.commit)
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", resumed.quarantine_ref, check=False),
            "",
        )
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", resumed.owner_ref, check=False),
            "",
        )

    def test_dangling_symbolic_quarantine_is_refused_before_object_retrieval(self) -> None:
        fresh = self.publish_for_recovery()
        request = self.recovery_request(operation_id="recovery:dangling-symbolic")
        digest = synchronization_module._destination_digest(self.destination)
        quarantine, _ = synchronization_module._recovery_refs(request, digest)
        unrelated = "refs/heads/unrelated-absent-target"
        git(fresh, "symbolic-ref", quarantine, unrelated)

        with (
            patch(
                "peoplebot.synchronization._transport_git",
                wraps=synchronization_module._transport_git,
            ) as transport,
            self.assertRaisesRegex(SynchronizationError, "ref_symbolic"),
        ):
            synchronization_module.recover_instance_memory(fresh, request)

        fetches = [call for call in transport.call_args_list if "fetch" in call.args]
        self.assertEqual(fetches, [])
        self.assertEqual(git(fresh, "symbolic-ref", quarantine), unrelated)
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", unrelated, check=False),
            "",
        )

    def test_dangling_symbolic_owner_is_refused_before_object_retrieval(self) -> None:
        fresh = self.publish_for_recovery()
        request = self.recovery_request(operation_id="recovery:dangling-owner")
        digest = synchronization_module._destination_digest(self.destination)
        quarantine, owner = synchronization_module._recovery_refs(request, digest)
        unrelated = "refs/heads/unrelated-absent-owner-target"
        git(fresh, "symbolic-ref", owner, unrelated)

        with (
            patch(
                "peoplebot.synchronization._transport_git",
                wraps=synchronization_module._transport_git,
            ) as transport,
            self.assertRaisesRegex(SynchronizationError, "ref_symbolic"),
        ):
            synchronization_module.recover_instance_memory(fresh, request)

        fetches = [call for call in transport.call_args_list if "fetch" in call.args]
        self.assertEqual(fetches, [])
        self.assertEqual(git(fresh, "symbolic-ref", owner), unrelated)
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", unrelated, check=False),
            "",
        )
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", quarantine, check=False),
            "",
        )

    def test_recovery_record_failure_retains_recovered_state_artifact(self) -> None:
        fresh = self.publish_for_recovery()
        start = ExecutionStart(
            execution_id="execution:recovery-record-failure",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Retain exact recovered State after record construction fails",
            started_at="2026-09-10T12:04:10Z",
            starting_state=StateRef(REPOSITORY, self.baseline),
            blueprint=self.blueprint,
            adapter=self.blueprint,
            input_states=(self.memory,),
        )
        store = GitAttemptStore(fresh, REPOSITORY)
        finish_calls = 0

        def finished_at() -> str:
            nonlocal finish_calls
            finish_calls += 1
            if finish_calls == 1:
                raise RuntimeError("injected one-time recovery record failure")
            return "2026-09-10T12:04:11Z"

        with patch(
            "peoplebot.synchronization._transport_git",
            wraps=synchronization_module._transport_git,
        ) as transport:
            result = run_memory_recovery_execution(
                self.root / "recovery-record-runtime",
                store,
                fresh,
                start,
                self.recovery_request(operation_id="recovery:record-failure"),
                finished_at,
            )

        fetches = [call for call in transport.call_args_list if "fetch" in call.args]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(finish_calls, 2)
        self.assertEqual(result.recovery.recovered_state, self.memory)
        self.assertEqual(
            result.observation.disposition,
            SynchronizationDisposition.REMOTE_VERIFIED,
        )
        self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.FAILED)
        self.assertIsNone(result.provenance.execution_record.resulting_state)
        self.assertEqual(result.provenance.execution_record.artifacts, (self.memory,))
        self.assertEqual(
            result.provenance.execution_record.terminal_outcome.code,
            "recovery.execution_record_failed",
        )
        evidence = result.observation_evidence
        self.assertIsNotNone(evidence)
        self.assertEqual(store.read_evidence_bytes(evidence), result.observation.to_json_bytes())
        contender = try_acquire_execution(
            self.root / "recovery-record-runtime",
            ENVIRONMENT,
            INSTANCE,
            "execution:after-record-failure",
        )
        self.assertTrue(contender.acquired)
        contender.admission.release()

    def test_unavailable_recovery_record_keeps_returned_recovery_fact(self) -> None:
        fresh = self.publish_for_recovery()
        start = ExecutionStart(
            execution_id="execution:recovery-record-unavailable",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Keep recovery result when no terminal timestamp is available",
            started_at="2026-09-10T12:04:15Z",
            starting_state=StateRef(REPOSITORY, self.baseline),
            blueprint=self.blueprint,
            adapter=self.blueprint,
            input_states=(self.memory,),
        )
        calls = 0

        def unavailable_timestamp() -> str:
            nonlocal calls
            calls += 1
            raise RuntimeError("injected unavailable terminal timestamp")

        with patch(
            "peoplebot.synchronization._transport_git",
            wraps=synchronization_module._transport_git,
        ) as transport:
            result = run_memory_recovery_execution(
                self.root / "recovery-record-unavailable-runtime",
                GitAttemptStore(fresh, REPOSITORY),
                fresh,
                start,
                self.recovery_request(operation_id="recovery:record-unavailable"),
                unavailable_timestamp,
            )

        fetches = [call for call in transport.call_args_list if "fetch" in call.args]
        self.assertEqual(len(fetches), 1)
        self.assertEqual(calls, 2)
        self.assertEqual(result.recovery.recovered_state, self.memory)
        self.assertEqual(git(fresh, "rev-parse", self.ref_name), self.memory.commit)
        self.assertIsNone(result.provenance.execution_record)
        self.assertIsNone(result.provenance.terminal_evidence)
        self.assertEqual(
            result.provenance.record_failure.code,
            "provenance.execution_record_unavailable",
        )
        self.assertIsNotNone(result.provenance.start_evidence)

    def test_recovery_transport_setup_error_reports_retained_owner(self) -> None:
        fresh = self.publish_for_recovery()
        start = ExecutionStart(
            execution_id="execution:recovery-setup-error",
            environment_id=ENVIRONMENT,
            instance_id=INSTANCE,
            objective="Preserve exact recovery refs after transport setup fails",
            started_at="2026-09-10T12:04:20Z",
            starting_state=StateRef(REPOSITORY, self.baseline),
            blueprint=self.blueprint,
            adapter=self.blueprint,
            input_states=(self.memory,),
        )
        store = GitAttemptStore(fresh, REPOSITORY)
        original_transport = synchronization_module._transport_git
        fetch_calls = 0

        def fail_fetch_setup(checkout: Path, timeout: int, *arguments: str) -> object:
            nonlocal fetch_calls
            if "fetch" in arguments:
                fetch_calls += 1
                raise OSError("injected process creation failure")
            return original_transport(checkout, timeout, *arguments)

        with patch(
            "peoplebot.synchronization._transport_git",
            side_effect=fail_fetch_setup,
        ):
            result = run_memory_recovery_execution(
                self.root / "recovery-setup-runtime",
                store,
                fresh,
                start,
                self.recovery_request(operation_id="recovery:setup-error"),
                lambda: "2026-09-10T12:04:21Z",
            )

        self.assertEqual(fetch_calls, 1)
        self.assertEqual(result.observation.disposition, SynchronizationDisposition.UNCERTAIN)
        self.assertEqual(result.observation.code, "recovery.transport_uncertain")
        self.assertTrue(result.observation.recovery_refs_retained)
        self.assertNotEqual(git(fresh, "rev-parse", result.recovery.owner_ref), "")
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", result.recovery.quarantine_ref, check=False),
            "",
        )
        self.assertEqual(result.provenance.execution_record.status, ExecutionStatus.FAILED)
        evidence = result.observation_evidence
        self.assertIsNotNone(evidence)
        self.assertEqual(store.read_evidence_bytes(evidence), result.observation.to_json_bytes())
        contender = try_acquire_execution(
            self.root / "recovery-setup-runtime",
            ENVIRONMENT,
            INSTANCE,
            "execution:after-setup-error",
        )
        self.assertTrue(contender.acquired)
        contender.admission.release()

    def test_busy_instance_refuses_recovery_before_fetch_or_local_ref_change(self) -> None:
        fresh = self.publish_for_recovery()
        runtime = self.root / "busy-runtime"
        owner = try_acquire_execution(runtime, ENVIRONMENT, INSTANCE, "execution:owner")
        self.assertTrue(owner.acquired)
        try:
            result = run_memory_recovery_execution(
                runtime,
                GitAttemptStore(fresh, REPOSITORY),
                fresh,
                ExecutionStart(
                    execution_id="execution:busy-recovery",
                    environment_id=ENVIRONMENT,
                    instance_id=INSTANCE,
                    objective="Refuse overlapping recovery",
                    started_at="2026-09-10T12:05:00Z",
                    starting_state=StateRef(REPOSITORY, self.baseline),
                    blueprint=self.blueprint,
                    adapter=self.blueprint,
                ),
                self.recovery_request(operation_id="recovery:busy"),
                lambda: "2026-09-10T12:05:01Z",
            )
        finally:
            owner.admission.release()
        self.assertFalse(result.provenance.task_started)
        self.assertIsNone(result.recovery)
        self.assertEqual(git(fresh, "rev-parse", "--verify", "--quiet", self.ref_name, check=False), "")

    def test_recovery_refuses_checked_out_destination_and_retains_quarantine(self) -> None:
        fresh = self.publish_for_recovery()
        branch = self.ref_name.removeprefix("refs/heads/")
        git(fresh, "checkout", "-b", branch)
        before = (git(fresh, "symbolic-ref", "HEAD"), git(fresh, "status", "--porcelain=v1", "-uall"))
        result = run_memory_recovery_execution(
            self.root / "checked-out-runtime",
            GitAttemptStore(fresh, REPOSITORY),
            fresh,
            ExecutionStart(
                execution_id="execution:checked-out-recovery",
                environment_id=ENVIRONMENT,
                instance_id=INSTANCE,
                objective="Refuse a checked-out recovery destination",
                started_at="2026-09-10T12:06:00Z",
                starting_state=StateRef(REPOSITORY, self.baseline),
                blueprint=self.blueprint,
                adapter=self.blueprint,
            ),
            self.recovery_request(operation_id="recovery:checked-out"),
            lambda: "2026-09-10T12:06:01Z",
        )
        self.assertEqual(result.observation.disposition, SynchronizationDisposition.FAILED)
        self.assertEqual(result.observation.code, "memory.destination_checked_out")
        self.assertTrue(result.observation.recovery_refs_retained)
        self.assertEqual(
            (git(fresh, "symbolic-ref", "HEAD"), git(fresh, "status", "--porcelain=v1", "-uall")),
            before,
        )
        self.assertEqual(git(fresh, "rev-parse", self.ref_name), self.baseline)
        self.assertEqual(git(fresh, "rev-parse", result.recovery.quarantine_ref), self.memory.commit)

    def test_recovery_refuses_competing_owner_before_fetch(self) -> None:
        fresh = self.publish_for_recovery()
        request = self.recovery_request(operation_id="recovery:competing-owner")
        digest = synchronization_module._destination_digest(self.destination)
        quarantine, owner = synchronization_module._recovery_refs(request, digest)
        git(fresh, "update-ref", owner, self.baseline)

        with self.assertRaisesRegex(SynchronizationError, "quarantine_owned"):
            synchronization_module.recover_instance_memory(fresh, request)

        self.assertEqual(git(fresh, "rev-parse", owner), self.baseline)
        self.assertEqual(git(fresh, "rev-parse", "--verify", "--quiet", quarantine, check=False), "")
        self.assertEqual(git(fresh, "rev-parse", "--verify", "--quiet", self.ref_name, check=False), "")

    def test_owner_replacement_prevents_canonical_publication(self) -> None:
        fresh = self.publish_for_recovery()
        request = self.recovery_request(operation_id="recovery:owner-replacement")
        digest = synchronization_module._destination_digest(self.destination)
        quarantine, owner = synchronization_module._recovery_refs(request, digest)
        original_updates = GitAttemptStore._update_refs
        replaced = False

        def replace_before_publication(
            store: GitAttemptStore,
            updates: Mapping[str, tuple[str | None, str]],
            **kwargs: object,
        ) -> None:
            nonlocal replaced
            canonical = updates.get(self.ref_name)
            if not replaced and canonical is not None and canonical[0] == self.memory.commit:
                git(Path(store.checkout), "update-ref", owner, self.baseline)
                replaced = True
            original_updates(store, updates, **kwargs)  # type: ignore[arg-type]

        with patch.object(GitAttemptStore, "_update_refs", new=replace_before_publication):
            result = synchronization_module.recover_instance_memory(fresh, request)

        self.assertTrue(replaced)
        self.assertEqual(result.observation.disposition, SynchronizationDisposition.FAILED)
        self.assertEqual(result.observation.code, "recovery.publication_conflict")
        self.assertTrue(result.observation.recovery_refs_retained)
        self.assertEqual(git(fresh, "rev-parse", owner), self.baseline)
        self.assertEqual(git(fresh, "rev-parse", quarantine), self.memory.commit)
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", self.ref_name, check=False),
            "",
        )

    def test_owner_replacement_prevents_quarantine_establishment(self) -> None:
        fresh = self.publish_for_recovery()
        request = self.recovery_request(operation_id="recovery:owner-before-quarantine")
        digest = synchronization_module._destination_digest(self.destination)
        quarantine, owner = synchronization_module._recovery_refs(request, digest)
        original_updates = GitAttemptStore._update_refs
        replaced = False

        def replace_after_fetch(
            store: GitAttemptStore,
            updates: Mapping[str, tuple[str | None, str]],
            **kwargs: object,
        ) -> None:
            nonlocal replaced
            quarantine_update = updates.get(quarantine)
            if (
                not replaced
                and quarantine_update is not None
                and quarantine_update[0] == self.memory.commit
                and self.ref_name not in updates
            ):
                git(Path(store.checkout), "update-ref", owner, self.baseline)
                replaced = True
            original_updates(store, updates, **kwargs)  # type: ignore[arg-type]

        with patch.object(GitAttemptStore, "_update_refs", new=replace_after_fetch):
            result = synchronization_module.recover_instance_memory(fresh, request)

        self.assertTrue(replaced)
        self.assertEqual(result.observation.disposition, SynchronizationDisposition.FAILED)
        self.assertEqual(result.observation.code, "recovery.quarantine_transition_conflict")
        self.assertTrue(result.observation.recovery_refs_retained)
        self.assertEqual(git(fresh, "rev-parse", owner), self.baseline)
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", quarantine, check=False),
            "",
        )
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", self.ref_name, check=False),
            "",
        )

    def test_quarantine_replacement_prevents_canonical_publication(self) -> None:
        fresh = self.publish_for_recovery()
        request = self.recovery_request(operation_id="recovery:quarantine-replacement")
        digest = synchronization_module._destination_digest(self.destination)
        quarantine, owner = synchronization_module._recovery_refs(request, digest)
        original_updates = GitAttemptStore._update_refs
        replaced = False

        def replace_before_publication(
            store: GitAttemptStore,
            updates: Mapping[str, tuple[str | None, str]],
            **kwargs: object,
        ) -> None:
            nonlocal replaced
            canonical = updates.get(self.ref_name)
            if not replaced and canonical is not None and canonical[0] == self.memory.commit:
                git(Path(store.checkout), "update-ref", quarantine, self.baseline)
                replaced = True
            original_updates(store, updates, **kwargs)  # type: ignore[arg-type]

        with patch.object(GitAttemptStore, "_update_refs", new=replace_before_publication):
            result = synchronization_module.recover_instance_memory(fresh, request)

        self.assertTrue(replaced)
        self.assertEqual(result.observation.disposition, SynchronizationDisposition.FAILED)
        self.assertEqual(result.observation.code, "recovery.publication_conflict")
        self.assertTrue(result.observation.recovery_refs_retained)
        self.assertNotEqual(git(fresh, "rev-parse", owner), "")
        self.assertEqual(git(fresh, "rev-parse", quarantine), self.baseline)
        self.assertEqual(
            git(fresh, "rev-parse", "--verify", "--quiet", self.ref_name, check=False),
            "",
        )

    def test_recovery_cleanup_refuses_replaced_quarantine(self) -> None:
        fresh = self.publish_for_recovery()
        request = self.recovery_request(operation_id="recovery:cleanup-race")
        digest = synchronization_module._destination_digest(self.destination)
        quarantine, owner = synchronization_module._recovery_refs(request, digest)
        original_updates = GitAttemptStore._update_refs
        replaced = False

        def replace_before_cleanup(
            store: GitAttemptStore,
            updates: Mapping[str, tuple[str | None, str]],
            **kwargs: object,
        ) -> None:
            nonlocal replaced
            cleanup = updates.get(quarantine)
            if not replaced and cleanup is not None and cleanup[0] == "0" * 40:
                git(Path(store.checkout), "update-ref", quarantine, self.baseline, self.memory.commit)
                replaced = True
            original_updates(store, updates, **kwargs)  # type: ignore[arg-type]

        with patch.object(GitAttemptStore, "_update_refs", new=replace_before_cleanup):
            result = synchronization_module.recover_instance_memory(fresh, request)

        self.assertTrue(replaced)
        self.assertEqual(result.observation.disposition, SynchronizationDisposition.REMOTE_VERIFIED)
        self.assertEqual(result.observation.code, "recovery.remote_verified_cleanup_retained")
        self.assertTrue(result.observation.recovery_refs_retained)
        self.assertEqual(git(fresh, "rev-parse", self.ref_name), self.memory.commit)
        self.assertEqual(git(fresh, "rev-parse", result.quarantine_ref), self.baseline)
        self.assertNotEqual(git(fresh, "rev-parse", result.owner_ref), "")

    def test_cleanup_owner_replacement_preserves_quarantine_atomically(self) -> None:
        fresh = self.publish_for_recovery()
        request = self.recovery_request(operation_id="recovery:cleanup-owner-race")
        digest = synchronization_module._destination_digest(self.destination)
        quarantine, owner = synchronization_module._recovery_refs(request, digest)
        original_updates = GitAttemptStore._update_refs
        replaced = False

        def replace_before_cleanup(
            store: GitAttemptStore,
            updates: Mapping[str, tuple[str | None, str]],
            **kwargs: object,
        ) -> None:
            nonlocal replaced
            cleanup = updates.get(owner)
            if not replaced and cleanup is not None and cleanup[0] == "0" * 40:
                git(Path(store.checkout), "update-ref", owner, self.baseline)
                replaced = True
            original_updates(store, updates, **kwargs)  # type: ignore[arg-type]

        with patch.object(GitAttemptStore, "_update_refs", new=replace_before_cleanup):
            result = synchronization_module.recover_instance_memory(fresh, request)

        self.assertTrue(replaced)
        self.assertEqual(result.observation.disposition, SynchronizationDisposition.REMOTE_VERIFIED)
        self.assertEqual(result.observation.code, "recovery.remote_verified_cleanup_retained")
        self.assertTrue(result.observation.recovery_refs_retained)
        self.assertEqual(git(fresh, "rev-parse", self.ref_name), self.memory.commit)
        self.assertEqual(git(fresh, "rev-parse", quarantine), self.memory.commit)
        self.assertEqual(git(fresh, "rev-parse", owner), self.baseline)

    def test_recovery_preserves_newer_local_memory_and_retains_quarantine(self) -> None:
        fresh = self.publish_for_recovery()
        newer = self.checkpoint_second()
        git(fresh, "fetch", str(self.repository), newer.commit)
        git(fresh, "update-ref", self.ref_name, newer.commit)
        request = self.recovery_request(
            expected_local_state=newer,
            operation_id="recovery:newer-local",
        )

        result = synchronization_module.recover_instance_memory(fresh, request)

        self.assertEqual(result.observation.disposition, SynchronizationDisposition.FAILED)
        self.assertEqual(result.observation.code, "recovery.local_state_conflict")
        self.assertTrue(result.observation.recovery_refs_retained)
        self.assertEqual(git(fresh, "rev-parse", self.ref_name), newer.commit)
        self.assertEqual(git(fresh, "rev-parse", result.quarantine_ref), self.memory.commit)

    def test_recovery_guardedly_fast_forwards_expected_local_memory(self) -> None:
        first_sync = synchronize_instance_memory(self.repository, self.request())
        self.assertEqual(first_sync.disposition, SynchronizationDisposition.REMOTE_VERIFIED)
        second = self.checkpoint_second()
        second_sync = synchronize_instance_memory(
            self.repository,
            self.request(memory_state=second, expected_remote_state=self.memory),
        )
        self.assertEqual(second_sync.disposition, SynchronizationDisposition.REMOTE_VERIFIED)
        git(self.repository, "push", str(self.remote), f"{self.baseline}:refs/heads/main")
        fresh = self.root / "fresh-fast-forward"
        subprocess.run(
            ["git", "clone", "--single-branch", "--branch", "main", str(self.remote), str(fresh)],
            capture_output=True,
            check=True,
            shell=False,
            timeout=15,
        )
        git(fresh, "remote", "rename", "origin", "memory")
        git(fresh, "remote", "set-url", "--push", "memory", str(self.remote))
        git(fresh, "fetch", str(self.repository), self.memory.commit)
        git(fresh, "update-ref", self.ref_name, self.memory.commit)
        request = MemoryRecoveryRequest(
            self.request(memory_state=second, expected_remote_state=second),
            "recovery:fast-forward",
            self.memory,
            True,
        )

        result = synchronization_module.recover_instance_memory(fresh, request)

        self.assertEqual(result.observation.disposition, SynchronizationDisposition.REMOTE_VERIFIED)
        self.assertEqual(result.recovered_state, second)
        self.assertEqual(git(fresh, "rev-parse", self.ref_name), second.commit)
        self.assertEqual(git(fresh, "rev-parse", "--verify", "--quiet", result.quarantine_ref, check=False), "")
        self.assertEqual(git(fresh, "rev-parse", "--verify", "--quiet", result.owner_ref, check=False), "")

    def test_recovery_rejects_invalid_remote_tree_and_keeps_quarantine(self) -> None:
        invalid, _ = self.memory_descendant_with_extra(remove_later=False)
        git(self.repository, "push", str(self.remote), f"{self.baseline}:refs/heads/main")
        git(self.repository, "push", str(self.remote), f"{invalid}:{self.ref_name}")
        fresh = self.root / "fresh-invalid"
        subprocess.run(
            ["git", "clone", "--single-branch", "--branch", "main", str(self.remote), str(fresh)],
            capture_output=True,
            check=True,
            shell=False,
            timeout=15,
        )
        git(fresh, "remote", "rename", "origin", "memory")
        git(fresh, "remote", "set-url", "--push", "memory", str(self.remote))
        sync_request = self.request(
            memory_state=StateRef(REPOSITORY, invalid),
            expected_remote_state=StateRef(REPOSITORY, invalid),
        )
        request = MemoryRecoveryRequest(
            sync_request,
            "recovery:invalid-tree",
            None,
            True,
        )

        store = GitAttemptStore(fresh, REPOSITORY)
        execution = run_memory_recovery_execution(
            self.root / "invalid-recovery-runtime",
            store,
            fresh,
            ExecutionStart(
                execution_id="execution:invalid-recovery",
                environment_id=ENVIRONMENT,
                instance_id=INSTANCE,
                objective="Refuse invalid remote memory",
                started_at="2026-09-10T12:07:00Z",
                starting_state=StateRef(REPOSITORY, self.baseline),
                blueprint=self.blueprint,
                adapter=self.blueprint,
            ),
            request,
            lambda: "2026-09-10T12:07:01Z",
        )
        result = execution.recovery

        self.assertEqual(result.observation.disposition, SynchronizationDisposition.FAILED)
        self.assertEqual(result.observation.code, "synchronization.memory_tree_invalid")
        self.assertTrue(result.observation.recovery_refs_retained)
        self.assertEqual(git(fresh, "rev-parse", result.quarantine_ref), invalid)
        self.assertEqual(git(fresh, "rev-parse", "--verify", "--quiet", self.ref_name, check=False), "")
        evidence = execution.observation_evidence
        self.assertIsNotNone(evidence)
        self.assertEqual(store.read_evidence_bytes(evidence), result.observation.to_json_bytes())


if __name__ == "__main__":
    unittest.main()
