from __future__ import annotations

import copy
import errno
import multiprocessing
import os
import pickle
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import peoplebot.admission as admission_module
from peoplebot import AdmissionError, run_with_admission, try_acquire_execution


ENVIRONMENT = "environment:test"
INSTANCE = "instance:test"


def _competing_process(
    runtime_root: str,
    start: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
    execution_id: str,
) -> None:
    try:
        if not start.wait(10):
            results.put(("worker.start_timeout", execution_id))
            return
        attempt = try_acquire_execution(
            runtime_root,
            ENVIRONMENT,
            INSTANCE,
            execution_id,
        )
        results.put((attempt.code, execution_id))
        if attempt.acquired:
            if not release.wait(10):
                results.put(("worker.release_timeout", execution_id))
            attempt.admission.release()
    except BaseException as error:
        results.put(("worker.error", f"{type(error).__name__}: {error}"))


def _terminable_owner(
    runtime_root: str,
    ready: multiprocessing.synchronize.Event,
    hold: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue,
) -> None:
    try:
        attempt = try_acquire_execution(
            runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:terminated",
        )
        results.put(attempt.code)
        if not attempt.acquired:
            return
        ready.set()
        hold.wait(30)
        attempt.admission.release()
    except BaseException as error:
        results.put(f"worker.error:{type(error).__name__}:{error}")


def _first_use_process(
    runtime_root: str,
    opened: multiprocessing.queues.Queue,
    begin_locking: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event,
    observations: multiprocessing.queues.Queue,
    execution_id: str,
) -> None:
    original_open = admission_module._open_resource

    def coordinated_open(path: Path) -> int:
        descriptor = original_open(path)
        opened.put(execution_id)
        if not begin_locking.wait(10):
            os.close(descriptor)
            raise RuntimeError("worker locking barrier timed out")
        return descriptor

    admission_module._open_resource = coordinated_open

    def task() -> None:
        observations.put(("task.entered", execution_id))
        if not release.wait(10):
            observations.put(("worker.release_timeout", execution_id))

    try:
        result = run_with_admission(
            runtime_root,
            ENVIRONMENT,
            INSTANCE,
            execution_id,
            task,
        )
        observations.put(
            ("run.result", execution_id, result.task_started, result.rejection_code)
        )
    except BaseException as error:
        observations.put(("worker.error", f"{type(error).__name__}: {error}"))


class _UnexpectedLockFailure:
    LK_NBLCK = 1
    LK_UNLCK = 0

    @staticmethod
    def locking(descriptor: int, mode: int, count: int) -> None:
        raise OSError(errno.EIO, "injected locking failure")


class _UnlockFailure:
    def __init__(self, locking_module: object) -> None:
        self._locking_module = locking_module
        self.LK_NBLCK = locking_module.LK_NBLCK
        self.LK_UNLCK = locking_module.LK_UNLCK

    def locking(self, descriptor: int, mode: int, count: int) -> None:
        if mode == self.LK_UNLCK:
            raise OSError(errno.EIO, "injected unlock failure")
        self._locking_module.locking(descriptor, mode, count)


