"""Run the genuine project-review Blueprint's findings path with a fake provider."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from peoplebot import (
    BLUEPRINT_PATH,
    ContextPolicy,
    ExecutionStart,
    GitAttemptStore,
    ProjectReviewAdapter,
    StateRef,
    run_project_review_execution,
)


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        encoding="utf-8",
        shell=False,
        timeout=15,
    ).stdout.strip()


class FakeRunner:
    def __init__(self, response: str) -> None:
        self.response = response

    def __call__(self, command, input_bytes, environment, timeout_seconds):
        if command[1:] == ("--version",):
            return subprocess.CompletedProcess(command, 0, b"codex-cli 0.153.4\n", b"")
        events = [
            {"type": "thread.started", "thread_id": "not-retained"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": self.response}},
            {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 8}},
        ]
        stdout = b"\n".join(
            json.dumps(item, separators=(",", ":")).encode("utf-8") for item in events
        ) + b"\n"
        return subprocess.CompletedProcess(command, 0, stdout, b"")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--framework-checkout", default=str(Path(__file__).parents[2]))
    parser.add_argument(
        "--framework-repository",
        default="https://github.com/peoplebot-framework/peoplebot",
    )
    args = parser.parse_args()
    framework = Path(args.framework_checkout).resolve()
    framework_commit = git(framework, "rev-parse", "HEAD")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        project = root / "project"
        project.mkdir()
        git(project, "init", "-b", "main")
        git(project, "config", "user.name", "PeopleBot Example")
        git(project, "config", "user.email", "example@example.invalid")
        (project / "src").mkdir()
        (project / "src" / "parser.py").write_text(
            "def first(items):\n    return items[0]\n", encoding="utf-8"
        )
        policy_value = {
            "exclusions": [],
            "format": "peoplebot.context-policy.v0",
            "max_blob_bytes": 32768,
            "max_entries": 4,
            "max_total_blob_bytes": 49152,
        }
        (project / "review-context-policy.json").write_text(
            json.dumps(policy_value, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        git(project, "add", ".")
        git(project, "commit", "-m", "synthetic review input")
        project_commit = git(project, "rev-parse", "HEAD")
        project_repository = "https://example.test/consumer/project"
        project_state = StateRef(project_repository, project_commit)
        policy_state = StateRef(project_repository, project_commit, "review-context-policy.json")
        document_state = StateRef(project_repository, project_commit, "src/parser.py")
        policy = ContextPolicy.from_dict(policy_state, policy_value)

        response = json.dumps(
            {
                "findings": [
                    {
                        "citations": [document_state.to_dict()],
                        "explanation": "first() indexes without proving that items is non-empty.",
                        "severity": "medium",
                        "suggested_action": "Handle the empty case before indexing.",
                        "title": "Empty input raises IndexError",
                    }
                ],
                "insufficient_evidence": None,
                "outcome": "findings",
            },
            separators=(",", ":"),
        )
        codex_home = root / "codex-home"
        codex_home.mkdir()
        executable = root / "codex.exe"
        executable.write_bytes(b"fake executable placeholder")
        adapter_state = StateRef(
            args.framework_repository, framework_commit, "peoplebot/adapters"
        )
        start = ExecutionStart(
            execution_id="execution:deterministic-project-review",
            environment_id="environment:example",
            instance_id="instance:project-review-example",
            objective="Identify a concrete correctness risk in the supplied parser.",
            started_at="2026-09-11T12:00:00Z",
            starting_state=project_state,
            blueprint=StateRef(args.framework_repository, framework_commit, BLUEPRINT_PATH),
            adapter=adapter_state,
            input_states=(policy_state, document_state),
        )
        result = run_project_review_execution(
            framework,
            project,
            root / "runtime",
            GitAttemptStore(project, project_repository),
            start,
            ProjectReviewAdapter(
                framework, adapter_state, executable, codex_home, runner=FakeRunner(response)
            ),
            ("src/parser.py",),
            policy,
            lambda: "2026-09-11T12:00:01Z",
        )
        print(json.dumps(result.adapter_observation.response.to_dict(), sort_keys=True))


if __name__ == "__main__":
    main()
