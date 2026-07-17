from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

try:
    import resource
except ImportError:  # pragma: no cover - platforms without POSIX resource limits
    resource = None  # type: ignore[assignment]

import pytest

import jobs_assistant.artifacts as artifacts
from jobs_assistant.artifacts import ArtifactRoot, ArtifactSecurityError


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_artifact_root_creates_private_root_run_dirs_and_files(tmp_path: Path) -> None:
    root_path = tmp_path / "data" / "application-runs"
    root = ArtifactRoot.open(root_path, cwd=tmp_path)
    run = root.create_run_dir(42)
    result = run.write_json("run.json", {"run_id": 42})

    assert mode(root_path) == 0o700
    assert mode(root_path / "run-42") == 0o700
    assert mode(root_path / "run-42" / "run.json") == 0o600
    assert result.relative_path == "run-42/run.json"
    assert result.sha256
    assert json.loads((root_path / "run-42" / "run.json").read_text()) == {"run_id": 42}



def test_artifact_run_copies_retained_fd_with_hash_and_no_replace(tmp_path: Path) -> None:
    source = tmp_path / "resume.pdf"
    payload = b"fixture resume"
    source.write_bytes(payload)
    with ArtifactRoot.open(tmp_path / "artifacts", cwd=tmp_path) as root:
        with root.create_run_dir(7) as run:
            with source.open("rb") as handle:
                result = run.copy_from_fd("input/resume.pdf", handle.fileno(), expected_sha256=hashlib.sha256(payload).hexdigest())
            assert result.relative_path == "run-7/input/resume.pdf"
            assert result.bytes_written == len(payload)
            assert (tmp_path / "artifacts" / "run-7" / "input" / "resume.pdf").read_bytes() == payload
            with source.open("rb") as handle, pytest.raises(FileExistsError):
                run.copy_from_fd("input/resume.pdf", handle.fileno(), expected_sha256=hashlib.sha256(payload).hexdigest())

def test_artifact_root_accepts_owner_0755_existing_data_parent_and_root(tmp_path: Path) -> None:
    data = tmp_path / "data"
    root_path = data / "application-runs"
    data.mkdir(mode=0o755)
    root_path.mkdir(mode=0o755)

    root = ArtifactRoot.open("data/application-runs", cwd=tmp_path)
    run = root.create_run_dir(1)

    assert root.ref_for_run(1) == "run-1"
    assert mode(root_path) == 0o755
    assert mode(root_path / "run-1") == 0o700
    assert str(root_path.resolve()) not in repr(root)
    assert str(root_path.resolve()) not in run.public_ref


def test_artifact_root_rejects_relative_escape_system_ancestors_and_symlinks(tmp_path: Path) -> None:
    with pytest.raises(ArtifactSecurityError):
        ArtifactRoot.open("../outside", cwd=tmp_path)

    with pytest.raises(ArtifactSecurityError):
        ArtifactRoot.open("/System/application-runs", cwd=tmp_path)

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ArtifactSecurityError):
        ArtifactRoot.open(link, cwd=tmp_path)


def test_artifact_root_accepts_sticky_tmp_ancestor(tmp_path: Path) -> None:
    sticky = tmp_path / "sticky"
    sticky.mkdir(mode=0o777)
    os.chmod(sticky, 0o1777)

    root = ArtifactRoot.open(sticky / "application-runs", cwd=tmp_path)
    assert root.ref_for_run(7) == "run-7"


def test_existing_root_must_be_directory_and_not_world_writable(tmp_path: Path) -> None:
    file_root = tmp_path / "file"
    file_root.write_text("not a dir")
    with pytest.raises(ArtifactSecurityError):
        ArtifactRoot.open(file_root, cwd=tmp_path)

    writable = tmp_path / "writable"
    writable.mkdir()
    os.chmod(writable, 0o777)
    try:
        with pytest.raises(ArtifactSecurityError):
            ArtifactRoot.open(writable, cwd=tmp_path)
    finally:
        os.chmod(writable, 0o700)


def test_artifact_operations_confine_paths_and_reject_symlink_replacement(tmp_path: Path) -> None:
    root_path = tmp_path / "application-runs"
    root = ArtifactRoot.open(root_path, cwd=tmp_path)
    run = root.create_run_dir(9)

    with pytest.raises(ArtifactSecurityError):
        run.write_bytes("../escape.txt", b"bad")
    with pytest.raises(ArtifactSecurityError):
        run.write_bytes("/absolute.txt", b"bad")

    target = root_path / "run-9" / "state.json"
    target.symlink_to(tmp_path / "outside")
    with pytest.raises(ArtifactSecurityError):
        run.write_json("state.json", {"bad": True})


