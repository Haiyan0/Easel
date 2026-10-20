#!/usr/bin/env python3
"""脚本侧持久浏览器 profile 与跨进程锁。

所有打开持久 profile 的技能脚本都必须经过 :func:`launch_persistent_locked`
（或显式使用 :class:`ProfileLock`）。锁文件固定在 profile 父目录的
``.locks/<profile 名>.lock``，不随 profile 目录删除；注销与打开者始终竞争
同一个互斥对象。锁覆盖浏览器拥有 profile 的完整生命周期：context 关闭后
才释放；启动失败、异常和取消都不会把锁留给下一个任务。

``easel.worker.browser`` 使用相同的锁文件位置与语义，供 worker 内调用方使用。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from runtime_paths import browser_profile_dir

LOCK_DIR_NAME = ".locks"
LOCK_SUFFIX = ".lock"


def lock_path_for(profile: str | Path) -> Path:
    """账号锁文件：位于 profile 父目录的 .locks/，删除 profile 不会删除锁。"""
    profile = Path(profile)
    return profile.parent / LOCK_DIR_NAME / f"{profile.name}{LOCK_SUFFIX}"


class ProfileLockTimeout(TimeoutError):
    """等待同一 profile 的锁超时。"""


class ProfileLock:
    """跨进程独占锁；持锁者退出时由操作系统自动释放。"""

    def __init__(self, profile: str | Path):
        self.path = lock_path_for(profile)
        self.fd: int | None = None

    def acquire(self, timeout: float = 60.0, poll: float = 0.2, *,
                clock=None) -> None:
        clock = clock or time.time
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = clock() + max(0.0, timeout)
        last: OSError | None = None
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR)
                try:
                    if os.name == "nt":
                        import msvcrt
                        if os.fstat(fd).st_size == 0:
                            os.write(fd, b"0")
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    raise
                self.fd = fd
                return
            except OSError as error:
                last = error
                if clock() >= deadline:
                    raise ProfileLockTimeout(
                        f"登录目录正被占用：{self.path}") from last
                time.sleep(poll)

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None

    def __enter__(self) -> "ProfileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


class _LockedContext:
    """代理 Playwright context：close() 完成后再释放 profile 锁（可重复调用）。"""

    def __init__(self, context, lock: ProfileLock):
        object.__setattr__(self, "_context", context)
        object.__setattr__(self, "_lock", lock)
        object.__setattr__(self, "_closed", False)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_context"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_context"), name, value)

    def close(self, *args, **kwargs):
        if object.__getattribute__(self, "_closed"):
            return
        context = object.__getattribute__(self, "_context")
        lock = object.__getattribute__(self, "_lock")
        try:
            return context.close(*args, **kwargs)
        finally:
            # 底层 close 已返回（或已抛出并完成其清理）后才允许下一个任务进入
            object.__setattr__(self, "_closed", True)
            lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()


def launch_persistent_locked(profile: str | Path, launch, *,
                             timeout: float = 60.0):
    """持锁启动持久 profile；返回的 context 在 close 后释放锁。"""
    lock = ProfileLock(profile)
    lock.acquire(timeout)
    try:
        context = launch()
    except BaseException:
        lock.release()
        raise
    return _LockedContext(context, lock)


__all__ = [
    "LOCK_DIR_NAME", "LOCK_SUFFIX", "ProfileLock", "ProfileLockTimeout",
    "browser_profile_dir", "launch_persistent_locked", "lock_path_for",
]
