"""只读读取正式 control 的当前 Stop 状态与 key 来源指纹。

由升级预检在只读容器内执行：先把 control.sqlite3（及可能的 WAL/SHM）
复制到 /tmp，再直接查询 control_state 表；不打开 Store、不执行迁移、不写
正式卷。只输出哈希与停止元数据，绝不输出 key 内容。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

_DB = "control.sqlite3"
_AUX = ("control.sqlite3-wal", "control.sqlite3-shm")
_KEYS = (
    ("master_key", "master.key"),
    ("signing_key", "snapshot.pem"),
    ("worker_public", "worker-public.key"),
    ("gateway_public", "gateway-public.pem"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--control-dir", required=True)
    parser.add_argument("--keys-dir", required=True)
    parser.add_argument("--tmp-dir", default="/tmp")
    args = parser.parse_args(argv)
    control = Path(args.control_dir)
    keys = Path(args.keys_dir)
    source = control / _DB
    if not source.is_file():
        print("错误：control_database_missing", file=sys.stderr)
        return 3
    try:
        with tempfile.TemporaryDirectory(dir=args.tmp_dir) as directory:
            target = Path(directory)
            shutil.copyfile(source, target / _DB)
            for name in _AUX:
                auxiliary = control / name
                if auxiliary.is_file():
                    shutil.copyfile(auxiliary, target / name)
            connection = sqlite3.connect(str(target / _DB))
            try:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT key, value FROM control_state"
                    " WHERE key IN ('admission_barrier', 'stop_completion')"
                ).fetchall()
            finally:
                connection.close()
            values = {row["key"]: json.loads(row["value"]) for row in rows}
            print("stop_state=" + json.dumps({
                "admission_barrier": values.get("admission_barrier"),
                "stop_completion": values.get("stop_completion"),
            }, sort_keys=True, separators=(",", ":")))
        for output, name in _KEYS:
            path = keys / name
            print(f"{output}_sha256=" + (
                _sha256(path) if path.is_file() else "missing"))
    except (OSError, ValueError, sqlite3.Error, TypeError):
        print("错误：stop_state_unavailable", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
