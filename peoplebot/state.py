"""Exact Git State references and deterministic local resolution."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ._json import stable_json_bytes


_FULL_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_GIT_GLOBAL_OPTIONS = ("--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks")
_GIT_ENVIRONMENT_TO_REMOVE = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_NO_LAZY_FETCH",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)
_GIT_CONFIG_ENVIRONMENT_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _require_identity(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty and have no surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must not contain control characters")


def _validate_git_path(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError("path must be a non-empty string when provided")
    if "\\" in value or "\x00" in value:
        raise ValueError("path must use canonical POSIX separators and contain no NUL")
    parsed = PurePosixPath(value)
    if (
        value == "."
        or parsed.is_absolute()
        or str(parsed) != value
        or any(part in {".", ".."} for part in parsed.parts)
    ):
        raise ValueError("path must be a canonical relative Git path")


@dataclass(frozen=True, slots=True)
class StateRef:
    """An exact repository State, optionally narrowed to one Git object path."""

    repository: str
    commit: str
    path: str | None = None

    def __post_init__(self) -> None:
        _require_identity(self.repository, "repository")
        if not isinstance(self.commit, str) or not _FULL_COMMIT.fullmatch(self.commit):
            raise ValueError("commit must be a lowercase full 40-hex Git commit ID")
        if self.path is not None:
            _validate_git_path(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit": self.commit,
            "path": self.path,
            "repository": self.repository,
        }


@dataclass(frozen=True, slots=True)
class ResolvedState:
    """Observed local resolution of a StateRef."""

    reference: StateRef
    root_tree: str
    selected_object: str
    selected_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "peoplebot.resolved-state.v0",
            "reference": self.reference.to_dict(),
            "root_tree": self.root_tree,
            "selected_object": self.selected_object,
            "selected_type": self.selected_type,
        }

    def to_json_bytes(self) -> bytes:
        return stable_json_bytes(self.to_dict())


class StateResolutionError(RuntimeError):
    """A bounded, classifiable failure to resolve exact State."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name in _GIT_ENVIRONMENT_TO_REMOVE or name.startswith(_GIT_CONFIG_ENVIRONMENT_PREFIXES):
            environment.pop(name, None)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _run_git(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=_git_environment(),
            shell=False,
            timeout=15,
        )
    except FileNotFoundError as error:
        raise StateResolutionError("git.unavailable", "Git executable was not found") from error
    except subprocess.TimeoutExpired as error:
        raise StateResolutionError("git.timeout", "Git operation exceeded 15 seconds") from error


def _require_git_features() -> None:
    support = _run_git(["git", *_GIT_GLOBAL_OPTIONS, "--version"])
    if support.returncode != 0:
        raise StateResolutionError(
            "git.unsupported",
            "Git 2.45 or newer with --no-lazy-fetch is required",
        )


def _git(checkout: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return _run_git(
        [
            "git",
            *_GIT_GLOBAL_OPTIONS,
            "-C",
            os.fspath(checkout),
            *arguments,
        ]
    )


def _stdout(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout.strip()


def resolve_state(checkout: str | Path, reference: StateRef) -> ResolvedState:
    """Resolve a pinned commit/path from a local Git repository without reading branches."""

    checkout_path = Path(checkout)
    if not checkout_path.is_dir():
        raise StateResolutionError("repository.unavailable", "checkout directory does not exist")

    _require_git_features()

    repository_check = _git(checkout_path, "rev-parse", "--git-dir")
    if repository_check.returncode != 0:
        raise StateResolutionError("repository.invalid", "checkout is not a Git repository")

    commit_type = _git(checkout_path, "cat-file", "-t", reference.commit)
    if commit_type.returncode != 0 or _stdout(commit_type) != "commit":
        raise StateResolutionError("state.commit_unavailable", "exact commit is unavailable")

    tree_result = _git(checkout_path, "show", "-s", "--format=%T", reference.commit)
    root_tree = _stdout(tree_result)
    if tree_result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", root_tree):
        raise StateResolutionError("state.tree_unavailable", "commit root tree could not be resolved")

    if reference.path is None:
        return ResolvedState(reference, root_tree, reference.commit, "commit")

    object_result = _git(
        checkout_path,
        "rev-parse",
        "--verify",
        f"{reference.commit}:{reference.path}",
    )
    selected_object = _stdout(object_result)
    if object_result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", selected_object):
        raise StateResolutionError("state.path_unavailable", "path is unavailable at exact commit")

    type_result = _git(checkout_path, "cat-file", "-t", selected_object)
    selected_type = _stdout(type_result)
    if type_result.returncode != 0 or selected_type not in {"blob", "tree", "commit", "tag"}:
        raise StateResolutionError("state.object_unavailable", "selected Git object could not be inspected")

    return ResolvedState(reference, root_tree, selected_object, selected_type)
