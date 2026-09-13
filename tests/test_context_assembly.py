from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from peoplebot import (
    ContextAssemblyError,
    ContextPathExclusion,
    ContextPolicy,
    StateRef,
    assemble_context,
)


REPOSITORY = "example.test/owner/repo"


def git_result(
    repository: Path,
    *arguments: str,
    check: bool = True,
    input_bytes: bytes | None = None,
    global_options: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *global_options, "-C", str(repository), *arguments],
        input=input_bytes,
        capture_output=True,
        check=check,
        shell=False,
    )


def git(repository: Path, *arguments: str) -> str:
    return git_result(repository, *arguments).stdout.decode("utf-8").strip()


def remote_children(trace: Path) -> list[dict[str, object]]:
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    return [
        event
        for event in events
        if event.get("event") == "child_start"
        and (
            event.get("child_class") == "promisor-remote"
            or any(
                token in {"fetch", "upload-pack"} or str(token).endswith("git-fetch")
                for token in event.get("argv", [])
            )
        )
    ]


class ContextAssemblyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.name", "PeopleBot Test")
        git(self.repository, "config", "user.email", "test@example.invalid")
        git(self.repository, "config", "core.autocrlf", "false")
        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "alpha.txt").write_bytes(b"alpha\r\n")
        (self.repository / "docs" / "beta.txt").write_bytes("\N{GREEK SMALL LETTER BETA}eta\n".encode("utf-8"))
        (self.repository / "small.txt").write_bytes(b"1234")
        (self.repository / "large.txt").write_bytes(b"12345")
        (self.repository / "invalid.bin").write_bytes(b"\xff\xfe")
        (self.repository / "contains-nul.txt").write_bytes(b"a\0b")
        (self.repository / "policy.json").write_text("{}\n", encoding="utf-8")
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "seed")
        seed = git(self.repository, "rev-parse", "HEAD")
        link_blob = git_result(
            self.repository,
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=b"docs/alpha.txt",
        ).stdout.decode("ascii").strip()
        git(
            self.repository,
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{link_blob},link.txt",
        )
        git(
            self.repository,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{seed},nested-repository",
        )
        git(self.repository, "commit", "-m", "fixture")
        self.commit = git(self.repository, "rev-parse", "HEAD")
        self.state = StateRef(REPOSITORY, self.commit)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def policy(
        self,
        *,
        max_entries: int = 32,
        max_blob_bytes: int = 1_024,
        max_total_blob_bytes: int = 4_096,
        exclusions: tuple[ContextPathExclusion, ...] = (),
    ) -> ContextPolicy:
        return ContextPolicy(
            StateRef(REPOSITORY, self.commit, "policy.json"),
            max_entries=max_entries,
            max_blob_bytes=max_blob_bytes,
            max_total_blob_bytes=max_total_blob_bytes,
            exclusions=exclusions,
        )

    def test_identical_inputs_produce_identical_content_and_json(self) -> None:
        requested = ("docs/beta.txt", "docs/alpha.txt", "docs")
        first = assemble_context(self.repository, self.state, requested, self.policy())
        second = assemble_context(self.repository, self.state, requested, self.policy())

        self.assertEqual(first.to_json_bytes(), second.to_json_bytes())
        self.assertEqual(
            ["docs/alpha.txt", "docs/beta.txt"],
            [document.source.path for document in first.documents],
        )
        self.assertEqual(b"alpha\r\n", first.documents[0].to_source_bytes())
        self.assertEqual(
            "\N{GREEK SMALL LETTER BETA}eta\n".encode("utf-8"),
            first.documents[1].to_source_bytes(),
        )
        self.assertEqual(self.commit, first.documents[0].source.commit)
        self.assertEqual(REPOSITORY, first.documents[0].source.repository)

    def test_branch_and_working_tree_changes_do_not_change_pinned_output(self) -> None:
        requested = ("docs",)
        before = assemble_context(self.repository, self.state, requested, self.policy())

        (self.repository / "docs" / "alpha.txt").write_text("new commit\n", encoding="utf-8")
        git(self.repository, "add", "docs/alpha.txt")
        git(self.repository, "commit", "-m", "move branch")
        (self.repository / "docs" / "beta.txt").write_text("dirty\n", encoding="utf-8")

        after = assemble_context(self.repository, self.state, requested, self.policy())
        self.assertEqual(before.to_json_bytes(), after.to_json_bytes())

    def test_size_boundaries_and_exclusions_are_explicit(self) -> None:
        policy = self.policy(
            max_blob_bytes=4,
            max_total_blob_bytes=4,
            exclusions=(ContextPathExclusion("docs/beta.txt", "not relevant"),),
        )
        assembled = assemble_context(
            self.repository,
            self.state,
            ("small.txt", "large.txt", "docs/beta.txt"),
            policy,
        )
        self.assertEqual(["small.txt"], [item.source.path for item in assembled.documents])
        self.assertEqual(4, assembled.manifest.selected_blob_bytes)
        self.assertEqual(
            ["policy.path_excluded", "policy.max_blob_bytes"],
            [item.reason_code for item in assembled.manifest.excluded],
        )

        total_limited = assemble_context(
            self.repository,
            self.state,
            ("large.txt", "small.txt"),
            self.policy(max_blob_bytes=5, max_total_blob_bytes=4),
        )
        self.assertEqual(["small.txt"], [item.source.path for item in total_limited.documents])
        self.assertEqual(
            "policy.max_total_blob_bytes",
            total_limited.manifest.excluded[0].reason_code,
        )

    def test_unsupported_content_and_entry_kinds_are_classified(self) -> None:
        cases = (
            ("invalid.bin", "context.encoding_unsupported"),
            ("contains-nul.txt", "context.nul_unsupported"),
            ("link.txt", "context.symlink_unsupported"),
            ("nested-repository", "context.gitlink_unsupported"),
        )
        for path, code in cases:
            with self.subTest(path=path):
                with self.assertRaises(ContextAssemblyError) as raised:
                    assemble_context(self.repository, self.state, (path,), self.policy())
                self.assertEqual(code, raised.exception.code)

    def test_missing_path_is_classified(self) -> None:
        with self.assertRaises(ContextAssemblyError) as raised:
            assemble_context(
                self.repository,
                self.state,
                ("absent.txt",),
                self.policy(),
            )
        self.assertEqual("context.path_unavailable", raised.exception.code)


