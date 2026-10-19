"""control 侧 worker 快照签名密钥加载。

私钥只由 control 显式传入的受保护文件加载，不进入 worker 环境、挂载或日志。
签名与验签的 envelope 定义在 :mod:`easel.worker.config`，worker 侧只加载公钥。
"""
from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def load_signing_key(path: str | Path) -> Ed25519PrivateKey:
    """读取 Ed25519 私钥；缺失、类型错误或 POSIX 权限过宽明确失败。"""
    key_path = Path(path)
    data = key_path.read_bytes()
    if os.name == "posix" and key_path.stat().st_mode & 0o077:
        raise ValueError("snapshot signing key must not be group/world accessible")
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("snapshot signing key must be Ed25519")
    return key
