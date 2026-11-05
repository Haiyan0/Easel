"""技能测试共用夹具：临时配置与项目文档目录。"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


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
