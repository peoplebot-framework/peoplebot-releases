from __future__ import annotations

import dataclasses
import subprocess
import tempfile
import unittest
from pathlib import Path

from peoplebot.messaging import (
    Message,
    MessageError,
    MessageKind,
    OutboundDestination,
    OutboundMessageStore,
    PeerSource,
    PublicationDisposition,
    _git_runner,
    append_and_publish_owned_message,
    append_owned_message,
    inspect_remote_tip,
    outbound_message_ref,
    publish_message,
    read_peer_messages,
    reconcile_message_publication,
    resolve_message_states,
    validate_correlated_reply,
)
from peoplebot.state import StateRef


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        capture_output=True,
        check=True,
        encoding="utf-8",
        shell=False,
        timeout=15,
    ).stdout.strip()


@unittest.skipUnless(__import__("os").name == "nt", "messaging admission uses Windows lock")
class MessagingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.a = self._repository("a")
        self.b = self._repository("b")
        self.a_remote = self.root / "a-outbound.git"
        self.b_remote = self.root / "b-outbound.git"
        git(self.root, "init", "--bare", str(self.a_remote))
        git(self.root, "init", "--bare", str(self.b_remote))
        git(self.a, "remote", "add", "outbound", str(self.a_remote))
        git(self.a, "remote", "add", "peer-b", str(self.b_remote))
        git(self.b, "remote", "add", "outbound", str(self.b_remote))
        git(self.b, "remote", "add", "peer-a", str(self.a_remote))
        self.a_id = "environment:a"
        self.b_id = "environment:b"
        self.a_repo = "https://example.test/a-outbound"
        self.b_repo = "https://example.test/b-outbound"
        self.a_ref = outbound_message_ref(self.a_id)
        self.b_ref = outbound_message_ref(self.b_id)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _repository(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        git(path, "init", "-b", "main")
        git(path, "config", "user.name", "PeopleBot Test")
        git(path, "config", "user.email", "test@example.invalid")
        (path / "artifact.txt").write_text(f"{name} artifact\n", encoding="utf-8")
        git(path, "add", ".")
        git(path, "commit", "-m", "fixture baseline")
        return path

    def _task(self, message_id: str = "message-001") -> Message:
        return Message(
            message_id=message_id,
            kind=MessageKind.TASK,
            sender=self.a_id,
            recipient=self.b_id,
            task_id="task-001",
            correlation_id="correlation-001",
            purpose="fixture.review",
            content="Review the exact synthetic artifact.",
            created_at="2026-09-14T01:00:00Z",
            states=(
                StateRef(self.a_repo, git(self.a, "rev-parse", "HEAD"), "artifact.txt"),
            ),
        )

    def _destination(self, owner: str) -> OutboundDestination:
        if owner == "a":
            return OutboundDestination(self.a_repo, "outbound", str(self.a_remote), self.a_ref)
        return OutboundDestination(self.b_repo, "outbound", str(self.b_remote), self.b_ref)

    def _source(self, reader: str) -> PeerSource:
        if reader == "a":
            return PeerSource(
                self.b_repo, "peer-b", str(self.b_remote), self.b_ref, (self.b_id,)
            )
        return PeerSource(
            self.a_repo, "peer-a", str(self.a_remote), self.a_ref, (self.a_id,)
        )

    def test_two_environment_publish_read_reply_and_exact_references(self) -> None:
        task = self._task()
        a_store = OutboundMessageStore(self.a, self.a_repo, self.a_ref)
        owned = append_and_publish_owned_message(
            self.runtime,
            self.a_id,
            "execution:publish-a",
            a_store,
            task,
            self._destination("a"),
        )
        local = owned.local
        self.assertEqual(
            owned.publication.disposition, PublicationDisposition.REMOTE_VERIFIED
        )

        tip, received = read_peer_messages(self.b, self._source("b"))
        self.assertEqual(tip, local.state.commit)
        self.assertEqual(received[0].message, task)
        resolved = resolve_message_states(task, {self.a_repo: self.a})
        self.assertEqual(resolved[0].selected_type, "blob")
        self.assertEqual(git(self.a, "show", f"{task.states[0].commit}:artifact.txt"), "a artifact")

        reply = Message(
            message_id="reply-001",
            kind=MessageKind.REPLY,
            sender=self.b_id,
            recipient=self.a_id,
            task_id=task.task_id,
            correlation_id=task.correlation_id,
            purpose="fixture.review.result",
            content="Synthetic review completed.",
            created_at="2026-09-14T01:00:01Z",
            reply_to=task.message_id,
            states=(received[0].state,),
        )
        b_store = OutboundMessageStore(self.b, self.b_repo, self.b_ref)
        owned_reply = append_and_publish_owned_message(
            self.runtime,
            self.b_id,
            "execution:publish-b",
            b_store,
            reply,
            self._destination("b"),
        )
        local_reply = owned_reply.local
        _, replies = read_peer_messages(self.a, self._source("a"))
        validate_correlated_reply(replies[0].message, task)
        self.assertEqual(replies[0].state, local_reply.state)

    def test_exact_commit_push_is_serialized_and_verified_at_distinct_push_url(self) -> None:
        fetch_remote = self.a_remote
        push_remote = self.root / "a-write.git"
        git(self.root, "init", "--bare", str(push_remote))
        git(self.a, "remote", "set-url", "--push", "outbound", str(push_remote))
        destination = OutboundDestination(
            self.a_repo, "outbound", str(push_remote), self.a_ref
        )
        store = OutboundMessageStore(self.a, self.a_repo, self.a_ref)
        competitor_attempts: list[str] = []
        triggered = False

        def interleaving_runner(checkout, arguments, timeout_seconds):
            nonlocal triggered
            if arguments[0] == "push" and not triggered:
                triggered = True
                with self.assertRaisesRegex(MessageError, "message.publisher_busy"):
                    append_and_publish_owned_message(
                        self.runtime,
                        self.a_id,
                        "execution:competing-publisher",
                        store,
                        self._task("message-competing"),
                        destination,
                    )
                competitor_attempts.append("blocked")
            return _git_runner(checkout, arguments, timeout_seconds)

        first = append_and_publish_owned_message(
            self.runtime,
            self.a_id,
            "execution:serialized-publisher",
            store,
            self._task("message-first"),
            destination,
            runner=interleaving_runner,
        )
        self.assertEqual(competitor_attempts, ["blocked"])
        self.assertEqual(
            first.publication.disposition, PublicationDisposition.REMOTE_VERIFIED
        )
        self.assertEqual(
            git(push_remote, "rev-parse", self.a_ref), first.local.state.commit
        )
        self.assertEqual(git(self.a, "ls-remote", str(fetch_remote), self.a_ref), "")

        second = store.append(self._task("message-second"))
        third = store.append(self._task("message-third"))
        advanced = publish_message(
            self.a,
            second,
            destination,
            first.local.state.commit,
        )
        self.assertEqual(advanced.observed_remote, second.state.commit)
        self.assertEqual(store._current(), third.state.commit)
        self.assertEqual(git(push_remote, "rev-parse", self.a_ref), second.state.commit)

    def test_duplicate_conflict_malformed_stale_and_owner_boundary(self) -> None:
        store = OutboundMessageStore(self.a, self.a_repo, self.a_ref)
        task = self._task()
        first = store.append(task)
        self.assertEqual(store.append(task), first)
        with self.assertRaisesRegex(MessageError, "message.id_conflict"):
            store.append(dataclasses.replace(task, content="Conflicting content."))
        with self.assertRaises(ValueError):
            Message.from_dict({"format": "peoplebot.message.v0"})
        stale = Message(
            "reply-stale",
            MessageKind.REPLY,
            self.b_id,
            self.a_id,
            task.task_id,
            "other-correlation",
            "fixture.review.result",
            "Stale.",
            "2026-09-14T01:00:01Z",
            "other-message",
        )
        with self.assertRaisesRegex(MessageError, "message.reply_stale"):
            validate_correlated_reply(stale, task)
        with self.assertRaises(ValueError):
            publish_message(self.b, first, self._source("b"), None)  # type: ignore[arg-type]

    def test_conflict_and_uncertain_publication_are_reconciled_without_retry(self) -> None:
        store = OutboundMessageStore(self.a, self.a_repo, self.a_ref)
        first = store.append(self._task("message-first"))
        verified = publish_message(self.a, first, self._destination("a"), None)
        second = store.append(
            Message(
                message_id="message-second",
                kind=MessageKind.TASK,
                sender=self.a_id,
                recipient=self.b_id,
                task_id="task-002",
                correlation_id="correlation-002",
                purpose="fixture.review",
                content="Second task.",
                created_at="2026-09-14T01:00:02Z",
            )
        )
        conflict = publish_message(self.a, second, self._destination("a"), None)
        self.assertEqual(conflict.code, "message.remote_conflict")

        def uncertain_after_write(checkout, arguments, timeout_seconds):
            if arguments[0] == "push":
                _git_runner(checkout, arguments, timeout_seconds)
                raise subprocess.TimeoutExpired(arguments, timeout_seconds)
            return _git_runner(checkout, arguments, timeout_seconds)

        uncertain = publish_message(
            self.a,
            second,
            self._destination("a"),
            verified.observed_remote,
            runner=uncertain_after_write,
        )
        self.assertEqual(uncertain.disposition, PublicationDisposition.UNCERTAIN)
        reconciled = reconcile_message_publication(
            self.a, uncertain, self._destination("a")
        )
        self.assertEqual(reconciled.disposition, PublicationDisposition.REMOTE_VERIFIED)
        self.assertEqual(
            inspect_remote_tip(self.a, self._destination("a"), push=True),
            second.state.commit,
        )


if __name__ == "__main__":
    unittest.main()
