"""Task 6：代码根/数据根/状态根/HOME 的显式拆分与干净运行环境。"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from easel.control.types import DomainError, WorkspacePaths
from easel.worker.config import WorkerConfigSnapshot, build_run_env
from easel.worker.context import resolve_paths


def worker_roots(tmp_path: Path, **overrides) -> dict[str, str]:
    env = {
        "EASEL_SERVICE_MODE": "worker",
        "EASEL_CODE_ROOT": str(tmp_path / "code"),
        "EASEL_WORKSPACE_ROOT": str(tmp_path / "workspace"),
        "EASEL_STATE_ROOT": str(tmp_path / "state"),
        "HOME": str(tmp_path / "home"),
    }
    env.update(overrides)
    return env


def paths_at(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(
        code_root=(tmp_path / "code").resolve(),
        workspace_root=(tmp_path / "workspace").resolve(),
        state_root=(tmp_path / "state").resolve(),
        home=(tmp_path / "home").resolve(),
    )


def snapshot(routes=None, agent=None) -> WorkerConfigSnapshot:
    return WorkerConfigSnapshot(
        "workspace-a", "run-a", 3,
        routes if routes is not None else {
            "broker_url": "http://broker.internal",
            "broker_token": "run-token-sentinel",
        },
        agent if agent is not None else {"runtime": "acp"},
    )


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def test_worker_requires_explicit_private_roots(tmp_path):
    code = tmp_path / "code"
    with pytest.raises(DomainError):
        resolve_paths({"EASEL_SERVICE_MODE": "worker"}, code)

    paths = resolve_paths(worker_roots(tmp_path), code)
    assert paths.code_root == code.resolve()
    assert paths.workspace_root == (tmp_path / "workspace").resolve()
    assert paths.state_root == (tmp_path / "state").resolve()
    assert paths.home == (tmp_path / "home").resolve()


@pytest.mark.parametrize("missing", [
    "EASEL_CODE_ROOT", "EASEL_WORKSPACE_ROOT", "EASEL_STATE_ROOT", "HOME",
])
def test_worker_rejects_missing_each_private_root(tmp_path, missing):
    env = worker_roots(tmp_path)
    env.pop(missing)
    with pytest.raises(DomainError):
        resolve_paths(env, tmp_path / "code")


@pytest.mark.parametrize("variable", [
    "EASEL_CODE_ROOT", "EASEL_WORKSPACE_ROOT", "EASEL_STATE_ROOT", "HOME",
])
def test_worker_rejects_relative_private_roots(tmp_path, variable):
    env = worker_roots(tmp_path, **{variable: "relative/path"})
    with pytest.raises(DomainError):
        resolve_paths(env, tmp_path / "code")


def test_worker_rejects_home_expansion_paths(tmp_path):
    for variable in ("EASEL_CODE_ROOT", "EASEL_WORKSPACE_ROOT",
                     "EASEL_STATE_ROOT", "HOME"):
        env = worker_roots(tmp_path, **{variable: "~/.easel"})
        with pytest.raises(DomainError):
            resolve_paths(env, tmp_path / "code")


def test_worker_rejects_code_root_overlapping_writable_data(tmp_path):
    code = tmp_path / "code"
    for variable in ("EASEL_WORKSPACE_ROOT", "EASEL_STATE_ROOT", "HOME"):
        env = worker_roots(tmp_path, **{variable: str(code / "inside")})
        with pytest.raises(DomainError):
            resolve_paths(env, code)
    env = worker_roots(tmp_path, EASEL_CODE_ROOT=str(tmp_path / "workspace"))
    with pytest.raises(DomainError):
        resolve_paths(env, tmp_path / "code")


def test_worker_easel_root_must_point_to_own_data_root(tmp_path):
    env = worker_roots(tmp_path, EASEL_ROOT=str(tmp_path / "code"))
    with pytest.raises(DomainError):
        resolve_paths(env, tmp_path / "code")
    accepted = worker_roots(
        tmp_path, EASEL_ROOT=str(tmp_path / "workspace"))
    assert resolve_paths(accepted, tmp_path / "code").workspace_root == (
        tmp_path / "workspace").resolve()


def test_worker_rejects_unknown_service_mode(tmp_path):
    with pytest.raises(DomainError):
        resolve_paths({"EASEL_SERVICE_MODE": "control"}, tmp_path / "code")


def test_local_keeps_existing_code_and_working_root(tmp_path):
    paths = resolve_paths({}, tmp_path / "code")
    assert paths.code_root == (tmp_path / "code").resolve()
    assert paths.workspace_root == (tmp_path / "code").resolve()
    assert paths.state_root == (tmp_path / "code").resolve()
    assert paths.home == Path.home().resolve()


def test_resolve_paths_never_mutates_process_environment(tmp_path, monkeypatch):
    before = dict(os.environ)
    resolve_paths(worker_roots(tmp_path), tmp_path / "code")
    assert dict(os.environ) == before


# ---------------------------------------------------------------------------
# 干净的运行环境
# ---------------------------------------------------------------------------


def test_build_run_env_keeps_only_allowlisted_safe_variables(tmp_path):
    base = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HTTPS_PROXY": "http://proxy.internal:3128",
        "PYTHONUTF8": "1",
        "HOME": "/host/home",
        "DSH_HOME": "/host/.dsh",
        "OPENAI_API_KEY": "admin-key-sentinel",
        "Api_Key": "case-secret-sentinel",
        "EASEL_DIRECT_API_KEY": "direct-sentinel",
        "AWS_SECRET_ACCESS_KEY": "aws-sentinel",
        "NPM_TOKEN": "npm-sentinel",
        "SOME_PASSWORD": "password-sentinel",
        "UNRELATED": "drop-me",
    }
    env = build_run_env(paths_at(tmp_path), snapshot(), base)
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LANG"] == "C.UTF-8"
    assert env["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert env["PYTHONUTF8"] == "1"
    assert "UNRELATED" not in env
    for forbidden in ("OPENAI_API_KEY", "Api_Key", "EASEL_DIRECT_API_KEY",
                      "AWS_SECRET_ACCESS_KEY", "NPM_TOKEN", "SOME_PASSWORD"):
        assert forbidden not in env
    joined = " ".join(f"{key}={value}" for key, value in env.items())
    for sentinel in ("admin-key-sentinel", "case-secret-sentinel",
                     "direct-sentinel", "aws-sentinel", "npm-sentinel",
                     "password-sentinel"):
        assert sentinel not in joined


def test_build_run_env_sets_private_paths_home_and_run_token(tmp_path):
    paths = paths_at(tmp_path)
    env = build_run_env(paths, snapshot(), {"PATH": "/usr/bin"})
    assert env["HOME"] == str(paths.home)
    assert env["USERPROFILE"] == str(paths.home)
    assert env["DSH_HOME"] == str(paths.home / ".dsh")
    assert env["DSH_AGENTS_HOME"] == str(paths.code_root)
    assert env["EASEL_CODE_ROOT"] == str(paths.code_root)
    assert env["EASEL_WORKSPACE_ROOT"] == str(paths.workspace_root)
    assert env["EASEL_STATE_ROOT"] == str(paths.state_root)
    assert env["EASEL_ROOT"] == str(paths.workspace_root)
    assert env["EASEL_SERVICE_MODE"] == "worker"
    assert env["EASEL_WORKSPACE_ID"] == "workspace-a"
    assert env["EASEL_RUN_ID"] == "run-a"
    assert env["EASEL_CONFIG_REVISION"] == "3"
    assert env["EASEL_BROKER_URL"] == "http://broker.internal"
    assert env["EASEL_BROKER_TOKEN"] == "run-token-sentinel"
    assert env["XDG_CACHE_HOME"] == str(paths.home / ".cache")
    assert env["TMPDIR"] == str(paths.state_root / "tmp")


def test_build_run_env_removes_case_duplicate_paths(tmp_path):
    base = {"Path": "/host/path", "Easel_Broker_Token": "host-token"}
    env = build_run_env(paths_at(tmp_path), snapshot(), base)
    assert [key for key in env if key.upper() == "PATH"] == ["Path"]
    assert env["Path"] == "/host/path"
    assert [key.upper() for key in env].count("EASEL_BROKER_TOKEN") == 1
    assert env["EASEL_BROKER_TOKEN"] == "run-token-sentinel"
    assert "host-token" not in env.values()


def test_build_run_env_reads_nested_broker_routes(tmp_path):
    routes = {"broker": {"url": "http://nested", "token": "nested-token"}}
    env = build_run_env(paths_at(tmp_path), snapshot(routes=routes), {})
    assert env["EASEL_BROKER_URL"] == "http://nested"
    assert env["EASEL_BROKER_TOKEN"] == "nested-token"


# ---------------------------------------------------------------------------
# 脚本侧路径 helper
# ---------------------------------------------------------------------------


def load_runtime_paths_module():
    import sys
    path = Path(__file__).resolve().parents[2] / "skills" / "shared" / "scripts" / "runtime_paths.py"
    spec = importlib.util.spec_from_file_location("easel_runtime_paths", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_shared_runtime_paths_helper_separates_roots(tmp_path):
    module = load_runtime_paths_module()
    paths = module.resolve_runtime_paths(worker_roots(tmp_path))
    assert paths.code_root == (tmp_path / "code").resolve()
    assert paths.workspace_root == (tmp_path / "workspace").resolve()
    assert paths.state_root == (tmp_path / "state").resolve()
    assert paths.home == (tmp_path / "home").resolve()
    with pytest.raises(module.RuntimePathError):
        module.resolve_runtime_paths({"EASEL_SERVICE_MODE": "worker"})
    local = module.resolve_runtime_paths({}, default_code_root=tmp_path / "code")
    assert local.workspace_root == (tmp_path / "code").resolve()
