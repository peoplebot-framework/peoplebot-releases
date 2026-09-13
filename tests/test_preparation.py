from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import peoplebot.preparation as preparation_module
from peoplebot import (
    ContextManifestError,
    ContextPathExclusion,
    ContextPolicy,
    StateRef,
    WorktreePreparationError,
    build_context_manifest,
    cleanup_prepared_worktree,
    prepare_detached_worktree,
)


REPOSITORY = "example.test/owner/repo"


def git_result(
    repository: Path,
    *arguments: str,
    check: bool = True,
    input_bytes: bytes | None = None,
    environment: dict[str, str] | None = None,
    global_options: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *global_options, "-C", str(repository), *arguments],
        input=input_bytes,
        capture_output=True,
        check=check,
        env=environment,
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


def registered_worktree_paths(repository: Path) -> set[Path]:
    return {
        Path(line.removeprefix("worktree ")).resolve(strict=False)
        for line in git(repository, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    }


class PreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        git(self.repository, "init", "-b", "main")
        git(self.repository, "config", "user.name", "PeopleBot Test")
        git(self.repository, "config", "user.email", "test@example.invalid")
        (self.repository / "docs").mkdir()
        (self.repository / "docs" / "a.txt").write_text("aaaa", encoding="utf-8")
        (self.repository / "docs" / "b.txt").write_text("bbbbbb", encoding="utf-8")
        (self.repository / "docs" / "c.txt").write_text("ccc", encoding="utf-8")
        (self.repository / "docs" / "skip.txt").write_text("skip", encoding="utf-8")
        (self.repository / "root.txt").write_text("root\n", encoding="utf-8")
        (self.repository / "context-policy.json").write_text(
            '{"format":"peoplebot.context-policy.v0"}\n',
            encoding="utf-8",
        )
        git(self.repository, "add", ".")
        git(self.repository, "commit", "-m", "fixture")
        self.commit = git(self.repository, "rev-parse", "HEAD")
        self.state = StateRef(REPOSITORY, self.commit)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def policy(
        self,
        *,
        commit: str | None = None,
        max_entries: int = 64,
        max_blob_bytes: int = 1_024,
        max_total_blob_bytes: int = 4_096,
        exclusions: tuple[ContextPathExclusion, ...] = (),
    ) -> ContextPolicy:
        return ContextPolicy(
            identity=StateRef(REPOSITORY, commit or self.commit, "context-policy.json"),
            max_entries=max_entries,
            max_blob_bytes=max_blob_bytes,
            max_total_blob_bytes=max_total_blob_bytes,
            exclusions=exclusions,
        )

    def test_pinned_preparation_preserves_moved_dirty_source(self) -> None:
        (self.repository / "root.txt").write_text("second\n", encoding="utf-8")
        git(self.repository, "add", "root.txt")
        git(self.repository, "commit", "-m", "move branch")
        moved_head = git(self.repository, "rev-parse", "HEAD")
        (self.repository / "root.txt").write_text("dirty\n", encoding="utf-8")
        (self.repository / "untracked.txt").write_text("untracked\n", encoding="utf-8")
        branch_before = git(self.repository, "branch", "--show-current")
        status_before = git(self.repository, "status", "--porcelain=v1")
        index_before = (self.repository / ".git" / "index").read_bytes()

        destination = self.root / "prepared"
        prepared = prepare_detached_worktree(self.repository, self.state, destination)

        self.assertEqual(self.commit, git(destination, "rev-parse", "HEAD"))
        symbolic = git_result(destination, "symbolic-ref", "-q", "HEAD", check=False)
        self.assertNotEqual(0, symbolic.returncode)
        self.assertEqual([".git"], sorted(path.name for path in destination.iterdir()))
        self.assertFalse(prepared.working_files_materialized)
        self.assertFalse(prepared.to_dict()["working_files_materialized"])
        self.assertEqual(branch_before, git(self.repository, "branch", "--show-current"))
        self.assertEqual(moved_head, git(self.repository, "rev-parse", "HEAD"))
        self.assertEqual(status_before, git(self.repository, "status", "--porcelain=v1"))
        self.assertEqual(index_before, (self.repository / ".git" / "index").read_bytes())

        manifest = build_context_manifest(
            destination,
            self.state,
            ("root.txt",),
            self.policy(),
        )
        expected_blob = git(self.repository, "rev-parse", f"{self.commit}:root.txt")
        self.assertEqual(expected_blob, manifest.selected[0].object_id)
        cleanup_prepared_worktree(prepared)
        self.assertFalse(destination.exists())

    def test_replacement_refs_do_not_change_preparation_or_manifest(self) -> None:
        original_blob = git_result(
            self.repository,
            "rev-parse",
            f"{self.commit}:root.txt",
            global_options=("--no-replace-objects",),
        ).stdout.decode("ascii").strip()
        (self.repository / "root.txt").write_text("replacement\n", encoding="utf-8")
        git(self.repository, "add", "root.txt")
        git(self.repository, "commit", "-m", "replacement")
        replacement = git(self.repository, "rev-parse", "HEAD")
        git(self.repository, "replace", self.commit, replacement)

        prepared = prepare_detached_worktree(
            self.repository,
            self.state,
            self.root / "replacement-safe",
        )
        try:
            manifest = build_context_manifest(
                prepared.destination,
                self.state,
                ("root.txt",),
                self.policy(),
            )
            self.assertEqual(self.commit, git(prepared.destination, "rev-parse", "HEAD"))
            self.assertEqual(original_blob, manifest.selected[0].object_id)
        finally:
            cleanup_prepared_worktree(prepared)

    def test_manifest_bytes_are_independent_of_destination(self) -> None:
        first = prepare_detached_worktree(self.repository, self.state, self.root / "first")
        second = prepare_detached_worktree(self.repository, self.state, self.root / "second")
        try:
            policy = self.policy()
            first_bytes = build_context_manifest(
                first.destination,
                self.state,
                ("root.txt", "docs/a.txt"),
                policy,
            ).to_json_bytes()
            second_bytes = build_context_manifest(
                second.destination,
                self.state,
                ("docs/a.txt", "root.txt"),
                policy,
            ).to_json_bytes()
            self.assertEqual(first_bytes, second_bytes)
            decoded = json.loads(first_bytes)
            self.assertNotIn(str(first.destination), first_bytes.decode("utf-8"))
            self.assertEqual("peoplebot.context-manifest.v0", decoded["format"])
        finally:
            cleanup_prepared_worktree(first)
            cleanup_prepared_worktree(second)

    def test_selection_overlap_order_exclusions_and_size_limits(self) -> None:
        policy = self.policy(
            max_blob_bytes=5,
            max_total_blob_bytes=5,
            exclusions=(ContextPathExclusion("docs/skip.txt", "generated fixture"),),
        )
        manifest = build_context_manifest(
            self.repository,
            self.state,
            ("docs/c.txt", "docs", "docs/a.txt", "docs"),
            policy,
        )

        self.assertEqual(("docs", "docs/a.txt", "docs/c.txt"), manifest.requested_paths)
        self.assertEqual(["docs/a.txt"], [entry.path for entry in manifest.selected])
        self.assertEqual(
            ["docs/b.txt", "docs/c.txt", "docs/skip.txt"],
            [entry.entry.path for entry in manifest.excluded],
        )
        self.assertEqual(
            [
                "policy.max_blob_bytes",
                "policy.max_total_blob_bytes",
                "policy.path_excluded",
            ],
            [entry.reason_code for entry in manifest.excluded],
        )
        self.assertEqual(4, manifest.selected_blob_bytes)
        self.assertEqual(policy.to_dict(), manifest.to_dict()["policy"])

    def test_entry_limit_and_missing_path_are_classified(self) -> None:
        with self.assertRaises(ContextManifestError) as too_many:
            build_context_manifest(
                self.repository,
                self.state,
                ("docs",),
                self.policy(max_entries=2),
            )
        self.assertEqual("context.entry_limit_exceeded", too_many.exception.code)

        with self.assertRaises(ContextManifestError) as missing:
            build_context_manifest(
                self.repository,
                self.state,
                ("missing.txt",),
                self.policy(),
            )
        self.assertEqual("context.path_unavailable", missing.exception.code)

    def test_symlinks_and_gitlinks_are_described_but_not_followed(self) -> None:
        link_blob = git_result(
            self.repository,
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=b"docs/a.txt",
        ).stdout.decode("ascii").strip()
        git(self.repository, "update-index", "--add", "--cacheinfo", f"120000,{link_blob},link")
        git(
            self.repository,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{'f' * 40},vendor/component",
        )
        git(self.repository, "commit", "-m", "special entries")
        special_commit = git(self.repository, "rev-parse", "HEAD")
        special_state = StateRef(REPOSITORY, special_commit)
        manifest = build_context_manifest(
            self.repository,
            special_state,
            ("link", "vendor/component"),
            self.policy(commit=special_commit),
        )
        by_path = {entry.path: entry for entry in manifest.selected}
        self.assertEqual(("symlink", 10, "blob"), (
            by_path["link"].kind,
            by_path["link"].size,
            by_path["link"].object_type,
        ))
        self.assertEqual(("gitlink", None, "commit"), (
            by_path["vendor/component"].kind,
            by_path["vendor/component"].size,
            by_path["vendor/component"].object_type,
        ))
        self.assertEqual("f" * 40, by_path["vendor/component"].object_id)
        self.assertFalse((self.repository / "vendor" / "component").exists())

    def test_no_hooks_or_checkout_filters_execute(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        post_checkout = hooks / "post-checkout"
        post_checkout.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8", newline="\n")
        os.chmod(post_checkout, 0o755)
        (self.repository / ".gitattributes").write_text(
            "*.txt filter=tripwire\n",
            encoding="utf-8",
        )
        git(self.repository, "add", ".gitattributes")
        git(self.repository, "commit", "-m", "hostile checkout configuration")
        git(self.repository, "config", "core.hooksPath", str(hooks))
        git(self.repository, "config", "filter.tripwire.smudge", "false")
        git(self.repository, "config", "filter.tripwire.required", "true")
        commit = git(self.repository, "rev-parse", "HEAD")
        prepared = prepare_detached_worktree(
            self.repository,
            StateRef(REPOSITORY, commit),
            self.root / "no-checkout",
        )
        self.assertEqual([".git"], [path.name for path in prepared.destination.iterdir()])
        cleanup_prepared_worktree(prepared)

    def test_linked_worktree_can_prepare_and_manifest(self) -> None:
        linked = self.root / "linked-input"
        git(self.repository, "worktree", "add", "--detach", str(linked), self.commit)
        prepared = prepare_detached_worktree(linked, self.state, self.root / "from-linked")
        try:
            manifest = build_context_manifest(
                linked,
                self.state,
                ("docs/a.txt",),
                self.policy(),
            )
            self.assertEqual("docs/a.txt", manifest.selected[0].path)
            self.assertFalse(prepared.working_files_materialized)
        finally:
            cleanup_prepared_worktree(prepared)
            git(self.repository, "worktree", "remove", str(linked))

    def test_existing_destination_and_user_files_are_never_removed(self) -> None:
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "user.txt"
        marker.write_text("keep\n", encoding="utf-8")
        with self.assertRaises(WorktreePreparationError) as raised:
            prepare_detached_worktree(self.repository, self.state, existing)
        self.assertEqual("worktree.destination_exists", raised.exception.code)
        self.assertEqual("keep\n", marker.read_text(encoding="utf-8"))

        with self.assertRaises(WorktreePreparationError) as nested:
            prepare_detached_worktree(
                self.repository / "docs",
                self.state,
                self.repository / "nested-worktree",
            )
        self.assertEqual("worktree.destination_inside_source", nested.exception.code)
        self.assertFalse((self.repository / "nested-worktree").exists())

        prepared = prepare_detached_worktree(
            self.repository,
            self.state,
            self.root / "owned",
        )
        user_file = prepared.destination / "user.txt"
        user_file.write_text("keep\n", encoding="utf-8")
        with self.assertRaises(WorktreePreparationError) as cleanup_error:
            cleanup_prepared_worktree(prepared)
        self.assertEqual("worktree.cleanup_not_empty", cleanup_error.exception.code)
        self.assertTrue(user_file.exists())
        user_file.unlink()
        cleanup_prepared_worktree(prepared)

    def test_cleanup_refuses_staged_only_index_work(self) -> None:
        destination = self.root / "staged-only"
        prepared = prepare_detached_worktree(self.repository, self.state, destination)
        blob = git_result(
            destination,
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=b"durable staged work\n",
        ).stdout.decode("ascii").strip()
        git(
            destination,
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{blob},staged-only.txt",
        )
        index_path = prepared.registration_git_dir / "index"
        index_before = index_path.read_bytes()

        try:
            with self.assertRaises(WorktreePreparationError) as raised:
                cleanup_prepared_worktree(prepared)
            self.assertEqual("worktree.cleanup_metadata_changed", raised.exception.code)
            self.assertEqual((str(destination),), raised.exception.recoverable_paths)
            self.assertTrue(destination.exists())
            self.assertIn(destination.resolve(), registered_worktree_paths(self.repository))
            self.assertEqual(index_before, index_path.read_bytes())
            staged = git(destination, "ls-files", "--stage", "staged-only.txt")
            self.assertIn(blob, staged)
        finally:
            if destination.exists():
                git(self.repository, "worktree", "remove", "--force", str(destination))

    def test_preparation_refuses_staged_work_before_initial_baseline(self) -> None:
        destination = self.root / "staged-during-preparation"
        original_write_marker = preparation_module._write_registration_marker
        staged_blob = ""
        index_before = b""

        def stage_then_write_marker(path: Path, token: str, identity: str) -> None:
            nonlocal staged_blob, index_before
            staged_blob = git_result(
                destination,
                "hash-object",
                "-w",
                "--stdin",
                input_bytes=b"work staged before the initial baseline\n",
            ).stdout.decode("ascii").strip()
            git(
                destination,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{staged_blob},staged-before-baseline.txt",
            )
            git_dir = Path(git(destination, "rev-parse", "--absolute-git-dir"))
            index_before = (git_dir / "index").read_bytes()
            original_write_marker(path, token, identity)

        try:
            with patch(
                "peoplebot.preparation._write_registration_marker",
                side_effect=stage_then_write_marker,
            ):
                with self.assertRaises(WorktreePreparationError) as raised:
                    prepare_detached_worktree(self.repository, self.state, destination)
            self.assertEqual(
                "worktree.initial_state_not_disposable",
                raised.exception.code,
            )
            self.assertEqual((str(destination),), raised.exception.recoverable_paths)
            self.assertTrue(destination.exists())
            self.assertIn(destination.resolve(), registered_worktree_paths(self.repository))
            git_dir = Path(git(destination, "rev-parse", "--absolute-git-dir"))
            self.assertEqual(index_before, (git_dir / "index").read_bytes())
            staged = git(
                destination,
                "ls-files",
                "--stage",
                "staged-before-baseline.txt",
            )
            self.assertIn(staged_blob, staged)
        finally:
            if destination.exists():
                git(self.repository, "worktree", "remove", "--force", str(destination))

    def test_preparation_accepts_clean_initial_index(self) -> None:
        destination = self.root / "clean-index-during-preparation"
        original_write_marker = preparation_module._write_registration_marker

        def create_clean_index_then_write_marker(
            path: Path,
            token: str,
            identity: str,
        ) -> None:
            git(destination, "read-tree", self.commit)
            original_write_marker(path, token, identity)

        with patch(
            "peoplebot.preparation._write_registration_marker",
            side_effect=create_clean_index_then_write_marker,
        ):
            prepared = prepare_detached_worktree(
                self.repository,
                self.state,
                destination,
            )
        self.assertTrue((prepared.registration_git_dir / "index").is_file())
        cleanup_prepared_worktree(prepared)
        self.assertFalse(destination.exists())
        self.assertNotIn(destination.resolve(), registered_worktree_paths(self.repository))

    def test_failed_add_does_not_remove_competing_registration(self) -> None:
        destination = self.root / "competing"
        original_run_git = preparation_module._run_git
        interleaved = False

        def add_competitor_then_fail(
            command: list[str],
        ) -> subprocess.CompletedProcess[str]:
            nonlocal interleaved
            if not interleaved and "worktree" in command and "add" in command:
                interleaved = True
                competing = original_run_git(command)
                self.assertEqual(0, competing.returncode, competing.stderr)
                return subprocess.CompletedProcess(
                    command,
                    128,
                    "",
                    "simulated competing registration",
                )
            return original_run_git(command)

        try:
            with patch("peoplebot.preparation._run_git", side_effect=add_competitor_then_fail):
                with self.assertRaises(WorktreePreparationError) as raised:
                    prepare_detached_worktree(self.repository, self.state, destination)
            self.assertEqual("worktree.add_failed", raised.exception.code)
            self.assertEqual((str(destination),), raised.exception.recoverable_paths)
            self.assertTrue(destination.exists())
            self.assertIn(destination.resolve(), registered_worktree_paths(self.repository))
            competing_git_dir = Path(git(destination, "rev-parse", "--absolute-git-dir"))
            self.assertFalse((competing_git_dir / "peoplebot-preparation-token").exists())
        finally:
            if destination.exists():
                git(self.repository, "worktree", "remove", "--force", str(destination))

    def test_cleanup_refuses_moved_handle_and_replacement_registration(self) -> None:
        original = self.root / "original"
        moved = self.root / "moved"
        prepared = prepare_detached_worktree(self.repository, self.state, original)
        marker = prepared.registration_git_dir / "peoplebot-preparation-token"
        marker_before = marker.read_bytes()
        git(self.repository, "worktree", "move", str(original), str(moved))
        git(
            self.repository,
            "worktree",
            "add",
            "--detach",
            "--no-checkout",
            str(original),
            self.commit,
        )
        replacement_git_dir = Path(git(original, "rev-parse", "--absolute-git-dir"))

        try:
            with self.assertRaises(WorktreePreparationError) as raised:
                cleanup_prepared_worktree(prepared)
            self.assertEqual("worktree.cleanup_registration_changed", raised.exception.code)
            registry = registered_worktree_paths(self.repository)
            self.assertIn(original.resolve(), registry)
            self.assertIn(moved.resolve(), registry)
            self.assertTrue(original.exists())
            self.assertTrue(moved.exists())
            self.assertEqual(marker_before, marker.read_bytes())
            self.assertFalse((replacement_git_dir / "peoplebot-preparation-token").exists())
        finally:
            registry = registered_worktree_paths(self.repository)
            if original.resolve() in registry:
                git(self.repository, "worktree", "remove", "--force", str(original))
            registry = registered_worktree_paths(self.repository)
            if moved.resolve() in registry:
                git(self.repository, "worktree", "remove", "--force", str(moved))

    def test_partial_failure_without_ownership_marker_remains_recoverable(self) -> None:
        destination = self.root / "partial-failure"
        try:
            with patch(
                "peoplebot.preparation._write_registration_marker",
                side_effect=OSError("simulated marker failure"),
            ):
                with self.assertRaises(WorktreePreparationError) as raised:
                    prepare_detached_worktree(self.repository, self.state, destination)
            self.assertEqual("worktree.preparation_failed", raised.exception.code)
            self.assertEqual((str(destination),), raised.exception.recoverable_paths)
            self.assertTrue(destination.exists())
            self.assertIn(destination.resolve(), registered_worktree_paths(self.repository))
            git_dir = Path(git(destination, "rev-parse", "--absolute-git-dir"))
            self.assertFalse((git_dir / "peoplebot-preparation-token").exists())
        finally:
            if destination.exists():
                git(self.repository, "worktree", "remove", "--force", str(destination))


class ContextPolicyTests(unittest.TestCase):
    def test_policy_content_round_trips_and_normalizes_rules(self) -> None:
        identity = StateRef(REPOSITORY, "a" * 40, "config/context-policy.json")
        content = {
            "format": "peoplebot.context-policy.v0",
            "max_entries": 256,
            "max_blob_bytes": 1_048_576,
            "max_total_blob_bytes": 4_194_304,
            "exclusions": [
                {"path": "vendor", "reason": "third-party content"},
                {"path": "build", "reason": "generated output"},
            ],
        }
        policy = ContextPolicy.from_dict(identity, content)
        self.assertEqual(["build", "vendor"], [rule.path for rule in policy.exclusions])
        self.assertEqual(content["format"], policy.content_dict()["format"])
        self.assertEqual(identity.to_dict(), policy.to_dict()["identity"])

    def test_policy_limits_are_bounded(self) -> None:
        identity = StateRef(REPOSITORY, "a" * 40, "config/context-policy.json")
        with self.assertRaisesRegex(ValueError, "max_entries"):
            ContextPolicy(identity, 0, 1, 1)
        with self.assertRaisesRegex(ValueError, "max_blob_bytes"):
            ContextPolicy(identity, 1, 16 * 1_024 * 1_024 + 1, 1)
        with self.assertRaisesRegex(ValueError, "path-specific"):
            ContextPolicy(StateRef(REPOSITORY, "a" * 40), 1, 1, 1)


class PartialCloneContextTests(unittest.TestCase):
    def test_preparation_and_manifest_never_fetch_missing_blob(self) -> None:
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

            state = StateRef(REPOSITORY, commit)
            policy = ContextPolicy(
                StateRef(REPOSITORY, commit, "policy.json"),
                max_entries=10,
                max_blob_bytes=1_024,
                max_total_blob_bytes=4_096,
            )
            trace = root / "trace.json"
            with patch.dict(os.environ, {"GIT_TRACE2_EVENT": str(trace)}, clear=False):
                prepared = prepare_detached_worktree(partial, state, root / "prepared")
                with self.assertRaises(ContextManifestError) as raised:
                    build_context_manifest(
                        prepared.destination,
                        state,
                        ("artifact.txt",),
                        policy,
                    )
            self.assertEqual("context.object_unavailable", raised.exception.code)
            self.assertFalse(prepared.working_files_materialized)
            cleanup_prepared_worktree(prepared)
            missing_after = git_result(
                partial,
                "cat-file",
                "-e",
                blob,
                check=False,
                global_options=("--no-lazy-fetch",),
            )
            self.assertNotEqual(0, missing_after.returncode)
            self.assertEqual([], remote_children(trace))


if __name__ == "__main__":
    unittest.main()
