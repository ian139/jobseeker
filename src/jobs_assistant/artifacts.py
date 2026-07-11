from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ArtifactSecurityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactWriteResult:
    relative_path: str
    sha256: str
    bytes_written: int


class ArtifactRoot:
    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd: int | None = fd
        self._closed = False
        self._lock = threading.RLock()
        self._children: set[ArtifactRun] = set()

    def __repr__(self) -> str:
        return "ArtifactRoot(<private>)"

    def __enter__(self) -> "ArtifactRoot":
        with self._lock:
            self._ensure_open()
            return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    @classmethod
    def open(cls, path: str | os.PathLike[str], *, cwd: str | os.PathLike[str]) -> "ArtifactRoot":
        raw_path = Path(path)
        cwd_path = Path(cwd).resolve(strict=True)
        if any(part == ".." for part in raw_path.parts):
            raise ArtifactSecurityError("artifact root path may not escape its base")
        if raw_path.is_absolute():
            parts = _path_parts(raw_path)
            display_path = _path_from_parts(parts)
        else:
            parts = _path_parts(cwd_path) + _path_parts(raw_path)
            display_path = _path_from_parts(parts)
            if not _is_relative_to(display_path, cwd_path):
                raise ArtifactSecurityError("artifact root escaped cwd")
        if not parts:
            raise ArtifactSecurityError("artifact root may not be filesystem root")
        if not _is_path_allowed(display_path):
            raise ArtifactSecurityError("artifact root is under a protected system location")

        root_fd = os.open("/", _OPEN_DIR_FLAGS)
        current_fd = root_fd
        opened: list[int] = []
        try:
            _validate_fd_directory_component(current_fd, ancestor=True)
            for index, part in enumerate(parts):
                is_final = index == len(parts) - 1
                child_fd = _open_or_create_dir_component(current_fd, part, final=is_final)
                opened.append(child_fd)
                current_fd = child_fd
            fd = os.dup(current_fd)
        finally:
            for opened_fd in reversed(opened):
                os.close(opened_fd)
            os.close(root_fd)
        try:
            _validate_fd_directory_component(fd, ancestor=False)
        except Exception:
            os.close(fd)
            raise
        return cls(display_path, fd)

    def _require_fd(self) -> int:
        fd = self._fd
        if self._closed or fd is None:
            raise RuntimeError("artifact root is closed")
        return fd

    def _ensure_open(self) -> None:
        self._require_fd()

    def _register_child(self, run: "ArtifactRun") -> None:
        with self._lock:
            self._ensure_open()
            self._children.add(run)

    def _unregister_child(self, run: "ArtifactRun") -> None:
        with self._lock:
            self._children.discard(run)

    def ref_for_run(self, run_id: int) -> str:
        with self._lock:
            self._ensure_open()
            return f"run-{run_id}"

    def create_run_dir(self, run_id: int) -> "ArtifactRun":
        with self._lock:
            self._ensure_open()
            name = self.ref_for_run(run_id)
            _validate_relative_artifact_path(name)
            root_fd = self._require_fd()
            run_fd = _open_private_child_dir(root_fd, name)
            return ArtifactRun(self, name, run_fd, _register=True)

    def open_run_dir(self, run_id: int) -> "ArtifactRun":
        """Open an existing private run directory without creating it."""
        with self._lock:
            self._ensure_open()
            name = self.ref_for_run(run_id)
            _validate_relative_artifact_path(name)
            root_fd = self._require_fd()
            try:
                run_fd = _open_existing_private_child_dir(root_fd, name)
            except FileNotFoundError:
                raise ArtifactSecurityError("artifact run is unavailable") from None
            return ArtifactRun(self, name, run_fd, _register=True)
    def open_artifact_ref(self, artifact_ref: str, *, run_id: int) -> "ArtifactRun":
        """Open an existing current or migrated run directory by its DB-bound ref."""
        if type(run_id) is not int or run_id <= 0:
            raise TypeError("run_id must be a positive integer")
        allowed = {f"run-{run_id}", f"legacy-run-{run_id}"}
        if artifact_ref not in allowed:
            raise ArtifactSecurityError("artifact ref does not match its run")
        with self._lock:
            self._ensure_open()
            _validate_relative_artifact_path(artifact_ref)
            try:
                run_fd = _open_existing_private_child_dir(self._require_fd(), artifact_ref)
            except FileNotFoundError:
                raise ArtifactSecurityError("artifact run is unavailable") from None
            return ArtifactRun(self, artifact_ref, run_fd, _register=True)


    def close(self) -> None:
        """Close this root and all owned runs created from it.

        Closing a root invalidates outstanding child handles rather than
        leaving them with usable descriptors detached from their owner.  Only
        handles created by ``create_run_dir`` are owned by the root; direct
        ``ArtifactRun`` construction remains a compatibility path for callers
        that retain and close the supplied descriptor themselves.  The state
        is marked closed before any descriptor is closed, making repeated or
        re-entrant close calls harmless and preventing stale descriptor reuse.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            fd = self._fd
            self._fd = None
            children = tuple(self._children)
            self._children.clear()
        first_error: BaseException | None = None
        for child in children:
            try:
                child.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if fd is not None:
            try:
                os.close(fd)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


class ArtifactRun:
    def __init__(
        self, root: ArtifactRoot, public_ref: str, fd: int, *, _register: bool = False
    ) -> None:
        self._root = root
        self.public_ref = public_ref
        self._fd: int | None = fd
        self._closed = False
        with root._lock:
            try:
                root._ensure_open()
                if _register:
                    root._children.add(self)
            except BaseException:
                self._closed = True
                self._fd = None
                os.close(fd)
                raise

    def __repr__(self) -> str:
        return f"ArtifactRun({self.public_ref!r})"

    def __enter__(self) -> "ArtifactRun":
        with self._root._lock:
            self._ensure_open()
            return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


    def _require_fd(self) -> int:
        fd = self._fd
        if self._closed or fd is None:
            raise RuntimeError("artifact run is closed")
        self._root._ensure_open()
        return fd

    def _ensure_open(self) -> None:
        self._require_fd()

    def _close_locked(self) -> None:
        if self._closed:
            self._root._children.discard(self)
            return
        self._closed = True
        fd = self._fd
        self._fd = None
        self._root._children.discard(self)
        if fd is not None:
            os.close(fd)

    def close(self) -> None:
        """Close this run's retained directory descriptor idempotently."""
        with self._root._lock:
            self._close_locked()

    def write_json(self, relative_path: str, value: Any) -> ArtifactWriteResult:
        with self._root._lock:
            self._ensure_open()
            payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            return self.write_bytes(relative_path, payload)

    def read_bytes(
        self,
        relative_path: str,
        *,
        max_bytes: int = 1024 * 1024,
        expected_sha256: str | None = None,
    ) -> bytes:
        """Read one bounded private artifact through retained directory FDs."""
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        ):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        with self._root._lock:
            self._ensure_open()
            parts = _validate_relative_artifact_path(relative_path)
            parent_fd = self._require_fd()
            opened: list[int] = []
            try:
                for directory in parts[:-1]:
                    next_fd = _open_existing_private_child_dir(parent_fd, directory)
                    opened.append(next_fd)
                    parent_fd = next_fd
                filename = parts[-1]
                try:
                    fd = os.open(
                        filename,
                        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=parent_fd,
                    )
                except OSError as exc:
                    raise ArtifactSecurityError("artifact target is unsafe") from exc
                try:
                    st = os.fstat(fd)
                    if (
                        not stat.S_ISREG(st.st_mode)
                        or stat.S_IMODE(st.st_mode) != 0o600
                        or st.st_uid != os.geteuid()
                    ):
                        raise ArtifactSecurityError("artifact target has unsafe identity")
                    if st.st_size > max_bytes:
                        raise ArtifactSecurityError("artifact exceeds budget")
                    payload = bytearray()
                    while len(payload) <= max_bytes:
                        chunk = os.read(fd, min(64 * 1024, max_bytes + 1 - len(payload)))
                        if not chunk:
                            break
                        payload.extend(chunk)
                    if len(payload) > max_bytes:
                        raise ArtifactSecurityError("artifact exceeds budget")
                    if expected_sha256 is not None:
                        observed_sha256 = hashlib.sha256(payload).hexdigest()
                        if not hmac.compare_digest(observed_sha256, expected_sha256):
                            raise ArtifactSecurityError("artifact source hash changed")
                    return bytes(payload)
                finally:
                    os.close(fd)
            finally:
                for opened_fd in reversed(opened):
                    os.close(opened_fd)

    def read_json(self, relative_path: str, *, max_bytes: int = 1024 * 1024) -> Any:
        """Read one bounded private JSON artifact through retained directory FDs."""
        try:
            return json.loads(self.read_bytes(relative_path, max_bytes=max_bytes))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ArtifactSecurityError("artifact JSON is invalid") from None

    def replace_json(self, relative_path: str, value: Any) -> ArtifactWriteResult:
        """Atomically replace one existing Python-owned JSON artifact."""
        with self._root._lock:
            self._ensure_open()
            parts = _validate_relative_artifact_path(relative_path)
            parent_fd = self._require_fd()
            opened: list[int] = []
            try:
                for directory in parts[:-1]:
                    next_fd = _open_existing_private_child_dir(parent_fd, directory)
                    opened.append(next_fd)
                    parent_fd = next_fd
                filename = parts[-1]
                _assert_regular_file(parent_fd, filename)
                payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
                digest = hashlib.sha256(payload).hexdigest()
                tmp_name = f".{filename}.{uuid.uuid4().hex}.tmp"
                fd = os.open(
                    tmp_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
                published = False
                try:
                    os.fchmod(fd, 0o600)
                    _write_all(fd, payload)
                    os.fsync(fd)
                    os.rename(tmp_name, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    published = True
                    _fsync_directory(parent_fd)
                finally:
                    os.close(fd)
                    if not published:
                        _unlink_if_exists(parent_fd, tmp_name)
                return ArtifactWriteResult(
                    f"{self.public_ref}/{'/'.join(parts)}",
                    digest,
                    len(payload),
                )
            finally:
                for opened_fd in reversed(opened):
                    os.close(opened_fd)

    def write_bytes(self, relative_path: str, payload: bytes) -> ArtifactWriteResult:
        with self._root._lock:
            self._ensure_open()
            parts = _validate_relative_artifact_path(relative_path)
            parent_fd = self._require_fd()
            opened: list[int] = []
            try:
                for directory in parts[:-1]:
                    next_fd = _open_private_child_dir(parent_fd, directory)
                    opened.append(next_fd)
                    parent_fd = next_fd
                filename = parts[-1]
                existing_hash = _existing_file_hash(parent_fd, filename)
                digest = hashlib.sha256(payload).hexdigest()
                if existing_hash == digest:
                    _fsync_directory(parent_fd)
                    return ArtifactWriteResult(f"{self.public_ref}/{'/'.join(parts)}", digest, len(payload))
                tmp_name = f".{filename}.{uuid.uuid4().hex}.tmp"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
                fd = os.open(tmp_name, flags, 0o600, dir_fd=parent_fd)
                published = False
                try:
                    try:
                        os.fchmod(fd, 0o600)
                        _write_all(fd, payload)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    os.rename(tmp_name, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    published = True
                finally:
                    if not published:
                        _unlink_if_exists(parent_fd, tmp_name)
                _assert_regular_file(parent_fd, filename)
                _fsync_directory(parent_fd)
                if _existing_file_hash(parent_fd, filename) != digest:
                    raise ArtifactSecurityError("artifact hash verification failed")
                return ArtifactWriteResult(f"{self.public_ref}/{'/'.join(parts)}", digest, len(payload))
            finally:
                for fd in reversed(opened):
                    os.close(fd)

    def copy_from_fd(self, relative_path: str, source_fd: int, *, expected_sha256: str) -> ArtifactWriteResult:
        """Copy a retained source FD into a new private artifact file."""
        with self._root._lock:
            self._ensure_open()
            if not isinstance(source_fd, int) or source_fd < 0 or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise ArtifactSecurityError("artifact source is invalid")
            parts = _validate_relative_artifact_path(relative_path)
            parent_fd = self._require_fd()
            opened: list[int] = []
            try:
                for directory in parts[:-1]:
                    next_fd = _open_private_child_dir(parent_fd, directory)
                    opened.append(next_fd)
                    parent_fd = next_fd
                filename = parts[-1]
                tmp_name = f".{filename}.{uuid.uuid4().hex}.tmp"
                expected_size = os.fstat(source_fd).st_size
                if expected_size < 0 or expected_size > 10 * 1024 * 1024:
                    raise ArtifactSecurityError("artifact source exceeds budget")
                fd = os.open(tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
                published = False
                digest = hashlib.sha256()
                try:
                    os.fchmod(fd, 0o600)
                    os.lseek(source_fd, 0, os.SEEK_SET)
                    remaining = expected_size
                    while remaining:
                        chunk = os.read(source_fd, min(1024 * 1024, remaining))
                        if not chunk:
                            raise ArtifactSecurityError("artifact source was truncated")
                        digest.update(chunk)
                        _write_all(fd, chunk)
                        remaining -= len(chunk)
                    if os.read(source_fd, 1):
                        raise ArtifactSecurityError("artifact source changed")
                    if digest.hexdigest() != expected_sha256:
                        raise ArtifactSecurityError("artifact source hash changed")
                    os.fsync(fd)
                    os.link(tmp_name, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                    os.unlink(tmp_name, dir_fd=parent_fd)
                    published = True
                finally:
                    os.close(fd)
                    if not published:
                        _unlink_if_exists(parent_fd, tmp_name)
                _assert_regular_file(parent_fd, filename)
                _fsync_directory(parent_fd)
                return ArtifactWriteResult(f"{self.public_ref}/{'/'.join(parts)}", expected_sha256, expected_size)
            finally:
                for opened_fd in reversed(opened):
                    os.close(opened_fd)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_path_allowed(path: Path) -> bool:
    protected = (Path("/System"), Path("/bin"), Path("/sbin"), Path("/usr/bin"), Path("/usr/sbin"))
    return not any(path == item or _is_relative_to(path, item) for item in protected)


_OPEN_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _path_parts(path: Path) -> tuple[str, ...]:
    return tuple(part for part in path.parts if part not in (path.anchor, ""))


def _path_from_parts(parts: tuple[str, ...]) -> Path:
    current = Path("/")
    for part in parts:
        current /= part
    return current


def _fsync_directory(fd: int) -> None:
    """Durably flush directory metadata through a retained directory FD."""
    os.fsync(fd)


def _open_or_create_dir_component(parent_fd: int, name: str, *, final: bool) -> int:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise ArtifactSecurityError("artifact path component could not be created") from exc

    try:
        fd = os.open(name, _OPEN_DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ArtifactSecurityError("artifact path component is unsafe") from exc
    try:
        _verify_child_identity(parent_fd, name, fd)
        if created:
            os.fchmod(fd, 0o700)
        _fsync_directory(fd)
        _fsync_directory(parent_fd)
        _validate_fd_directory_component(fd, ancestor=not final)
        return fd
    except Exception:
        os.close(fd)
        raise

def _verify_child_identity(parent_fd: int, name: str, child_fd: int) -> None:
    by_fd = os.fstat(child_fd)
    by_name = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if by_fd.st_dev != by_name.st_dev or by_fd.st_ino != by_name.st_ino:
        raise ArtifactSecurityError("artifact path component changed while opening")


def _validate_fd_directory_component(fd: int, *, ancestor: bool) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise ArtifactSecurityError("artifact path component is not a directory")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o022:
        if ancestor and mode & stat.S_ISVTX:
            return
        raise ArtifactSecurityError("artifact path component is group/world writable")
    if not ancestor and st.st_uid != os.geteuid():
        raise ArtifactSecurityError("artifact root must be owned by current user")


def _validate_relative_artifact_path(path: str) -> tuple[str, ...]:
    candidate = Path(path)
    if candidate.is_absolute():
        raise ArtifactSecurityError("artifact path must be relative")
    parts = candidate.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ArtifactSecurityError("artifact path escapes run directory")
    return parts


def _validate_child_dir(fd: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISDIR(st.st_mode):
        raise ArtifactSecurityError("artifact child is not a directory")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise ArtifactSecurityError("artifact child directory is not private")


def _open_private_child_dir(parent_fd: int, name: str) -> int:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    try:
        fd = os.open(name, _OPEN_DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise ArtifactSecurityError("artifact child is unsafe") from exc
    try:
        _verify_child_identity(parent_fd, name, fd)
        if created:
            os.fchmod(fd, 0o700)
        _fsync_directory(fd)
        _fsync_directory(parent_fd)
        _validate_child_dir(fd)
        return fd
    except Exception:
        os.close(fd)
        raise

def _open_existing_private_child_dir(parent_fd: int, name: str) -> int:
    fd = os.open(name, _OPEN_DIR_FLAGS, dir_fd=parent_fd)
    try:
        _verify_child_identity(parent_fd, name, fd)
        _validate_child_dir(fd)
        return fd
    except Exception:
        os.close(fd)
        raise


def _assert_regular_file(parent_fd: int, name: str) -> None:
    st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise ArtifactSecurityError("artifact target is not a regular file")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise ArtifactSecurityError("artifact target has unsafe permissions")


def _existing_file_hash(parent_fd: int, name: str) -> str | None:
    try:
        _assert_regular_file(parent_fd, name)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArtifactSecurityError("artifact target is unsafe") from exc
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    total = 0
    while total < len(view):
        try:
            written = os.write(fd, view[total:])
        except InterruptedError:
            continue
        if written <= 0:
            raise ArtifactSecurityError("artifact write made no progress")
        total += written


def _unlink_if_exists(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    _fsync_directory(parent_fd)
