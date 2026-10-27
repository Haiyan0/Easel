#!/usr/bin/env python3
"""A3 持久路径预检：只在容器内以 UID 10001 运行。

不接触宿主 HOME/开发者目录。检查创建、SQLite/WAL、跨进程 flock、fsync、
同目录原子替换、symlink 越界识别，并可在新容器中复核已有数据。
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import sys
from pathlib import Path


def _write_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _lock_child(lock_path: Path) -> int:
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return 4
    finally:
        os.close(fd)


def _first(root: Path) -> dict:
    probe = root / "probe"
    probe.mkdir(parents=True, exist_ok=True)
    sentinel = probe / "sentinel.bin"
    if not sentinel.exists():
        fd = os.open(sentinel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"a3-persistence\n")
            os.fsync(fd)
        finally:
            os.close(fd)
    _fsync_dir(probe)

    database = probe / "state.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS probe (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT OR REPLACE INTO probe(id, value) VALUES (1, ?)",
                           ("persistent",))
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
    finally:
        connection.close()

    lock_path = probe / "lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        child = os.spawnv(
            os.P_WAIT, sys.executable,
            [sys.executable, __file__, "--lock-child", str(lock_path)])
    finally:
        os.close(lock_fd)

    temporary = probe / "atomic.tmp"
    final = probe / "atomic.txt"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, b"atomic-data")
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, final)
    _fsync_dir(probe)

    link = probe / "escape-link"
    try:
        if link.is_symlink():
            link.unlink()
        link.symlink_to("/etc")
        escaped = not str(link.resolve()).startswith(str(root.resolve()) + os.sep)
        link.unlink()
    except OSError:
        escaped = None

    payload = {
        "phase": "first",
        "uid": os.getuid(),
        "gid": os.getgid(),
        "root": str(root),
        "create": sentinel.is_file(),
        "sqlite_read": connection_value(database) == "persistent",
        "lock_cross_process": child == 0,
        "fsync": True,
        "atomic_replace": final.read_text(encoding="ascii") == "atomic-data",
        "symlink_escape_detected": escaped,
    }
    _write_json(probe / "result.json", payload)
    return payload


def connection_value(path: Path) -> str | None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT value FROM probe WHERE id = 1").fetchone()
        return str(row[0]) if row else None
    finally:
        connection.close()


def _verify(root: Path) -> dict:
    probe = root / "probe"
    result_path = probe / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    payload = {
        "phase": "verify",
        "uid": os.getuid(),
        "gid": os.getgid(),
        "root": str(root),
        "result_present": result.get("phase") == "first",
        "sentinel": (probe / "sentinel.bin").is_file(),
        "sqlite_read": connection_value(probe / "state.sqlite3") == "persistent",
        "atomic_replace": (probe / "atomic.txt").read_text(
            encoding="ascii") == "atomic-data",
        "symlink_escape_detected": result.get("symlink_escape_detected"),
    }
    _write_json(probe / "verify.json", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="")
    parser.add_argument("--phase", choices=("first", "verify"), default="first")
    parser.add_argument("--lock-child", default="")
    args = parser.parse_args(argv)
    if args.lock_child:
        return _lock_child(Path(args.lock_child))
    root = Path(args.root)
    if args.phase == "first":
        payload = _first(root)
    else:
        payload = _verify(root)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0 if all(value is not False for key, value in payload.items()
                    if key not in ("uid", "gid", "root", "phase")) else 1


if __name__ == "__main__":
    sys.exit(main())
