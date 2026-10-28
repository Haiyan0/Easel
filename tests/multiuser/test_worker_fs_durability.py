"""A4 R1-06：worker 持久化只容忍明确开发挂载的已知限制。"""
from __future__ import annotations

import errno
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from easel.control.types import DomainError
from easel.worker import fsutil
from easel.worker.sessions import WorkspaceSessionStore

WORKSPACE = "workspace-a"
ROOT = Path(__file__).resolve().parents[2]


def _wsl_available() -> bool:
    return shutil.which("wsl") is not None


def _store(tmp_path: Path) -> WorkspaceSessionStore:
    store = WorkspaceSessionStore(tmp_path, WORKSPACE)
    store.bind("s", "acp", "native", 1)
    store.append_message("s", {"role": "user", "content": "old"})
    return store


def test_native_directory_fsync_eio_is_not_swallowed(tmp_path, monkeypatch):
    """原生 Linux 目录 fsync EIO 必须向上传播，不能当成平台不支持。"""
    descriptor = os.open(tmp_path / "fd-holder", os.O_CREAT | os.O_RDWR)
    try:
        monkeypatch.setattr(fsutil, "_posix", lambda: True)
        monkeypatch.setattr(fsutil, "is_windows_bind_mount",
                            lambda _path: False)
        monkeypatch.setattr(fsutil.os, "open",
                            lambda _path, _flags: os.dup(descriptor))

        def fail(_fd: int) -> None:
            raise OSError(errno.EIO, "io error")

        monkeypatch.setattr(fsutil.os, "fsync", fail)
        with pytest.raises(OSError) as rejected:
            fsutil.fsync_directory(tmp_path)
        assert rejected.value.errno == errno.EIO
    finally:
        os.close(descriptor)


def test_known_unsupported_directory_fsync_is_tolerated_only_on_bind(
        tmp_path, monkeypatch):
    descriptor = os.open(tmp_path / "fd-holder", os.O_CREAT | os.O_RDWR)
    try:
        monkeypatch.setattr(fsutil, "_posix", lambda: True)
        monkeypatch.setattr(fsutil.os, "open",
                            lambda _path, _flags: os.dup(descriptor))
        monkeypatch.setattr(fsutil.os, "fsync",
                            lambda _fd: (_ for _ in ()).throw(
                                OSError(errno.EPERM, "not permitted")))

        monkeypatch.setattr(fsutil, "is_windows_bind_mount",
                            lambda _path: True)
        fsutil.fsync_directory(tmp_path)  # 9p 的已知 EPERM 不误报

        monkeypatch.setattr(fsutil, "is_windows_bind_mount",
                            lambda _path: False)
        with pytest.raises(OSError) as rejected:
            fsutil.fsync_directory(tmp_path)
        assert rejected.value.errno == errno.EPERM
    finally:
        os.close(descriptor)


def test_append_file_fsync_eio_preserves_old_messages_and_cleans_temp(
        tmp_path, monkeypatch):
    store = _store(tmp_path)
    real_fsync = os.fsync
    calls = {"count": 0}

    def fail_first_file(fd: int) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError(errno.EIO, "io error")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_file)
    with pytest.raises(DomainError) as rejected:
        store.append_message("s", {"role": "assistant", "content": "new"})
    assert (rejected.value.code, rejected.value.status) == (
        "session_write_failed", 500)
    assert [item["content"] for item in store.messages(
        "s", limit=10, offset=0)] == ["old"]
    directory = store.messages_path("s").parent
    assert list(directory.glob(".*.tmp")) == []


def test_bind_fchmod_eperm_is_rejected_without_bind_evidence(
        tmp_path, monkeypatch):
    descriptor = os.open(tmp_path / "fd-holder", os.O_CREAT | os.O_RDWR)
    try:
        monkeypatch.setattr(fsutil.os, "fchmod",
                            lambda _fd, _mode: (_ for _ in ()).throw(
                                OSError(errno.EPERM, "not permitted")),
                            raising=False)
        monkeypatch.setattr(fsutil, "is_windows_bind_mount",
                            lambda _path: False)
        with pytest.raises(OSError) as rejected:
            fsutil.best_effort_fchmod(descriptor, tmp_path)
        assert rejected.value.errno == errno.EPERM

        monkeypatch.setattr(fsutil, "is_windows_bind_mount",
                            lambda _path: True)
        fsutil.best_effort_fchmod(descriptor, tmp_path)
    finally:
        os.close(descriptor)


