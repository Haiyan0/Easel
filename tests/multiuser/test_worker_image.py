"""Task 8 R1：最终 worker 镜像必须包含实际执行代码、依赖与构建期冒烟。"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy" / "multiuser" / "Dockerfile.worker"
REQUIREMENTS = ROOT / "deploy" / "multiuser" / "requirements-worker.txt"
SMOKE = ROOT / "deploy" / "multiuser" / "smoke_worker.py"
DOCKERIGNORE = ROOT / ".dockerignore"


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_final_image_contains_worker_execution_code_and_skills():
    text = _text(DOCKERFILE)
    assert "FROM chromium AS execution" in text
    assert "FROM execution AS runtime" in text
    assert "COPY web ./web" in text
    assert "COPY skills ./skills" in text
    assert "COPY agents ./agents" in text
    assert "COPY deploy/multiuser/managed /opt/easel/managed" in text
    assert "requirements-worker.txt" in text
    assert "smoke_worker.py --check-files" in text
    assert "smoke_worker.py --check-files --require-dsh" in text
    executable_text = "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith("#"))
    assert "--no-sandbox" not in executable_text
    assert "chmod -R a+w" not in executable_text


def test_worker_requirements_are_pinned_and_cover_runtime_imports():
    lines = [
        line.strip() for line in _text(REQUIREMENTS).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "必须显式固定 worker 运行依赖"
    assert all("==" in line for line in lines), lines
    names = {line.split("==", 1)[0].lower() for line in lines}
    for required in (
            "sse-starlette", "python-multipart", "cryptography", "requests",
            "beautifulsoup4", "websocket-client", "segno", "markdown",
            "pillow", "numpy", "pandas", "matplotlib", "opencv-python-headless",
            "jieba"):
        assert required in names, f"缺少受管依赖：{required}"


def test_build_smoke_checks_files_modules_worker_mode_and_fake_harness():
    text = _text(SMOKE)
    assert "REQUIRED_FILES" in text
    assert "web/app.py" in text
    assert "agents/workspace/AGENTS.md" in text
    assert "skills/shared/scripts/runtime_paths.py" in text
    assert "REQUIRED_MODULES" in text
    assert "chromium_launch_args" in text
    assert "chromium_sandbox_options" in text
    assert "sign_worker_snapshot" in text
    assert "load_worker_snapshot" in text
    assert "ACPAdapter" in text
    assert "session/prompt" in text
    assert "FORBIDDEN_FILE_NAMES" in text
    assert "镜像包含开发者私有文件" in text
    assert "FAKE_ACP_FLAG" in text
    assert "--require-dsh" in text
    assert "acp-profile.json" in text


def test_build_context_excludes_skill_secrets_tests_and_caches():
    lines = {line.strip() for line in _text(DOCKERIGNORE).splitlines()}
    for pattern in (
            "skills/**/tests/",
            "skills/**/.github/",
            "skills/**/wechat-publisher.yaml",
            "skills/**/cookies.json",
            "skills/**/*.token",
            "skills/**/.cache/",
            "skills/**/profiles/",
            "**/.env",
            "**/.env.*"):
        assert pattern in lines, f"构建上下文未排除：{pattern}"
    # 技能运行代码本身必须保留，不能因排除规则被整体丢弃。
    assert "skills/" not in lines
    assert "web/" not in lines
