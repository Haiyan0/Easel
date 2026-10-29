"""只读探测 Initialize 前的 control DB/身份材料状态。

该脚本由 Initialize-WorkspaceEasel.ps1 在专用只读容器内执行；只输出
present/absent 与是否有文件，不读取 key 内容、不写卷。任何目录不可读
都以非零退出，调用脚本必须失败关闭而不是当成“全新部署”。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _has_entries(path: Path) -> bool:
    try:
        next(path.iterdir())
    except StopIteration:
        return False
    except OSError:
        raise SystemExit(3) from None
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--control", required=True)
    parser.add_argument("--keys", required=True)
    args = parser.parse_args(argv)
    control = Path(args.control)
    keys = Path(args.keys)
    try:
        if not control.is_dir() or not keys.is_dir():
            raise SystemExit(3)
        database = (control / "control.sqlite3").is_file()
        control_any = _has_entries(control)
        master = (keys / "master.key").is_file()
        signing = (keys / "snapshot.pem").is_file()
        keys_any = _has_entries(keys)
    except OSError:
        raise SystemExit(3) from None
    print(f"database={'present' if database else 'absent'}")
    print(f"control_any={'yes' if control_any else 'no'}")
    print(f"master={'present' if master else 'absent'}")
    print(f"signing={'present' if signing else 'absent'}")
    print(f"keys_any={'yes' if keys_any else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