def test_artifact_root_rejects_component_replacement_during_fd_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    data = tmp_path / "data"
    real_open = os.open
    swapped = False

    def replacing_open(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        if not swapped and path == "data" and dir_fd is not None and data.is_dir() and not data.is_symlink():
            swapped = True
            data.rmdir()
            data.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(artifacts.os, "open", replacing_open)

    with pytest.raises(ArtifactSecurityError):
        ArtifactRoot.open(data / "application-runs", cwd=tmp_path)

    assert swapped
    assert data.is_symlink()
    assert not (outside / "application-runs").exists()

def test_artifact_root_open_existing_does_not_recreate_deleted_final_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root_path = tmp_path / "private" / "application-runs"
    root_path.parent.mkdir(mode=0o700)
    root_path.mkdir(mode=0o700)
    real_open = os.open
    removed = False

    def deleting_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal removed
        if (
            not removed
            and path == root_path.name
            and dir_fd is not None
            and root_path.is_dir()
            and not root_path.is_symlink()
        ):
            removed = True
            root_path.rmdir()
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(artifacts.os, "open", deleting_open)

    with pytest.raises(ArtifactSecurityError):
        ArtifactRoot.open_existing(root_path, cwd=tmp_path)

    assert removed
    assert not root_path.exists()
    assert not list(root_path.parent.iterdir())



def test_artifact_write_rejects_directory_replacement_during_fd_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root_path = tmp_path / "application-runs"
    root = ArtifactRoot.open(root_path, cwd=tmp_path)
    run = root.create_run_dir(11)
    nested = root_path / "run-11" / "nested"
    real_open = os.open
    swapped = False

    def replacing_open(path: str | bytes | os.PathLike[str] | os.PathLike[bytes], flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        nonlocal swapped
        if not swapped and path == "nested" and dir_fd is not None and nested.is_dir() and not nested.is_symlink():
            swapped = True
            nested.rmdir()
            nested.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(artifacts.os, "open", replacing_open)

    with pytest.raises(ArtifactSecurityError):
        run.write_bytes("nested/state.json", b'{"escaped":true}')

    assert swapped
    assert nested.is_symlink()
    assert not (outside / "state.json").exists()
    assert not list(outside.iterdir())


def test_atomic_write_retries_short_os_write_without_partial_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    run = root.create_run_dir(12)
    payload = b"abcdefghijklmnopqrstuvwxyz"
    real_write = os.write
    write_sizes: list[int] = []

    def short_write(fd: int, data: bytes | bytearray | memoryview) -> int:
        limited = memoryview(data)[:3]
        written = real_write(fd, limited)
        write_sizes.append(written)
        return written

    monkeypatch.setattr(artifacts.os, "write", short_write)

    result = run.write_bytes("state.bin", payload)
    final_path = tmp_path / "application-runs" / "run-12" / "state.bin"

    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.bytes_written == len(payload)
    assert final_path.read_bytes() == payload
    assert mode(final_path) == 0o600
    assert len(write_sizes) > 1
    assert not list(final_path.parent.glob(".*.tmp"))


def test_atomic_write_returns_verified_hash_and_deduplicates_existing_bytes(tmp_path: Path) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    run = root.create_run_dir(5)

    first = run.write_bytes("iterations/0001/observation.json", b'{"ok":true}')
    second = run.write_bytes("iterations/0001/observation.json", b'{"ok":true}')

    assert first.sha256 == second.sha256
    assert first.bytes_written == len(b'{"ok":true}')
    assert (tmp_path / "application-runs" / "run-5" / "iterations" / "0001").is_dir()
    assert mode(tmp_path / "application-runs" / "run-5" / "iterations" / "0001" / "observation.json") == 0o600


def test_artifact_root_sticky_canonicalization_does_not_follow_later_cwd_changes(tmp_path: Path) -> None:
    root_path = tmp_path / "data" / "application-runs"
    root = ArtifactRoot.open("data/application-runs", cwd=tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    old_cwd = Path.cwd()
    try:
        os.chdir(other)
        root.create_run_dir(3).write_json("run.json", {"ok": True})
    finally:
        os.chdir(old_cwd)

    assert (root_path / "run-3" / "run.json").is_file()
    assert not (other / "data").exists()


def test_package_data_policy_loads_from_installed_wheel(tmp_path: Path) -> None:
    script = (
        "from importlib import resources; "
        "import jobs_assistant; "
        "p=resources.files('jobs_assistant').joinpath('safety_policy.json'); "
        "r=resources.files('jobs_assistant').joinpath('puppeteer_runner.js'); "
        "assert p.is_file(), p; assert r.is_file(), r; "
        "print(p.read_text(encoding='utf-8')[:1] + r.read_text(encoding='utf-8')[:1])"
    )
    result = subprocess.run(
        ["uv", "pip", "install", "--no-deps", "--target", str(tmp_path / "site"), "."],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert result.returncode == 0

    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp_path / "site")
    loaded = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert loaded.stdout.strip() == "{#"

def _open_fd_count(limit: int) -> int:
    count = 0
    for fd in range(limit):
        try:
            os.fstat(fd)
        except OSError:
            continue
        count += 1
    return count


def test_artifact_handles_close_explicitly_and_reject_use_after_close(tmp_path: Path) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    run = root.create_run_dir(20)
    run.write_json("state.json", {"ok": True})

    run.close()
    run.close()
    with pytest.raises(RuntimeError, match="run is closed"):
        run.write_json("after-close.json", {"ok": True})
    with pytest.raises(RuntimeError, match="run is closed"):
        run.write_bytes("after-close.bin", b"nope")
    with pytest.raises(RuntimeError, match="run is closed"):
        run.copy_from_fd("after-copy.bin", -1, expected_sha256="invalid")

    root.close()
    root.close()
    with pytest.raises(RuntimeError, match="root is closed"):
        root.ref_for_run(21)
    with pytest.raises(RuntimeError, match="root is closed"):
        root.create_run_dir(21)


def test_artifact_handles_context_manager_close_on_success_and_exception(tmp_path: Path) -> None:
    success_root: ArtifactRoot
    success_run = None
    with ArtifactRoot.open(tmp_path / "success", cwd=tmp_path) as success_root:
        with success_root.create_run_dir(1) as success_run:
            success_run.write_json("run.json", {"ok": True})
        assert success_run._fd is None
    assert success_root._fd is None
    with pytest.raises(RuntimeError, match="run is closed"):
        success_run.write_bytes("after.json", b"no")

    exception_root: ArtifactRoot
    exception_run = None
    with pytest.raises(ValueError, match="boom"):
        with ArtifactRoot.open(tmp_path / "exception", cwd=tmp_path) as exception_root:
            with exception_root.create_run_dir(2) as exception_run:
                raise ValueError("boom")
    assert exception_run is not None
    assert exception_run._fd is None
    assert exception_root._fd is None


def test_root_close_cascades_outstanding_children_without_double_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    first = root.create_run_dir(1)
    second = root.create_run_dir(2)
    root_fd = root._fd
    first_fd = first._fd
    second_fd = second._fd
    assert root_fd is not None
    assert first_fd is not None
    assert second_fd is not None

    real_close = artifacts.os.close
    closed: list[int] = []

    def recording_close(fd: int) -> None:
        closed.append(fd)
        real_close(fd)

    monkeypatch.setattr(artifacts.os, "close", recording_close)
    root.close()
    root.close()
    first.close()
    second.close()

    assert closed.count(root_fd) == 1
    assert closed.count(first_fd) == 1
    assert closed.count(second_fd) == 1
    with pytest.raises(RuntimeError, match="run is closed"):
        first.write_bytes("after-close.bin", b"no")
    with pytest.raises(RuntimeError, match="run is closed"):
        second.write_bytes("after-close.bin", b"no")

def test_directory_creation_fsyncs_child_then_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    root_fd = root._fd
    assert root_fd is not None
    root_inode = os.fstat(root_fd).st_ino
    real_fsync = artifacts.os.fsync
    fsynced_inodes: list[int] = []

    def recording_fsync(fd: int) -> None:
        fsynced_inodes.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(artifacts.os, "fsync", recording_fsync)
    try:
        run = root.create_run_dir(30)
        run_fd = run._fd
        assert run_fd is not None
        run_inode = os.fstat(run_fd).st_ino
        assert fsynced_inodes[-2:] == [run_inode, root_inode]
    finally:
        root.close()


def test_directory_fsync_failure_does_not_publish_run_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    real_fsync = artifacts.os.fsync

    def fail_fsync(fd: int) -> None:
        raise OSError("injected directory sync failure")

    monkeypatch.setattr(artifacts.os, "fsync", fail_fsync)
    try:
        with pytest.raises(OSError, match="directory sync failure"):
            root.create_run_dir(31)
        run_path = tmp_path / "application-runs" / "run-31"
        assert run_path.is_dir()
        assert not (run_path / "run.json").exists()
    finally:
        monkeypatch.setattr(artifacts.os, "fsync", real_fsync)
        root.close()


def test_file_fsync_failure_leaves_no_published_target_or_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    run = root.create_run_dir(32)
    real_fsync = artifacts.os.fsync

    def fail_fsync(fd: int) -> None:
        raise OSError("injected file sync failure")

    monkeypatch.setattr(artifacts.os, "fsync", fail_fsync)
    try:
        with pytest.raises(OSError, match="file sync failure"):
            run.write_bytes("state.bin", b"must not publish")
        run_path = tmp_path / "application-runs" / "run-32"
        assert not (run_path / "state.bin").exists()
        assert not list(run_path.glob(".*.tmp"))
    finally:
        monkeypatch.setattr(artifacts.os, "fsync", real_fsync)
        root.close()


def test_rename_failure_durably_cleans_unpublished_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    run = root.create_run_dir(33)

    def fail_rename(*args: object, **kwargs: object) -> None:
        raise OSError("injected rename failure")

    monkeypatch.setattr(artifacts.os, "rename", fail_rename)
    try:
        with pytest.raises(OSError, match="rename failure"):
            run.write_bytes("state.bin", b"must not publish")
        run_path = tmp_path / "application-runs" / "run-33"
        assert not (run_path / "state.bin").exists()
        assert not list(run_path.glob(".*.tmp"))
    finally:
        root.close()

def test_matching_existing_hash_retries_parent_fsync_after_post_rename_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    run = root.create_run_dir(34)
    real_fsync = artifacts.os.fsync
    parent_syncs = 0
    fail_once = True

    def fail_first_parent_sync(fd: int) -> None:
        nonlocal parent_syncs, fail_once
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            parent_syncs += 1
            if fail_once:
                fail_once = False
                raise OSError("injected post-rename parent sync failure")
        real_fsync(fd)

    monkeypatch.setattr(artifacts.os, "fsync", fail_first_parent_sync)
    try:
        with pytest.raises(OSError, match="post-rename parent sync failure"):
            run.write_bytes("state.bin", b"durability retry")
        target = tmp_path / "application-runs" / "run-34" / "state.bin"
        assert target.read_bytes() == b"durability retry"

        result = run.write_bytes("state.bin", b"durability retry")
        assert result.bytes_written == len(b"durability retry")
        assert parent_syncs >= 2
    finally:
        root.close()




def test_existing_child_retry_rechecks_child_and_parent_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    real_fsync = artifacts.os.fsync
    directory_syncs = 0

    def fail_first_parent_sync(fd: int) -> None:
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs += 1
            if directory_syncs == 2:
                raise OSError("injected parent directory sync failure")
        real_fsync(fd)

    monkeypatch.setattr(artifacts.os, "fsync", fail_first_parent_sync)
    try:
        with pytest.raises(OSError, match="parent directory sync failure"):
            root.create_run_dir(35)
        with root.create_run_dir(35) as run:
            assert directory_syncs == 4
            run.write_json("run.json", {"run_id": 35})
    finally:
        root.close()


def test_many_sequential_runs_close_under_low_nofile_limit(tmp_path: Path) -> None:
    if resource is None or not hasattr(resource, "RLIMIT_NOFILE"):
        pytest.skip("platform has no RLIMIT_NOFILE")
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    probe_limit = 4096 if hard == resource.RLIM_INFINITY else min(max(64, soft), 4096)
    inherited_fds = _open_fd_count(probe_limit)
    low = max(128, inherited_fds + 32)
    if hard != resource.RLIM_INFINITY:
        low = min(low, hard)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (low, hard))
    except (OSError, ValueError) as exc:
        pytest.skip(f"cannot lower RLIMIT_NOFILE: {exc}")
    root: ArtifactRoot | None = None
    try:
        before = _open_fd_count(low)
        root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
        for run_id in range(200):
            with root.create_run_dir(run_id) as run:
                run.write_bytes("state.bin", str(run_id).encode("ascii"))
        root.close()
        root = None
        after = _open_fd_count(low)
        assert after == before
    finally:
        if root is not None:
            root.close()
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

def test_root_close_does_not_close_reused_descriptor_from_external_legacy_close(
    tmp_path: Path,
) -> None:
    root = ArtifactRoot.open(tmp_path / "application-runs", cwd=tmp_path)
    run_fd = artifacts._open_private_child_dir(root._fd, "legacy-run-1")
    artifacts.ArtifactRun(root, "legacy-run-1", run_fd)

    # The migration compatibility caller historically closed run._fd directly.
    os.close(run_fd)
    replacement_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    if replacement_fd != run_fd:
        os.close(replacement_fd)
        root.close()
        pytest.skip("descriptor number was not immediately reused")
    try:
        root.close()
        os.fstat(replacement_fd)
    finally:
        os.close(replacement_fd)