def test_wsl_native_directory_fd_eio_propagates_and_preserves_old_data():
    """WSL 原生文件系统上注入真实目录 fd EIO，验证不吞错且旧记录可读。"""
    if not _wsl_available():
        pytest.skip("本机没有 wsl")
    script = textwrap.dedent(
        """
        import errno, os, stat, sys, tempfile
        from pathlib import Path
        from easel.control.types import DomainError
        from easel.worker import fsutil
        from easel.worker.sessions import WorkspaceSessionStore

        root = Path(tempfile.mkdtemp(prefix="easel-fs-r1-"))
        store = WorkspaceSessionStore(root, "workspace-a")
        store.bind("s", "acp", "native", 1)
        store.append_message("s", {"role": "user", "content": "old"})

        real_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "injected directory io error")
            return real_fsync(fd)
        fsutil.os.fsync = fail_directory
        try:
            store.append_message(
                "s", {"role": "assistant", "content": "new"})
        except DomainError as error:
            assert (error.code, error.status) == ("session_write_failed", 500)
        else:
            raise SystemExit("directory EIO was swallowed")
        finally:
            fsutil.os.fsync = real_fsync

        messages = store.messages("s", limit=10, offset=0)
        # 目录 fsync 失败发生在原子 replace 之后：旧消息必须仍可读，且
        # 不能伪报成功/产生重复；调用方必须看到明确失败。
        assert messages[0]["content"] == "old", messages
        assert [item["content"] for item in messages].count("old") == 1
        assert not list(store.messages_path("s").parent.glob(".*.tmp"))

        store.bind("s2", "acp", "native-2", 1)
        store.append_message("s2", {"role": "user", "content": "old"})
        def fail_file(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "injected file io error")
            return real_fsync(fd)
        fsutil.os.fsync = fail_file
        try:
            store.append_message(
                "s2", {"role": "assistant", "content": "file-eio"})
        except DomainError as error:
            assert (error.code, error.status) == ("session_write_failed", 500)
        else:
            raise SystemExit("file EIO was swallowed")
        finally:
            fsutil.os.fsync = real_fsync
        assert [item["content"] for item in store.messages(
            "s2", limit=10, offset=0)] == ["old"]
        assert not list(store.messages_path("s2").parent.glob(".*.tmp"))

        fd = os.open(root, os.O_RDONLY)
        try:
            fsutil.os.fchmod = lambda _fd, _mode: (_ for _ in ()).throw(
                OSError(errno.EPERM, "injected fchmod"))
            try:
                fsutil.best_effort_fchmod(fd, root)
            except OSError as error:
                assert error.errno == errno.EPERM
            else:
                raise SystemExit("native fchmod EPERM was swallowed")
        finally:
            os.close(fd)
        print("WSL_FS_R1_OK")
        """
    )
    result = subprocess.run(
        ["wsl", "-d", "Ubuntu-26.04", "--", "python3", "-c", script],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
    assert result.returncode == 0, result.stderr[-4000:]
    assert "WSL_FS_R1_OK" in result.stdout


def test_wsl_known_9p_directory_fsync_restriction_is_tolerated():
    """真实 9p 挂载上目录 fsync 的已知错误可容忍；EIO 仍失败。"""
    if not _wsl_available():
        pytest.skip("本机没有 wsl")
    script = textwrap.dedent(
        """
        import errno, os, stat, tempfile
        from pathlib import Path
        from easel.worker import fsutil

        root = Path("/mnt/c")
        fd = os.open(root, os.O_RDONLY)
        real_fsync = os.fsync
        try:
            fsutil.os.fsync = lambda _fd: (_ for _ in ()).throw(
                OSError(errno.EINVAL, "unsupported"))
            fsutil.fsync_directory(root)
            fsutil.os.fsync = lambda _fd: (_ for _ in ()).throw(
                OSError(errno.EIO, "io error"))
            try:
                fsutil.fsync_directory(root)
            except OSError as error:
                assert error.errno == errno.EIO
            else:
                raise SystemExit("9p EIO was swallowed")
        finally:
            fsutil.os.fsync = real_fsync
            os.close(fd)
        print("WSL_9P_R1_OK")
        """
    )
    result = subprocess.run(
        ["wsl", "-d", "Ubuntu-26.04", "--", "python3", "-c", script],
        cwd=ROOT, capture_output=True, text=True, check=False, timeout=120)
    if result.returncode != 0 and "Permission denied" in result.stderr:
        pytest.skip("当前 WSL 无法读取 /mnt/c")
    assert result.returncode == 0, result.stderr[-4000:]
    assert "WSL_9P_R1_OK" in result.stdout
