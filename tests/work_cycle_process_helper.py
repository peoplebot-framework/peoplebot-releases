"""Fresh-process verifier for deterministic work-cycle tests only."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT))

from peoplebot import ContextPolicy, StateRef, assemble_instance_memory_context
from peoplebot._json import stable_json_bytes
from peoplebot.work_cycle import (
    ReaderProgressStore,
    TaskDisposition,
    TaskHandlerResult,
    load_cycle_bindings,
    load_task_policy,
    run_work_cycle_tick,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bindings", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--expected-disposition", required=True)
    parser.add_argument("--expected-reply-commit")
    parser.add_argument("--counter", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--finished-at", required=True)
    parser.add_argument("--blueprint-repository")
    parser.add_argument("--blueprint-commit")
    parser.add_argument("--blueprint-path")
    parser.add_argument("--memory-path")
    parser.add_argument("--expected-memory")
    args = parser.parse_args()

    bindings = load_cycle_bindings(Path(args.bindings))
    policy = load_task_policy(Path(args.policy))
    persisted = ReaderProgressStore(
        bindings.local_checkout, bindings.local_repository, bindings.progress_ref
    ).load()
    if persisted is None:
        raise AssertionError("durable reader progress is absent")
    matches = [
        item for item in persisted.progress.tasks if item.message_id == args.message_id
    ]
    if len(matches) != 1 or matches[0].disposition.value != args.expected_disposition:
        raise AssertionError("durable task disposition does not match")
    if args.expected_reply_commit is not None:
        reply_state = matches[0].reply_state
        if reply_state is None or reply_state.commit != args.expected_reply_commit:
            raise AssertionError("durable task reply State does not match")

    memory_content = None
    if args.expected_memory is not None:
        task = matches[0]
        if task.memory_state is None:
            raise AssertionError("durable task progress has no exact memory State")
        blueprint = StateRef(
            args.blueprint_repository, args.blueprint_commit, args.blueprint_path
        )
        context = assemble_instance_memory_context(
            bindings.local_checkout,
            task.memory_state,
            bindings.environment_id,
            bindings.instance_id,
            blueprint,
            (args.memory_path,),
            ContextPolicy(
                StateRef(
                    bindings.local_repository,
                    task.memory_state.commit,
                    "memory.json",
                ),
                1,
                4096,
                4096,
            ),
        )
        memory_content = context.documents[0].content
        if memory_content != args.expected_memory:
            raise AssertionError("exact recovered memory bytes do not match")

    counter = Path(args.counter)

    def unexpected_dispatch(_message):
        value = int(counter.read_text(encoding="ascii")) + 1
        counter.write_text(str(value), encoding="ascii")
        return TaskHandlerResult(TaskDisposition.COMPLETED, "Unexpected redispatch.")

    status = run_work_cycle_tick(
        bindings,
        policy,
        {"fixture.complete": unexpected_dispatch},
        args.execution_id,
        args.started_at,
        args.finished_at,
    )
    after = ReaderProgressStore(
        bindings.local_checkout, bindings.local_repository, bindings.progress_ref
    ).load()
    if after is None:
        raise AssertionError("durable reader progress disappeared")
    print(
        stable_json_bytes(
            {
                "blocking_task": matches[0].to_dict(),
                "counter": int(counter.read_text(encoding="ascii")),
                "memory_content": memory_content,
                "persisted_disposition": matches[0].disposition.value,
                "progress_unchanged": after.state == persisted.state,
                "status": status.to_dict(),
            }
        ).decode("utf-8"),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
