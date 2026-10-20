"""worker 持久浏览器 profile：本人 HOME 分区 + 跨进程独占锁。

profile 根固定为 ``<本人 HOME>/.easel-browser-profiles``，按平台/账号分成
``<platform>/<account_id>``。锁文件与技能脚本侧一致，位于 profile 父目录的
``.locks/<profile 名>.lock``：注销删除 profile 时锁对象仍然存在。context
manager 在退出时（包括异常/取消）才释放锁。

路径只来自调用方显式传入的 :class:`WorkspacePaths`，不读取宿主 HOME。
"""
from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from easel.control.types import WorkspacePaths

PROFILE_ROOT_NAME = ".easel-browser-profiles"
LOCK_DIR_NAME = ".locks"
LOCK_SUFFIX = ".lock"


def lock_path_for(profile: str | Path) -> Path:
    """账号锁文件；不放在 profile 目录内，注销删除 profile 不会破坏互斥。"""
    profile = Path(profile)
    return profile.parent / LOCK_DIR_NAME / f"{profile.name}{LOCK_SUFFIX}"
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class BrowserProfileError(ValueError):
    """平台/账号标识或 profile 路径非法。"""


def _segment(value: str, label: str) -> str:
    if (not isinstance(value, str) or not _SEGMENT.fullmatch(value)
            or value in (".", "..") or ".." in value):
        raise BrowserProfileError(f"{label} 非法：{value!r}")
    return value


class ProfileLock:
    """与 ``skills/shared/scripts/browser_state.ProfileLock`` 同一锁文件协议。"""

    def __init__(self, profile: str | Path):
        self.path = lock_path_for(profile)
        self.fd: int | None = None

    def acquire(self, timeout: float = 60.0, poll: float = 0.2) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(0.0, timeout)
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
                if time.time() >= deadline:
                    raise TimeoutError(
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


@dataclass(frozen=True)
class BrowserProfiles:
    """按 workspace 隔离的持久 profile 管理器。"""

    paths: WorkspacePaths

    @property
    def root(self) -> Path:
        return self.paths.home / PROFILE_ROOT_NAME

    def profile_dir(self, platform: str, account_id: str = "default") -> Path:
        return (self.root / _segment(platform, "platform")
                / _segment(account_id, "account_id"))

    @contextmanager
    def persistent_profile(self, platform: str, account_id: str = "default",
                           *, timeout: float = 60.0):
        """独占持有 profile 直到 context 退出；先验证再建目录。"""
        profile = self.profile_dir(platform, account_id)
        profile.mkdir(parents=True, exist_ok=True)
        lock = ProfileLock(profile)
        lock.acquire(timeout)
        try:
            yield profile
        finally:
            lock.release()


__all__ = ["BrowserProfileError", "BrowserProfiles", "LOCK_DIR_NAME",
           "LOCK_SUFFIX", "PROFILE_ROOT_NAME", "ProfileLock", "lock_path_for"]
