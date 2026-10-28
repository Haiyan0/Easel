"""worker 持久化写入的文件系统兼容边界。

Docker Desktop/WSL 的 Windows bind mount 使用 9p/drvfs/virtiofs，可能拒绝
``fchmod`` 或不支持目录 ``fsync``；这些是明确的开发平台限制。原生 Linux
文件系统上的 EIO/ENOSPC/EPERM 等持久化错误必须原样传播，不能被当成“平台
不支持”而误报写入成功。
"""
from __future__ import annotations

import errno
import os
from pathlib import Path

# 只容忍明确由 Windows/WSL 开发挂载暴露的文件系统类型。
_DEV_BIND_TYPES = frozenset({"9p", "drvfs", "virtiofs"})

# 这些 errno 才可能是“该文件系统不支持该操作”；EIO/ENOSPC 永不容忍。
_KNOWN_UNSUPPORTED = frozenset({
    errno.EPERM,
    errno.EINVAL,
    errno.ENOTSUP,
    errno.EOPNOTSUPP,
    errno.ENOSYS,
})


def _posix() -> bool:
    return os.name == "posix"


def _mount_type(path: Path | str) -> str:
    """返回覆盖该路径的最长 mountinfo 文件系统类型；无法读取时返回空。"""
    try:
        target = os.path.realpath(os.fspath(path))
        with open("/proc/self/mountinfo", encoding="utf-8",
                  errors="replace") as stream:
            best_length = -1
            best_type = ""
            for line in stream:
                left, separator, right = line.partition(" - ")
                if not separator:
                    continue
                fields = left.split()
                if len(fields) < 5:
                    continue
                mountpoint = fields[4].replace("\\040", " ").replace(
                    "\\011", "\t").replace("\\012", "\n").replace(
                    "\\134", "\\")
                if not (target == mountpoint or target.startswith(
                        mountpoint.rstrip("/") + "/")):
                    continue
                options = right.split()
                if not options or len(mountpoint) <= best_length:
                    continue
                best_length = len(mountpoint)
                best_type = options[0]
            return best_type
    except OSError:
        return ""


def is_windows_bind_mount(path: Path | str) -> bool:
    """路径是否位于 Windows/WSL 开发挂载；原生 Linux 一律为 False。"""
    if not _posix():
        return False
    return _mount_type(path) in _DEV_BIND_TYPES


def best_effort_fchmod(descriptor: int, path: Path | str) -> None:
    """mkstemp 已建 0600；只容忍开发挂载上的明确不支持错误。"""
    if not hasattr(os, "fchmod"):
        return
    try:
        os.fchmod(descriptor, 0o600)
    except OSError as error:
        if (is_windows_bind_mount(path)
                and error.errno in _KNOWN_UNSUPPORTED):
            return
        raise


def fsync_directory(path: Path | str) -> None:
    """持久化目录项；原生 Linux 的任何 fsync/close 错误都必须失败。"""
    if not _posix():
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if (is_windows_bind_mount(path)
                    and error.errno in _KNOWN_UNSUPPORTED):
                return
            raise
    finally:
        os.close(descriptor)
