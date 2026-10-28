"""专用 master key 与 Ed25519 签名 key 的创建与只读检查。

该脚本会被复制进 control 镜像，由初始化脚本以 UID 10001 在专用密钥卷内
执行。它只接受显式路径，不读取环境变量、HOME 或任何供应商秘密；重复执行
不得覆盖已有 key。错误信息只包含状态码，不回显 key 内容。

签名 key 只生成一次：私钥 `/keys/snapshot.pem` 由 control 独占；worker/
gateway 只得到派生公钥。公钥缺失时可从现有私钥重建，但绝不因公钥缺失或
不匹配而轮换私钥。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

MASTER_KEY_BYTES = 32


def _read_key(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) != MASTER_KEY_BYTES:
        raise ValueError("invalid_master_key")
    return data


def _write_new(path: Path, data: bytes, mode: int) -> None:
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_BINARY", 0))
    fd = os.open(str(path), flags, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _write_replace(path: Path, data: bytes, mode: int) -> None:
    temporary = path.with_name(path.name + ".tmp")
    _write_new(temporary, data, mode)
    os.replace(temporary, path)


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


def _load_private(path: Path) -> Ed25519PrivateKey:
    if not path.exists():
        raise ValueError("signing_key_missing")
    try:
        key = serialization.load_pem_private_key(
            path.read_bytes(), password=None)
    except (ValueError, TypeError):
        raise ValueError("signing_key_invalid") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("signing_key_invalid")  # noqa: TRY004
    return key


def _public_bytes(key: Ed25519PrivateKey) -> tuple[bytes, bytes]:
    public = key.public_key()
    return (
        public.public_bytes_raw(),
        public.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo),
    )


def _check_public(raw_path: Path, pem_path: Path,
                  private: Ed25519PrivateKey) -> None:
    raw, pem = _public_bytes(private)
    if not raw_path.is_file() or raw_path.read_bytes() != raw:
        raise ValueError("signing_public_mismatch")
    if not pem_path.is_file() or pem_path.read_bytes() != pem:
        raise ValueError("signing_public_mismatch")


def ensure_signing_key(private_path: str | Path, raw_path: str | Path,
                       pem_path: str | Path) -> str:
    """创建或校验签名私钥与公钥；私钥存在时绝不轮换。"""
    private = Path(private_path)
    raw = Path(raw_path)
    pem = Path(pem_path)
    private.parent.mkdir(parents=True, exist_ok=True)
    if private.exists():
        key = _load_private(private)
        _write_derived_public(key, raw, pem)
        return "exists"
    if raw.exists() or pem.exists():
        # 只有公钥没有私钥无法恢复，也绝不能覆盖已有公钥生成新私钥。
        raise ValueError("signing_key_incomplete")
    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())
    try:
        _write_new(private, private_bytes, 0o600)
    except FileExistsError:
        # 并发初始化：只读取另一方创建的私钥并重建公钥。
        key = _load_private(private)
        _write_derived_public(key, raw, pem)
        return "exists"
    _write_derived_public(key, raw, pem)
    return "created"


def _write_derived_public(key: Ed25519PrivateKey, raw: Path,
                          pem: Path) -> None:
    raw_bytes, pem_bytes = _public_bytes(key)
    for path, data in ((raw, raw_bytes), (pem, pem_bytes)):
        if path.exists() and path.read_bytes() == data:
            continue
        _write_replace(path, data, 0o644)


def check_signing_key(private_path: str | Path, raw_path: str | Path,
                      pem_path: str | Path) -> str:
    key = _load_private(Path(private_path))
    _check_public(Path(raw_path), Path(pem_path), key)
    return "exists"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--ensure", action="store_true",
                        help="不存在时创建；已存在时只校验")
    action.add_argument("--check", action="store_true",
                        help="只读检查，不创建")
    parser.add_argument("--path", default=None,
                        help="32 字节 master key 路径")
    parser.add_argument("--signing-key", default=None,
                        help="Ed25519 私钥 PEM 路径")
    parser.add_argument("--worker-public", default=None,
                        help="worker 只读公钥（32 字节 raw）")
    parser.add_argument("--gateway-public", default=None,
                        help="gateway 只读公钥（PEM）")
    parser.add_argument("--fingerprints", action="store_true",
                        help="从私钥派生公钥并只输出 SHA-256，不输出 key 内容")
    args = parser.parse_args(argv)
    signing = (args.signing_key, args.worker_public, args.gateway_public)
    signing_count = sum(value is not None for value in signing)
    if signing_count not in (0, 3):
        print("错误：signing_key_incomplete", file=sys.stderr)
        return 4
    if args.path is None and signing_count == 0:
        print("错误：key_path_missing", file=sys.stderr)
        return 4
    if args.fingerprints and signing_count != 3:
        print("错误：signing_key_incomplete", file=sys.stderr)
        return 4
    try:
        if args.ensure:
            if args.path is not None:
                status = ensure_master_key(args.path)
                print(f"key={status}")
            if signing_count:
                status = ensure_signing_key(*signing)
                print(f"signing_key={status}")
        else:
            if args.path is not None:
                status = check_master_key(args.path)
                print(f"key={status}")
            if signing_count:
                status = check_signing_key(*signing)
                print(f"signing_key={status}")
        if args.fingerprints:
            key = _load_private(Path(args.signing_key))
            raw, pem = _public_bytes(key)
            print(f"worker_public_sha256={hashlib.sha256(raw).hexdigest()}")
            print(f"gateway_public_sha256={hashlib.sha256(pem).hexdigest()}")
    except (OSError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        # 3/4 是稳定的诊断码；调用脚本不必依赖 stderr 文本或解析 key 内容。
        return 3 if str(error) == "key_missing" else 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
