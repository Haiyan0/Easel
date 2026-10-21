"""Task 8 R1：worker 模式不得以 --no-sandbox 换取 Chromium 可运行。"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHARED = ROOT / "skills" / "shared" / "scripts"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))

import runtime_paths  # noqa: E402


def _load(path: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_worker_strips_every_sandbox_disabling_flag(monkeypatch):
    monkeypatch.setenv("EASEL_SERVICE_MODE", "worker")
    kept = runtime_paths.chromium_launch_args([
        "--disable-gpu", "--no-sandbox",
        "--disable-setuid-sandbox", "--no-sandbox=true",
        "--font-render-hinting=none"])
    assert kept == ["--disable-gpu", "--font-render-hinting=none"]


def test_local_keeps_existing_compatibility_flags(monkeypatch):
    monkeypatch.setenv("EASEL_SERVICE_MODE", "local")
    original = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"]
    assert runtime_paths.chromium_launch_args(original) == original


class _FakeChromium:
    def __init__(self, *, fail: Exception | None = None):
        self.kwargs = None
        self.fail = fail

    def launch_persistent_context(self, path, **kwargs):
        self.kwargs = kwargs
        if self.fail is not None:
            raise self.fail
        return object()


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium):
        self.chromium = chromium


def _launch_setup(monkeypatch, tmp_path, *, mode: str,
                  fail: Exception | None = None):
    module = _load(SHARED / "douyin_publish.py", "browser_sandbox_douyin")
    chromium = _FakeChromium(fail=fail)
    monkeypatch.setenv("EASEL_SERVICE_MODE", mode)
    monkeypatch.setattr(module, "_profile_dir", lambda base: tmp_path)
    monkeypatch.setattr(
        module, "launch_persistent_locked",
        lambda profile, action, **kwargs: action())
    return module, chromium


def _launch(module, chromium):
    return module._launch(_FakePlaywright(chromium), headed=False, base=None,
                          proxy=None)


def test_actual_worker_launch_kwargs_drop_sandbox_flags(monkeypatch, tmp_path):
    module, chromium = _launch_setup(monkeypatch, tmp_path, mode="worker")
    _launch(module, chromium)
    assert chromium.kwargs is not None
    args = chromium.kwargs["args"]
    assert "--no-sandbox" not in args
    assert "--disable-setuid-sandbox" not in args
    assert all(not str(value).startswith("--no-sandbox")
               for value in args)


def test_actual_local_launch_kwargs_keep_compatibility(monkeypatch, tmp_path):
    module, chromium = _launch_setup(monkeypatch, tmp_path, mode="local")
    _launch(module, chromium)
    assert "--no-sandbox" in chromium.kwargs["args"]


def test_missing_sandbox_capability_fails_instead_of_falling_back(
        monkeypatch, tmp_path):
    module, chromium = _launch_setup(
        monkeypatch, tmp_path, mode="worker",
        fail=RuntimeError("No usable sandbox!"))
    # 第一次真实启动即失败，且失败参数从未包含关闭沙箱的开关；调用方
    # 必须看到异常，而不是被静默重试为 --no-sandbox。
    with pytest.raises(RuntimeError, match="No usable sandbox"):
        _launch(module, chromium)
    assert chromium.kwargs is not None
    assert "--no-sandbox" not in chromium.kwargs["args"]


def test_every_script_with_no_sandbox_routes_through_the_mode_filter():
    offenders = []
    for path in (ROOT / "skills").rglob("*.py"):
        if path.name == "runtime_paths.py" or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "--no-sandbox" in text and "chromium_launch_args(" not in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], f"未按服务模式过滤沙箱参数：{offenders}"
