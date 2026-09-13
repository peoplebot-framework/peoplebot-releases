"""Callable local checkpoint-and-resume example; performs no work on import."""

from __future__ import annotations

from pathlib import Path

from peoplebot import (
    ContextPolicy,
    ExecutionStart,
    GitAttemptStore,
    GitMemoryStore,
    MemoryCheckpointRequest,
    MemoryExecutionResult,
    MemoryItem,
    StateRef,
    assemble_instance_memory_context,
    run_instance_memory_execution,
)


def checkpoint_and_resume(
    checkout: str | Path,
    runtime_root: str | Path,
    repository: str,
    seed_commit: str,
    blueprint: StateRef,
    adapter: StateRef,
) -> tuple[MemoryExecutionResult, bytes]:
    """Create one explicit checkpoint, then retrieve one item from its exact State."""

    environment_id = "environment:example"
    instance_id = "instance:example"
    starting_state = StateRef(repository, seed_commit)
    start = ExecutionStart(
        execution_id="execution:example-memory-a",
        environment_id=environment_id,
        instance_id=instance_id,
        objective="Save and retrieve an explicit Instance-memory checkpoint",
        started_at="2026-09-10T12:00:00Z",
        starting_state=starting_state,
        blueprint=blueprint,
        adapter=adapter,
    )
    request = MemoryCheckpointRequest(
        repository=repository,
        environment_id=environment_id,
        instance_id=instance_id,
        blueprint=blueprint,
        expected_state=starting_state,
        items=(MemoryItem("decisions.md", "Use exact Git State.\n"),),
        saved_at="2026-09-10T12:00:00Z",
        initial=True,
    )
    result = run_instance_memory_execution(
        runtime_root,
        GitAttemptStore(checkout, repository),
        GitMemoryStore(checkout, repository),
        start,
        request,
        lambda: "2026-09-10T12:00:01Z",
    )
    if result.checkpoint is None:
        raise RuntimeError("memory checkpoint did not run")

    memory_state = result.checkpoint.state
    policy = ContextPolicy(
        StateRef(repository, memory_state.commit, "memory.json"),
        max_entries=1,
        max_blob_bytes=65_536,
        max_total_blob_bytes=65_536,
    )
    assembly = assemble_instance_memory_context(
        checkout,
        memory_state,
        environment_id,
        instance_id,
        blueprint,
        ("decisions.md",),
        policy,
    )
    return result, assembly.documents[0].to_source_bytes()
