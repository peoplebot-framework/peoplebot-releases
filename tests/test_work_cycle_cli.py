from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from peoplebot.messaging import outbound_message_ref
from peoplebot.work_cycle import load_cycle_bindings, load_task_policy, reader_progress_ref


@unittest.skipUnless(os.name == "nt", "Windows single-tick launcher")
class WorkCycleCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        subprocess.run(
            ("git", "-C", str(self.checkout), "init", "-b", "main"),
            capture_output=True,
            check=True,
            shell=False,
        )
        self.stop = self.root / "control" / "stop.request"
        self.stop.parent.mkdir()
        self.stop.write_text("stop\n", encoding="utf-8")
        self.status = self.root / "status" / "cycle.json"
        environment = "environment:fixture"
        outbound = outbound_message_ref(environment)
        bindings = {
            "destination": {
                "expected_url": "https://example.invalid/owner.git",
                "ref_name": outbound,
                "remote": "outbound",
                "repository": "https://example.invalid/owner",
            },
            "environment_id": environment,
            "format": "peoplebot.cycle-bindings.v0",
            "instance_id": "instance:fixture",
            "local_checkout": str(self.checkout),
            "local_repository": "https://example.invalid/owner",
            "outbound_ref": outbound,
            "progress_ref": reader_progress_ref(environment, "instance:fixture"),
            "runtime_root": str(self.root / "runtime"),
            "sources": [
                {
                    "allowed_senders": ["environment:peer"],
                    "expected_url": "https://example.invalid/peer.git",
                    "ref_name": outbound_message_ref("environment:peer"),
                    "remote": "peer",
                    "repository": "https://example.invalid/peer",
                }
            ],
            "status_path": str(self.status),
            "stop_path": str(self.stop),
        }
        policy = {
            "allow_stop_messages": True,
            "format": "peoplebot.task-policy.v0",
            "maximum_completed_tasks": 1,
            "routes": [{"handler": "fixture.complete", "purpose": "fixture.task"}],
        }
        self.bindings_path = self.root / "cycle-bindings.json"
        self.policy_path = self.root / "task-policy.json"
        self.bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
        self.policy_path.write_text(json.dumps(policy), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_strict_loaders_and_direct_cli_persist_stopped_status(self) -> None:
        self.assertEqual(load_cycle_bindings(self.bindings_path).stop_path, self.stop)
        self.assertEqual(load_task_policy(self.policy_path).routes[0].handler, "fixture.complete")
        result = subprocess.run(
            (
                sys.executable,
                "-m",
                "peoplebot",
                "work-cycle-tick",
                "--bindings",
                str(self.bindings_path),
                "--policy",
                str(self.policy_path),
                "--execution-id",
                "execution:cli-fixture",
                "--offline-fixture",
            ),
            cwd=Path(__file__).parents[1],
            capture_output=True,
            check=False,
            encoding="utf-8",
            shell=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["code"], "cycle.stopped")
        self.assertEqual(output["execution_id"], "execution:cli-fixture")
        self.assertEqual(json.loads(self.status.read_text())["work_invoked"], False)

    def test_powershell_launcher_remains_attached_and_validates_status(self) -> None:
        root = Path(__file__).parents[1]
        launcher = root / "operations" / "windows" / "peoplebot-single-tick.ps1"
        result = subprocess.run(
            (
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-PythonExecutable",
                sys.executable,
                "-ModuleRoot",
                str(root),
                "-BindingsPath",
                str(self.bindings_path),
                "-PolicyPath",
                str(self.policy_path),
                "-StatusPath",
                str(self.status),
            ),
            capture_output=True,
            check=False,
            encoding="utf-8",
            shell=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"code":"cycle.stopped"', result.stdout)
        status = json.loads(self.status.read_text())
        self.assertTrue(status["execution_id"].startswith("execution:scheduled-"))

    def _launcher(self, policy: Path, status: Path) -> subprocess.CompletedProcess[str]:
        root = Path(__file__).parents[1]
        launcher = root / "operations" / "windows" / "peoplebot-single-tick.ps1"
        return subprocess.run(
            (
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
                "-PythonExecutable",
                sys.executable,
                "-ModuleRoot",
                str(root),
                "-BindingsPath",
                str(self.bindings_path),
                "-PolicyPath",
                str(policy),
                "-StatusPath",
                str(status),
            ),
            capture_output=True,
            check=False,
            encoding="utf-8",
            shell=False,
            timeout=30,
        )

    def test_launcher_rejects_stale_status_after_configuration_failure(self) -> None:
        self.status.parent.mkdir(parents=True)
        self.status.write_text(
            json.dumps(
                {
                    "code": "cycle.completed",
                    "disposition": "completed",
                    "execution_id": "execution:old",
                    "format": "peoplebot.work-cycle-status.v0",
                }
            ),
            encoding="utf-8",
        )
        invalid_policy = self.root / "invalid-policy.json"
        invalid_policy.write_text("{", encoding="utf-8")
        result = self._launcher(invalid_policy, self.status)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("cycle.completed", result.stdout)
        self.assertIn("did not write status for this exact execution", result.stderr)

    def test_launcher_rejects_status_path_mismatch_before_tick(self) -> None:
        mismatch = self.root / "other" / "old-status.json"
        mismatch.parent.mkdir()
        mismatch.write_text(
            '{"execution_id":"execution:old","format":"peoplebot.work-cycle-status.v0"}',
            encoding="utf-8",
        )
        result = self._launcher(self.policy_path, mismatch)
        self.assertEqual(result.returncode, 21)
        self.assertEqual(result.stdout, "")
        self.assertIn("does not match bindings.status_path", result.stderr)

    def test_scheduler_template_is_inactive_and_finite(self) -> None:
        template = (
            Path(__file__).parents[1]
            / "operations"
            / "windows"
            / "peoplebot-single-tick-task.xml.template"
        ).read_text(encoding="utf-8")
        self.assertIn("<Enabled>false</Enabled>", template)
        self.assertIn("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", template)
        self.assertIn("<ExecutionTimeLimit>PT35M</ExecutionTimeLimit>", template)
        self.assertIn("__PEOPLEBOT_COMMAND__", template)
        self.assertIn("__AUTHORITY_PATH__", template)
        self.assertIn("__OPERATIONS_PATH__", template)
        self.assertNotIn("Register-ScheduledTask", template)


if __name__ == "__main__":
    unittest.main()
