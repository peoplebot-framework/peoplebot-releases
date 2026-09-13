"""Command-line entry point for deterministic PeopleBot utilities."""

from __future__ import annotations

import argparse
import sys

from ._json import stable_json_bytes
from .alpha import (
    AlphaAdoptionRequest,
    AlphaError,
    AlphaFramework,
    AlphaSetupRequest,
    read_alpha_selection,
    resume_alpha_instance,
    run_alpha_framework_adoption,
    setup_alpha_environment,
)
from .provenance import ExecutionStart, GitAttemptStore
from .state import StateRef, StateResolutionError, resolve_state


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="peoplebot")
    commands = parser.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve-state", help="resolve an exact commit/path locally")
    resolve.add_argument("--repository", required=True, help="stable repository identity")
    resolve.add_argument("--checkout", required=True, help="local Git checkout used for resolution")
    resolve.add_argument("--commit", required=True, help="lowercase full 40-hex commit ID")
    resolve.add_argument("--path", help="optional canonical relative Git path")

    setup = commands.add_parser(
        "alpha-setup", help="record an exact synthetic alpha framework selection"
    )
    _environment_arguments(setup)
    _framework_arguments(setup, "framework")
    setup.add_argument("--created-at", required=True)

    adopt = commands.add_parser(
        "alpha-adopt", help="adopt one compatible synthetic framework State"
    )
    _environment_arguments(adopt)
    _framework_arguments(adopt, "candidate")
    adopt.add_argument("--current-selection-commit", required=True)
    adopt.add_argument("--runtime-root", required=True)
    adopt.add_argument("--execution-id", required=True)
    adopt.add_argument("--started-at", required=True)
    adopt.add_argument("--finished-at", required=True)

    resume = commands.add_parser(
        "alpha-resume", help="resume exact synthetic Instance memory in this fresh process"
    )
    _environment_arguments(resume)
    resume.add_argument("--framework-checkout", required=True)
    resume.add_argument("--selection-commit", required=True)
    resume.add_argument("--memory-checkout", required=True)
    resume.add_argument("--memory-commit", required=True)
    resume.add_argument("--memory-path", action="append", required=True)
    return parser


def _environment_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--environment-checkout", required=True)
    parser.add_argument("--environment-repository", required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--instance-id", required=True)


def _framework_arguments(parser: argparse.ArgumentParser, prefix: str) -> None:
    parser.add_argument(f"--{prefix}-checkout", required=True)
    parser.add_argument(f"--{prefix}-repository", required=True)
    parser.add_argument(f"--{prefix}-commit", required=True)
    parser.add_argument(f"--{prefix}-blueprint-path", required=True)
    parser.add_argument(f"--{prefix}-compatibility-path", required=True)
    parser.add_argument(f"--{prefix}-source-path", required=True)


def _framework(args: argparse.Namespace, prefix: str) -> AlphaFramework:
    repository = getattr(args, f"{prefix}_repository")
    commit = getattr(args, f"{prefix}_commit")
    return AlphaFramework(
        StateRef(repository, commit),
        StateRef(repository, commit, getattr(args, f"{prefix}_blueprint_path")),
        StateRef(repository, commit, getattr(args, f"{prefix}_compatibility_path")),
        StateRef(repository, commit, getattr(args, f"{prefix}_source_path")),
    )


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        if args.command == "resolve-state":
            reference = StateRef(args.repository, args.commit, args.path)
            result = resolve_state(args.checkout, reference).to_dict()
        elif args.command == "alpha-setup":
            request = AlphaSetupRequest(
                repository=args.environment_repository,
                environment_id=args.environment_id,
                instance_id=args.instance_id,
                framework_checkout=args.framework_checkout,
                framework=_framework(args, "framework"),
                created_at=args.created_at,
            )
            result = setup_alpha_environment(args.environment_checkout, request).to_dict()
        elif args.command == "alpha-adopt":
            current = StateRef(
                args.environment_repository,
                args.current_selection_commit,
            )
            candidate = _framework(args, "candidate")
            current_selection = read_alpha_selection(args.environment_checkout, current)
            start = ExecutionStart(
                execution_id=args.execution_id,
                environment_id=args.environment_id,
                instance_id=args.instance_id,
                objective="Adopt one compatible synthetic framework State",
                started_at=args.started_at,
                starting_state=current,
                blueprint=current_selection.framework.blueprint,
                adapter=current_selection.framework.source,
                input_states=(
                    candidate.state,
                    candidate.blueprint,
                    candidate.compatibility,
                    candidate.source,
                ),
            )
            result = run_alpha_framework_adoption(
                args.runtime_root,
                GitAttemptStore(args.environment_checkout, args.environment_repository),
                args.environment_checkout,
                start,
                AlphaAdoptionRequest(args.candidate_checkout, current, candidate),
                lambda: args.finished_at,
            ).to_dict()
        else:
            selection_state = StateRef(
                args.environment_repository,
                args.selection_commit,
            )
            result = resume_alpha_instance(
                args.environment_checkout,
                selection_state,
                args.framework_checkout,
                args.memory_checkout,
                StateRef(args.environment_repository, args.memory_commit),
                args.environment_id,
                args.instance_id,
                tuple(args.memory_path),
            ).to_dict()
    except ValueError as error:
        sys.stderr.write(f"input.invalid: {error}\n")
        return 2
    except StateResolutionError as error:
        sys.stderr.write(f"{error}\n")
        return 3
    except AlphaError as error:
        sys.stderr.write(f"{error}\n")
        return 4

    sys.stdout.buffer.write(stable_json_bytes(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
