"""技能测试共用夹具：临时配置与项目文档目录。"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

# 在仓库根运行 pytest 时不会加载技能内 pytest.ini 的 `pythonpath = scripts`，
# 因此在收集测试模块前把技能 scripts 目录加入 sys.path，保证 `dbquery` 可导入。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest  # noqa: E402


@pytest.fixture
def write_config(tmp_path: Path):
    """写入一个临时 YAML 配置并返回路径；内容自动去缩进。"""

    def _write(text: str, name: str = "config.yaml") -> Path:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def project_docs(tmp_path: Path):
    """建立最小 project-docs 目录并返回根路径。"""

    root = tmp_path / "project-docs"
    (root / "references").mkdir(parents=True)
    return root
