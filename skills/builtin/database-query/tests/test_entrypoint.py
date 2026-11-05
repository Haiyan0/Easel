"""入口必须按自身文件位置定位模块，不依赖 cwd 或 PYTHONPATH。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]


def test_entrypoint_runs_from_other_cwd_with_unicode_space_path(tmp_path: Path) -> None:
    target = tmp_path / "外部 项目" / "database-query"
    shutil.copytree(
        SKILL_ROOT,
        target,
        ignore=shutil.ignore_patterns(
            "state", "outputs", "__pycache__", ".pytest_cache", "config.yaml"
        ),
    )
    references = target / "project-docs" / "references"
    references.mkdir(parents=True)
    (references / "projects.yaml").write_text("version: 1\nprojects: []\n", encoding="utf-8")
    config = target / "config.yaml"
    config.write_text("version: 1\nproject_docs_root: ./project-docs\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": ""}

    result = subprocess.run(
        [
            sys.executable,
            str(target / "scripts" / "db_query.py"),
            "--config",
            str(config),
            "projects",
            "list",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["projects"] == []
