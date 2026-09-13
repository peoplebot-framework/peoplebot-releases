from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peoplebot import StateRef, StateResolutionError, resolve_state
from peoplebot.state import _git_environment


def git_result(
    repository: Path,
    *arguments: str,
    global_options: tuple[str, ...] = (),
    check: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *global_options, "-C", str(repository), *arguments],
        capture_output=True,
        check=check,
        encoding="utf-8",
        env=environment,
        shell=False,
    )
    return result


def git(
    repository: Path,
    *arguments: str,
    global_options: tuple[str, ...] = (),
) -> str:
    return git_result(repository, *arguments, global_options=global_options).stdout.strip()


class StateRefTests(unittest.TestCase):
    def test_requires_full_lowercase_commit(self) -> None:
        with self.assertRaisesRegex(ValueError, "full 40-hex"):
            StateRef("example.test/owner/repo", "abc123")
        with self.assertRaisesRegex(ValueError, "full 40-hex"):
            StateRef("example.test/owner/repo", "A" * 40)

    def test_requires_canonical_relative_path(self) -> None:
        commit = "a" * 40
        for invalid in (".", "/absolute", "../escape", "a/../b", "a\\b", "a//b", "a/"):
            with self.subTest(path=invalid), self.assertRaises(ValueError):
                StateRef("example.test/owner/repo", commit, invalid)


class StateResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.name", "PeopleBot Test")
        git(self.repository, "config", "user.email", "test@example.invalid")
        (self.repository / "artifact.txt").write_text("first\n", encoding="utf-8")
        git(self.repository, "add", "artifact.txt")
        git(self.repository, "commit", "-m", "first")
        self.first_commit = git(self.repository, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pinned_state_survives_branch_movement_and_dirty_worktree(self) -> None:
        reference = StateRef(
            "example.test/owner/repo",
            self.first_commit,
            "artifact.txt",
        )
        before = resolve_state(self.repository, reference)

        (self.repository / "artifact.txt").write_text("second\n", encoding="utf-8")
        git(self.repository, "add", "artifact.txt")
        git(self.repository, "commit", "-m", "second")
        (self.repository / "artifact.txt").write_text("dirty\n", encoding="utf-8")

        after = resolve_state(self.repository, reference)
        self.assertEqual(before, after)
        self.assertEqual("blob", after.selected_type)
        self.assertNotEqual(self.first_commit, git(self.repository, "rev-parse", "main"))

        encoded = json.loads(after.to_json_bytes())
        self.assertEqual(self.first_commit, encoded["reference"]["commit"])
        self.assertEqual("peoplebot.resolved-state.v0", encoded["format"])

    def test_missing_path_is_classified(self) -> None:
        reference = StateRef("example.test/owner/repo", self.first_commit, "missing.txt")
        with self.assertRaises(StateResolutionError) as raised:
            resolve_state(self.repository, reference)
        self.assertEqual("state.path_unavailable", raised.exception.code)

    def test_missing_commit_is_classified(self) -> None:
        reference = StateRef("example.test/owner/repo", "0" * 40)
        with self.assertRaises(StateResolutionError) as raised:
            resolve_state(self.repository, reference)
        self.assertEqual("state.commit_unavailable", raised.exception.code)

    def test_replacement_refs_do_not_change_exact_resolution(self) -> None:
        original = StateRef("example.test/owner/repo", self.first_commit, "artifact.txt")
        original_tree = git(
            self.repository,
            "show",
            "-s",
            "--format=%T",
            self.first_commit,
            global_options=("--no-replace-objects",),
        )
        original_blob = git(
            self.repository,
            "rev-parse",
            f"{self.first_commit}:artifact.txt",
            global_options=("--no-replace-objects",),
        )

        (self.repository / "artifact.txt").write_text("replacement\n", encoding="utf-8")
        git(self.repository, "add", "artifact.txt")
        git(self.repository, "commit", "-m", "replacement")
        replacement_commit = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "replace", self.first_commit, replacement_commit)

        self.assertNotEqual(
            original_tree,
            git(self.repository, "show", "-s", "--format=%T", self.first_commit),
        )
        resolved = resolve_state(self.repository, original)
        self.assertEqual(original_tree, resolved.root_tree)
        self.assertEqual(original_blob, resolved.selected_object)

    def test_ambient_git_dir_cannot_redirect_checkout(self) -> None:
        other = self.root / "other"
        other.mkdir()
        git(other, "init", "-b", "main")
        git(other, "config", "user.name", "PeopleBot Test")
        git(other, "config", "user.email", "test@example.invalid")
        (other / "other.txt").write_text("other\n", encoding="utf-8")
        git(other, "add", "other.txt")
        git(other, "commit", "-m", "other")
        other_commit = git(other, "rev-parse", "HEAD")

        with patch.dict(os.environ, {"GIT_DIR": str(other / ".git")}, clear=False):
            resolved = resolve_state(
                self.repository,
                StateRef("example.test/owner/repo", self.first_commit),
            )
            self.assertEqual(self.first_commit, resolved.selected_object)
            with self.assertRaises(StateResolutionError) as raised:
                resolve_state(
                    self.repository,
                    StateRef("example.test/owner/repo", other_commit),
                )
        self.assertEqual("state.commit_unavailable", raised.exception.code)

    def test_ambient_command_scope_config_is_ignored(self) -> None:
        injected = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.repositoryformatversion",
            "GIT_CONFIG_VALUE_0": "999",
        }
        with patch.dict(os.environ, injected, clear=False):
            resolved = resolve_state(
                self.repository,
                StateRef("example.test/owner/repo", self.first_commit),
            )
        self.assertEqual(self.first_commit, resolved.selected_object)

    def test_environment_sanitizer_preserves_global_config_source(self) -> None:
        injected = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "core.repositoryformatversion",
            "GIT_CONFIG_VALUE_0": "999",
            "GIT_CONFIG_GLOBAL": "C:/example/global.gitconfig",
            "GIT_DIR": "C:/example/redirected.git",
        }
        with patch.dict(os.environ, injected, clear=False):
            sanitized = _git_environment()
        self.assertNotIn("GIT_CONFIG_COUNT", sanitized)
        self.assertNotIn("GIT_CONFIG_KEY_0", sanitized)
        self.assertNotIn("GIT_CONFIG_VALUE_0", sanitized)
        self.assertNotIn("GIT_DIR", sanitized)
        self.assertEqual("C:/example/global.gitconfig", sanitized["GIT_CONFIG_GLOBAL"])

    def test_resolves_linked_worktree_normally(self) -> None:
        linked = self.root / "linked"
        git(self.repository, "worktree", "add", "--detach", str(linked), self.first_commit)
        resolved = resolve_state(
            linked,
            StateRef("example.test/owner/repo", self.first_commit, "artifact.txt"),
        )
        self.assertEqual("blob", resolved.selected_type)


