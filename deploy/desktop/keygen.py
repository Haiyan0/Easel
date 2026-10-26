"""专用 master key 的创建与只读检查。

该脚本会被复制进 control 镜像，由初始化脚本以 UID 10001 在专用密钥卷内
执行。它只接受显式路径，不读取环境变量、HOME 或任何供应商秘密；重复执行
不得覆盖已有 key。错误信息只包含状态码，不回显 key 内容。
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

MASTER_KEY_BYTES = 32


def _read_key(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) != MASTER_KEY_BYTES:
        raise ValueError("invalid_master_key")
    return data


def ensure_master_key(path: str | Path) -> str:
    """确保显式路径存在 32 字节 key；已存在时只校验，不覆盖。"""
    key_path = Path(path)
    try:
        if key_path.exists():
            _read_key(key_path)
            return "exists"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL 保证并发初始化时最多一个进程创建成功；已有文件绝不被截断。
        # Windows 上必须显式 O_BINARY，否则随机字节中的 \n 会被扩成 \r\n，
        # 产生 33 字节的“key”。Linux 无 O_BINARY，按 0 处理。
        flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, "O_BINARY", 0))
        fd = os.open(str(key_path), flags, 0o600)
        try:
            os.write(fd, secrets.token_bytes(MASTER_KEY_BYTES))
            os.fsync(fd)
        finally:
            os.close(fd)
        return "created"
    except FileExistsError:
        # 另一个初始化抢先创建：只校验，不触碰它写入的内容。
        _read_key(key_path)
        return "exists"


def check_master_key(path: str | Path) -> str:
    """只读检查；缺失或长度错误时抛出明确状态码，不创建文件。"""
    key_path = Path(path)
    if not key_path.exists():
        raise ValueError("key_missing")
    _read_key(key_path)
    return "exists"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--ensure", action="store_true",
                        help="不存在时创建；已存在时只校验")
    action.add_argument("--check", action="store_true",
                        help="只读检查，不创建")
    parser.add_argument("--path", required=True)
    args = parser.parse_args(argv)
    try:
        status = (ensure_master_key(args.path) if args.ensure
                  else check_master_key(args.path))
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        # 3/4 是稳定的诊断码；调用脚本不必依赖 stderr 文本或解析 key 内容。
        return 3 if str(error) == "key_missing" else 4
    print(f"key={status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
