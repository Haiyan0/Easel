#!/usr/bin/env python3
"""技能入口：从自身文件位置定位 dbquery 包，不依赖 cwd 或 PYTHONPATH。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dbquery.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
