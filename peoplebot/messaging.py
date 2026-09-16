"""Bounded Git-backed owner-publishes/peer-reads messaging v0."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from ._json import stable_json_bytes
from .admission import try_acquire_execution
from .execution import _require_text, _utc_timestamp
from .provenance import GitAttemptStore, ProvenanceError
from .state import ResolvedState, StateRef, StateResolutionError, resolve_state


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")
_MESSAGE_REF_PREFIX = "refs/heads/peoplebot/messages/v0/"
_MAX_CONTENT_BYTES = 4096
_MAX_MESSAGE_BYTES = 16_384
_MAX_STATE_REFS = 8
_MAX_MESSAGES = 256
_ZERO_OBJECT_ID = "0" * 40


def _identifier(value: str, field: str) -> None:
    _require_text(value, field)
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} is not a bounded v0 identifier")


def _message_ref(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.startswith(_MESSAGE_REF_PREFIX)
        or any(character in value for character in "\0\r\n ")
    ):
        raise ValueError("message ref is not an owner-outbound v0 branch")


def outbound_message_ref(environment_id: str) -> str:
    _identifier(environment_id, "environment_id")
    digest = hashlib.sha256(environment_id.encode("utf-8")).hexdigest()
    return f"{_MESSAGE_REF_PREFIX}{digest}"


def _state_from_dict(value: object) -> StateRef:
    if not isinstance(value, Mapping) or set(value) != {"repository", "commit", "path"}:
        raise ValueError("message State reference fields are invalid")
    return StateRef(value["repository"], value["commit"], value["path"])  # type: ignore[arg-type]


class MessageKind(StrEnum):
    TASK = "task"
    REPLY = "reply"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class Message:
    message_id: str
    kind: MessageKind
    sender: str
    recipient: str
    task_id: str
    correlation_id: str
    purpose: str
    content: str
    created_at: str
    reply_to: str | None = None
    states: tuple[StateRef, ...] = ()

    def __post_init__(self) -> None:
        for field in (
            "message_id",
            "sender",
            "recipient",
            "task_id",
            "correlation_id",
            "purpose",
        ):
            _identifier(getattr(self, field), field)
        if not isinstance(self.kind, MessageKind):
            raise ValueError("kind must be a MessageKind")
        _require_text(self.content, "content")
        if len(self.content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise ValueError("message content exceeds 4096 bytes")
        _utc_timestamp(self.created_at, "created_at")
        if self.reply_to is not None:
            _identifier(self.reply_to, "reply_to")
        if self.kind is MessageKind.REPLY and self.reply_to is None:
            raise ValueError("reply messages require reply_to")
        if self.kind is not MessageKind.REPLY and self.reply_to is not None:
            raise ValueError("only reply messages may set reply_to")
        if not isinstance(self.states, tuple) or not all(
            isinstance(state, StateRef) for state in self.states
        ):
            raise ValueError("states must be a tuple of StateRef")
        if len(self.states) > _MAX_STATE_REFS:
            raise ValueError("message exceeds eight State references")
        if len({(item.repository, item.commit, item.path) for item in self.states}) != len(
            self.states
        ):
            raise ValueError("message State references must be unique")
        if len(self.to_json_bytes()) > _MAX_MESSAGE_BYTES:
            raise ValueError("serialized message exceeds 16384 bytes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
            "format": "peoplebot.message.v0",
            "kind": self.kind.value,
            "message_id": self.message_id,
            "purpose": self.purpose,
            "recipient": self.recipient,
            "reply_to": self.reply_to,
            "sender": self.sender,
            "states": [state.to_dict() for state in self.states],
            "task_id": self.task_id,
        }

    def to_json_bytes(self) -> bytes:
        return stable_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: object) -> Message:
        expected = {
            "content",
            "correlation_id",
            "created_at",
            "format",
            "kind",
            "message_id",
            "purpose",
            "recipient",
            "reply_to",
            "sender",
            "states",
            "task_id",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("message fields are invalid")
        if value.get("format") != "peoplebot.message.v0":
            raise ValueError("message format is invalid")
        states = value.get("states")
        if not isinstance(states, list):
            raise ValueError("message states must be an array")
        try:
            kind = MessageKind(value.get("kind"))
        except (TypeError, ValueError) as error:
            raise ValueError("message kind is invalid") from error
        return cls(
            message_id=value.get("message_id"),  # type: ignore[arg-type]
            kind=kind,
            sender=value.get("sender"),  # type: ignore[arg-type]
            recipient=value.get("recipient"),  # type: ignore[arg-type]
            task_id=value.get("task_id"),  # type: ignore[arg-type]
            correlation_id=value.get("correlation_id"),  # type: ignore[arg-type]
            purpose=value.get("purpose"),  # type: ignore[arg-type]
            content=value.get("content"),  # type: ignore[arg-type]
            created_at=value.get("created_at"),  # type: ignore[arg-type]
            reply_to=value.get("reply_to"),  # type: ignore[arg-type]
            states=tuple(_state_from_dict(item) for item in states),
        )


class MessageError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class PublishedMessage:
    message: Message
    state: StateRef
    ref_name: str


class OutboundMessageStore:
    """Append immutable message commits to one environment-owned direct ref."""

    def __init__(self, checkout: str | Path, repository: str, ref_name: str) -> None:
        _require_text(repository, "repository")
        _message_ref(ref_name)
        self._plumbing = GitAttemptStore(checkout, repository)
        self.checkout = self._plumbing.checkout
        self.repository = repository
        self.ref_name = ref_name

    def _current(self) -> str | None:
        symbolic = self._plumbing._git(
            "symbolic-ref", "--quiet", "--no-recurse", self.ref_name
        )
        if symbolic.returncode == 0:
            raise MessageError("message.ref_symbolic", "outbound message ref is symbolic")
        if symbolic.returncode != 1:
            raise MessageError(
                "message.ref_inspection_failed", "outbound message ref identity is unknown"
            )
        result = self._plumbing._git("rev-parse", "--verify", "--quiet", self.ref_name)
        if result.returncode == 1:
            return None
        value = result.stdout.decode("ascii", "replace").strip()
        if result.returncode != 0 or not _OBJECT_ID.fullmatch(value):
            raise MessageError(
                "message.ref_inspection_failed", "outbound message ref value is invalid"
            )
        return value

    def _tree(self, content: bytes) -> str:
        blob = self._plumbing._write_blob(content)
        return self._plumbing._object_id(
            self._plumbing._git(
                "mktree", input_bytes=f"100644 blob {blob}\tmessage.json\n".encode("ascii")
            ),
            "write the message tree",
        )

    def messages(self, tip: str | None = None) -> tuple[PublishedMessage, ...]:
        tip = tip if tip is not None else self._current()
        if tip is None:
            return ()
        if not _OBJECT_ID.fullmatch(tip):
            raise ValueError("message tip must be a full commit ID")
        history = self._plumbing._git("rev-list", "--first-parent", tip)
        if history.returncode != 0:
            raise MessageError("message.history_unavailable", "message history is unavailable")
        commits = history.stdout.decode("ascii", "replace").splitlines()
        if len(commits) > _MAX_MESSAGES:
            raise MessageError("message.history_limit_exceeded", "message history exceeds 256")
        published: list[PublishedMessage] = []
        seen: set[str] = set()
        for commit in reversed(commits):
            parents = self._plumbing._git("rev-list", "--parents", "-n", "1", commit)
            parts = parents.stdout.decode("ascii", "replace").split()
            if parents.returncode != 0 or not parts or len(parts) > 2:
                raise MessageError(
                    "message.history_invalid", "message history contains a merge or invalid commit"
                )
            state = StateRef(self.repository, commit, "message.json")
            try:
                resolved = resolve_state(self.checkout, state)
            except StateResolutionError as error:
                raise MessageError(error.code, error.detail) from error
            tree = self._plumbing._git("ls-tree", "-rz", "--full-tree", commit)
            expected_tree = (
                f"100644 blob {resolved.selected_object}\tmessage.json\0".encode("ascii")
            )
            if tree.returncode != 0 or tree.stdout != expected_tree:
                raise MessageError(
                    "message.history_invalid", "message commit tree is not message-only"
                )
            content = self._plumbing._git("cat-file", "blob", resolved.selected_object)
            if content.returncode != 0 or len(content.stdout) > _MAX_MESSAGE_BYTES:
                raise MessageError("message.invalid", "message blob is unavailable or oversized")
            try:
                value = json.loads(content.stdout)
                message = Message.from_dict(value)
            except (UnicodeDecodeError, ValueError) as error:
                raise MessageError("message.invalid", "message blob is malformed") from error
            if message.message_id in seen:
                raise MessageError("message.duplicate_id", "message history repeats an ID")
            seen.add(message.message_id)
            published.append(PublishedMessage(message, state, self.ref_name))
        return tuple(published)

    def append(self, message: Message) -> PublishedMessage:
        if not isinstance(message, Message):
            raise ValueError("message must be a Message")
        current = self._current()
        for existing in self.messages(current):
            if existing.message.message_id == message.message_id:
                if existing.message.to_json_bytes() == message.to_json_bytes():
                    return existing
                raise MessageError("message.id_conflict", "message ID has conflicting content")
        parents = (current,) if current else ()
        try:
            tree = self._tree(message.to_json_bytes())
            commit = self._plumbing._write_commit(
                tree, parents, message.created_at, f"PeopleBot message {message.message_id}"
            )
            self._plumbing._update_ref(
                self.ref_name,
                commit,
                current or _ZERO_OBJECT_ID,
                reflog_message="peoplebot outbound message v0",
                conflict_code="message.publication_conflict",
                conflict_detail="outbound message ref changed",
                symbolic_code="message.ref_symbolic",
                symbolic_detail="outbound message ref is symbolic",
                inspection_code="message.ref_inspection_failed",
                inspection_detail="outbound message ref identity is unknown",
                persistence_code="message.persistence_failed",
                persistence_detail="message commit could not be attached",
            )
        except ProvenanceError as error:
            raise MessageError(error.code, error.detail) from error
        return PublishedMessage(
            message,
            StateRef(self.repository, commit, "message.json"),
            self.ref_name,
        )


@dataclass(frozen=True, slots=True)
class OutboundDestination:
    repository: str
    remote: str
    expected_url: str
    ref_name: str

    def __post_init__(self) -> None:
        _require_text(self.repository, "repository")
        _identifier(self.remote, "remote")
        _require_text(self.expected_url, "expected_url")
        _message_ref(self.ref_name)
        parsed = urlsplit(self.expected_url)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("expected_url must not contain credentials")


@dataclass(frozen=True, slots=True)
class PeerSource:
    repository: str
    remote: str
    expected_url: str
    ref_name: str
    allowed_senders: tuple[str, ...]

    def __post_init__(self) -> None:
        OutboundDestination(
            self.repository, self.remote, self.expected_url, self.ref_name
        )
        if not self.allowed_senders:
            raise ValueError("peer source requires allowed senders")
        for sender in self.allowed_senders:
            _identifier(sender, "allowed sender")


class GitRunner(Protocol):
    def __call__(
        self, checkout: Path, arguments: tuple[str, ...], timeout_seconds: int
    ) -> subprocess.CompletedProcess[bytes]: ...


def _git_runner(
    checkout: Path, arguments: tuple[str, ...], timeout_seconds: int
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ("git", "-c", f"core.hooksPath={os.devnull}", "-C", os.fspath(checkout), *arguments),
        capture_output=True,
        check=False,
        shell=False,
        timeout=timeout_seconds,
    )


def _verify_remote(
    checkout: Path,
    endpoint: OutboundDestination | PeerSource,
    *,
    push: bool,
    runner: GitRunner,
    timeout_seconds: int,
) -> None:
    try:
        rewrite = runner(
            checkout,
            ("config", "--show-origin", "--get-regexp", r"^url\..*\.(insteadOf|pushInsteadOf)$"),
            timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise MessageError(
            "message.remote_inspection_failed", "Git URL routing inspection timed out"
        ) from error
    if rewrite.returncode not in (0, 1) or rewrite.returncode == 0:
        raise MessageError(
            "message.transport_override_unsupported",
            "Git URL rewrites are configured or could not be inspected",
        )
    arguments = ["remote", "get-url"]
    if push:
        arguments.append("--push")
    arguments.extend(("--all", endpoint.remote))
    try:
        result = runner(checkout, tuple(arguments), timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise MessageError(
            "message.remote_inspection_failed", "Git remote URL inspection timed out"
        ) from error
    urls = result.stdout.decode("utf-8", "replace").splitlines()
    if result.returncode != 0 or urls != [endpoint.expected_url]:
        raise MessageError(
            "message.destination_mismatch" if push else "message.source_mismatch",
            "configured Git remote does not match its exact expected URL",
        )


def _inspect_remote(
    checkout: Path,
    endpoint: OutboundDestination | PeerSource,
    runner: GitRunner,
    timeout_seconds: int,
) -> str | None:
    try:
        result = runner(
            checkout,
            ("ls-remote", "--refs", endpoint.expected_url, endpoint.ref_name),
            timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise MessageError("message.remote_inspection_failed", "remote ref inspection timed out") from error
    if result.returncode != 0:
        raise MessageError("message.remote_inspection_failed", "remote ref is unavailable")
    lines = [line for line in result.stdout.decode("ascii", "replace").splitlines() if line]
    if not lines:
        return None
    if len(lines) != 1:
        raise MessageError("message.remote_inspection_ambiguous", "remote ref is ambiguous")
    fields = lines[0].split("\t")
    if len(fields) != 2 or fields[1] != endpoint.ref_name or not _OBJECT_ID.fullmatch(fields[0]):
        raise MessageError("message.remote_inspection_invalid", "remote ref result is invalid")
    return fields[0]


class PublicationDisposition(StrEnum):
    REMOTE_VERIFIED = "remote_verified"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class MessagePublication:
    code: str
    disposition: PublicationDisposition
    local_state: StateRef
    expected_remote: str | None
    observed_remote: str | None


@dataclass(frozen=True, slots=True)
class OwnedMessagePublication:
    local: PublishedMessage
    publication: MessagePublication


def publish_message(
    checkout: str | Path,
    local: PublishedMessage,
    destination: OutboundDestination,
    expected_remote: str | None,
    *,
    runner: GitRunner = _git_runner,
    timeout_seconds: int = 30,
) -> MessagePublication:
    if not isinstance(destination, OutboundDestination):
        raise ValueError("publication requires an owner outbound destination")
    path = Path(checkout)
    _verify_remote(path, destination, push=True, runner=runner, timeout_seconds=timeout_seconds)
    before = _inspect_remote(path, destination, runner, timeout_seconds)
    if before != expected_remote:
        return MessagePublication(
            "message.remote_conflict",
            PublicationDisposition.FAILED,
            local.state,
            expected_remote,
            before,
        )
    try:
        pushed = runner(
            path,
            (
                "push",
                "--porcelain",
                "--no-tags",
                "--no-recurse-submodules",
                destination.remote,
                f"{local.state.commit}:{destination.ref_name}",
            ),
            timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return MessagePublication(
            "message.publication_uncertain",
            PublicationDisposition.UNCERTAIN,
            local.state,
            expected_remote,
            None,
        )
    try:
        observed = _inspect_remote(path, destination, runner, timeout_seconds)
    except MessageError:
        return MessagePublication(
            "message.publication_uncertain",
            PublicationDisposition.UNCERTAIN,
            local.state,
            expected_remote,
            None,
        )
    if observed == local.state.commit:
        return MessagePublication(
            "message.remote_verified",
            PublicationDisposition.REMOTE_VERIFIED,
            local.state,
            expected_remote,
            observed,
        )
    if pushed.returncode != 0 and observed == expected_remote:
        return MessagePublication(
            "message.publication_failed",
            PublicationDisposition.FAILED,
            local.state,
            expected_remote,
            observed,
        )
    return MessagePublication(
        "message.publication_uncertain",
        PublicationDisposition.UNCERTAIN,
        local.state,
        expected_remote,
        observed,
    )


def reconcile_message_publication(
    checkout: str | Path,
    publication: MessagePublication,
    destination: OutboundDestination,
    *,
    runner: GitRunner = _git_runner,
    timeout_seconds: int = 30,
) -> MessagePublication:
    path = Path(checkout)
    _verify_remote(path, destination, push=True, runner=runner, timeout_seconds=timeout_seconds)
    observed = _inspect_remote(path, destination, runner, timeout_seconds)
    if observed == publication.local_state.commit:
        disposition = PublicationDisposition.REMOTE_VERIFIED
        code = "message.remote_verified"
    elif observed == publication.expected_remote:
        disposition = PublicationDisposition.FAILED
        code = "message.prior_state_observed"
    else:
        disposition = PublicationDisposition.UNCERTAIN
        code = "message.intervening_state"
    return MessagePublication(
        code,
        disposition,
        publication.local_state,
        publication.expected_remote,
        observed,
    )


def read_peer_messages(
    checkout: str | Path,
    source: PeerSource,
    *,
    runner: GitRunner = _git_runner,
    timeout_seconds: int = 30,
) -> tuple[str | None, tuple[PublishedMessage, ...]]:
    if not isinstance(source, PeerSource):
        raise ValueError("message reading requires a peer source")
    path = Path(checkout)
    _verify_remote(path, source, push=False, runner=runner, timeout_seconds=timeout_seconds)
    tip = _inspect_remote(path, source, runner, timeout_seconds)
    if tip is None:
        return None, ()
    try:
        fetched = runner(
            path,
            ("fetch", "--no-tags", "--no-write-fetch-head", source.remote, tip),
            timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise MessageError("message.read_uncertain", "peer fetch timed out") from error
    if fetched.returncode != 0:
        raise MessageError("message.read_failed", "peer message State could not be fetched")
    store = OutboundMessageStore(path, source.repository, source.ref_name)
    messages = store.messages(tip)
    if any(item.message.sender not in source.allowed_senders for item in messages):
        raise MessageError("message.sender_unauthorized", "peer history has an unapproved sender")
    return tip, messages


def inspect_remote_tip(
    checkout: str | Path,
    endpoint: OutboundDestination | PeerSource,
    *,
    push: bool = False,
    runner: GitRunner = _git_runner,
    timeout_seconds: int = 30,
) -> str | None:
    path = Path(checkout)
    _verify_remote(path, endpoint, push=push, runner=runner, timeout_seconds=timeout_seconds)
    return _inspect_remote(path, endpoint, runner, timeout_seconds)


def resolve_message_states(
    message: Message,
    checkouts: Mapping[str, str | Path],
) -> tuple[ResolvedState, ...]:
    """Resolve each referenced exact State only through its caller-supplied checkout."""

    if not isinstance(message, Message):
        raise ValueError("message must be a Message")
    resolved: list[ResolvedState] = []
    for state in message.states:
        checkout = checkouts.get(state.repository)
        if checkout is None:
            raise MessageError(
                "message.state_repository_unavailable",
                "no caller-supplied checkout exists for a referenced State",
            )
        try:
            resolved.append(resolve_state(checkout, state))
        except StateResolutionError as error:
            raise MessageError(error.code, error.detail) from error
    return tuple(resolved)


def validate_correlated_reply(reply: Message, request: Message) -> None:
    if reply.kind is not MessageKind.REPLY or request.kind is not MessageKind.TASK:
        raise MessageError("message.reply_invalid", "reply correlation requires reply and task")
    if (
        reply.reply_to != request.message_id
        or reply.task_id != request.task_id
        or reply.correlation_id != request.correlation_id
        or reply.sender != request.recipient
        or reply.recipient != request.sender
    ):
        raise MessageError("message.reply_stale", "reply does not match the exact task identity")


def append_owned_message(
    runtime_root: str | Path,
    environment_id: str,
    execution_id: str,
    store: OutboundMessageStore,
    message: Message,
) -> PublishedMessage:
    """Serialize all local Instance publications through one environment-owned lock."""

    publisher_instance = f"message-publisher:{environment_id}"
    attempt = try_acquire_execution(
        runtime_root, environment_id, publisher_instance, execution_id
    )
    if not attempt.acquired:
        raise MessageError("message.publisher_busy", "environment publisher is already active")
    assert attempt.admission is not None
    try:
        return store.append(message)
    finally:
        attempt.admission.release()


def append_and_publish_owned_message(
    runtime_root: str | Path,
    environment_id: str,
    execution_id: str,
    store: OutboundMessageStore,
    message: Message,
    destination: OutboundDestination,
    *,
    runner: GitRunner = _git_runner,
    timeout_seconds: int = 30,
) -> OwnedMessagePublication:
    """Serialize append, exact-commit push, and write-endpoint verification."""

    if store.repository != destination.repository or store.ref_name != destination.ref_name:
        raise ValueError("message store and owner destination must identify the same repository/ref")
    publisher_instance = f"message-publisher:{environment_id}"
    attempt = try_acquire_execution(
        runtime_root, environment_id, publisher_instance, execution_id
    )
    if not attempt.acquired:
        raise MessageError("message.publisher_busy", "environment publisher is already active")
    assert attempt.admission is not None
    try:
        remote_before = inspect_remote_tip(
            store.checkout,
            destination,
            push=True,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
        local_before = store._current()
        if local_before != remote_before:
            raise MessageError(
                "message.local_remote_mismatch",
                "local outbound history does not match the verified write endpoint",
            )
        local = store.append(message)
        publication = publish_message(
            store.checkout,
            local,
            destination,
            remote_before,
            runner=runner,
            timeout_seconds=timeout_seconds,
        )
        return OwnedMessagePublication(local, publication)
    finally:
        attempt.admission.release()