class PartialCloneResolutionTests(unittest.TestCase):
    def test_missing_promisor_blob_is_not_fetched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin"
            origin.mkdir()
            git(origin, "init", "-b", "main")
            git(origin, "config", "user.name", "PeopleBot Test")
            git(origin, "config", "user.email", "test@example.invalid")
            git(origin, "config", "uploadpack.allowFilter", "true")
            git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
            (origin / "artifact.txt").write_text("promised content\n", encoding="utf-8")
            git(origin, "add", "artifact.txt")
            git(origin, "commit", "-m", "promised")
            commit = git(origin, "rev-parse", "HEAD")
            blob = git(origin, "rev-parse", "HEAD:artifact.txt")

            partial = root / "partial"
            subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "clone",
                    "--filter=blob:none",
                    "--no-checkout",
                    origin.resolve().as_uri(),
                    str(partial),
                ],
                capture_output=True,
                check=True,
                encoding="utf-8",
                shell=False,
            )
            missing_before = git_result(
                partial,
                "cat-file",
                "-e",
                blob,
                global_options=("--no-lazy-fetch",),
                check=False,
            )
            self.assertNotEqual(0, missing_before.returncode)

            trace = root / "trace.json"
            with patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}, clear=False):
                with self.assertRaises(StateResolutionError) as raised:
                    resolve_state(
                        partial,
                        StateRef("example.test/owner/repo", commit, "artifact.txt"),
                    )
            self.assertEqual("state.object_unavailable", raised.exception.code)

            missing_after = git_result(
                partial,
                "cat-file",
                "-e",
                blob,
                global_options=("--no-lazy-fetch",),
                check=False,
            )
            self.assertNotEqual(0, missing_after.returncode)

            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            child_events = [event for event in events if event.get("event") == "child_start"]
            remote_children = [
                event
                for event in child_events
                if event.get("child_class") == "promisor-remote"
                or any(
                    token in {"fetch", "upload-pack"} or token.endswith("git-fetch")
                    for token in event.get("argv", [])
                )
            ]
            self.assertEqual([], remote_children)