class PartialCloneAssemblyTests(unittest.TestCase):
    def test_missing_object_never_fetches_or_creates_worktree(self) -> None:
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
            (origin / "policy.json").write_text("{}\n", encoding="utf-8")
            git(origin, "add", ".")
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
                shell=False,
            )
            missing_before = git_result(
                partial,
                "cat-file",
                "-e",
                blob,
                check=False,
                global_options=("--no-lazy-fetch",),
            )
            self.assertNotEqual(0, missing_before.returncode)
            registry_before = git(partial, "worktree", "list", "--porcelain")

            state = StateRef(REPOSITORY, commit)
            policy = ContextPolicy(
                StateRef(REPOSITORY, commit, "policy.json"),
                max_entries=10,
                max_blob_bytes=1_024,
                max_total_blob_bytes=4_096,
            )
            trace = root / "trace.json"
            with patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}, clear=False):
                with self.assertRaises(ContextAssemblyError) as raised:
                    assemble_context(partial, state, ("artifact.txt",), policy)

            self.assertEqual("context.object_unavailable", raised.exception.code)
            missing_after = git_result(
                partial,
                "cat-file",
                "-e",
                blob,
                check=False,
                global_options=("--no-lazy-fetch",),
            )
            self.assertNotEqual(0, missing_after.returncode)
            self.assertEqual(registry_before, git(partial, "worktree", "list", "--porcelain"))
            self.assertEqual([], remote_children(trace))


if __name__ == "__main__":
    unittest.main()
