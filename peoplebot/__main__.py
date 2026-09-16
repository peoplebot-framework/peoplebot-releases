"""Command-line entry point for deterministic PeopleBot utilities."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

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
from .development import (
    CodexDevelopmentAdapter,
    DevelopmentError,
    ExactProjectReviewer,
    load_development_authority,
    load_development_cycle_operations,
    load_development_reader_recovery,
    reconcile_development_cycle_reader,
    run_development_cycle_command,
)
from .adapters.project_review import ProjectReviewAdapter
from .state import StateRef, StateResolutionError, resolve_state
from .work_cycle import (
    CycleError,
    TaskDisposition,
    TaskHandlerResult,
    load_cycle_bindings,
    load_task_policy,
    run_work_cycle_tick,
)
from .usage import (
    collect_usage,
    load_instance_usage_profile,
    load_usage_collection_config,
    report_run_usage,
)


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

    tick = commands.add_parser(
        "work-cycle-tick", help="run one finite message-driven work-cycle tick"
    )
    tick.add_argument("--bindings", required=True, help="absolute cycle-bindings JSON path")
    tick.add_argument("--policy", required=True, help="absolute task-policy JSON path")
    tick.add_argument("--execution-id", required=True)
    tick.add_argument("--started-at", help="optional deterministic fixture timestamp")
    tick.add_argument("--finished-at", help="optional deterministic fixture timestamp")
    tick.add_argument(
        "--offline-fixture",
        action="store_true",
        help="enable only the deterministic fixture.complete handler",
    )
    development = commands.add_parser(
        "development-cycle-tick",
        help="run one approved private implementation and exact-review cycle",
    )
    development.add_argument("--bindings", required=True)
    development.add_argument("--policy", required=True)
    development.add_argument("--authority", required=True)
    development.add_argument("--operations", required=True)
    development.add_argument("--execution-id", required=True)
    recovery = commands.add_parser(
        "development-cycle-reconcile",
        help="reconcile one exact evidence-proven stopped development task",
    )
    recovery.add_argument("--bindings", required=True)
    recovery.add_argument("--recovery", required=True)
    recovery.add_argument("--execution-id", required=True)
    usage = commands.add_parser(
        "usage-collect",
        help="incrementally collect sanitized local provider usage and check autonomous admission",
    )
    usage.add_argument("--config", required=True)
    usage.add_argument("--phase", choices=("entry", "exit", "manual"), required=True)
    usage.add_argument("--execution-id")
    usage.add_argument("--task-id")
    usage.add_argument("--observed-at", help="optional deterministic fixture timestamp")
    report = commands.add_parser(
        "usage-report-run",
        help="save one run's available usage through its Instance environment profile",
    )
    report.add_argument("--profile", required=True)
    report.add_argument("--run-id", required=True)
    report.add_argument("--trigger", choices=("manual", "scheduled"), required=True)
    report.add_argument("--phase", choices=("start", "completion", "recovery"), required=True)
    report.add_argument("--observed-at", help="optional deterministic fixture timestamp")
    report.add_argument("--started-at")
    report.add_argument("--finished-at")
    report.add_argument("--outcome", choices=("success", "failed", "stopped", "idle", "unknown"))
    report.add_argument("--turn-id", action="append", default=[])
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
        elif args.command == "alpha-resume":
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
        elif args.command == "usage-report-run":
            timestamp = args.observed_at or (
                datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            )
            result = report_run_usage(
                load_instance_usage_profile(args.profile),
                args.run_id,
                args.trigger,
                args.phase,
                timestamp,
                started_at=args.started_at,
                finished_at=args.finished_at,
                outcome=args.outcome,
                provider_turn_ids=tuple(args.turn_id),
            )
            exit_code = 0
        elif args.command == "usage-collect":
            timestamp = args.observed_at or (
                datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            )
            result = collect_usage(
                load_usage_collection_config(args.config),
                timestamp,
                phase=args.phase,
                execution_id=args.execution_id,
                task_id=args.task_id,
            )
            exit_code = 0 if args.phase != "entry" or result["admitted"] else 11
        elif args.command == "development-cycle-reconcile":
            bindings = load_cycle_bindings(args.bindings)
            recovery = load_development_reader_recovery(args.recovery)
            recovered = reconcile_development_cycle_reader(
                bindings,
                recovery,
                args.execution_id,
                datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            )
            result = recovered.to_dict()
            exit_code = 0 if recovered.reconciled else 13
        elif args.command == "development-cycle-tick":
            authority = load_development_authority(args.authority)
            operations = load_development_cycle_operations(args.operations)
            if not authority.active or not operations.active:
                result = {
                    "active": False,
                    "code": "development.inactive",
                    "format": "peoplebot.development-cycle-readiness.v0",
                    "provider_invoked": False,
                }
                exit_code = 13
            else:
                bindings = load_cycle_bindings(args.bindings)
                policy = load_task_policy(args.policy)
                if (
                    bindings.environment_id != authority.environment_id
                    or bindings.instance_id != authority.coordinator_instance_id
                ):
                    raise ValueError("cycle bindings do not match development coordinator authority")
                implementer = CodexDevelopmentAdapter(
                    authority.framework_checkout, authority.development_adapter,
                    authority.executable, authority.codex_home, authority.worktree_root,
                )
                reviewer = ExactProjectReviewer(ProjectReviewAdapter(
                    authority.framework_checkout, authority.review_adapter,
                    authority.executable, authority.codex_home,
                ))
                command_result = run_development_cycle_command(
                    authority, operations, bindings, policy, implementer, reviewer,
                    args.execution_id,
                    datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                )
                result = command_result.to_dict()
                exit_code = 0 if command_result.code == "development.completed" else 13
        else:
            bindings = load_cycle_bindings(args.bindings)
            policy = load_task_policy(args.policy)
            timestamp = (
                datetime.now(UTC)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )

            def fixture_handler(_message):
                return TaskHandlerResult(
                    TaskDisposition.COMPLETED,
                    "Offline fixture task completed without invoking a provider.",
                )

            handlers = {"fixture.complete": fixture_handler} if args.offline_fixture else {}
            status = run_work_cycle_tick(
                bindings,
                policy,
                handlers,
                args.execution_id,
                args.started_at or timestamp,
                args.finished_at or timestamp,
            )
            result = status.to_dict()
            exit_code = {
                "completed": 0,
                "idle": 0,
                "stopped": 0,
                "busy": 10,
                "exhausted": 11,
                "failed": 12,
                "unresolved": 13,
            }.get(status.disposition, 14)
    except ValueError as error:
        sys.stderr.write(f"input.invalid: {error}\n")
        return 2
    except StateResolutionError as error:
        sys.stderr.write(f"{error}\n")
        return 3
    except AlphaError as error:
        sys.stderr.write(f"{error}\n")
        return 4
    except CycleError as error:
        sys.stderr.write(f"{error}\n")
        return 13
    except DevelopmentError as error:
        sys.stderr.write(f"{error}\n")
        return 13

    sys.stdout.buffer.write(stable_json_bytes(result))
    return exit_code if args.command in {
        "work-cycle-tick", "development-cycle-reconcile", "development-cycle-tick",
        "usage-collect",
        "usage-report-run",
    } else 0


if __name__ == "__main__":
    raise SystemExit(main())
