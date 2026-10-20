"""Task 6 返工：脚本侧 output_paths 在 worker 下必须失败关闭，不回退。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "shared" / "scripts"


def clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {
               "EASEL_SERVICE_MODE", "EASEL_ROOT", "EASEL_CODE_ROOT",
               "EASEL_WORKSPACE_ROOT", "EASEL_STATE_ROOT", "PYTHONPATH",
           }}
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def worker_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = clean_env()
    env.update({
        "EASEL_SERVICE_MODE": "worker",
        "EASEL_CODE_ROOT": str(tmp_path / "code"),
        "EASEL_WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "EASEL_STATE_ROOT": str(tmp_path / "state"),
        "HOME": str(tmp_path / "home"),
    })
    env.update(overrides)
    return env


def run_import(tmp_path: Path, env: dict[str, str],
               scripts: Path = SCRIPTS) -> subprocess.CompletedProcess:
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    code = ("import sys; sys.path.insert(0, {!r}); import output_paths;"
            " print(output_paths.PROJECT_ROOT)").format(str(scripts))
    return subprocess.run(
        [sys.executable, "-c", code], cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120)


def run_script(tmp_path: Path, env: dict[str, str],
               scripts: Path = SCRIPTS,
               target: str = "outputs/成都文旅/成品.md"
               ) -> subprocess.CompletedProcess:
    cwd = tmp_path / "cwd"
    cwd.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, str(scripts / "output_paths.py"), target],
        cwd=str(cwd), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)


def isolated_scripts(tmp_path: Path) -> Path:
    """复制 output_paths.py 但不提供 runtime_paths helper。"""
    scripts = tmp_path / "isolated-scripts"
    scripts.mkdir()
    shutil.copy2(SCRIPTS / "output_paths.py", scripts / "output_paths.py")
    return scripts


def test_worker_missing_explicit_roots_does_not_fall_back_to_easel_root(tmp_path):
    env = worker_env(tmp_path, EASEL_ROOT=str(tmp_path / "legacy"))
    for name in ("EASEL_CODE_ROOT", "EASEL_WORKSPACE_ROOT",
                 "EASEL_STATE_ROOT", "HOME"):
        env.pop(name)

    result = run_import(tmp_path, env)

    assert result.returncode != 0, result.stdout
    assert result.stdout.strip() == ""
    assert not (tmp_path / "legacy").exists()


@pytest.mark.parametrize("variable", [
    "EASEL_CODE_ROOT", "EASEL_WORKSPACE_ROOT", "EASEL_STATE_ROOT", "HOME",
])
def test_worker_rejects_relative_root_before_any_write(tmp_path, variable):
    env = worker_env(tmp_path, **{variable: "relative/path"})

    result = run_import(tmp_path, env)

    assert result.returncode != 0, result.stdout
    assert result.stdout.strip() == ""


@pytest.mark.parametrize("legacy", ["code", "other"])
def test_worker_rejects_easel_root_alias_not_pointing_to_workspace(
        tmp_path, legacy):
    env = worker_env(tmp_path, EASEL_ROOT=str(tmp_path / legacy))

    result = run_import(tmp_path, env)

    assert result.returncode != 0, result.stdout
    assert result.stdout.strip() == ""


@pytest.mark.parametrize(("code_parts", "workspace_parts"), [
    # 代码根在 workspace 内
    (("workspace", "code"), ("workspace",)),
    # workspace 在代码根内
    (("code",), ("code", "workspace")),
    # 相等
    (("shared",), ("shared",)),
])
def test_worker_rejects_code_root_overlapping_workspace(
        tmp_path, code_parts, workspace_parts):
    env = worker_env(
        tmp_path,
        EASEL_CODE_ROOT=str(tmp_path.joinpath(*code_parts)),
        EASEL_WORKSPACE_ROOT=str(tmp_path.joinpath(*workspace_parts)))

    result = run_import(tmp_path, env)

    assert result.returncode != 0, result.stdout
    assert result.stdout.strip() == ""


def test_worker_helper_unavailable_fails_without_local_fallback(tmp_path):
    scripts = isolated_scripts(tmp_path)
    env = worker_env(tmp_path, EASEL_ROOT=str(tmp_path / "workspace"))

    result = run_import(tmp_path, env, scripts=scripts)

    assert result.returncode != 0, result.stdout
    assert result.stdout.strip() == ""
    assert not (tmp_path / "workspace").exists()


def test_worker_valid_separated_roots_use_workspace_outputs(tmp_path):
    env = worker_env(tmp_path, EASEL_ROOT=str(tmp_path / "workspace"))

    imported = run_import(tmp_path, env)
    assert imported.returncode == 0, imported.stderr
    assert Path(imported.stdout.strip()) == (tmp_path / "workspace").resolve()

    scripted = run_script(tmp_path, env)
    assert scripted.returncode == 0, scripted.stderr
    target = Path(scripted.stdout.strip())
    assert target == (tmp_path / "workspace" / "outputs" / "成都文旅"
                      / "成品.md").resolve()
    assert not (tmp_path / "workspace").exists(), "校验路径不得创建目录"


def test_script_entry_worker_missing_roots_is_rejected_before_writing(tmp_path):
    env = worker_env(tmp_path, EASEL_ROOT=str(tmp_path / "legacy"))
    for name in ("EASEL_CODE_ROOT", "EASEL_WORKSPACE_ROOT",
                 "EASEL_STATE_ROOT", "HOME"):
        env.pop(name)

    result = run_script(tmp_path, env)

    assert result.returncode != 0, result.stdout
    assert result.stdout.strip() == ""
    assert not (tmp_path / "legacy").exists()


def test_local_helper_unavailable_keeps_legacy_easel_root(tmp_path):
    scripts = isolated_scripts(tmp_path)
    env = clean_env()
    env["EASEL_ROOT"] = str(tmp_path / "local-root")

    result = run_import(tmp_path, env, scripts=scripts)

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == (tmp_path / "local-root").resolve()


def test_local_without_configuration_keeps_source_scan(tmp_path):
    # 真实仓库布局：沿 __file__ 向上能找到 outputs 目录
    result = run_import(tmp_path, clean_env())

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == SCRIPTS.parents[2].resolve()
