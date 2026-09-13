"""Environment-local, nonblocking admission for one Execution per Instance."""

from __future__ import annotations

import errno
import hashlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised through a patched Windows test
    _msvcrt = None


_MAX_IDENTITY_CHARACTERS = 256
_LOCK_BYTE_COUNT = 1
_CONSTRUCTOR_KEY = object()
_T = TypeVar("_T")


def _require_identity(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty and have no surrounding whitespace")
    if len(value) > _MAX_IDENTITY_CHARACTERS:
        raise ValueError(
            f"{field} must contain at most {_MAX_IDENTITY_CHARACTERS} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} must not contain control characters")


def _resource_digest(domain: bytes, *identities: str) -> str:
    digest = hashlib.sha256(b"peoplebot.instance-admission.v0\0" + domain + b"\0")
    for identity in identities:
        encoded = identity.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _resource_path(runtime_root: Path, environment_id: str, instance_id: str) -> Path:
    environment_digest = _resource_digest(b"environment", environment_id)
    instance_digest = _resource_digest(b"instance", environment_id, instance_id)
    return (
        runtime_root
        / "instance-admission-v0"
        / f"environment-{environment_digest}"
        / f"instance-{instance_digest}.lock"
    )


class AdmissionError(RuntimeError):
    """A bounded failure to establish or release operating-system admission."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class AdmissionRelease:
    """The truthful result of one release attempt."""

    code: str
    released: bool


class ExecutionAdmission:
    """Opaque ownership handle retaining the descriptor that holds the OS lock."""

    __slots__ = (
        "_descriptor",
        "_guard",
        "_locking_module",
        "environment_id",
        "execution_id",
        "instance_id",
    )

    def __init__(
        self,
        constructor_key: object,
        *,
        environment_id: str,
        instance_id: str,
        execution_id: str,
        descriptor: int,
        locking_module: object,
    ) -> None:
        if constructor_key is not _CONSTRUCTOR_KEY:
            raise TypeError("ExecutionAdmission handles are returned by try_acquire_execution")
        self.environment_id = environment_id
        self.instance_id = instance_id
        self.execution_id = execution_id
        self._descriptor: int | None = descriptor
        self._locking_module = locking_module
        self._guard = threading.Lock()

    def __copy__(self) -> ExecutionAdmission:
        raise TypeError("ExecutionAdmission handles cannot be copied")

    def __deepcopy__(self, memo: object) -> ExecutionAdmission:
        raise TypeError("ExecutionAdmission handles cannot be deep-copied")

    def __reduce__(self) -> object:
        raise TypeError("ExecutionAdmission handles cannot be serialized")

    def __reduce_ex__(self, protocol: int) -> object:
        raise TypeError("ExecutionAdmission handles cannot be serialized")

    @property
    def owns_admission(self) -> bool:
        """Return whether this handle still retains its acquired descriptor."""

        with self._guard:
            return self._descriptor is not None

    def release(self) -> AdmissionRelease:
        """Release only this handle's byte-range lock and close its descriptor."""

        with self._guard:
            descriptor = self._descriptor
            if descriptor is None:
                return AdmissionRelease("admission.not_owner", False)
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                self._locking_module.locking(
                    descriptor,
                    self._locking_module.LK_UNLCK,
                    _LOCK_BYTE_COUNT,
                )
            except (OSError, ValueError) as error:
                raise AdmissionError(
                    "admission.release_failed",
                    "the owning descriptor could not establish operating-system unlock",
                ) from error
            try:
                os.close(descriptor)
            except OSError as error:
                self._descriptor = None
                raise AdmissionError(
                    "admission.release_failed",
                    "the unlocked descriptor could not be closed cleanly",
                ) from error
            self._descriptor = None
            return AdmissionRelease("admission.released", True)


@dataclass(frozen=True, slots=True)
class AdmissionAttempt:
    """One immediate acquisition result."""

    code: str
    admission: ExecutionAdmission | None

    @property
    def acquired(self) -> bool:
        return self.admission is not None


@dataclass(frozen=True, slots=True)
class AdmissionRunResult(Generic[_T]):
    """Whether a synchronous callback entered admission, plus its return value."""

    task_started: bool
    rejection_code: str | None
    value: _T | None


def _locking_primitive() -> object:
    if (
        os.name != "nt"
        or _msvcrt is None
        or not hasattr(_msvcrt, "locking")
        or not hasattr(_msvcrt, "LK_NBLCK")
        or not hasattr(_msvcrt, "LK_UNLCK")
    ):
        raise AdmissionError(
            "admission.unsupported",
            "instance admission v0 requires Windows msvcrt byte-range locking",
        )
    return _msvcrt


def _open_resource(path: Path) -> int:
    flags = os.O_CREAT | os.O_RDWR | os.O_BINARY
    if hasattr(os, "O_NOINHERIT"):
        flags |= os.O_NOINHERIT
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, flags, 0o600)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except (OSError, ValueError) as error:
        try:
            os.close(descriptor)
        except (NameError, OSError):
            pass
        raise AdmissionError(
            "admission.runtime_unavailable",
            "the configured runtime lock resource could not be prepared",
        ) from error


def _is_lock_contention(error: OSError) -> bool:
    return error.errno == errno.EACCES or getattr(error, "winerror", None) == 33


def try_acquire_execution(
    runtime_root: str | Path,
    environment_id: str,
    instance_id: str,
    execution_id: str,
) -> AdmissionAttempt:
    """Attempt one immediate Windows OS lock for this environment and Instance."""

    _require_identity(environment_id, "environment_id")
    _require_identity(instance_id, "instance_id")
    _require_identity(execution_id, "execution_id")
    locking_module = _locking_primitive()
    root = Path(runtime_root)
    if not root.is_absolute():
        raise AdmissionError(
            "admission.runtime_unavailable",
            "the configured runtime root must be an absolute path",
        )
    resource = _resource_path(root, environment_id, instance_id)
    descriptor = _open_resource(resource)
    try:
        locking_module.locking(
            descriptor,
            locking_module.LK_NBLCK,
            _LOCK_BYTE_COUNT,
        )
    except OSError as error:
        try:
            os.close(descriptor)
        except OSError as close_error:
            raise AdmissionError(
                "admission.lock_unavailable",
                "the failed contender's descriptor could not be closed cleanly",
            ) from close_error
        if _is_lock_contention(error):
            return AdmissionAttempt("instance.already_running", None)
        raise AdmissionError(
            "admission.lock_unavailable",
            "the operating-system lock attempt failed for an unexpected reason",
        ) from error

    return AdmissionAttempt(
        "admission.acquired",
        ExecutionAdmission(
            _CONSTRUCTOR_KEY,
            environment_id=environment_id,
            instance_id=instance_id,
            execution_id=execution_id,
            descriptor=descriptor,
            locking_module=locking_module,
        ),
    )


def run_with_admission(
    runtime_root: str | Path,
    environment_id: str,
    instance_id: str,
    execution_id: str,
    task: Callable[[], _T],
) -> AdmissionRunResult[_T]:
    """Run one synchronous callback only while its Instance admission is held."""

    if not callable(task):
        raise ValueError("task must be callable")
    attempt = try_acquire_execution(
        runtime_root,
        environment_id,
        instance_id,
        execution_id,
    )
    if not attempt.acquired:
        return AdmissionRunResult(False, attempt.code, None)

    admission = attempt.admission
    assert admission is not None
    try:
        value = task()
    finally:
        admission.release()
    return AdmissionRunResult(True, None, value)