@unittest.skipUnless(os.name == "nt", "instance admission v0 is Windows-only")
class AdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime_root = Path(self.temporary.name) / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_two_processes_admit_exactly_one_owner(self) -> None:
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        release = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_competing_process,
                args=(str(self.runtime_root), start, release, results, f"execution:{index}"),
            )
            for index in range(2)
        ]
        try:
            for process in processes:
                process.start()
            start.set()
            outcomes = [results.get(timeout=10) for _ in processes]
            self.assertCountEqual(
                [outcome[0] for outcome in outcomes],
                ["admission.acquired", "instance.already_running"],
            )
        finally:
            release.set()
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        results.close()
        results.join_thread()

    def test_rejected_attempt_never_enters_task_code(self) -> None:
        owner = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:owner",
        )
        entered: list[bool] = []
        try:
            result = run_with_admission(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:rejected",
                lambda: entered.append(True),
            )
            self.assertFalse(result.task_started)
            self.assertEqual(result.rejection_code, "instance.already_running")
            self.assertIsNone(result.value)
            self.assertEqual(entered, [])
        finally:
            owner.admission.release()

    def test_duplicate_calls_in_one_process_are_exclusive(self) -> None:
        first = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:first",
        )
        try:
            second = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:second",
            )
            self.assertTrue(first.acquired)
            self.assertFalse(second.acquired)
            self.assertEqual(second.code, "instance.already_running")
        finally:
            first.admission.release()

    def test_different_instances_acquire_independently(self) -> None:
        first = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            "instance:first",
            "execution:first",
        )
        second = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            "instance:second",
            "execution:second",
        )
        other_environment = try_acquire_execution(
            self.runtime_root,
            "environment:other",
            "instance:first",
            "execution:other-environment",
        )
        try:
            self.assertTrue(first.acquired)
            self.assertTrue(second.acquired)
            self.assertTrue(other_environment.acquired)
        finally:
            first.admission.release()
            second.admission.release()
            other_environment.admission.release()

    def test_first_use_empty_resource_has_no_prelock_initialization_race(self) -> None:
        context = multiprocessing.get_context("spawn")
        opened = context.Queue()
        begin_locking = context.Event()
        release = context.Event()
        observations = context.Queue()
        processes = [
            context.Process(
                target=_first_use_process,
                args=(
                    str(self.runtime_root),
                    opened,
                    begin_locking,
                    release,
                    observations,
                    f"execution:first-use:{index}",
                ),
            )
            for index in range(2)
        ]
        try:
            for process in processes:
                process.start()
            opened_ids = {opened.get(timeout=10) for _ in processes}
            self.assertEqual(
                opened_ids,
                {"execution:first-use:0", "execution:first-use:1"},
            )
            resources = list(self.runtime_root.rglob("*.lock"))
            self.assertEqual(len(resources), 1)
            self.assertEqual(resources[0].read_bytes(), b"")

            begin_locking.set()
            while_running = [observations.get(timeout=10) for _ in processes]
            self.assertEqual(
                [item[0] for item in while_running].count("task.entered"),
                1,
            )
            rejected = [
                item
                for item in while_running
                if item[0] == "run.result" and item[3] == "instance.already_running"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertFalse(rejected[0][2])
            release.set()
            owner_result = observations.get(timeout=10)
            self.assertEqual(owner_result[0], "run.result")
            self.assertTrue(owner_result[2])
            self.assertIsNone(owner_result[3])
        finally:
            begin_locking.set()
            release.set()
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=10)
        self.assertTrue(all(process.exitcode == 0 for process in processes))
        for queue in (opened, observations):
            queue.close()
            queue.join_thread()

    def test_normal_return_and_exception_release_admission(self) -> None:
        result = run_with_admission(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:normal",
            lambda: "value",
        )
        self.assertTrue(result.task_started)
        self.assertIsNone(result.rejection_code)
        self.assertEqual(result.value, "value")

        def fail() -> None:
            raise RuntimeError("task failed")

        with self.assertRaisesRegex(RuntimeError, "task failed"):
            run_with_admission(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:exception",
                fail,
            )
        reacquired = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:after-exception",
        )
        self.assertTrue(reacquired.acquired)
        reacquired.admission.release()

    def test_stale_handle_cannot_release_later_owner(self) -> None:
        stale = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:stale",
        )
        self.assertEqual(stale.admission.release().code, "admission.released")
        current = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:current",
        )
        try:
            stale_release = stale.admission.release()
            self.assertEqual(stale_release.code, "admission.not_owner")
            self.assertFalse(stale_release.released)
            contender = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:contender",
            )
            self.assertEqual(contender.code, "instance.already_running")
        finally:
            current.admission.release()

    def test_ownership_handle_cannot_be_copied_or_serialized(self) -> None:
        owner = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:uncopyable",
        )
        descriptor = owner.admission._descriptor
        with self.assertRaisesRegex(TypeError, "cannot be copied"):
            copy.copy(owner.admission)
        with self.assertRaisesRegex(TypeError, "cannot be deep-copied"):
            copy.deepcopy(owner.admission)
        with self.assertRaisesRegex(TypeError, "cannot be serialized"):
            pickle.dumps(owner.admission)

        owner.admission.release()
        newer = try_acquire_execution(
            self.runtime_root,
            ENVIRONMENT,
            INSTANCE,
            "execution:newer",
        )
        try:
            self.assertEqual(newer.admission._descriptor, descriptor)
            stale_release = owner.admission.release()
            self.assertEqual(stale_release.code, "admission.not_owner")
            contender = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:protected",
            )
            self.assertEqual(contender.code, "instance.already_running")
            self.assertTrue(newer.admission.owns_admission)
        finally:
            newer.admission.release()

    def test_abrupt_process_termination_allows_proven_reacquisition(self) -> None:
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        hold = context.Event()
        results = context.Queue()
        process = context.Process(
            target=_terminable_owner,
            args=(str(self.runtime_root), ready, hold, results),
        )
        process.start()
        try:
            self.assertEqual(results.get(timeout=10), "admission.acquired")
            self.assertTrue(ready.wait(timeout=10))
            blocked = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:while-child-runs",
            )
            self.assertEqual(blocked.code, "instance.already_running")
            process.terminate()
            process.join(timeout=10)
            self.assertFalse(process.is_alive())
            self.assertIsNotNone(process.exitcode)
            reacquired = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:after-termination",
            )
            self.assertTrue(reacquired.acquired)
            reacquired.admission.release()
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
            results.close()
            results.join_thread()

    def test_repeated_release_keeps_one_stable_lock_resource(self) -> None:
        resource_identity: tuple[int, int] | None = None
        for index in range(64):
            owner = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                f"execution:{index}",
            )
            self.assertTrue(owner.acquired)
            contender = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                f"execution:{index}:contender",
            )
            self.assertEqual(contender.code, "instance.already_running")
            owner.admission.release()
            resources = list(self.runtime_root.rglob("*.lock"))
            self.assertEqual(len(resources), 1)
            self.assertNotIn(ENVIRONMENT, str(resources[0]))
            self.assertNotIn(INSTANCE, str(resources[0]))
            self.assertEqual(resources[0].read_bytes(), b"")
            stat = resources[0].stat()
            observed = (stat.st_dev, stat.st_ino)
            if resource_identity is None:
                resource_identity = observed
            self.assertEqual(observed, resource_identity)

    def test_unsupported_locking_is_explicit(self) -> None:
        with patch.object(admission_module, "_msvcrt", None):
            with self.assertRaises(AdmissionError) as raised:
                try_acquire_execution(
                    self.runtime_root,
                    ENVIRONMENT,
                    INSTANCE,
                    "execution:unsupported",
                )
        self.assertEqual(raised.exception.code, "admission.unsupported")

    def test_unavailable_runtime_and_locking_are_explicit(self) -> None:
        with self.assertRaises(AdmissionError) as relative_error:
            try_acquire_execution(
                "relative-runtime",
                ENVIRONMENT,
                INSTANCE,
                "execution:relative-runtime",
            )
        self.assertEqual(relative_error.exception.code, "admission.runtime_unavailable")

        unavailable_root = Path(self.temporary.name) / "not-a-directory"
        unavailable_root.write_text("file", encoding="utf-8")
        with self.assertRaises(AdmissionError) as runtime_error:
            try_acquire_execution(
                unavailable_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:runtime-unavailable",
            )
        self.assertEqual(runtime_error.exception.code, "admission.runtime_unavailable")

        with patch.object(admission_module, "_msvcrt", _UnexpectedLockFailure()):
            with self.assertRaises(AdmissionError) as locking_error:
                try_acquire_execution(
                    self.runtime_root,
                    ENVIRONMENT,
                    INSTANCE,
                    "execution:lock-unavailable",
                )
        self.assertEqual(locking_error.exception.code, "admission.lock_unavailable")

    def test_release_failure_is_explicit_and_does_not_claim_release(self) -> None:
        real_locking = admission_module._msvcrt
        with patch.object(admission_module, "_msvcrt", _UnlockFailure(real_locking)):
            owner = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:release-failure",
            )
        try:
            with self.assertRaises(AdmissionError) as raised:
                owner.admission.release()
            self.assertEqual(raised.exception.code, "admission.release_failed")
            self.assertTrue(owner.admission.owns_admission)
            contender = try_acquire_execution(
                self.runtime_root,
                ENVIRONMENT,
                INSTANCE,
                "execution:still-blocked",
            )
            self.assertEqual(contender.code, "instance.already_running")
        finally:
            owner.admission._locking_module = real_locking
            owner.admission.release()


if __name__ == "__main__":
    unittest.main()
